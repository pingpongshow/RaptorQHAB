//
//  Protocol.swift
//  RaptorHabMobile
//
//  Protocol definitions matching RaptorHab airborne unit
//  Packet structures, CRC, and serialization
//

import Foundation

// MARK: - Constants

struct RaptorProtocol {
    static let syncWord: [UInt8] = [0x52, 0x41, 0x50, 0x54]  // "RAPT"
    static let syncWordUInt32: UInt32 = 0x52415054
    
    static let syncSize = 4
    static let typeSize = 1
    static let seqSize = 2
    static let flagsSize = 1
    static let crcSize = 4
    static let headerSize = syncSize + typeSize + seqSize + flagsSize  // 8 bytes
    static let maxPacketSize = 255
    static let maxPayloadSize = maxPacketSize - headerSize - crcSize  // 243 bytes
    
    static let telemetryPayloadSize = 36
    static let imageMetaPayloadSize = 22
    static let fountainSymbolSize = 200
}

// MARK: - Modem RF Configuration

struct ModemConfig: Codable {
    var frequencyMHz: Double = 915.0      // RF frequency in MHz
    var bitrateKbps: Double = 96.0        // Bit rate in kbps
    var deviationKHz: Double = 50.0       // Frequency deviation in kHz
    var bandwidthKHz: Double = 467.0      // RX bandwidth in kHz
    var preambleBits: Int = 32            // Preamble length in bits
    
    // Generate configuration command for modem
    var configCommand: String {
        return String(format: "CFG:%.1f,%.1f,%.1f,%.1f,%d\n",
                     frequencyMHz, bitrateKbps, deviationKHz, bandwidthKHz, preambleBits)
    }
    
    // Parse confirmation response from modem
    static func parseConfirmation(_ response: String) -> ModemConfig? {
        guard response.hasPrefix("CFG_OK:") else { return nil }
        let params = response.dropFirst(7)
        let parts = params.split(separator: ",")
        guard parts.count == 5 else { return nil }
        
        guard let freq = Double(parts[0]),
              let bitrate = Double(parts[1]),
              let deviation = Double(parts[2]),
              let bandwidth = Double(parts[3]),
              let preamble = Int(parts[4].trimmingCharacters(in: .whitespacesAndNewlines)) else {
            return nil
        }
        
        return ModemConfig(
            frequencyMHz: freq,
            bitrateKbps: bitrate,
            deviationKHz: deviation,
            bandwidthKHz: bandwidth,
            preambleBits: preamble
        )
    }
}

// MARK: - Packet Types

enum PacketType: UInt8 {
    // Air -> Ground
    case telemetry = 0x00
    case imageMeta = 0x01
    case imageData = 0x02
    case textMessage = 0x03
    case commandAck = 0x10
    
    // Ground -> Air
    case cmdPing = 0x80
    case cmdSetParam = 0x81
    case cmdCapture = 0x82
    case cmdReboot = 0x83
    
    case unknown = 0xFF
    
    var name: String {
        switch self {
        case .telemetry: return "Telemetry"
        case .imageMeta: return "ImageMeta"
        case .imageData: return "ImageData"
        case .textMessage: return "Text"
        case .commandAck: return "CmdAck"
        case .cmdPing: return "CmdPing"
        case .cmdSetParam: return "CmdSetParam"
        case .cmdCapture: return "CmdCapture"
        case .cmdReboot: return "CmdReboot"
        case .unknown: return "Unknown"
        }
    }
}

// MARK: - GPS Fix Type

enum FixType: UInt8 {
    case none = 0
    case fix2D = 1
    case fix3D = 2
    
    var description: String {
        switch self {
        case .none: return "No Fix"
        case .fix2D: return "2D Fix"
        case .fix3D: return "3D Fix"
        }
    }
}

// MARK: - Packet Flags

struct PacketFlags: OptionSet {
    let rawValue: UInt8
    
    static let none = PacketFlags([])
    static let urgent = PacketFlags(rawValue: 0x01)
    static let retransmit = PacketFlags(rawValue: 0x02)
    static let lastPacket = PacketFlags(rawValue: 0x04)
    static let compressed = PacketFlags(rawValue: 0x08)
}

// MARK: - Packet Header

struct PacketHeader {
    let packetType: PacketType
    let sequence: UInt16
    let flags: PacketFlags
    
