//
//  MeshtasticProtocol.swift
//  RaptorHabGS
//
//  Meshtastic packet framing, channel encryption, and the application
//  messages the app needs to read.
//
//  Mirrors payload/common/meshtastic/. Field numbers are part of the wire contract
//  and are written out explicitly rather than generated.
//
//  Encryption is AES-256-CTR via CommonCrypto — a system framework, so still
//  no external dependency. CryptoKit does not expose raw CTR.
//

import CommonCrypto
import Foundation

// MARK: - Crypto

enum MeshtasticCrypto {
    /// Meshtastic's default channel key. Published in every client, so
    /// traffic using it is readable by anyone: obfuscated, not private.
    static let defaultPSK = Data([
        0xD4, 0xF1, 0xBB, 0x3A, 0x20, 0x29, 0x07, 0x59,
        0xF0, 0xBC, 0xFF, 0xAB, 0xCF, 0x4E, 0x69, 0x01,
    ])

    /// The 16-byte AES-CTR counter block:
    /// packet id (UInt64 LE) ‖ sender (UInt32 LE) ‖ zero (UInt32 LE).
    static func nonce(packetID: UInt32, sender: UInt32) -> Data {
        var nonce = Data(capacity: 16)
        withUnsafeBytes(of: UInt64(packetID).littleEndian) { nonce.append(contentsOf: $0) }
        withUnsafeBytes(of: sender.littleEndian) { nonce.append(contentsOf: $0) }
        withUnsafeBytes(of: UInt32(0).littleEndian) { nonce.append(contentsOf: $0) }
        return nonce
    }

    /// AES-CTR. Symmetric: the same call encrypts and decrypts.
    ///
    /// CommonCrypto increments the counter block as a big-endian 128-bit
    /// integer, which is what the mbedtls implementation in Meshtastic
    /// firmware does. A little-endian word counter would produce a correct
    /// first block and garbage after it.
    static func ctr(key: Data, nonce: Data, data: Data) -> Data? {
        guard [16, 24, 32].contains(key.count), nonce.count == 16 else { return nil }
        guard !data.isEmpty else { return Data() }

        var cryptor: CCCryptorRef?
        var output = Data(count: data.count)

        let status = key.withUnsafeBytes { keyBytes in
            nonce.withUnsafeBytes { nonceBytes in
                CCCryptorCreateWithMode(
                    CCOperation(kCCEncrypt),
                    CCMode(kCCModeCTR),
                    CCAlgorithm(kCCAlgorithmAES),
                    CCPadding(ccNoPadding),
                    nonceBytes.baseAddress,
                    keyBytes.baseAddress, key.count,
                    nil, 0, 0,
                    CCModeOptions(kCCModeOptionCTR_BE),
                    &cryptor
                )
            }
        }

        guard status == kCCSuccess, let cryptor else { return nil }
        defer { CCCryptorRelease(cryptor) }

        // `output.count` inside the withUnsafeMutableBytes closure would be a
        // second access to `output` while the first is still exclusive, so
        // capture the capacity first.
        let capacity = output.count
        var moved = 0

        let updateStatus = data.withUnsafeBytes { input in
            output.withUnsafeMutableBytes { out in
                CCCryptorUpdate(
                    cryptor,
                    input.baseAddress, data.count,
                    out.baseAddress, capacity,
                    &moved
                )
            }
        }

        guard updateStatus == kCCSuccess else { return nil }
        return output.prefix(moved)
    }

    /// Expand a Meshtastic PSK into a usable AES key.
    ///
    /// A single byte selects one of the well-known default keys; an empty PSK
    /// means the channel is unencrypted.
    static func expand(psk: Data) -> Data {
        if psk.isEmpty { return Data() }

        if psk.count == 1 {
            let index = psk[psk.startIndex]
            if index == 0 { return Data() }
            var key = defaultPSK
            key[key.index(before: key.endIndex)] =
                defaultPSK[defaultPSK.index(before: defaultPSK.endIndex)] &+ (index - 1)
            return key
        }

        return [16, 24, 32].contains(psk.count) ? psk : Data()
    }

