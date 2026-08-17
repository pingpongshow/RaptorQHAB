//
//  GPSManager.swift
//  RaptorHabGS
//
//  Manages external USB GPS devices for ground station position
//  Parses NMEA 0183 sentences from serial GPS receivers
//

import Foundation
import CoreLocation
import Combine
import Darwin

// MARK: - GPS Data Model

struct GPSPosition: Equatable {
    var latitude: Double
    var longitude: Double
    var altitude: Double  // meters
    var speed: Double     // m/s
    var course: Double    // degrees true
    var satellites: Int
    var fixQuality: GPSFixQuality
    var hdop: Double
    var timestamp: Date
    
    var coordinate: CLLocationCoordinate2D {
        CLLocationCoordinate2D(latitude: latitude, longitude: longitude)
    }
    
    var isValid: Bool {
        fixQuality != .noFix && latitude != 0 && longitude != 0
    }
    
    static func == (lhs: GPSPosition, rhs: GPSPosition) -> Bool {
        lhs.latitude == rhs.latitude && lhs.longitude == rhs.longitude && lhs.altitude == rhs.altitude
    }
}

enum GPSFixQuality: Int, CustomStringConvertible {
    case noFix = 0
    case gpsFix = 1
    case dgpsFix = 2
    case ppsFix = 3
    case rtkFixed = 4
    case rtkFloat = 5
    case estimated = 6
    case manual = 7
    case simulation = 8
    
    var description: String {
        switch self {
        case .noFix: return "No Fix"
        case .gpsFix: return "GPS"
        case .dgpsFix: return "DGPS"
        case .ppsFix: return "PPS"
        case .rtkFixed: return "RTK Fixed"
        case .rtkFloat: return "RTK Float"
        case .estimated: return "Estimated"
        case .manual: return "Manual"
        case .simulation: return "Simulation"
        }
    }
}

// MARK: - Bearing and Distance

struct BearingDistance {
    var bearing: Double      // degrees true (0-360)
    var distance: Double     // meters
    var elevation: Double    // degrees (positive = look up)
    var altitudeDiff: Double // meters
    
    var bearingCardinal: String {
        let directions = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                          "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
        let index = Int((bearing + 11.25) / 22.5) % 16
        return directions[index]
    }
    
    var distanceFormatted: String {
        if distance < 1000 {
            return String(format: "%.0f m", distance)
        } else {
            return String(format: "%.2f km", distance / 1000)
        }
    }
    
    var distanceMiles: String {
        let miles = distance / 1609.344
        if miles < 1 {
            let feet = distance * 3.28084
            return String(format: "%.0f ft", feet)
        }
        return String(format: "%.2f mi", miles)
    }
}

// MARK: - GPS Manager

class GPSManager: ObservableObject {
    
    static let shared = GPSManager()
    
    // Published properties
    @Published var isConnected = false
    @Published var currentPosition: GPSPosition?
    @Published var bearingToPayload: BearingDistance?
    @Published var availablePorts: [String] = []
    @Published var selectedPort: String = ""
    @Published var statusMessage: String = "Disconnected"
    @Published var baudRate: Int = 9600
    @Published var lastNMEAReceived: Date?  // Track when we last got valid NMEA data
    
    // Check if NMEA data is actively being received (within last 3 seconds)
    var isReceivingData: Bool {
        guard let lastReceived = lastNMEAReceived else { return false }
        return Date().timeIntervalSince(lastReceived) < 3.0
    }
    
    // Serial port
    private var fileDescriptor: Int32 = -1
    private var readThread: Thread?
    private var shouldRead = false
    private var receiveBuffer = ""
    
    // NMEA parsing state
    private var pendingPosition = GPSPosition(
        latitude: 0, longitude: 0, altitude: 0, speed: 0, course: 0,
        satellites: 0, fixQuality: .noFix, hdop: 99.9, timestamp: Date()
    )
    
    private init() {
        refreshPorts()
    }
    
    deinit {
        disconnect()
    }
    
    // MARK: - Port Management
    
