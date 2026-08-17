//
//  SerialPortManager.swift
//  RaptorHabGS
//
//  Manages serial port communication with Heltec SX1262 bridge
//  Receives framed packets and passes them to the ground station
//

import Foundation
import IOKit
import IOKit.serial

// MARK: - Serial Port Manager

class SerialPortManager: ObservableObject, @unchecked Sendable {
    
    // MARK: - Published State
    
    @Published var isConnected = false
    @Published var availablePorts: [String] = []
    @Published var selectedPort: String = ""
    @Published var packetsReceived: Int = 0
    @Published var lastRSSI: Float = 0
    @Published var lastSNR: Float = 0
    @Published var bytesReceived: Int = 0
    
    // Stats for debugging
    @Published var framesExtracted: Int = 0
    @Published var checksumFailures: Int = 0
    @Published var noRaptFailures: Int = 0
    
    // MARK: - Configuration
    
    static let baudRate: speed_t = 921600
    static let frameDelimiter: UInt8 = 0x7E
    
    // MARK: - Callbacks
    
    var onPacketReceived: ((Data, Float, Float) -> Void)?  // (packet, rssi, snr)
    var onError: ((String) -> Void)?
    
    // MARK: - Private State
    
    private var fileDescriptor: Int32 = -1
    private var readThread: Thread?
    private var shouldRun = false
    private var rxBuffer = Data()
    private let bufferLock = NSLock()  // Thread safety for rxBuffer
    
    // MARK: - Debug
    
    static var debugEnabled = true
    
    private func debugLog(_ message: String) {
        if SerialPortManager.debugEnabled {
        }
    }
    
    // MARK: - Initialization
    
    init() {
        refreshAvailablePorts()
    }
    
    deinit {
        disconnect()
    }
    
    // MARK: - Port Discovery
    
    func refreshAvailablePorts() {
        var ports: [String] = []
        
        // Find all serial ports
        let matchingDict = IOServiceMatching(kIOSerialBSDServiceValue)
        var iterator: io_iterator_t = 0
        
        let result = IOServiceGetMatchingServices(kIOMainPortDefault, matchingDict, &iterator)
        guard result == KERN_SUCCESS else {
            debugLog("Failed to get matching services")
            return
        }
        
        var service = IOIteratorNext(iterator)
        while service != 0 {
            if let pathCF = IORegistryEntryCreateCFProperty(service, kIOCalloutDeviceKey as CFString, kCFAllocatorDefault, 0) {
                let path = pathCF.takeRetainedValue() as! String
                // Filter for USB serial ports (typical names)
                if path.contains("usbserial") || path.contains("usbmodem") || path.contains("cu.") {
                    ports.append(path)
                }
            }
            IOObjectRelease(service)
            service = IOIteratorNext(iterator)
        }
        IOObjectRelease(iterator)
        
        DispatchQueue.main.async {
            self.availablePorts = ports.sorted()
            self.debugLog("Found ports: \(ports)")
        }
    }
    
    // MARK: - Connection Management
    
    func connect(to port: String) -> Bool {
        guard !isConnected else {
            debugLog("Already connected")
            return true
        }
        
        debugLog("Connecting to \(port)...")
        
        // Open port
        fileDescriptor = open(port, O_RDWR | O_NOCTTY | O_NONBLOCK)
        guard fileDescriptor >= 0 else {
            let error = String(cString: strerror(errno))
            debugLog("Failed to open port: \(error)")
            onError?("Failed to open \(port): \(error)")
            return false
        }
        
        // Configure port
        var options = termios()
        tcgetattr(fileDescriptor, &options)
        
        // Set baud rate
        cfsetispeed(&options, Self.baudRate)
        cfsetospeed(&options, Self.baudRate)
        
        // 8N1, no flow control
        options.c_cflag &= ~UInt(PARENB)  // No parity
        options.c_cflag &= ~UInt(CSTOPB)  // 1 stop bit
        options.c_cflag &= ~UInt(CSIZE)
        options.c_cflag |= UInt(CS8)       // 8 data bits
        options.c_cflag &= ~UInt(CRTSCTS) // No hardware flow control
        options.c_cflag |= UInt(CLOCAL | CREAD)
        
        // Raw input
        options.c_lflag &= ~UInt(ICANON | ECHO | ECHOE | ISIG)
        options.c_iflag &= ~UInt(IXON | IXOFF | IXANY)
        options.c_iflag &= ~UInt(ICRNL | INLCR)
        
        // Raw output
        options.c_oflag &= ~UInt(OPOST)
        
        // Set VMIN and VTIME using withUnsafeMutableBytes
        // VMIN = 0 (non-blocking), VTIME = 1 (0.1 second timeout)
        withUnsafeMutableBytes(of: &options.c_cc) { ptr in
            ptr[Int(VMIN)] = 0
            ptr[Int(VTIME)] = 1
        }
        
        // Apply settings
        tcsetattr(fileDescriptor, TCSANOW, &options)
        
        // Clear any pending data
        tcflush(fileDescriptor, TCIOFLUSH)
        
        // Start read thread
        shouldRun = true
        readThread = Thread { [weak self] in
            self?.readLoop()
        }
        readThread?.name = "SerialReadThread"
        readThread?.start()
        
        DispatchQueue.main.async {
            self.selectedPort = port
            self.isConnected = true
        }
        
        debugLog("Connected to \(port)")
        return true
    }
    
