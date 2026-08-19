//
//  MeshtasticProtobuf.swift
//  RaptorHabGS
//
//  Minimal protocol buffer codec, hand-rolled to keep the project free of
//  external dependencies.
//
//  swift-protobuf plus the generated Meshtastic schema would be a large
//  dependency for the handful of message types this app actually parses. Only
//  the wire format is implemented here, which is small and fully specified:
//
//      key    = (field_number << 3) | wire_type
//      varint = base-128, little-endian, high bit set on continuation bytes
//
//  Wire types: 0 varint, 1 fixed64, 2 length-delimited, 5 fixed32.
//  Groups (3 and 4) are deprecated in proto3 and unsupported.
//
//  The Python half of this lives in payload/common/meshtastic/protobuf.py.
//

import Foundation

enum WireType: UInt8 {
    case varint = 0
    case fixed64 = 1
    case lengthDelimited = 2
    case fixed32 = 5
}

enum ProtobufError: LocalizedError {
    case truncated(String)
    case unsupportedWireType(UInt8)
    case invalidFieldNumber

    var errorDescription: String? {
        switch self {
        case .truncated(let detail):        return "Truncated protobuf: \(detail)"
        case .unsupportedWireType(let type): return "Unsupported protobuf wire type \(type)"
        case .invalidFieldNumber:            return "Protobuf field number 0 is not valid"
        }
    }
}

// MARK: - Writer

/// Builds a protobuf message field by field.
///
/// Proto3 default values are omitted unless forced, matching what the
/// official library emits — which keeps messages small on a link running at
/// about a kilobit per second.
struct ProtobufWriter {
    private(set) var data = Data()

    static func encodeVarint(_ value: UInt64) -> Data {
        var remaining = value
        var out = Data()
        repeat {
            var byte = UInt8(remaining & 0x7F)
            remaining >>= 7
            if remaining != 0 { byte |= 0x80 }
            out.append(byte)
        } while remaining != 0
        return out
    }

    private mutating func appendKey(_ field: Int, _ type: WireType) {
        data.append(ProtobufWriter.encodeVarint(UInt64(field) << 3 | UInt64(type.rawValue)))
    }

    mutating func uint32(_ field: Int, _ value: UInt32, force: Bool = false) {
        guard value != 0 || force else { return }
        appendKey(field, .varint)
        data.append(ProtobufWriter.encodeVarint(UInt64(value)))
    }

    mutating func uint64(_ field: Int, _ value: UInt64, force: Bool = false) {
        guard value != 0 || force else { return }
        appendKey(field, .varint)
        data.append(ProtobufWriter.encodeVarint(value))
    }

    mutating func int32(_ field: Int, _ value: Int32, force: Bool = false) {
        guard value != 0 || force else { return }
        appendKey(field, .varint)
        // Protobuf sign-extends a negative int32 to 64 bits, so it always
        // occupies ten bytes.
        data.append(ProtobufWriter.encodeVarint(UInt64(bitPattern: Int64(value))))
    }

    mutating func bool(_ field: Int, _ value: Bool, force: Bool = false) {
        guard value || force else { return }
        appendKey(field, .varint)
        data.append(value ? 1 : 0)
    }

    mutating func enumValue(_ field: Int, _ value: Int, force: Bool = false) {
        uint32(field, UInt32(max(0, value)), force: force)
    }

    mutating func fixed32(_ field: Int, _ value: UInt32, force: Bool = false) {
        guard value != 0 || force else { return }
        appendKey(field, .fixed32)
        withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) }
    }

    mutating func sfixed32(_ field: Int, _ value: Int32, force: Bool = false) {
        guard value != 0 || force else { return }
        appendKey(field, .fixed32)
        withUnsafeBytes(of: value.littleEndian) { data.append(contentsOf: $0) }
    }

    mutating func float(_ field: Int, _ value: Float, force: Bool = false) {
        guard value != 0 || force else { return }
        appendKey(field, .fixed32)
        withUnsafeBytes(of: value.bitPattern.littleEndian) { data.append(contentsOf: $0) }
    }

    mutating func bytes(_ field: Int, _ value: Data, force: Bool = false) {
        guard !value.isEmpty || force else { return }
        appendKey(field, .lengthDelimited)
        data.append(ProtobufWriter.encodeVarint(UInt64(value.count)))
        data.append(value)
    }

    mutating func string(_ field: Int, _ value: String, force: Bool = false) {
        guard !value.isEmpty || force else { return }
        bytes(field, Data(value.utf8), force: true)
    }

    mutating func message(_ field: Int, _ writer: ProtobufWriter, force: Bool = false) {
        guard !writer.data.isEmpty || force else { return }
        bytes(field, writer.data, force: true)
    }
}

