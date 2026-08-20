//
//  FrameScanner.swift
//  RaptorHabGS
//
//  Splits the modem's USB stream into frames and status lines.
//
//  The modem sends both on one cable, so something has to tell them apart.
//  The framing is what does it: bytes inside a frame are frame bytes, and
//  whatever falls outside one is status text. Scanning for newlines first
//  cannot work, because 0x0A is an ordinary data byte inside a frame and the
//  modem does not escape it -- a 240-byte image frame contains a 0x0A about
//  61% of the time, and deleting up to it destroys the frame.
//
//  Frames are delimited at both ends:
//
//      0x7E <stuffed frame> 0x7E   0x7E <stuffed frame> 0x7E
//
//  so a scanner that loses one byte can pair a closing delimiter with the
//  next frame's opening one and stay wrong forever, silently discarding
//  every frame that follows. Recovery is therefore not optional: a candidate
//  is checked against its own length field and checksum before any of it is
//  consumed, and a candidate that fails costs exactly one byte. That way a
//  false start cannot swallow the real frames sitting inside it.
//

import Foundation

final class FrameScanner {

    static let frameDelimiter: UInt8 = 0x7E

    /// The dual-radio modem carries two streams down one cable: RAPTOR image
    /// traffic on 0x7E, and whole Meshtastic LoRa packets on 0x7B. A
    /// single-radio modem only ever sends 0x7E, so handling both here means
    /// one build talks to either board.
    static let meshtasticDelimiter: UInt8 = 0x7B

    static let escape: UInt8 = 0x7D

    /// Longest run of bytes that could still be one frame. A 210-byte image
    /// packet frames to about 240 bytes, or roughly 480 if every byte needed
    /// escaping, so anything beyond this is not a frame that started here.
    static let maxFrameSpan = 2000

    /// Hard ceiling on retained bytes, so a wedged stream cannot grow without
    /// bound.
    static let maxBuffer = 10000

    struct Frame {
        /// The delimiter the frame arrived on, which is what routes it.
        let delimiter: UInt8
        /// De-stuffed, delimiters removed:
        /// [LEN_HI][LEN_LO][RSSI_INT][RSSI_FRAC][SNR_INT][SNR_FRAC][DATA...][CHECKSUM]
        let data: Data

        var isMeshtastic: Bool { delimiter == FrameScanner.meshtasticDelimiter }

        /// The modem's payload, without the transport header or checksum.
        var payload: Data {
            let dataLen = (Int(data[data.startIndex]) << 8) | Int(data[data.startIndex + 1])
            return data.subdata(in: (data.startIndex + 6)..<(data.startIndex + 6 + dataLen))
        }
    }

    struct Output {
        var frames: [Frame] = []
        var textLines: [String] = []
    }

    private var buf = [UInt8]()
    /// Bytes before this index are consumed; compacted periodically so that
    /// draining the buffer stays cheap on a busy link.
    private var head = 0
    private var textLine = ""

    /// Candidate frame starts rejected. A steadily climbing count means the
    /// stream is being corrupted somewhere upstream.
    private(set) var resyncs = 0

    private var available: Int { buf.count - head }

    func reset() {
        buf.removeAll(keepingCapacity: true)
        head = 0
        textLine.removeAll(keepingCapacity: true)
    }

    func feed(_ data: Data) -> Output {
        buf.append(contentsOf: data)
        var out = Output()

        drain: while true {
            switch scan(into: &out) {
            case .frame(let frame):
                out.frames.append(frame)
            case .resynced:
                continue
            case .needMoreData:
                break drain
            }
        }

        if available > Self.maxBuffer {
            consume(available)
        }
        return out
    }

    // MARK: - Internals

    private enum Scan {
        case frame(Frame)
        /// The buffer was advanced past something that was not a frame.
        case resynced
        case needMoreData
    }

    private func consume(_ count: Int) {
        head += count
        // Compact occasionally rather than on every frame, so a busy link is
        // not memmoving the whole buffer thousands of times a second.
        if head == buf.count || head > 4096 {
            buf.removeFirst(head)
            head = 0
        }
    }