    func disconnect() {
        guard isConnected else { return }
        
        debugLog("Disconnecting...")
        
        // Signal read thread to stop
        shouldRun = false
        
        // Wait briefly for thread to exit
        Thread.sleep(forTimeInterval: 0.1)
        
        readThread?.cancel()
        readThread = nil
        
        if fileDescriptor >= 0 {
            close(fileDescriptor)
            fileDescriptor = -1
        }
        
        // Clear buffer with lock
        bufferLock.lock()
        rxBuffer.removeAll()
        bufferLock.unlock()
        
        // Clear config state
        configResponseBuffer = ""
        isConfigured = false
        
        DispatchQueue.main.async {
            self.isConnected = false
        }
        
        debugLog("Disconnected")
    }
    
    // MARK: - Write to Serial Port
    
    func write(_ data: Data) -> Bool {
        guard fileDescriptor >= 0 else {
            debugLog("Cannot write: not connected")
            return false
        }
        
        let result = data.withUnsafeBytes { ptr in
            Darwin.write(fileDescriptor, ptr.baseAddress, data.count)
        }
        
        if result < 0 {
            let error = String(cString: strerror(errno))
            debugLog("Write error: \(error)")
            return false
        }
        
        debugLog("Wrote \(result) bytes")
        return result == data.count
    }
    
    func write(_ string: String) -> Bool {
        guard let data = string.data(using: .utf8) else {
            debugLog("Failed to encode string")
            return false
        }
        return write(data)
    }
    
    // MARK: - Modem Configuration
    
    @Published var isConfigured = false
    @Published var configurationError: String?
    private var configResponseBuffer = ""
    private var configConfirmedConfig: ModemConfig?
    var onConfigResponse: ((String) -> Void)?
    
    /// Send configuration to modem and wait for confirmation
    /// Returns the confirmed configuration or nil on failure
    func configureModem(_ config: ModemConfig, timeout: TimeInterval = 5.0, maxRetries: Int = 3) -> ModemConfig? {
        debugLog("Configuring modem: \(config.configCommand.trimmingCharacters(in: .newlines))")
        
        configResponseBuffer = ""
        configConfirmedConfig = nil
        
        for attempt in 1...maxRetries {
            debugLog("Configuration attempt \(attempt)/\(maxRetries)")
            
            // Send configuration command
            guard write(config.configCommand) else {
                debugLog("Failed to send config command")
                continue
            }
            
            // Wait for response
            let startTime = Date()
            while Date().timeIntervalSince(startTime) < timeout {
                // Check if we got a valid confirmation
                if let confirmed = configConfirmedConfig {
                    DispatchQueue.main.async {
                        self.isConfigured = true
                        self.configurationError = nil
                    }
                    debugLog("Modem configured successfully")
                    return confirmed
                }
                
                // Check for error response
                if configResponseBuffer.contains("CFG_ERR:") {
                    let errorMsg = configResponseBuffer
                        .components(separatedBy: "CFG_ERR:").last?
                        .components(separatedBy: "\n").first ?? "Unknown error"
                    debugLog("Configuration error: \(errorMsg)")
                    DispatchQueue.main.async {
                        self.configurationError = errorMsg
                    }
                    configResponseBuffer = ""
                    break  // Try again
                }
                
                Thread.sleep(forTimeInterval: 0.05)
            }
            
            debugLog("Configuration timeout on attempt \(attempt)")
        }
        
        DispatchQueue.main.async {
            self.configurationError = "Failed to configure modem after \(maxRetries) attempts"
        }
        return nil
    }
    