    /// Parse a PSK from base64 (as the Meshtastic apps show it) or hex.
    static func parsePSK(_ text: String) -> Data? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { return Data() }

        let stripped = trimmed
            .replacingOccurrences(of: "0x", with: "")
            .replacingOccurrences(of: ":", with: "")
            .replacingOccurrences(of: " ", with: "")

        if [2, 32, 48, 64].contains(stripped.count), let hex = Data(hex: stripped) {
            return hex
        }
        return Data(base64Encoded: trimmed)
    }

    /// The single-byte channel hash carried in the packet header. Receivers
    /// use it to pick which key to try, so it must match the firmware's
    /// XOR-fold of the name and key exactly.
    static func channelHash(name: String, key: Data) -> UInt8 {
        var value: UInt8 = 0
        for byte in Data(name.utf8) { value ^= byte }
        for byte in key { value ^= byte }
        return value
    }
}

extension Data {
    init?(hex: String) {
        guard hex.count % 2 == 0 else { return nil }
        var data = Data(capacity: hex.count / 2)
        var index = hex.startIndex
        while index < hex.endIndex {
            let next = hex.index(index, offsetBy: 2)
            guard let byte = UInt8(hex[index..<next], radix: 16) else { return nil }
            data.append(byte)
            index = next
        }
        self = data
    }

    var hexString: String {
        map { String(format: "%02x", $0) }.joined()
    }
}

// MARK: - Packet

enum MeshtasticPortNum: Int {
    case unknown = 0
    case textMessage = 1
    case position = 3
    case nodeInfo = 4
    case routing = 5
    case admin = 6
    case telemetry = 67

    var label: String {
        switch self {
        case .unknown:     return "Unknown"
        case .textMessage: return "Text"
        case .position:    return "Position"
        case .nodeInfo:    return "Node Info"
        case .routing:     return "Routing"
        case .admin:       return "Admin"
        case .telemetry:   return "Telemetry"
        }
    }
}

struct MeshtasticHeader {
    static let size = 16
    static let broadcast: UInt32 = 0xFFFFFFFF

    var destination: UInt32 = broadcast
    var sender: UInt32 = 0
    var packetID: UInt32 = 0
    var hopLimit: UInt8 = 0
    var wantAck = false
    var viaMQTT = false
    var hopStart: UInt8 = 0
    var channelHash: UInt8 = 0
    var nextHop: UInt8 = 0
    var relayNode: UInt8 = 0

    var isBroadcast: Bool { destination == MeshtasticHeader.broadcast }

    func serialized() -> Data {
        var flags = hopLimit & 0x07
        if wantAck { flags |= 0x08 }
        if viaMQTT { flags |= 0x10 }
        flags |= (hopStart << 5) & 0xE0

        var data = Data(capacity: MeshtasticHeader.size)
        withUnsafeBytes(of: destination.littleEndian) { data.append(contentsOf: $0) }
        withUnsafeBytes(of: sender.littleEndian) { data.append(contentsOf: $0) }
        withUnsafeBytes(of: packetID.littleEndian) { data.append(contentsOf: $0) }
        data.append(flags)
        data.append(channelHash)
        data.append(nextHop)
        data.append(relayNode)
        return data
    }

    static func parse(_ data: Data) -> MeshtasticHeader? {
        guard data.count >= size else { return nil }
        let bytes = [UInt8](data.prefix(size))

        func uint32(_ offset: Int) -> UInt32 {
            UInt32(bytes[offset])
                | UInt32(bytes[offset + 1]) << 8
                | UInt32(bytes[offset + 2]) << 16
                | UInt32(bytes[offset + 3]) << 24
        }

        let flags = bytes[12]
        return MeshtasticHeader(
            destination: uint32(0),
            sender: uint32(4),
            packetID: uint32(8),
            hopLimit: flags & 0x07,
            wantAck: flags & 0x08 != 0,
            viaMQTT: flags & 0x10 != 0,
            hopStart: (flags & 0xE0) >> 5,
            channelHash: bytes[13],
            nextHop: bytes[14],
            relayNode: bytes[15]
        )
    }
}

