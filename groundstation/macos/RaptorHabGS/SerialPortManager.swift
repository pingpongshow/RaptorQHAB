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

    /// The dual-radio modem carries two streams down one cable: RAPTOR image
    /// traffic on 0x7E, and whole Meshtastic LoRa packets on 0x7B. A
    /// single-radio modem only ever sends 0x7E, so handling both here means
    /// one build talks to either board.
    static let meshtasticDelimiter: UInt8 = 0x7B

    /// Decodes the Meshtastic stream, and transmits through the modem.
    let meshtastic = ModemMeshtasticLink()

    /// Set as soon as something unmistakably modem-shaped arrives: a framed
    /// packet, or one of the lines the firmware prints. Used to tell a
    /// RaptorHAB modem from any other serial device that happens to enumerate
    /// first -- the payload's own USB console is one, and it sits on this
    /// machine's other usbmodem port.
    private let modemSeenLock = NSLock()
    private var _sawModemTraffic = false
    var sawModemTraffic: Bool {
        modemSeenLock.lock(); defer { modemSeenLock.unlock() }
        return _sawModemTraffic
    }
    private func noteModemTraffic() {
        modemSeenLock.lock(); _sawModemTraffic = true; modemSeenLock.unlock()
    }
    private func clearModemTraffic() {
        modemSeenLock.lock(); _sawModemTraffic = false; modemSeenLock.unlock()
    }
    
    // MARK: - Callbacks
    
    var onPacketReceived: ((Data, Float, Float) -> Void)?  // (packet, rssi, snr)
    var onError: ((String) -> Void)?
    
    // MARK: - Private State
    
    private var fileDescriptor: Int32 = -1
    private var readThread: Thread?
    private var shouldRun = false
    private let bufferLock = NSLock()  // Serialises access to the scanner
    
    // MARK: - Debug
    
    static var debugEnabled = true
    
    private func debugLog(_ message: String) {
        if SerialPortManager.debugEnabled {
        }
    }
    
    // MARK: - Initialization
    
    init() {
        // The link needs the port to transmit through.
        meshtastic.attach(serial: self)
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
                // as?, not as!. This value comes from a driver's IORegistry
                // entry, and a force cast on data the app does not control
                // turns an odd device into a crash -- in code that runs at
                // launch and on every port refresh. A device that does not
                // describe its callout path as a string simply is not one we
                // can open.
                guard let path = pathCF.takeRetainedValue() as? String else {
                    IOObjectRelease(service)
                    service = IOIteratorNext(iterator)
                    continue
                }
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
        scanner.reset()
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
        // Lines only a RaptorHAB modem prints.
        if line.hasPrefix("[STATS]") || line.hasPrefix("[RADIO]")
            || line.hasPrefix("CFG_") || line.hasPrefix("MTX_")
            || line.hasPrefix("MCFG_") || line.hasPrefix("[CONFIG]") {
            noteModemTraffic()
        }

        // A transmit verdict comes back on the same text channel as everything
        // else the modem prints, and the sender is blocked waiting for it.
        if meshtastic.handleModemLine(line) { return }

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

        // The payload transmits around 100 packets a second, roughly 24 kB/s
        // over USB. Reading 1 KB and then sleeping unconditionally capped this
        // loop at 51 kB/s and slept even while bytes were queued, so a burst
        // overflowed the tty buffer -- and losing bytes mid-frame is what
        // desynchronises the parser. Read large, and only wait when the port
        // is genuinely idle.
        let bufferSize = 16384
        let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: bufferSize)
        defer { buffer.deallocate() }

        while shouldRun && !Thread.current.isCancelled {
            // Check if file descriptor is still valid
            let descriptor = fileDescriptor
            guard descriptor >= 0 else {
                debugLog("File descriptor invalid, exiting read loop")
                break
            }

            let bytesRead = read(descriptor, buffer, bufferSize)

            if bytesRead > 0 {
                let data = Data(bytes: buffer, count: bytesRead)

                DispatchQueue.main.async { [weak self] in
                    self?.bytesReceived += bytesRead
                }

                processReceivedData(data)

                // More may already be queued behind this read; go straight
                // back for it rather than sleeping through the next burst.
                continue
            }

            if bytesRead < 0 {
                let err = errno
                if err != EAGAIN && err != EWOULDBLOCK && err != EINTR {
                    let error = String(cString: strerror(err))
                    debugLog("Read error: \(error)")
                    break
                }
            }

            // Nothing waiting. Block in poll() instead of a fixed sleep, so a
            // packet arriving one millisecond from now is picked up then and
            // not up to 20 ms later.
            var pollFD = pollfd(fd: descriptor, events: Int16(POLLIN), revents: 0)
            _ = poll(&pollFD, 1, 20)
        }

        debugLog("Read loop ended")
    }
    
    // MARK: - Frame Parsing
    
    /*
     * Frame format from Heltec:
     * [0x7E][LEN_HI][LEN_LO][RSSI_INT][RSSI_FRAC][SNR_INT][SNR_FRAC][DATA...][CHECKSUM][0x7E]
     */
    
    /// Splits the modem's stream into frames and status lines. Kept in its own
    /// dependency-free type so the framing can be tested directly -- the bug
    /// it replaced discarded every frame in silence.
    private let scanner = FrameScanner()

    private func processReceivedData(_ data: Data) {
        bufferLock.lock()
        let out = scanner.feed(data)
        bufferLock.unlock()

        for frame in out.frames {
            // The two streams are kept apart: a Meshtastic packet handed to
            // parseFrame would be read as an image symbol, since everything
            // downstream assumes RAPTOR.
            if frame.isMeshtastic {
                handleMeshtasticFrame(frame.data)
            } else {
                DispatchQueue.main.async { [weak self] in
                    self?.framesExtracted += 1
                }
                parseFrame(frame.data)
            }
        }

        for line in out.textLines {
            processTextLine(line)
        }
    }

    /// A whole Meshtastic LoRa packet the modem's second radio heard.
    ///
    /// Still encrypted: the modem holds no channel keys, so decrypting and
    /// parsing happen in ModemMeshtasticLink.
    private func handleMeshtasticFrame(_ frame: Data) {
        guard frame.count >= 7 else { return }
        let bytes = [UInt8](frame)

        let dataLen = (Int(bytes[0]) << 8) | Int(bytes[1])
        guard dataLen > 0, frame.count >= 6 + dataLen + 1 else { return }

        let rssiInt = Int8(bitPattern: bytes[2])
        let snrInt = Int8(bitPattern: bytes[4])
        let rssi = Float(rssiInt) + (rssiInt < 0 ? -Float(bytes[3]) / 100 : Float(bytes[3]) / 100)
        let snr = Float(snrInt) + (snrInt < 0 ? -Float(bytes[5]) / 100 : Float(bytes[5]) / 100)

        let payload = Data(bytes[6..<(6 + dataLen)])
        meshtastic.handleFrames([(rssi: rssi, snr: snr, data: payload)])
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
        
        noteModemTraffic()

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
    
    /// Connect to the first port that proves it is a RaptorHAB modem.
    ///
    /// Opening a port succeeds for any serial device, so the old version --
    /// "connect to the first thing whose name contains usbmodem" -- would
    /// settle happily on the wrong one and report success while receiving
    /// nothing. That is not hypothetical here: the payload's own USB console
    /// enumerates as a second `cu.usbmodem*` on the same machine, and which of
    /// the two sorts first is luck.
    ///
    /// So each candidate is opened and listened to. A framed packet or a line
    /// the firmware prints settles it; silence moves on to the next port.
    ///
    /// Asynchronous because the listening is real waiting, and this is called
    /// from a button.
    func autoConnect(perPortTimeout: TimeInterval = 2.5,
                     completion: @escaping (Bool, String) -> Void) {
        refreshAvailablePorts()

        let candidates = availablePorts.filter { Self.isLikelyESP32Port($0) }
            + availablePorts.filter { !Self.isLikelyESP32Port($0) }

        guard !candidates.isEmpty else {
            completion(false, "no serial ports found")
            return
        }

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            var tried: [String] = []

            for port in candidates {
                guard let self else { return }
                tried.append(port)

                self.clearModemTraffic()
                guard self.connect(to: port) else { continue }

                let deadline = Date().addingTimeInterval(perPortTimeout)
                while Date() < deadline && !self.sawModemTraffic {
                    Thread.sleep(forTimeInterval: 0.05)
                }

                if self.sawModemTraffic {
                    DispatchQueue.main.async { completion(true, port) }
                    return
                }
                self.disconnect()
            }

            DispatchQueue.main.async {
                completion(false,
                    "opened \(tried.count) port(s) but none sent modem traffic: "
                    + tried.joined(separator: ", "))
            }
        }
    }

    /// The async form.
    @discardableResult
    func autoConnect(perPortTimeout: TimeInterval = 2.5) async -> (Bool, String) {
        await withCheckedContinuation { continuation in
            autoConnect(perPortTimeout: perPortTimeout) { ok, detail in
                continuation.resume(returning: (ok, detail))
            }
        }
    }
}