    static func deserialize(from data: Data) -> PacketHeader? {
        guard data.count >= 4 else { return nil }
        
        let type = PacketType(rawValue: data[0]) ?? .unknown
        let sequence = UInt16(data[1]) << 8 | UInt16(data[2])
        let flags = PacketFlags(rawValue: data[3])
        
        return PacketHeader(packetType: type, sequence: sequence, flags: flags)
    }
}

// MARK: - Telemetry Payload

struct TelemetryPayload {
    var latitude: Double = 0.0          // degrees
    var longitude: Double = 0.0         // degrees
    var altitude: Double = 0.0          // meters
    var speed: Double = 0.0             // m/s
    var heading: Double = 0.0           // degrees
    var satellites: UInt8 = 0
    var fixType: FixType = .none
    var gpsTime: UInt32 = 0             // Unix timestamp
    var batteryMV: UInt16 = 0           // millivolts
    var cpuTemp: Double = 0.0           // Celsius
    var radioTemp: Double = 0.0         // Celsius
    var imageId: UInt16 = 0
    var imageProgress: UInt8 = 0        // percent
    var rssi: Int8 = 0                  // dBm
    
    static func deserialize(from data: Data) -> TelemetryPayload? {
        guard data.count >= RaptorProtocol.telemetryPayloadSize else { return nil }
        
        var payload = TelemetryPayload()
        var offset = 0
        
        // Latitude (int32, scaled by 1e7)
        let latRaw = Int32(bigEndian: data.subdata(in: offset..<offset+4).withUnsafeBytes { $0.load(as: Int32.self) })
        payload.latitude = Double(latRaw) / 1e7
        offset += 4
        
        // Longitude (int32, scaled by 1e7)
        let lonRaw = Int32(bigEndian: data.subdata(in: offset..<offset+4).withUnsafeBytes { $0.load(as: Int32.self) })
        payload.longitude = Double(lonRaw) / 1e7
        offset += 4
        
        // Altitude (uint32, scaled by 1000)
        let altRaw = UInt32(bigEndian: data.subdata(in: offset..<offset+4).withUnsafeBytes { $0.load(as: UInt32.self) })
        payload.altitude = Double(altRaw) / 1000.0
        offset += 4
        
        // Speed (uint16, scaled by 100)
        let speedRaw = UInt16(bigEndian: data.subdata(in: offset..<offset+2).withUnsafeBytes { $0.load(as: UInt16.self) })
        payload.speed = Double(speedRaw) / 100.0
        offset += 2
        
        // Heading (uint16, scaled by 100)
        let headingRaw = UInt16(bigEndian: data.subdata(in: offset..<offset+2).withUnsafeBytes { $0.load(as: UInt16.self) })
        payload.heading = Double(headingRaw) / 100.0
        offset += 2
        
        // Satellites (uint8)
        payload.satellites = data[offset]
        offset += 1
        
        // Fix type (uint8)
        payload.fixType = FixType(rawValue: data[offset]) ?? .none
        offset += 1
        
        // GPS time (uint32)
        payload.gpsTime = UInt32(bigEndian: data.subdata(in: offset..<offset+4).withUnsafeBytes { $0.load(as: UInt32.self) })
        offset += 4
        
        // Battery mV (uint16)
        payload.batteryMV = UInt16(bigEndian: data.subdata(in: offset..<offset+2).withUnsafeBytes { $0.load(as: UInt16.self) })
        offset += 2
        
        // CPU temp (int16, scaled by 100)
        let cpuTempRaw = Int16(bigEndian: data.subdata(in: offset..<offset+2).withUnsafeBytes { $0.load(as: Int16.self) })
        payload.cpuTemp = Double(cpuTempRaw) / 100.0
        offset += 2
        
        // Radio temp (int16, scaled by 100)
        let radioTempRaw = Int16(bigEndian: data.subdata(in: offset..<offset+2).withUnsafeBytes { $0.load(as: Int16.self) })
        payload.radioTemp = Double(radioTempRaw) / 100.0
        offset += 2
        
        // Image ID (uint16)
        payload.imageId = UInt16(bigEndian: data.subdata(in: offset..<offset+2).withUnsafeBytes { $0.load(as: UInt16.self) })
        offset += 2
        
        // Image progress (uint8)
        payload.imageProgress = data[offset]
        offset += 1
        
        // RSSI (int8)
        payload.rssi = Int8(bitPattern: data[offset])
        
        return payload
    }
    