/// The decoded contents of a received Meshtastic packet.
struct MeshtasticPacket {
    let header: MeshtasticHeader
    let portNum: MeshtasticPortNum
    let payload: Data
    var rssi: Int?
    var snr: Float?
    let receivedAt: Date

    // Uses the formatter in this file rather than MeshtasticNode's, so the
    // protocol layer stays independent of the manager layer above it.
    var senderID: String { MeshtasticProtocol.nodeIDString(header.sender) }
}

enum MeshtasticProtocol {

    static func nodeIDString(_ id: UInt32) -> String {
        String(format: "!%08x", id)
    }

    /// Build an encrypted packet ready for a radio.
    static func buildPacket(
        portNum: MeshtasticPortNum,
        payload: Data,
        sender: UInt32,
        destination: UInt32 = MeshtasticHeader.broadcast,
        channelKey: Data,
        channelHash: UInt8,
        hopLimit: UInt8 = 3,
        wantAck: Bool = false,
        packetID: UInt32? = nil
    ) -> Data {
        var data = ProtobufWriter()
        data.enumValue(1, portNum.rawValue, force: true)
        data.bytes(2, payload, force: true)
        if wantAck { data.bool(3, true) }

        let id = packetID ?? UInt32.random(in: 1...UInt32.max)
        var body = data.data

        if !channelKey.isEmpty {
            let nonce = MeshtasticCrypto.nonce(packetID: id, sender: sender)
            body = MeshtasticCrypto.ctr(key: channelKey, nonce: nonce, data: body) ?? body
        }

        let header = MeshtasticHeader(
            destination: destination,
            sender: sender,
            packetID: id,
            hopLimit: hopLimit,
            wantAck: wantAck,
            hopStart: hopLimit,
            channelHash: channelHash
        )

        return header.serialized() + body
    }

    /// Parse a received packet, trying each candidate channel key.
    ///
    /// AES-CTR cannot report a wrong key: it yields plausible bytes either
    /// way. The only usable test is whether the result parses as a Data
    /// protobuf with a port number we recognise, so that is the test used.
    static func parsePacket(
        _ raw: Data, channelKeys: [Data], rssi: Int? = nil, snr: Float? = nil
    ) -> MeshtasticPacket? {
        guard let header = MeshtasticHeader.parse(raw) else { return nil }
        let body = raw.dropFirst(MeshtasticHeader.size)
        guard !body.isEmpty else { return nil }

        var candidates = channelKeys
        candidates.append(Data())  // an unencrypted channel is possible too

        for key in candidates {
            let plaintext: Data
            if key.isEmpty {
                plaintext = Data(body)
            } else {
                let nonce = MeshtasticCrypto.nonce(
                    packetID: header.packetID, sender: header.sender
                )
                guard let decrypted = MeshtasticCrypto.ctr(
                    key: key, nonce: nonce, data: Data(body)
                ) else { continue }
                plaintext = decrypted
            }

            guard let fields = try? ProtobufReader(plaintext).fields() else { continue }

            let rawPort = Int(fields[1]?.last?.uintValue ?? 0)
            guard let portNum = MeshtasticPortNum(rawValue: rawPort) else { continue }

            let payload = fields[2]?.last?.dataValue ?? Data()

            // A zero port with no payload is what random bytes decode to.
            if portNum == .unknown && payload.isEmpty { continue }

            return MeshtasticPacket(
                header: header, portNum: portNum, payload: payload,
                rssi: rssi, snr: snr, receivedAt: Date()
            )
        }

        return nil
    }

    // MARK: - Messages

    static func buildPosition(
        latitude: Double, longitude: Double, altitude: Int32 = 0
    ) -> Data {
        var writer = ProtobufWriter()
        writer.sfixed32(1, Int32(clamping: Int(latitude * 1e7)), force: true)
        writer.sfixed32(2, Int32(clamping: Int(longitude * 1e7)), force: true)
        writer.int32(3, altitude)
        writer.fixed32(4, UInt32(Date().timeIntervalSince1970))
        return writer.data
    }