    func refreshPorts() {
        var ports: [String] = []
        
        if let devices = try? FileManager.default.contentsOfDirectory(atPath: "/dev") {
            for device in devices {
                // USB serial adapters commonly used for GPS
                if device.hasPrefix("cu.usbserial") ||
                   device.hasPrefix("cu.usbmodem") ||
                   device.hasPrefix("cu.SLAB_USBtoUART") ||  // CP210x
                   device.hasPrefix("cu.wchusbserial") ||    // CH340
                   device.hasPrefix("cu.PL2303") ||          // Prolific
                   device.contains("GPS") ||
                   device.contains("gps") {
                    ports.append("/dev/" + device)
                }
            }
        }
        
        availablePorts = ports.sorted()
        
        // Auto-select if one port found
        if selectedPort.isEmpty && ports.count == 1 {
            selectedPort = ports[0]
        }
    }
    
    // MARK: - Connection
    
    func connect() {
        guard !selectedPort.isEmpty else {
            statusMessage = "No port selected"
            return
        }
        
        disconnect()
        
        // Open serial port with nonblocking to prevent hanging on open
        fileDescriptor = open(selectedPort, O_RDWR | O_NOCTTY | O_NONBLOCK)
        
        guard fileDescriptor != -1 else {
            statusMessage = "Failed to open port"
            return
        }
        
        // Configure port - use cfmakeraw for simplest raw mode
        var options = termios()
        tcgetattr(fileDescriptor, &options)
        
        // Set to raw mode (minimal processing)
        cfmakeraw(&options)
        
        // Set baud rate (may be ignored by USB CDC devices)
        let speed: speed_t
        switch baudRate {
        case 4800: speed = speed_t(B4800)
        case 9600: speed = speed_t(B9600)
        case 19200: speed = speed_t(B19200)
        case 38400: speed = speed_t(B38400)
        case 57600: speed = speed_t(B57600)
        case 115200: speed = speed_t(B115200)
        default: speed = speed_t(B9600)
        }
        cfsetspeed(&options, speed)
        
        // Enable receiver and set local mode
        options.c_cflag |= UInt(CLOCAL | CREAD)
        
        // Apply settings
        tcsetattr(fileDescriptor, TCSANOW, &options)
        
        // Flush any pending data
        tcflush(fileDescriptor, TCIOFLUSH)
        
        isConnected = true
        let portName = selectedPort.components(separatedBy: "/").last ?? selectedPort
        statusMessage = "Connected: \(portName)"
        
        // Start reading thread
        shouldRead = true
        readThread = Thread { [weak self] in
            self?.readLoop()
        }
        readThread?.qualityOfService = .userInitiated
        readThread?.start()
    }
    
    func disconnect() {
        shouldRead = false
        readThread?.cancel()
        readThread = nil
        
        if fileDescriptor != -1 {
            close(fileDescriptor)
            fileDescriptor = -1
        }
        
        isConnected = false
        statusMessage = "Disconnected"
        currentPosition = nil
        bearingToPayload = nil
        lastNMEAReceived = nil
    }
    
    // MARK: - Serial Reading
    
    private func readLoop() {
        var buffer = [UInt8](repeating: 0, count: 512)
        
        while shouldRead && fileDescriptor != -1 {
            let bytesRead = read(fileDescriptor, &buffer, buffer.count)
            
            if bytesRead > 0 {
                // Process as raw bytes directly
                let data = Data(buffer[0..<bytesRead])
                processBytes(data)
                
            } else if bytesRead == 0 {
                // Timeout or EOF - normal for VTIME (50ms = 20 reads/sec max)
                Thread.sleep(forTimeInterval: 0.05)
            } else {
                // Error
                let err = errno
                if err == EAGAIN || err == EWOULDBLOCK {
                    // No data available in non-blocking mode (50ms = 20 reads/sec max)
                    Thread.sleep(forTimeInterval: 0.05)
                } else if err != EINTR {
                    DispatchQueue.main.async { [weak self] in
                        self?.disconnect()
                        self?.statusMessage = "Read error: \(err)"
                    }
                    break
                }
            }
        }
    }
    
    private var byteBuffer = Data()
    