    private func scan(into out: inout Output) -> Scan {
        guard available > 0 else { return .needMoreData }

        // Find the start of either stream, whichever comes first.
        var start = -1
        for i in head..<buf.count where buf[i] == Self.frameDelimiter
                                     || buf[i] == Self.meshtasticDelimiter {
            start = i
            break
        }

        // Nothing framed in here: it is all status text. Any trailing partial
        // line is kept in textLine, so consuming the buffer loses nothing.
        guard start >= 0 else {
            harvestText(head..<buf.count, into: &out)
            consume(available)
            return .needMoreData
        }

        // Anything ahead of the delimiter is status text, not frame payload.
        if start > head {
            harvestText(head..<start, into: &out)
            consume(start - head)
            return .resynced
        }

        // Start delimiter + header + a byte of data + checksum + end delimiter
        guard available >= 10 else { return .needMoreData }

        // A frame ends on the same delimiter it began with. The other stream's
        // delimiter is escaped inside a frame, so it cannot appear unescaped.
        let opening = buf[head]
        var end = -1
        var i = head + 1
        while i < buf.count {
            if buf[i] == opening { end = i; break }
            // Skip escape sequences, so an escaped delimiter is not mistaken
            // for the end of the frame.
            i += (buf[i] == Self.escape && i + 1 < buf.count) ? 2 : 1
        }

        guard end >= 0 else {
            // No closing delimiter within any plausible frame length, so this
            // opening byte was payload. Step over it; clearing the buffer
            // would take real frames with it.
            if available > Self.maxFrameSpan {
                resyncs += 1
                consume(1)
                return .resynced
            }
            return .needMoreData
        }

        // De-stuff before consuming anything. A candidate that fails
        // validation means this byte was payload rather than a delimiter, and
        // consuming the whole span would swallow the frames inside it.
        var destuffed = [UInt8]()
        destuffed.reserveCapacity(end - head)
        i = head + 1
        while i < end {
            guard buf[i] == Self.escape, i + 1 < end else {
                destuffed.append(buf[i])
                i += 1
                continue
            }
            switch buf[i + 1] {
            case 0x5E: destuffed.append(Self.frameDelimiter)
            case 0x5B: destuffed.append(Self.meshtasticDelimiter)
            case 0x5D: destuffed.append(Self.escape)
            default:
                // Not a sequence the modem emits, so this is not a frame.
                return reject()
            }
            i += 2
        }

        // len(2) + rssi(2) + snr(2) + data(1+) + checksum(1)
        guard destuffed.count >= 8 else { return reject() }
        let dataLen = (Int(destuffed[0]) << 8) | Int(destuffed[1])
        guard dataLen > 0, dataLen <= 255, destuffed.count == 7 + dataLen else {
            return reject()
        }

        // The modem XORs every byte it sends into the checksum, so a complete
        // valid frame XORs to zero. This is what makes resynchronisation
        // trustworthy: a false start has to satisfy the length field and then
        // hit 1 chance in 256 to be accepted.
        var parity: UInt8 = 0
        for byte in destuffed { parity ^= byte }
        guard parity == 0 else { return reject() }

        consume(end - head + 1)
        return .frame(Frame(delimiter: opening, data: Data(destuffed)))
    }

    private func reject() -> Scan {
        resyncs += 1
        consume(1)
        return .resynced
    }

    /// Accumulate bytes that fell outside any frame and split them into lines.
    private func harvestText(_ range: Range<Int>, into out: inout Output) {
        for i in range {
            let byte = buf[i]
            if byte == 0x0A || byte == 0x0D {
                let trimmed = textLine.trimmingCharacters(in: .whitespacesAndNewlines)
                textLine.removeAll(keepingCapacity: true)
                guard !trimmed.isEmpty else { continue }
                if trimmed.hasPrefix("[") || trimmed.hasPrefix("CFG")
                    || trimmed.hasPrefix("RaptorHab") || trimmed.hasPrefix("Heltec")
                    || trimmed.hasPrefix("=") {
                    out.textLines.append(trimmed)
                }
            } else if byte >= 0x20 && byte < 0x7F {
                textLine.unicodeScalars.append(Unicode.Scalar(byte))
                // A "line" this long is framing debris, not a status message.
                if textLine.count > 512 { textLine.removeAll(keepingCapacity: true) }
            } else {
                // Binary debris between frames is not text.
                textLine.removeAll(keepingCapacity: true)
            }
        }
    }
}