    struct Position {
        var latitude: Double = 0
        var longitude: Double = 0
        var altitude: Int32 = 0
        var timestamp: Date?
        var satellites: Int = 0
        var groundSpeed: Int = 0
        var groundTrack: Double = 0

        var isValid: Bool {
            (latitude != 0 || longitude != 0)
                && abs(latitude) <= 90 && abs(longitude) <= 180
        }
    }

    static func parsePosition(_ data: Data) -> Position? {
        guard let fields = try? ProtobufReader(data).fields() else { return nil }

        var position = Position()
        if let value = fields[1]?.last?.intValue { position.latitude = Double(value) / 1e7 }
        if let value = fields[2]?.last?.intValue { position.longitude = Double(value) / 1e7 }
        if let value = fields[3]?.last?.intValue { position.altitude = value }
        if let value = fields[4]?.last?.uintValue, value > 0 {
            position.timestamp = Date(timeIntervalSince1970: TimeInterval(value))
        }
        if let value = fields[17]?.last?.uintValue { position.satellites = Int(value) }
        if let value = fields[20]?.last?.uintValue { position.groundSpeed = Int(value) }
        if let value = fields[21]?.last?.uintValue { position.groundTrack = Double(value) / 1e5 }

        return position.isValid ? position : nil
    }

    struct User {
        var id = ""
        var longName = ""
        var shortName = ""
    }

    static func parseUser(_ data: Data) -> User? {
        guard let fields = try? ProtobufReader(data).fields() else { return nil }
        var user = User()
        user.id = fields[1]?.last?.stringValue ?? ""
        user.longName = fields[2]?.last?.stringValue ?? ""
        user.shortName = fields[3]?.last?.stringValue ?? ""
        return user.longName.isEmpty && user.shortName.isEmpty ? nil : user
    }

    struct DeviceMetrics {
        var batteryLevel: Int?
        var voltage: Float?
        var uptimeSeconds: Int?
        var channelUtilization: Float?
    }

    struct Telemetry {
        var device: DeviceMetrics?
        var temperature: Float?
        var timestamp: Date?
    }

    static func parseTelemetry(_ data: Data) -> Telemetry? {
        guard let fields = try? ProtobufReader(data).fields() else { return nil }

        var telemetry = Telemetry()
        if let value = fields[1]?.last?.uintValue, value > 0 {
            telemetry.timestamp = Date(timeIntervalSince1970: TimeInterval(value))
        }

        if let deviceData = fields[2]?.last?.dataValue,
           let deviceFields = try? ProtobufReader(deviceData).fields() {
            var metrics = DeviceMetrics()
            metrics.batteryLevel = (deviceFields[1]?.last?.uintValue).map { Int($0) }
            metrics.voltage = deviceFields[2]?.last?.floatValue
            metrics.channelUtilization = deviceFields[3]?.last?.floatValue
            metrics.uptimeSeconds = (deviceFields[5]?.last?.uintValue).map { Int($0) }
            telemetry.device = metrics
        }

        if let environmentData = fields[3]?.last?.dataValue,
           let environmentFields = try? ProtobufReader(environmentData).fields() {
            telemetry.temperature = environmentFields[1]?.last?.floatValue
        }

        return telemetry
    }

    static func buildTextMessage(_ text: String, maxBytes: Int = 200) -> Data {
        var encoded = Data(text.utf8)
        guard encoded.count > maxBytes else { return encoded }

        // Truncate on a character boundary; slicing raw bytes would split a
        // multi-byte character and render as a replacement glyph.
        encoded = encoded.prefix(maxBytes)
        while let last = encoded.last, last & 0xC0 == 0x80 {
            encoded = encoded.dropLast()
        }
        if let last = encoded.last, last & 0x80 != 0 {
            encoded = encoded.dropLast()
        }
        return Data(encoded)
    }
}