// MARK: - Reader

/// A decoded protobuf field.
enum ProtobufValue {
    case varint(UInt64)
    case fixed32(UInt32)
    case fixed64(UInt64)
    case bytes(Data)

    var uintValue: UInt64? {
        switch self {
        case .varint(let value):  return value
        case .fixed32(let value): return UInt64(value)
        case .fixed64(let value): return value
        case .bytes:              return nil
        }
    }

    var intValue: Int32? {
        guard let raw = uintValue else { return nil }
        let truncated = UInt32(truncatingIfNeeded: raw)
        return Int32(bitPattern: truncated)
    }

    var boolValue: Bool { (uintValue ?? 0) != 0 }

    var floatValue: Float? {
        guard case .fixed32(let bits) = self else { return nil }
        return Float(bitPattern: bits)
    }

    var dataValue: Data? {
        if case .bytes(let value) = self { return value }
        return nil
    }

    var stringValue: String? {
        guard let data = dataValue else { return nil }
        return String(data: data, encoding: .utf8)
    }
}

/// Reads the fields of a protobuf message.
///
/// Deliberately tolerant of unknown field numbers: Meshtastic adds fields
/// over time, and a beacon from newer firmware must still parse.
struct ProtobufReader {
    private let data: Data
    private var offset: Int

    init(_ data: Data) {
        self.data = data
        self.offset = data.startIndex
    }

    static func decodeVarint(_ data: Data, at start: Int) throws -> (UInt64, Int) {
        var result: UInt64 = 0
        var shift: UInt64 = 0
        var index = start

        while true {
            guard index < data.endIndex else {
                throw ProtobufError.truncated("varint at \(start)")
            }
            guard shift < 64 else {
                throw ProtobufError.truncated("varint at \(start) exceeds 64 bits")
            }

            let byte = data[index]
            index += 1
            result |= UInt64(byte & 0x7F) << shift
            if byte & 0x80 == 0 { return (result, index) }
            shift += 7
        }
    }

    /// Collect every field into `[fieldNumber: [values]]`.
    ///
    /// Repeated fields accumulate naturally. Protobuf says the last value
    /// wins for a singular field, so callers should take `.last`.
    func fields() throws -> [Int: [ProtobufValue]] {
        var out: [Int: [ProtobufValue]] = [:]
        var cursor = offset

        while cursor < data.endIndex {
            let (key, afterKey) = try ProtobufReader.decodeVarint(data, at: cursor)
            cursor = afterKey

            let fieldNumber = Int(key >> 3)
            guard fieldNumber > 0 else { throw ProtobufError.invalidFieldNumber }

            guard let wireType = WireType(rawValue: UInt8(key & 0x07)) else {
                throw ProtobufError.unsupportedWireType(UInt8(key & 0x07))
            }

            switch wireType {
            case .varint:
                let (value, next) = try ProtobufReader.decodeVarint(data, at: cursor)
                cursor = next
                out[fieldNumber, default: []].append(.varint(value))

            case .fixed32:
                guard cursor + 4 <= data.endIndex else {
                    throw ProtobufError.truncated("fixed32 at \(cursor)")
                }
                let value = data[cursor..<cursor + 4].withUnsafeBytes {
                    $0.loadUnaligned(as: UInt32.self).littleEndian
                }
                cursor += 4
                out[fieldNumber, default: []].append(.fixed32(value))

            case .fixed64:
                guard cursor + 8 <= data.endIndex else {
                    throw ProtobufError.truncated("fixed64 at \(cursor)")
                }
                let value = data[cursor..<cursor + 8].withUnsafeBytes {
                    $0.loadUnaligned(as: UInt64.self).littleEndian
                }
                cursor += 8
                out[fieldNumber, default: []].append(.fixed64(value))

            case .lengthDelimited:
                let (length, afterLength) = try ProtobufReader.decodeVarint(data, at: cursor)
                cursor = afterLength
                let end = cursor + Int(length)
                guard length <= UInt64(Int.max), end <= data.endIndex else {
                    throw ProtobufError.truncated(
                        "length-delimited field \(fieldNumber) at \(cursor)"
                    )
                }
                out[fieldNumber, default: []].append(.bytes(Data(data[cursor..<end])))
                cursor = end
            }
        }

        return out
    }
}