    /// Process text lines from modem (for configuration responses)
    private func processTextLine(_ line: String) {
        debugLog("Modem text: \(line)")
        
        configResponseBuffer += line + "\n"
        
        // Check for configuration confirmation
        if line.hasPrefix("CFG_OK:") {
            if let confirmed = ModemConfig.parseConfirmation(line) {
                configConfirmedConfig = confirmed
            }
        }
        
        // Call callback if set
        onConfigResponse?(line)
    }
    
    // MARK: - Read Loop
    
    private func readLoop() {
        debugLog("Read loop started")
        
        let bufferSize = 1024
        let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: bufferSize)
        defer { buffer.deallocate() }
        
        while shouldRun && !Thread.current.isCancelled {
            // Check if file descriptor is still valid
            guard fileDescriptor >= 0 else {
                debugLog("File descriptor invalid, exiting read loop")
                break
            }
            
            let bytesRead = read(fileDescriptor, buffer, bufferSize)
            
            if bytesRead > 0 {
                let data = Data(bytes: buffer, count: bytesRead)
                
                DispatchQueue.main.async { [weak self] in
                    self?.bytesReceived += bytesRead
                }
                
                // Process received data
                processReceivedData(data)
                
            } else if bytesRead < 0 {
                let err = errno
                if err != EAGAIN && err != EWOULDBLOCK && err != EINTR {
                    let error = String(cString: strerror(err))
                    debugLog("Read error: \(error)")
                    break
                }
            }
            
            // Small delay to prevent busy loop (20ms = 50 reads/sec max)
            Thread.sleep(forTimeInterval: 0.02)
        }
        