    func serialize() -> Data {
        var data = Data()
        
        func appendBE<T: FixedWidthInteger>(_ value: T) {
            var v = value.bigEndian
            data.append(contentsOf: withUnsafeBytes(of: &v) { Array($0) })
        }
        
        appendBE(Int32(latitude * 1e7))
        appendBE(Int32(longitude * 1e7))
        appendBE(UInt32(altitude * 1000))
        appendBE(UInt16(speed * 100))
        appendBE(UInt16(heading * 100))
        data.append(satellites)
        data.append(fixType.rawValue)
        appendBE(gpsTime)
        appendBE(batteryMV)
        appendBE(Int16(cpuTemp * 100))
        appendBE(Int16(radioTemp * 100))
        appendBE(imageId)
        data.append(imageProgress)
        data.append(UInt8(bitPattern: rssi))
        data.append(contentsOf: [0, 0, 0, 0])  // reserved
        
        return data
    }
}

// MARK: - Image Meta Payload

struct ImageMetaPayload {
    var imageId: UInt16 = 0
    var totalSize: UInt32 = 0
    var width: UInt16 = 0
    var height: UInt16 = 0
    var numSourceSymbols: UInt16 = 0
    var symbolSize: UInt16 = 200
    var crc32: UInt32 = 0
    
    static func deserialize(from data: Data) -> ImageMetaPayload? {
        guard data.count >= RaptorProtocol.imageMetaPayloadSize else { return nil }
        
        var payload = ImageMetaPayload()
        var offset = 0
        
        payload.imageId = UInt16(bigEndian: data.subdata(in: offset..<offset+2).withUnsafeBytes { $0.load(as: UInt16.self) })
        offset += 2
        
        payload.totalSize = UInt32(bigEndian: data.subdata(in: offset..<offset+4).withUnsafeBytes { $0.load(as: UInt32.self) })
        offset += 4
        
        payload.width = UInt16(bigEndian: data.subdata(in: offset..<offset+2).withUnsafeBytes { $0.load(as: UInt16.self) })
        offset += 2
        
        payload.height = UInt16(bigEndian: data.subdata(in: offset..<offset+2).withUnsafeBytes { $0.load(as: UInt16.self) })
        offset += 2
        
        payload.numSourceSymbols = UInt16(bigEndian: data.subdata(in: offset..<offset+2).withUnsafeBytes { $0.load(as: UInt16.self) })
        offset += 2
        
        payload.symbolSize = UInt16(bigEndian: data.subdata(in: offset..<offset+2).withUnsafeBytes { $0.load(as: UInt16.self) })
        offset += 2
        
        payload.crc32 = UInt32(bigEndian: data.subdata(in: offset..<offset+4).withUnsafeBytes { $0.load(as: UInt32.self) })
        
        return payload
    }
}

// MARK: - Image Data Payload

struct ImageDataPayload {
    var imageId: UInt16 = 0
    var symbolId: UInt32 = 0
    var esi: UInt32 = 0
    var symbolData: Data = Data()
    
    static func deserialize(from data: Data) -> ImageDataPayload? {
        guard data.count >= 10 else { return nil }
        
        var payload = ImageDataPayload()
        
        payload.imageId = UInt16(bigEndian: data.subdata(in: 0..<2).withUnsafeBytes { $0.load(as: UInt16.self) })
        payload.symbolId = UInt32(bigEndian: data.subdata(in: 2..<6).withUnsafeBytes { $0.load(as: UInt32.self) })
        
        let raptorqData = data.subdata(in: 6..<data.count)
        
        if raptorqData.count >= 4 {
            payload.esi = UInt32(bigEndian: raptorqData.subdata(in: 0..<4).withUnsafeBytes { $0.load(as: UInt32.self) })
        }
        
        payload.symbolData = raptorqData
        
        return payload
    }
}

// MARK: - Text Message Payload

struct TextMessagePayload {
    var message: String = ""
    
    static func deserialize(from data: Data) -> TextMessagePayload? {
        guard let message = String(data: data, encoding: .utf8) else { return nil }
        return TextMessagePayload(message: message)
    }
}

// MARK: - CRC-32 Implementation

