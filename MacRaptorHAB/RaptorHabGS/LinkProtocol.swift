//
//  LinkProtocol.swift
//  RaptorHabGS
//
//  Framing for the USB link to the payload. The Swift half of
//  Pi/common/linkproto.py -- the two must agree byte for byte.
//
//  One serial line carries two independent conversations, a JSON
//  configuration API and a raw terminal, so the bytes need a frame that says
//  which is which. Without it, console output would interleave into the
//  middle of a JSON reply and corrupt both.
//
//  Frame layout:
//      offset  size  field
//      0       2     magic, 0x52 0x48 ("RH")
//      2       1     channel
//      3       1     flags (reserved, must be 0)
//      4       4     payload length, big-endian UInt32
//      8       n     payload
//      8+n     4     CRC-32 of bytes 2 through the end of the payload
//

import Foundation

enum LinkChannel: UInt8 {
    case control = 0   // JSON request/response
    case console = 1   // raw PTY bytes
    case event   = 2   // unsolicited JSON from the payload
}

enum LinkProtocolError: LocalizedError {
    case payloadTooLarge(Int)

    var errorDescription: String? {
        switch self {
        case .payloadTooLarge(let size):
            return "Frame payload of \(size) bytes exceeds the \(LinkFrame.maxPayload)-byte limit"
        }
    }
}

enum LinkFrame {
    static let magic: [UInt8] = [0x52, 0x48]  // "RH"
    static let headerSize = 8
    static let crcSize = 4
    static let overhead = headerSize + crcSize

    /// Bounded so a corrupt length field cannot make the reader allocate
    /// wildly. The largest legitimate payload is a config schema.
    static let maxPayload = 1 << 20

    static func encode(channel: LinkChannel, payload: Data) throws -> Data {
        guard payload.count <= maxPayload else {
            throw LinkProtocolError.payloadTooLarge(payload.count)
        }

        var body = Data()
        body.append(channel.rawValue)
        body.append(0)  // flags
        body.append(contentsOf: withUnsafeBytes(of: UInt32(payload.count).bigEndian) { Array($0) })
        body.append(payload)

        var frame = Data(magic)
        frame.append(body)
        frame.append(contentsOf: withUnsafeBytes(of: CRC32.calculate(data: body).bigEndian) { Array($0) })
        return frame
    }
}

/// Incremental frame reader for a byte stream.
///
/// Feed it whatever arrives; it returns complete frames and buffers the rest.
/// Resynchronises on garbage by scanning forward for the next magic, which is
/// what makes connecting mid-stream work -- a serial line has no message
/// boundaries and the payload may be mid-transmission when the app attaches.
final class LinkFrameDecoder {
    private var buffer = Data()
    private let maxBuffer: Int

    private(set) var resyncs = 0
    private(set) var crcErrors = 0

    init(maxBuffer: Int = 4 * LinkFrame.maxPayload) {
        self.maxBuffer = maxBuffer
    }

    var bufferedBytes: Int { buffer.count }

    func reset() {
        buffer.removeAll(keepingCapacity: true)
    }

    func feed(_ data: Data) -> [(channel: UInt8, payload: Data)] {
        buffer.append(data)

        // A buffer this large means we are reading noise, not frames.
        if buffer.count > maxBuffer {
            buffer.removeFirst(buffer.count / 2)
            resyncs += 1
        }

        var frames: [(UInt8, Data)] = []
        while let frame = nextFrame() {
            frames.append(frame)
        }
        return frames
    }

    private func nextFrame() -> (UInt8, Data)? {
        while true {
            guard buffer.count >= LinkFrame.headerSize else { return nil }

            guard buffer[buffer.startIndex] == LinkFrame.magic[0],
                  buffer[buffer.startIndex + 1] == LinkFrame.magic[1] else {
                if !resync() { return nil }
                continue
            }

            let channel = buffer[buffer.startIndex + 2]
            let length = Int(readUInt32(at: 4))

            guard length <= LinkFrame.maxPayload else {
                // A length this large is corruption, not a real frame.
                discardMagic()
                continue
            }

            let total = LinkFrame.overhead + length
            guard buffer.count >= total else { return nil }

            let bodyStart = buffer.startIndex + 2
            let bodyEnd = buffer.startIndex + LinkFrame.headerSize + length
            let body = buffer[bodyStart..<bodyEnd]
            let expected = readUInt32(at: LinkFrame.headerSize + length)

            guard CRC32.calculate(data: Data(body)) == expected else {
                crcErrors += 1
                discardMagic()
                continue
            }

            let payloadStart = buffer.startIndex + LinkFrame.headerSize
            let payload = Data(buffer[payloadStart..<bodyEnd])
            buffer.removeFirst(total)
            return (channel, payload)
        }
    }

    private func readUInt32(at offset: Int) -> UInt32 {
        let index = buffer.startIndex + offset
        return (UInt32(buffer[index]) << 24)
            | (UInt32(buffer[index + 1]) << 16)
            | (UInt32(buffer[index + 2]) << 8)
            | UInt32(buffer[index + 3])
    }

    /// Scan forward to the next plausible frame start.
    private func resync() -> Bool {
        let bytes = [UInt8](buffer)
        var index = 1
        while index + 1 < bytes.count {
            if bytes[index] == LinkFrame.magic[0] && bytes[index + 1] == LinkFrame.magic[1] {
                buffer.removeFirst(index)
                resyncs += 1
                return true
            }
            index += 1
        }

        // Keep the last byte: it could be the first half of a magic that
        // straddles this read and the next.
        let keep = buffer.last == LinkFrame.magic[0] ? 1 : 0
        if buffer.count > keep {
            resyncs += 1
            buffer.removeFirst(buffer.count - keep)
        }
        return false
    }

    private func discardMagic() {
        buffer.removeFirst(min(2, buffer.count))
    }
}