        debugLog("Read loop ended")
    }
    
    // MARK: - Frame Parsing
    
    /*
     * Frame format from Heltec:
     * [0x7E][LEN_HI][LEN_LO][RSSI_INT][RSSI_FRAC][SNR_INT][SNR_FRAC][DATA...][CHECKSUM][0x7E]
     */
    
    private var textLineBuffer = ""
    
    private func processReceivedData(_ data: Data) {
        bufferLock.lock()
        rxBuffer.append(data)
        bufferLock.unlock()
        
        // First, check for text lines (modem status messages)
        extractTextLines()
        
        // Process complete frames
        while let frame = extractFrame() {
            DispatchQueue.main.async { [weak self] in
                self?.framesExtracted += 1
            }
            parseFrame(frame)
        }
        
        // Prevent buffer from growing too large
        bufferLock.lock()
        if rxBuffer.count > 10000 {
            debugLog("Buffer overflow, clearing")
            rxBuffer.removeAll()
        }
        bufferLock.unlock()
    }
    
    /// Extract and process text lines from the buffer (for modem status/config messages)
    private func extractTextLines() {
        var linesToProcess: [String] = []
        
        bufferLock.lock()
        
        // Look for newlines in the buffer and collect text lines
        while let newlineIndex = rxBuffer.firstIndex(of: 0x0A) {  // '\n'
            // Safety check
            let countToRemove = newlineIndex + 1
            guard countToRemove <= rxBuffer.count else {
                debugLog("Warning: Invalid newline index \(newlineIndex), buffer size \(rxBuffer.count)")
                break
            }
            
            let lineData = rxBuffer.prefix(upTo: newlineIndex)
            rxBuffer.removeFirst(countToRemove)  // Remove line including newline
            
            // Convert to string
            if let lineStr = String(data: Data(lineData), encoding: .utf8) {
                let trimmed = lineStr.trimmingCharacters(in: .whitespacesAndNewlines)
                
                // Check if this looks like a text line (not a binary frame)
                // Text lines start with '[' or 'CFG' and don't start with frame delimiter
                if !trimmed.isEmpty && lineData.first != Self.frameDelimiter {
                    if trimmed.hasPrefix("[") || trimmed.hasPrefix("CFG") || 
                       trimmed.hasPrefix("RaptorHab") || trimmed.hasPrefix("Heltec") ||
                       trimmed.hasPrefix("=") {
                        linesToProcess.append(trimmed)
                    }
                }
            }
        }
        
        bufferLock.unlock()
        
        // Process collected lines outside the lock to avoid race conditions
        for line in linesToProcess {
            processTextLine(line)
        }
    }
    
    private func extractFrame() -> Data? {
        bufferLock.lock()
        defer { bufferLock.unlock() }
        
        // Need at least start delimiter + some data + end delimiter
        guard rxBuffer.count >= 10 else { return nil }
        
        let bytes = [UInt8](rxBuffer)
        
        // Find start delimiter
        var startOffset = -1
        for i in 0..<bytes.count {
            if bytes[i] == Self.frameDelimiter {
                startOffset = i
                break
            }
        }
        
        guard startOffset >= 0 else { return nil }
        
        // Remove any data before start delimiter
        if startOffset > 0 {
            rxBuffer.removeFirst(startOffset)
            return nil  // Re-call to start fresh
        }
        
        // Find end delimiter - scan for 0x7E that's NOT part of escape sequence
        // In properly stuffed data, 0x7E only appears as delimiter
        // 0x7D 0x5E = escaped 0x7E data byte
        // 0x7D 0x5D = escaped 0x7D data byte
        var endOffset: Int? = nil
        var i = 1
        while i < bytes.count {
            if bytes[i] == Self.frameDelimiter {
                endOffset = i
                break
            }
            // Skip escape sequences
            if bytes[i] == 0x7D && i + 1 < bytes.count {
                i += 2  // Skip escape byte and following byte
            } else {
                i += 1
            }
        }
        
        guard let frameEnd = endOffset else {
            // No end delimiter yet
            if rxBuffer.count > 2000 {
                debugLog("Buffer too large (\(rxBuffer.count)), clearing")
                rxBuffer.removeAll()
            }
            return nil
        }
        
        // Extract stuffed frame (excluding delimiters)
        let stuffedData = Array(bytes[1..<frameEnd])
        
        // Remove frame from buffer (including both delimiters)
        rxBuffer.removeFirst(frameEnd + 1)
        
        // De-stuff the data (HDLC-style)
        var destuffed = [UInt8]()
        destuffed.reserveCapacity(stuffedData.count)
        
        i = 0
        while i < stuffedData.count {
            if stuffedData[i] == 0x7D && i + 1 < stuffedData.count {
                let nextByte = stuffedData[i + 1]
                if nextByte == 0x5E {
                    destuffed.append(0x7E)
                    i += 2
                } else if nextByte == 0x5D {
                    destuffed.append(0x7D)
                    i += 2
                } else {
                    // Invalid escape - just pass through
                    debugLog("Invalid escape sequence: 7D \(String(format: "%02X", nextByte))")
                    destuffed.append(stuffedData[i])
                    i += 1
                }
            } else {
                destuffed.append(stuffedData[i])
                i += 1
            }
        }
        
        // Debug: show stuffed vs destuffed size
        if stuffedData.count != destuffed.count {
            debugLog("De-stuffed: \(stuffedData.count) -> \(destuffed.count) bytes")
        }
        
        // Validate minimum frame size: len(2) + rssi(2) + snr(2) + data(1+) + checksum(1) = 8+
        guard destuffed.count >= 8 else {
            debugLog("Frame too short after de-stuffing: \(destuffed.count)")
            return nil
        }
        
        // Validate length field
        let lenHi = Int(destuffed[0])
        let lenLo = Int(destuffed[1])
        let dataLen = (lenHi << 8) | lenLo
        
        guard dataLen > 0 && dataLen <= 255 else {
            debugLog("Invalid frame length: \(dataLen)")
            return nil
        }
        
        // Expected: len(2) + rssi(2) + snr(2) + data(dataLen) + checksum(1) 
        let expectedSize = 2 + 2 + 2 + dataLen + 1
        guard destuffed.count >= expectedSize else {
            debugLog("Frame size mismatch: got \(destuffed.count), expected \(expectedSize) (dataLen=\(dataLen))")
            return nil
        }
        
        // Trim any extra bytes (shouldn't happen, but be safe)
        if destuffed.count > expectedSize {
            debugLog("Trimming \(destuffed.count - expectedSize) extra bytes")
            destuffed = Array(destuffed.prefix(expectedSize))
        }
        
        return Data(destuffed)
    }
    
    private func parseFrame(_ frame: Data) {
        // Frame format (after de-stuffing, no delimiters):
        // [LEN_HI][LEN_LO][RSSI_INT][RSSI_FRAC][SNR_INT][SNR_FRAC][DATA...][CHECKSUM]
        
        guard frame.count >= 8 else {
            debugLog("Frame too short: \(frame.count)")
            return
        }
        
        // Convert to byte array for safe indexing
        let bytes = [UInt8](frame)
        
        // Parse header
        let lenHi = Int(bytes[0])
        let lenLo = Int(bytes[1])
        let dataLen = (lenHi << 8) | lenLo
        
        let rssiInt = Int8(bitPattern: bytes[2])
        let rssiFrac = bytes[3]
        let snrInt = Int8(bitPattern: bytes[4])
        let snrFrac = bytes[5]
        
        // Calculate RSSI and SNR (handle negative values properly)
        var rssi = Float(rssiInt)
        if rssiInt < 0 {
            rssi -= Float(rssiFrac) / 100.0
        } else {
            rssi += Float(rssiFrac) / 100.0
        }
        
        var snr = Float(snrInt)
        if snrInt < 0 {
            snr -= Float(snrFrac) / 100.0
        } else {
            snr += Float(snrFrac) / 100.0
        }
        
        // Extract data (starts at offset 6, checksum is last byte)
        let dataStart = 6
        let dataEnd = dataStart + dataLen
        guard dataEnd <= bytes.count - 1 else {
            debugLog("Frame data bounds error: dataEnd=\(dataEnd), bytes.count=\(bytes.count)")
            return
        }
        
        let packetData = Data(bytes[dataStart..<dataEnd])
        
        // Verify checksum (XOR of all bytes except checksum)
        let receivedChecksum = bytes[dataEnd]
        var calculatedChecksum: UInt8 = 0
        for i in 0..<dataEnd {
            calculatedChecksum ^= bytes[i]
        }
        
        guard receivedChecksum == calculatedChecksum else {
            debugLog("Serial checksum mismatch: received \(String(format: "%02X", receivedChecksum)), calculated \(String(format: "%02X", calculatedChecksum))")
            DispatchQueue.main.async { [weak self] in
                self?.checksumFailures += 1
            }
            return
        }
        
        // Validate that packet starts with RAPT sync (0x52 0x41 0x50 0x54)
        guard packetData.count >= 8,
              packetData[0] == 0x52,
              packetData[1] == 0x41,
              packetData[2] == 0x50,
              packetData[3] == 0x54 else {
            debugLog("Packet missing RAPT sync: \(packetData.prefix(4).map { String(format: "%02X", $0) }.joined(separator: " "))")
            DispatchQueue.main.async { [weak self] in
                self?.noRaptFailures += 1
            }
            return
        }
        
        // Valid frame!
        debugLog("Received packet: \(dataLen) bytes, RSSI: \(rssi) dBm, SNR: \(snr) dB")
        debugLog("  Hex: \(packetData.prefix(20).map { String(format: "%02X", $0) }.joined(separator: " "))\(packetData.count > 20 ? "..." : "")")
        
        DispatchQueue.main.async { [weak self] in
            self?.packetsReceived += 1
            self?.lastRSSI = rssi
            self?.lastSNR = snr
        }
        
        // Deliver packet on main thread
        DispatchQueue.main.async { [weak self] in
            self?.onPacketReceived?(packetData, rssi, snr)
        }
    }
    
    // MARK: - Stats
    
    func getStats() -> [String: Any] {
        return [
            "connected": isConnected,
            "port": selectedPort,
            "packetsReceived": packetsReceived,
            "bytesReceived": bytesReceived,
            "lastRSSI": lastRSSI,
            "lastSNR": lastSNR,
            "framesExtracted": framesExtracted,
            "checksumFailures": checksumFailures,
            "noRaptFailures": noRaptFailures
        ]
    }
    
    var successRate: Double {
        guard framesExtracted > 0 else { return 0 }
        return Double(packetsReceived) / Double(framesExtracted) * 100
    }
}

// MARK: - Port List Helper

extension SerialPortManager {
    /// Get list of common ESP32 port names to help with auto-detection
    static func isLikelyESP32Port(_ port: String) -> Bool {
        let patterns = [
            "usbmodem",      // ESP32-S3 native USB
            "SLAB_USB",      // CP2102/CP2104
            "usbserial",     // FTDI / CH340
            "wchusbserial",  // CH340
        ]
        return patterns.contains { port.contains($0) }
    }
    
    /// Auto-connect to first available ESP32-like port
    func autoConnect() -> Bool {
        refreshAvailablePorts()
        
        // Try likely ESP32 ports first
        for port in availablePorts {
            if Self.isLikelyESP32Port(port) {
                if connect(to: port) {
                    return true
                }
            }
        }
        
        // Try any available port
        if let port = availablePorts.first {
            return connect(to: port)
        }
        
        return false
    }
}