    private func processData(_ data: String) {
        // Convert string back to data and process as bytes
        if let newData = data.data(using: .ascii) {
            processBytes(newData)
        }
    }
    
    private func processBytes(_ data: Data) {
        byteBuffer.append(data)
        
        // Look for complete NMEA sentences (end with CR LF or just LF)
        while true {
            // Find $ which starts an NMEA sentence
            guard let dollarIndex = byteBuffer.firstIndex(of: 0x24) else { // 0x24 = '$'
                byteBuffer.removeAll()
                break
            }
            
            // Remove any garbage before the $
            if dollarIndex > byteBuffer.startIndex {
                byteBuffer.removeSubrange(byteBuffer.startIndex..<dollarIndex)
            }
            
            // Look for LF (0x0A) which ends the sentence
            guard let lfIndex = byteBuffer.firstIndex(of: 0x0A) else { // 0x0A = '\n'
                break
            }
            
            // Extract the sentence (without CR LF)
            var endIndex = lfIndex
            if endIndex > byteBuffer.startIndex {
                let prevIndex = byteBuffer.index(before: endIndex)
                if byteBuffer[prevIndex] == 0x0D { // 0x0D = '\r'
                    endIndex = prevIndex
                }
            }
            
            let sentenceData = byteBuffer[byteBuffer.startIndex..<endIndex]
            if let sentence = String(data: sentenceData, encoding: .ascii) {
                parseNMEASentence(sentence)
            }
            
            // Remove processed sentence from buffer (including CR LF)
            let removeEnd = byteBuffer.index(after: lfIndex)
            byteBuffer.removeSubrange(byteBuffer.startIndex..<removeEnd)
        }
        
        // Prevent buffer overflow
        if byteBuffer.count > 4096 {
            byteBuffer.removeAll()
        }
    }
    
    // MARK: - NMEA Parsing
    
    private func parseNMEASentence(_ sentence: String) {
        // Validate checksum first
        guard validateChecksum(sentence) else { return }
        
        // Update activity timestamp on valid NMEA sentence
        DispatchQueue.main.async { [weak self] in
            self?.lastNMEAReceived = Date()
        }
        
        let parts = sentence.components(separatedBy: "*")[0]
        let fields = parts.components(separatedBy: ",")
        guard fields.count > 0 else { return }
        
        let type = fields[0]
        
        if type.hasSuffix("GGA") {
            parseGGA(fields)
        } else if type.hasSuffix("RMC") {
            parseRMC(fields)
        } else if type.hasSuffix("VTG") {
            parseVTG(fields)
        }
    }
    
    private func validateChecksum(_ sentence: String) -> Bool {
        guard sentence.hasPrefix("$"),
              let asteriskIndex = sentence.firstIndex(of: "*") else { return false }
        
        let data = sentence[sentence.index(after: sentence.startIndex)..<asteriskIndex]
        let checksumStr = String(sentence[sentence.index(after: asteriskIndex)...]).prefix(2)
        
        var checksum: UInt8 = 0
        for char in data.utf8 {
            checksum ^= char
        }
        
        return String(format: "%02X", checksum) == checksumStr.uppercased()
    }
    
    // GGA - GPS Fix Data
    private func parseGGA(_ fields: [String]) {
        guard fields.count >= 15 else { return }
        
        let lat = parseLatitude(fields[2], direction: fields[3])
        let lon = parseLongitude(fields[4], direction: fields[5])
        let quality = Int(fields[6]) ?? 0
        let satellites = Int(fields[7]) ?? 0
        let hdop = Double(fields[8]) ?? 99.9
        let altitude = Double(fields[9]) ?? 0
        
        pendingPosition.latitude = lat
        pendingPosition.longitude = lon
        pendingPosition.altitude = altitude
        pendingPosition.satellites = satellites
        pendingPosition.fixQuality = GPSFixQuality(rawValue: quality) ?? .noFix
        pendingPosition.hdop = hdop
        pendingPosition.timestamp = Date()
        
        if pendingPosition.isValid {
            publishPosition()
        }
    }
    