struct CRC32 {
    private static let polynomial: UInt32 = 0xEDB88320
    private static var table: [UInt32] = {
        var table = [UInt32](repeating: 0, count: 256)
        for i in 0..<256 {
            var crc = UInt32(i)
            for _ in 0..<8 {
                if crc & 1 != 0 {
                    crc = (crc >> 1) ^ polynomial
                } else {
                    crc >>= 1
                }
            }
            table[i] = crc
        }
        return table
    }()
    
    static func calculate(data: Data, initial: UInt32 = 0xFFFFFFFF) -> UInt32 {
        var crc = initial
        for byte in data {
            let index = Int((crc ^ UInt32(byte)) & 0xFF)
            crc = table[index] ^ (crc >> 8)
        }
        return crc ^ 0xFFFFFFFF
    }
    
    static func verify(packet: Data) -> Bool {
        guard packet.count >= 4 else { return false }
        
        let dataWithoutCRC = packet.subdata(in: 0..<packet.count-4)
        let receivedCRC = packet.subdata(in: packet.count-4..<packet.count)
            .withUnsafeBytes { $0.load(as: UInt32.self) }
            .bigEndian
        
        let calculatedCRC = calculate(data: dataWithoutCRC)
        
        return calculatedCRC == receivedCRC
    }
}

// MARK: - Packet Parser

struct PacketParser {
    
    static func parse(_ data: Data) -> (PacketType, UInt16, PacketFlags, Data)? {
        guard data.count >= RaptorProtocol.headerSize + RaptorProtocol.crcSize else {
            return nil
        }
        
        // Find sync word
        var startIndex = 0
        if data.count >= 4 {
            let syncData = data.subdata(in: 0..<4)
            if syncData.elementsEqual(RaptorProtocol.syncWord) {
                startIndex = 0
            }
        }
        
        guard data.count >= startIndex + RaptorProtocol.headerSize else {
            return nil
        }
        
        // Parse header
        let headerStart = startIndex + RaptorProtocol.syncSize
        let headerData = data.subdata(in: headerStart..<headerStart+4)
        
        guard let header = PacketHeader.deserialize(from: headerData) else {
            return nil
        }
        
        // Calculate expected packet length
        let expectedPayloadLen = getExpectedPayloadLength(for: header.packetType)
        let expectedTotal = RaptorProtocol.headerSize + expectedPayloadLen + RaptorProtocol.crcSize
        
        guard data.count >= startIndex + expectedTotal else {
            return nil
        }
        
        // Extract actual packet
        let actualPacket = data.subdata(in: startIndex..<startIndex + expectedTotal)
        
        // Verify CRC
        guard CRC32.verify(packet: actualPacket) else {
            return nil
        }
        
        // Extract payload
        let payloadStart = RaptorProtocol.headerSize
        let payloadEnd = actualPacket.count - RaptorProtocol.crcSize
        let payload = actualPacket.subdata(in: payloadStart..<payloadEnd)
        
        return (header.packetType, header.sequence, header.flags, payload)
    }
    
    private static func getExpectedPayloadLength(for type: PacketType) -> Int {
        switch type {
        case .telemetry:
            return RaptorProtocol.telemetryPayloadSize
        case .imageMeta:
            return RaptorProtocol.imageMetaPayloadSize
        case .imageData:
            return 2 + 4 + 4 + RaptorProtocol.fountainSymbolSize
        case .textMessage:
            return RaptorProtocol.maxPayloadSize
        case .commandAck:
            return 4
        default:
            return RaptorProtocol.maxPayloadSize
        }
    }
}

// MARK: - Received Packet

struct ReceivedPacket {
    let timestamp: Date
    let rssi: Double
    let snr: Float
    let type: PacketType
    let sequence: UInt16
    let flags: PacketFlags
    let payload: Data
    let rawData: Data
    
    var telemetry: TelemetryPayload? {
        guard type == .telemetry else { return nil }
        return TelemetryPayload.deserialize(from: payload)
    }
    
    var imageMeta: ImageMetaPayload? {
        guard type == .imageMeta else { return nil }
        return ImageMetaPayload.deserialize(from: payload)
    }
    
    var imageData: ImageDataPayload? {
        guard type == .imageData else { return nil }
        return ImageDataPayload.deserialize(from: payload)
    }
    
    var textMessage: TextMessagePayload? {
        guard type == .textMessage else { return nil }
        return TextMessagePayload.deserialize(from: payload)
    }
}