    // RMC - Recommended Minimum
    private func parseRMC(_ fields: [String]) {
        guard fields.count >= 12, fields[2] == "A" else { return }
        
        let lat = parseLatitude(fields[3], direction: fields[4])
        let lon = parseLongitude(fields[5], direction: fields[6])
        let speedKnots = Double(fields[7]) ?? 0
        let course = Double(fields[8]) ?? 0
        
        pendingPosition.latitude = lat
        pendingPosition.longitude = lon
        pendingPosition.speed = speedKnots * 0.514444
        pendingPosition.course = course
        pendingPosition.timestamp = Date()
        
        if pendingPosition.fixQuality == .noFix {
            pendingPosition.fixQuality = .gpsFix
        }
        
        if pendingPosition.isValid {
            publishPosition()
        }
    }
    
    // VTG - Course Over Ground
    private func parseVTG(_ fields: [String]) {
        guard fields.count >= 9 else { return }
        
        if let course = Double(fields[1]), course > 0 {
            pendingPosition.course = course
        }
        if let speedKmh = Double(fields[7]) {
            pendingPosition.speed = speedKmh / 3.6
        }
    }
    
    private func parseLatitude(_ value: String, direction: String) -> Double {
        guard value.count >= 4 else { return 0 }
        let degrees = Double(value.prefix(2)) ?? 0
        let minutes = Double(value.dropFirst(2)) ?? 0
        var result = degrees + minutes / 60.0
        if direction == "S" { result = -result }
        return result
    }
    
    private func parseLongitude(_ value: String, direction: String) -> Double {
        guard value.count >= 5 else { return 0 }
        let degrees = Double(value.prefix(3)) ?? 0
        let minutes = Double(value.dropFirst(3)) ?? 0
        var result = degrees + minutes / 60.0
        if direction == "W" { result = -result }
        return result
    }
    
    private func publishPosition() {
        let pos = pendingPosition
        DispatchQueue.main.async { [weak self] in
            self?.currentPosition = pos
        }
    }
    
    // MARK: - Bearing/Distance Calculation
    
    func updateBearing(toLatitude lat: Double, toLongitude lon: Double, toAltitude alt: Double) {
        guard let pos = currentPosition, pos.isValid else {
            DispatchQueue.main.async { [weak self] in
                self?.bearingToPayload = nil
            }
            return
        }
        
        let bearing = calculateBearing(from: pos.coordinate,
                                        to: CLLocationCoordinate2D(latitude: lat, longitude: lon))
        let distance = calculateDistance(from: pos.coordinate,
                                          to: CLLocationCoordinate2D(latitude: lat, longitude: lon))
        let altDiff = alt - pos.altitude
        let elevation = atan2(altDiff, distance) * 180 / .pi
        
        DispatchQueue.main.async { [weak self] in
            self?.bearingToPayload = BearingDistance(
                bearing: bearing,
                distance: distance,
                elevation: elevation,
                altitudeDiff: altDiff
            )
        }
    }
    
    private func calculateBearing(from: CLLocationCoordinate2D, to: CLLocationCoordinate2D) -> Double {
        let lat1 = from.latitude * .pi / 180
        let lat2 = to.latitude * .pi / 180
        let dLon = (to.longitude - from.longitude) * .pi / 180
        
        let y = sin(dLon) * cos(lat2)
        let x = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dLon)
        
        let bearing = atan2(y, x) * 180 / .pi
        return (bearing + 360).truncatingRemainder(dividingBy: 360)
    }
    
    private func calculateDistance(from: CLLocationCoordinate2D, to: CLLocationCoordinate2D) -> Double {
        let R = 6371000.0  // Earth radius in meters
        
        let lat1 = from.latitude * .pi / 180
        let lat2 = to.latitude * .pi / 180
        let dLat = (to.latitude - from.latitude) * .pi / 180
        let dLon = (to.longitude - from.longitude) * .pi / 180
        
        let a = sin(dLat/2) * sin(dLat/2) +
                cos(lat1) * cos(lat2) * sin(dLon/2) * sin(dLon/2)
        let c = 2 * atan2(sqrt(a), sqrt(1-a))
        
        return R * c
    }
}
