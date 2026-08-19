//
//  GroundStationManager.swift
//  RaptorHabGS
//
//  Main coordinator for ground station operations
//  Manages RTL-SDR or Serial input, demodulation, packet processing, and data storage
//

import Foundation
import Combine
import SwiftUI

// MARK: - Input Mode

enum InputMode: String, CaseIterable, Identifiable {
    case rtlsdr = "RTL-SDR"
    case serial = "Serial (SX1262)"
    
    var id: String { rawValue }
}

// MARK: - Telemetry Point

struct TelemetryPoint: Identifiable, Codable, Equatable {
    let id: UUID
    let timestamp: Date
    let sequence: UInt16
    let rssi: Int
    let snr: Float
    let latitude: Double
    let longitude: Double
    let altitude: Double
    let speed: Double
    let heading: Double
    let satellites: UInt8
    let fixType: String
    let gpsTime: Date
    let batteryMV: UInt16
    let cpuTemp: Double
    let radioTemp: Double
    let imageId: UInt16
    let imageProgress: UInt8
    
    init(from telemetry: TelemetryPayload, sequence: UInt16, rssi: Int, snr: Float = 0) {
        self.id = UUID()
        self.timestamp = Date()
        self.sequence = sequence
        self.rssi = rssi
        self.snr = snr
        self.latitude = telemetry.latitude
        self.longitude = telemetry.longitude
        self.altitude = telemetry.altitude
        self.speed = telemetry.speed
        self.heading = telemetry.heading
        self.satellites = telemetry.satellites
        self.fixType = telemetry.fixType.description
        self.gpsTime = Date(timeIntervalSince1970: TimeInterval(telemetry.gpsTime))
        self.batteryMV = telemetry.batteryMV
        self.cpuTemp = telemetry.cpuTemp
        self.radioTemp = telemetry.radioTemp
        self.imageId = telemetry.imageId
        self.imageProgress = telemetry.imageProgress
    }
    
    // For Codable compatibility with old data without SNR
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(UUID.self, forKey: .id)
        timestamp = try container.decode(Date.self, forKey: .timestamp)
        sequence = try container.decode(UInt16.self, forKey: .sequence)
        rssi = try container.decode(Int.self, forKey: .rssi)
        snr = try container.decodeIfPresent(Float.self, forKey: .snr) ?? 0
        latitude = try container.decode(Double.self, forKey: .latitude)
        longitude = try container.decode(Double.self, forKey: .longitude)
        altitude = try container.decode(Double.self, forKey: .altitude)
        speed = try container.decode(Double.self, forKey: .speed)
        heading = try container.decode(Double.self, forKey: .heading)
        satellites = try container.decode(UInt8.self, forKey: .satellites)
        fixType = try container.decode(String.self, forKey: .fixType)
        gpsTime = try container.decode(Date.self, forKey: .gpsTime)
        batteryMV = try container.decode(UInt16.self, forKey: .batteryMV)
        cpuTemp = try container.decode(Double.self, forKey: .cpuTemp)
        radioTemp = try container.decode(Double.self, forKey: .radioTemp)
        imageId = try container.decode(UInt16.self, forKey: .imageId)
        imageProgress = try container.decode(UInt8.self, forKey: .imageProgress)
    }
    
    // Convenience property for lowercase batteryMv (used in some places)
    var batteryMv: UInt16 { batteryMV }
    
    var batteryVoltage: Double {
        return Double(batteryMV) / 1000.0
    }
    
    var altitudeFeet: Double {
        return altitude * 3.28084
    }
    
    var speedMph: Double {
        return speed * 2.23694
    }
}

// MARK: - Receiver Statistics

struct ReceiverStatistics {
    var packetsReceived: Int = 0
    var packetsValid: Int = 0
    var packetsInvalid: Int = 0
    var telemetryPackets: Int = 0
    var imageMetaPackets: Int = 0
    var imageDataPackets: Int = 0
    var textPackets: Int = 0
    var lastRSSI: Int = 0
    var lastPacketTime: Date?
    var syncWordsDetected: Int = 0
    
    var successRate: Double {
        guard packetsReceived > 0 else { return 0 }
        return Double(packetsValid) / Double(packetsReceived) * 100
    }
}

// MARK: - Pending Image

struct PendingImage: Identifiable {
    let id: UInt16
    var metadata: ImageMetaPayload?
    var symbols: [UInt32: Data] = [:]
    var firstReceived: Date = Date()
    var lastReceived: Date = Date()

    var progress: Double {
        guard let meta = metadata, meta.numSourceSymbols > 0 else { return 0 }
        return min(100, Double(symbols.count) / Double(meta.numSourceSymbols) * 100)
    }

    var isDecodable: Bool {
        guard let meta = metadata else { return false }
        // RaptorQ needs approximately K symbols to decode (K = numSourceSymbols)
        // We try once we have K symbols; decoder will tell us if it needs more
        return symbols.count >= Int(meta.numSourceSymbols)
    }
}

// MARK: - Ground Station Manager

@MainActor
class GroundStationManager: ObservableObject {
    
    // Static reference for managers that need access
    static var shared: GroundStationManager?
    
    // MARK: - Published Properties
    
    @Published var isReceiving = false
    @Published var errorMessage: String?
    @Published var showRadioConfig = false
    
    // Input mode
    @Published var inputMode: InputMode = .serial  // Default to serial for SX1262
    
    // Radio state (RTL-SDR)
    @Published var radioConfig = RadioConfig()
    @Published var signalStrength: Double = 0
    @Published var isRTLSDRConnected = false
    
    // Serial state
    @Published var isSerialConnected = false
    @Published var availableSerialPorts: [String] = []
    @Published var selectedSerialPort: String = ""
    @Published var serialRSSI: Float = 0
    @Published var serialSNR: Float = 0
    
    // Modem RF configuration (Heltec SX1262)
    @Published var modemConfig = ModemConfig() {
        didSet {
            saveModemConfig()
        }
    }
    @Published var isModemConfigured = false
    @Published var modemConfigError: String?
    
    // Telemetry
    @Published var latestTelemetry: TelemetryPoint?
    @Published var telemetryHistory: [TelemetryPoint] = []
    @Published var maxHistorySize = 1000
    
    // Images (in-memory for current session display)
    @Published var pendingImages: [UInt16: PendingImage] = [:]
    @Published var completedImages: [UInt16: Data] = [:]
    @Published var latestImageId: UInt16?

    // Image limits to prevent memory growth
    private let maxPendingImages = 1
    private let maxCompletedImagesInMemory = 20
    
    // Messages
    @Published var textMessages: [(Date, String)] = []
    
    // Statistics
    @Published var statistics = ReceiverStatistics()
    
    // MARK: - Private Properties
    
    private var rtlsdr: RTLSDRManager
    private var demodulator: FSKDemodulator
    private var serialPort: SerialPortManager
    private var cancellables = Set<AnyCancellable>()
    
    // Data storage
    private let dataDirectory: URL
    private var logFileHandle: FileHandle?
    
    // Inactivity timer for auto status dump
    private var inactivityTimer: Timer?
    private var lastPacketTime: Date?

    // Telemetry throttling - only save/record 1 packet per 10 seconds
    private var lastSavedTelemetryTime: Date?
    private let telemetryThrottleInterval: TimeInterval = 10.0
    
    // MARK: - Initialization
    
    init() {
        // Initialize components
        rtlsdr = RTLSDRManager()
        serialPort = SerialPortManager()
        
        let demodConfig = FSKDemodulator.Config(
            sampleRate: 1000000,
            bitRate: 96000,
            frequencyDev: 50000,
            syncWord: RaptorProtocol.syncWord
        )
        demodulator = FSKDemodulator(config: demodConfig)
        
        // Create data directory
        let documentsPath = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first!
        dataDirectory = documentsPath.appendingPathComponent("RaptorHabGS")
        try? FileManager.default.createDirectory(at: dataDirectory, withIntermediateDirectories: true)
        
        // Load modem config
        loadModemConfig()
        
        // Setup callbacks
        setupCallbacks()
        
        // Scan for devices
        rtlsdr.scanDevices()
        serialPort.refreshAvailablePorts()
        
        // Setup inactivity timer (fires every 5 seconds to check)
        inactivityTimer = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.checkInactivity()
            }
        }

        // Set shared reference (must be after all properties initialized)
        GroundStationManager.shared = self
    }
    
    // MARK: - Modem Config Persistence
    
    private func saveModemConfig() {
        if let data = try? JSONEncoder().encode(modemConfig) {
            UserDefaults.standard.set(data, forKey: "ModemConfig")
        }
    }
    
    private func loadModemConfig() {
        if let data = UserDefaults.standard.data(forKey: "ModemConfig"),
           let config = try? JSONDecoder().decode(ModemConfig.self, from: data) {
            modemConfig = config
        }
    }
    
    // MARK: - Setup
    
    private func setupCallbacks() {
        // RTL-SDR sample callback
        rtlsdr.onSamplesReceived = { [weak self] samples in
            self?.demodulator.processSamples(samples)
        }
        
        // Demodulator packet callback (for RTL-SDR mode)
        demodulator.onPacketDetected = { [weak self] data in
            Task { @MainActor in
                self?.processPacket(data)
            }
        }
        
        // Serial port packet callback (already demodulated by SX1262)
        serialPort.onPacketReceived = { [weak self] (data: Data, rssi: Float, snr: Float) in
            Task { @MainActor in
                self?.serialRSSI = rssi
                self?.serialSNR = snr
                // The SX1262 gives us the raw packet starting with protocol sync "RAPT"
                // No need to strip RF framing - it's handled in firmware
                self?.processPacket(data)
            }
        }
        
        serialPort.onError = { [weak self] (error: String) in
            Task { @MainActor in
                self?.errorMessage = error
            }
        }
        
        // Bind RTL-SDR state
        rtlsdr.$isConnected
            .receive(on: DispatchQueue.main)
            .assign(to: &$isRTLSDRConnected)
        
        rtlsdr.$signalStrength
            .receive(on: DispatchQueue.main)
            .assign(to: &$signalStrength)
        
        // Note: RTL-SDR errors are only shown when user attempts to connect
        // (not on startup to avoid popup when RTL-SDR is not available)
        
        // Bind Serial state
        serialPort.$isConnected
            .receive(on: DispatchQueue.main)
            .assign(to: &$isSerialConnected)
        
        serialPort.$availablePorts
            .receive(on: DispatchQueue.main)
            .assign(to: &$availableSerialPorts)
        
        serialPort.$selectedPort
            .receive(on: DispatchQueue.main)
            .assign(to: &$selectedSerialPort)
        
        serialPort.$lastRSSI
            .receive(on: DispatchQueue.main)
            .assign(to: &$serialRSSI)
        
        serialPort.$lastSNR
            .receive(on: DispatchQueue.main)
            .assign(to: &$serialSNR)
    }
    
    // MARK: - Control
    
    func startReceiving() {
        guard !isReceiving else { return }
        
        switch inputMode {
        case .rtlsdr:
            startRTLSDR()
        case .serial:
            startSerial()
        }
    }
    
    private func startRTLSDR() {
        // Connect to RTL-SDR if not connected
        if !rtlsdr.isConnected {
            guard rtlsdr.connect(deviceIndex: 0, config: radioConfig) else {
                errorMessage = rtlsdr.errorMessage ?? "Failed to connect to RTL-SDR"
                return
            }
        }
        
        // Start streaming
        rtlsdr.startStreaming()
        
        // Open log file
        openLogFile()
        
        isReceiving = true
        errorMessage = nil
    }
    
    private func startSerial() {
        // Connect to serial port if not connected
        if !serialPort.isConnected {
            guard !selectedSerialPort.isEmpty else {
                errorMessage = "No serial port selected"
                return
            }
            guard serialPort.connect(to: selectedSerialPort) else {
                errorMessage = "Failed to connect to \(selectedSerialPort)"
                return
            }
        }
        
        // Configure modem with RF settings
        // Run configuration in background to avoid blocking UI
        isModemConfigured = false
        modemConfigError = nil
        
        // Capture values before entering background thread
        let config = modemConfig
        let port = serialPort
        
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            
            // Wait a moment for modem to be ready after connection
            Thread.sleep(forTimeInterval: 0.5)
            
            // Send configuration and wait for confirmation
            if port.configureModem(config, timeout: 5.0, maxRetries: 3) != nil {
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    self.isModemConfigured = true
                    self.modemConfigError = nil
                    
                    // Open log file
                    self.openLogFile()
                    
                    self.isReceiving = true
                    self.errorMessage = nil
                }
            } else {
                DispatchQueue.main.async {
                    guard let self = self else { return }
                    self.isModemConfigured = false
                    self.modemConfigError = port.configurationError ?? "Failed to configure modem"

                    // Don't show popup - just continue in fallback mode
                    // Modem status is visible in the Radio Config UI
                    self.openLogFile()
                    self.isReceiving = true
                }
            }
        }
    }
    
    func stopReceiving() {
        switch inputMode {
        case .rtlsdr:
            rtlsdr.stopStreaming()
        case .serial:
            serialPort.disconnect()
            isModemConfigured = false
        }
        closeLogFile()
        isReceiving = false
    }
    
    // MARK: - Debug Methods
    
    func dumpImageStatus() {
        // Debug info available via statistics property
    }
    
    func forceDecodeAttempt() {
        for (imageId, pending) in pendingImages {
            if pending.metadata != nil {
                lastDecodeAttempt[imageId] = nil  // Clear throttle
                tryDecodeImage(imageId)
            }
        }
    }
    
    private var statusDumped = false
    
    private func checkInactivity() {
        guard let lastTime = lastPacketTime else { return }
        
        let elapsed = Date().timeIntervalSince(lastTime)
        
        // If no packets for 10 seconds and we have pending images, dump status
        if elapsed > 10 && !pendingImages.isEmpty && !statusDumped {
            dumpImageStatus()
            statusDumped = true
            
            // Try to decode whatever we have
            forceDecodeAttempt()
        }
    }
    
    private func resetInactivityTimer() {
        lastPacketTime = Date()
        statusDumped = false
    }
    
    // MARK: - Serial Port Control
    
    func refreshSerialPorts() {
        serialPort.refreshAvailablePorts()
    }
    
    func selectSerialPort(_ port: String) {
        selectedSerialPort = port
    }
    
    /// Find and connect to a RaptorHAB modem, verifying it is one.
    ///
    /// Reports why it failed rather than just returning false: "no modem found"
    /// and "found three serial ports, none of them a modem" send the operator
    /// to very different places.
    func autoConnectSerial(completion: ((Bool, String) -> Void)? = nil) {
        serialPort.autoConnect { [weak self] ok, detail in
            if !ok { self?.errorMessage = "Auto-connect failed: \(detail)" }
            completion?(ok, detail)
        }
    }
    
    func updateRadioConfig(_ config: RadioConfig) {
        radioConfig = config
        
        // Update demodulator
        let demodConfig = FSKDemodulator.Config(
            sampleRate: config.sampleRate,
            bitRate: config.bitrateBPS,
            frequencyDev: config.frequencyDevHz,
            syncWord: RaptorProtocol.syncWord
        )
        demodulator = FSKDemodulator(config: demodConfig)
        demodulator.onPacketDetected = { [weak self] data in
            Task { @MainActor in
                self?.processPacket(data)
            }
        }
        
        // Update RTL-SDR if connected
        if rtlsdr.isConnected {
            _ = rtlsdr.configure(config)
        }
    }
    
    func scanDevices() {
        rtlsdr.scanDevices()
    }
    
    var availableDevices: [RTLSDRDeviceInfo] {
        rtlsdr.availableDevices
    }
    
    // MARK: - Packet Processing
    
    private func processPacket(_ data: Data) {
        statistics.packetsReceived += 1
        resetInactivityTimer()
        
        // Parse packet
        guard let (packetType, sequence, _, payload) = PacketParser.parse(data) else {
            statistics.packetsInvalid += 1
            return
        }
        
        statistics.packetsValid += 1
        statistics.lastPacketTime = Date()
        
        // Log raw packet
        logPacket(data, type: packetType)
        
        // Dispatch to handler
        switch packetType {
        case .telemetry:
            handleTelemetry(payload: payload, sequence: sequence)
        case .imageMeta:
            handleImageMeta(payload: payload)
        case .imageData:
            handleImageData(payload: payload)
        case .textMessage:
            handleTextMessage(payload: payload)
        default:
            break
        }
    }
    
    private func handleTelemetry(payload: Data, sequence: UInt16) {
        statistics.telemetryPackets += 1

        guard let telemetry = TelemetryPayload.deserialize(from: payload) else {
            return
        }

        // Update RSSI from ground station's serial modem (not the balloon's RSSI reading)
        statistics.lastRSSI = Int(serialRSSI)

        // Create telemetry point (use ground station RSSI and SNR from serial port)
        let point = TelemetryPoint(from: telemetry, sequence: sequence, rssi: statistics.lastRSSI, snr: serialSNR)

        // Always update latest telemetry for real-time display
        latestTelemetry = point

        // Feed the position fusion, which reconciles this against Meshtastic
        // beacons and third-party reports. RAPTOR is the highest-priority
        // source, so this normally wins outright.
        if point.latitude != 0 || point.longitude != 0 {
            Task { @MainActor in PositionFusion.shared.submitRaptor(point) }
        }

        // Throttle: only save/record telemetry at most once per 10 seconds
        let now = Date()
        let timeSinceLastSave = lastSavedTelemetryTime.map { now.timeIntervalSince($0) }
        let shouldSave = timeSinceLastSave == nil || timeSinceLastSave! >= telemetryThrottleInterval

        if shouldSave {
            lastSavedTelemetryTime = now
            print("Saving telemetry point (interval: \(timeSinceLastSave ?? 0)s)")

            // Add to history
            telemetryHistory.append(point)

            // Trim history
            if telemetryHistory.count > maxHistorySize {
                telemetryHistory.removeFirst(telemetryHistory.count - maxHistorySize)
            }

            // Save to mission
            saveTelemetry(point)
        }
    }
    
    private func handleImageMeta(payload: Data) {
        statistics.imageMetaPackets += 1
        
        guard let meta = ImageMetaPayload.deserialize(from: payload) else {
            return
        }
        
        // Skip if this image was already decoded
        if decodedImages.contains(meta.imageId) {
            return
        }
        
        
        // Update or create pending image
        if var pending = pendingImages[meta.imageId] {
            pending.metadata = meta
            pending.lastReceived = Date()
            pendingImages[meta.imageId] = pending
        } else {
            var newImage = PendingImage(id: meta.imageId)
            newImage.metadata = meta
            pendingImages[meta.imageId] = newImage
        }
        
        latestImageId = meta.imageId

        // Prune old pending images if we exceed the limit
        prunePendingImages(keepImageId: meta.imageId)

        // Try to decode if we have enough symbols
        tryDecodeImage(meta.imageId)
    }

    private func handleImageData(payload: Data) {
        statistics.imageDataPackets += 1
        
        guard let imgData = ImageDataPayload.deserialize(from: payload) else {
            return
        }
        
        // Skip if this image was already decoded
        if decodedImages.contains(imgData.imageId) {
            return
        }
        
        // Use symbolId as key - matches Pi behavior
        let symbolKey = imgData.symbolId
        
        // Update or create pending image
        if var pending = pendingImages[imgData.imageId] {
            let isNew = pending.symbols[symbolKey] == nil
            
            if isNew {
                pending.symbols[symbolKey] = imgData.symbolData
            }
            pending.lastReceived = Date()
            pendingImages[imgData.imageId] = pending
        } else {
            var newImage = PendingImage(id: imgData.imageId)
            newImage.symbols[symbolKey] = imgData.symbolData
            pendingImages[imgData.imageId] = newImage
        }
        
        // Prune old pending images if we exceed the limit
        prunePendingImages(keepImageId: imgData.imageId)

        // Try to decode
        tryDecodeImage(imgData.imageId)
    }

    /// Removes oldest pending images when we exceed maxPendingImages limit.
    /// Always keeps the specified imageId (the one currently being received).
    private func prunePendingImages(keepImageId: UInt16) {
        guard pendingImages.count > maxPendingImages else { return }

        // Sort by lastReceived (oldest first), excluding the current image
        let sortedIds = pendingImages
            .filter { $0.key != keepImageId }
            .sorted { $0.value.lastReceived < $1.value.lastReceived }
            .map { $0.key }

        // Remove oldest entries until we're at the limit
        let removeCount = pendingImages.count - maxPendingImages
        for i in 0..<min(removeCount, sortedIds.count) {
            let idToRemove = sortedIds[i]
            pendingImages.removeValue(forKey: idToRemove)
            lastDecodeAttempt.removeValue(forKey: idToRemove)
        }
    }

    /// Removes oldest completed images from memory when we exceed the limit.
    /// Images are saved to the mission folder.
    private func pruneCompletedImagesInMemory() {
        guard completedImages.count > maxCompletedImagesInMemory else { return }

        // Sort by image ID (oldest/lowest first) and remove oldest
        let sortedIds = completedImages.keys.sorted()
        let removeCount = completedImages.count - maxCompletedImagesInMemory

        for i in 0..<removeCount {
            completedImages.removeValue(forKey: sortedIds[i])
        }
    }

    private func handleTextMessage(payload: Data) {
        statistics.textPackets += 1
        
        guard let msg = TextMessagePayload.deserialize(from: payload) else {
            return
        }
        
        textMessages.append((Date(), msg.message))
        
        // Trim messages
        if textMessages.count > 100 {
            textMessages.removeFirst(textMessages.count - 100)
        }
    }
    
    // MARK: - Image Decoding (RaptorQ via Python)
    
    private func tryDecodeImage(_ imageId: UInt16) {
        guard let pending = pendingImages[imageId],
              let meta = pending.metadata else {
            return
        }
        
        let minSymbols = Int(meta.numSourceSymbols)
        let have = pending.symbols.count
        
        // Need at least K symbols for RaptorQ to decode
        guard have >= minSymbols else {
            // Log progress at milestones
            if have == minSymbols - 50 || have == minSymbols - 20 || have == minSymbols - 10 {
            }
            return
        }
        
        // Check if we've already decoded this image
        if decodedImages.contains(imageId) {
            return
        }
        
        // Try decoding at specific milestones: K, K+5%, K+10%, K+15%, K+20%
        let decodeThresholds = [0, 5, 10, 15, 20, 25]  // percent overhead
        
        var shouldDecode = false
        for threshold in decodeThresholds {
            let symbolsAtThreshold = minSymbols + Int(Double(minSymbols) * Double(threshold) / 100.0)
            if have == symbolsAtThreshold {
                shouldDecode = true
                break
            }
        }
        
        // Also decode if we haven't tried in a while (2 seconds)
        let now = Date()
        if let lastAttempt = lastDecodeAttempt[imageId] {
            if now.timeIntervalSince(lastAttempt) >= 2.0 && have >= minSymbols {
                shouldDecode = true
            }
        } else if have >= minSymbols {
            // First time reaching K - definitely try
            shouldDecode = true
        }
        
        guard shouldDecode else { return }
        
        lastDecodeAttempt[imageId] = now
        
        
        // Write symbols to temp file
        let tempDir = FileManager.default.temporaryDirectory
        let symbolsFile = tempDir.appendingPathComponent("symbols_\(imageId).bin")
        let outputFile = tempDir.appendingPathComponent("decoded_\(imageId).jpg")
        
        // Expected raptorq packet size: 4-byte header + symbol_size data
        let raptorqPacketSize = 4 + Int(meta.symbolSize)
        
        var symbolData = Data()
        for (symbolId, raptorqPacket) in pending.symbols {
            // Write symbol_id as little-endian uint32 (used for deduplication key)
            var symbolIdLE = symbolId.littleEndian
            symbolData.append(Data(bytes: &symbolIdLE, count: 4))
            
            // Write the full raptorq serialized packet
            // Must be exactly (4 + symbolSize) bytes for decoder
            var packetData = raptorqPacket
            if packetData.count < raptorqPacketSize {
                packetData.append(Data(repeating: 0, count: raptorqPacketSize - packetData.count))
            }
            symbolData.append(packetData.prefix(raptorqPacketSize))
        }
        
        do {
            try symbolData.write(to: symbolsFile)
        } catch {
            return
        }
        
        // Find decoder (native or Python)
        guard let decoder = findDecoder() else {
            return
        }
        
        // Call decoder
        let process = Process()
        let pipe = Pipe()
        process.standardError = pipe
        process.standardOutput = pipe
        
        switch decoder {
        case .native(let binaryPath):
            process.executableURL = URL(fileURLWithPath: binaryPath)
            process.arguments = [
                String(meta.totalSize),
                String(meta.symbolSize),
                String(meta.numSourceSymbols),
                symbolsFile.path,
                outputFile.path
            ]
            
        case .python(let scriptPath):
            // Try Homebrew Python first, then system Python
            let homebrewPython = "/opt/homebrew/bin/python3"
            let systemPython = "/usr/bin/python3"
            let pythonPath = FileManager.default.fileExists(atPath: homebrewPython) ? homebrewPython : systemPython
            
            process.executableURL = URL(fileURLWithPath: pythonPath)
            process.arguments = [
                scriptPath,
                String(meta.totalSize),
                String(meta.symbolSize),
                String(meta.numSourceSymbols),
                symbolsFile.path,
                outputFile.path
            ]
        }
        
        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            return
        }
        
        let outputData = pipe.fileHandleForReading.readDataToEndOfFile()
        let output = String(data: outputData, encoding: .utf8) ?? ""
        if !output.isEmpty {
        }
        
        // Check if decoding succeeded
        if process.terminationStatus == 0 {
            // Read decoded data
            do {
                let decoded = try Data(contentsOf: outputFile)
                
                // Verify checksum
                let crc = CRC32.calculate(decoded)
                if crc == meta.checksum {
                    completedImages[imageId] = decoded
                    decodedImages.insert(imageId)  // Mark as decoded to prevent re-decoding
                    pendingImages.removeValue(forKey: imageId)
                    lastDecodeAttempt.removeValue(forKey: imageId)
                    saveImage(imageId, data: decoded, meta: meta)

                    // Prune old completed images from memory (they're saved to disk)
                    pruneCompletedImagesInMemory()

                    // Upload to SondeHub if enabled
                    SondeHubManager.shared.uploadImage(decoded, imageId: imageId, telemetry: latestTelemetry)
                } else {
                }
                
                // Cleanup temp files
                try? FileManager.default.removeItem(at: symbolsFile)
                try? FileManager.default.removeItem(at: outputFile)
                
            } catch {
            }
        } else {
            // Cleanup
            try? FileManager.default.removeItem(at: symbolsFile)
        }
    }
    
    private var lastDecodeAttempt: [UInt16: Date] = [:]
    private var decodedImages: Set<UInt16> = []
    
    // Decoder types
    private enum DecoderType {
        case native(String)   // Path to native raptorq_decode binary
        case python(String)   // Path to Python script
    }
    
    private func findDecoder() -> DecoderType? {
        // First, look for native decoder (faster, no Python dependency)
        // Check app bundle first (for distributed apps)
        var nativePaths: [String] = []
        
        // App bundle executables folder (preferred for sandboxed apps)
        if let executablesPath = Bundle.main.executablePath {
            let executablesDir = (executablesPath as NSString).deletingLastPathComponent
            nativePaths.append(executablesDir + "/raptorq_decode")
        }
        
        // App bundle resources folder
        if let resourcePath = Bundle.main.path(forResource: "raptorq_decode", ofType: nil) {
            nativePaths.append(resourcePath)
        }
        
        // User's RaptorHabGS folder
        nativePaths.append(NSHomeDirectory() + "/RaptorHabGS/raptorq_decode")
        
        // System paths
        nativePaths.append("/usr/local/bin/raptorq_decode")
        nativePaths.append("/opt/homebrew/bin/raptorq_decode")
        nativePaths.append(dataDirectory.appendingPathComponent("raptorq_decode").path)
        
        for path in nativePaths {
            if FileManager.default.isExecutableFile(atPath: path) {
                return .native(path)
            }
        }
        
        // Fall back to Python script
        let pythonPaths: [String] = [
            Bundle.main.path(forResource: "raptorq_decoder", ofType: "py"),
            NSHomeDirectory() + "/RaptorHabGS/raptorq_decoder.py",
            FileManager.default.currentDirectoryPath + "/raptorq_decoder.py",
            "/usr/local/bin/raptorq_decoder.py",
            dataDirectory.appendingPathComponent("raptorq_decoder.py").path
        ].compactMap { $0 }
        
        for path in pythonPaths {
            if FileManager.default.fileExists(atPath: path) {
                return .python(path)
            }
        }
        
        return nil
    }
    
    // MARK: - Data Persistence
    
    private func openLogFile() {
        let dateFormatter = DateFormatter()
        dateFormatter.dateFormat = "yyyy-MM-dd_HH-mm-ss"
        let filename = "packets_\(dateFormatter.string(from: Date())).log"
        let fileURL = dataDirectory.appendingPathComponent(filename)
        
        FileManager.default.createFile(atPath: fileURL.path, contents: nil)
        logFileHandle = try? FileHandle(forWritingTo: fileURL)
    }
    
    private func closeLogFile() {
        try? logFileHandle?.close()
        logFileHandle = nil
    }
    
    private func logPacket(_ data: Data, type: PacketType) {
        let timestamp = ISO8601DateFormatter().string(from: Date())
        let hexString = data.map { String(format: "%02x", $0) }.joined()
        let logLine = "\(timestamp),\(type.name),\(data.count),\(hexString)\n"
        
        if let logData = logLine.data(using: .utf8) {
            try? logFileHandle?.write(contentsOf: logData)
        }
    }
    
    private func saveTelemetry(_ point: TelemetryPoint) {
        // Record to mission (recording starts automatically on first telemetry)
        MissionManager.shared.recordTelemetry(point)
    }
    
    private func saveImage(_ imageId: UInt16, data: Data, meta: ImageMetaPayload) {
        // Record to mission (recording starts automatically if needed)
        MissionManager.shared.recordImage(imageId: imageId, data: data, telemetry: latestTelemetry)
    }
    
    // MARK: - Export
    
    func exportTelemetryCSV() -> URL? {
        let dateFormatter = DateFormatter()
        dateFormatter.dateFormat = "yyyy-MM-dd_HH-mm-ss"
        let filename = "telemetry_export_\(dateFormatter.string(from: Date())).csv"
        let fileURL = dataDirectory.appendingPathComponent(filename)
        
        var csv = "timestamp,sequence,latitude,longitude,altitude_m,speed_ms,heading,satellites,fix_type,battery_mv,cpu_temp_c,radio_temp_c,rssi,image_id,image_progress\n"
        
        let isoFormatter = ISO8601DateFormatter()
        
        for point in telemetryHistory {
            let line = [
                isoFormatter.string(from: point.timestamp),
                String(point.sequence),
                String(format: "%.7f", point.latitude),
                String(format: "%.7f", point.longitude),
                String(format: "%.1f", point.altitude),
                String(format: "%.2f", point.speed),
                String(format: "%.1f", point.heading),
                String(point.satellites),
                point.fixType,
                String(point.batteryMV),
                String(format: "%.1f", point.cpuTemp),
                String(format: "%.1f", point.radioTemp),
                String(point.rssi),
                String(point.imageId),
                String(point.imageProgress)
            ].joined(separator: ",")
            csv += line + "\n"
        }
        
        try? csv.write(to: fileURL, atomically: true, encoding: .utf8)
        return fileURL
    }
    
    // MARK: - Simulation
    
    func injectSimulatedTelemetry() {
        // For testing without hardware
        var telemetry = TelemetryPayload()
        telemetry.latitude = 40.7128 + Double.random(in: -0.01...0.01)
        telemetry.longitude = -74.0060 + Double.random(in: -0.01...0.01)
        telemetry.altitude = 10000 + Double.random(in: -100...100)
        telemetry.speed = 25 + Double.random(in: -5...5)
        telemetry.heading = Double.random(in: 0...360)
        telemetry.satellites = UInt8.random(in: 6...12)
        telemetry.fixType = .fix3D
        telemetry.gpsTime = UInt32(Date().timeIntervalSince1970)
        telemetry.batteryMV = UInt16.random(in: 3500...4200)
        telemetry.cpuTemp = 35 + Double.random(in: -5...10)
        telemetry.radioTemp = 30 + Double.random(in: -5...10)
        
        let sequence = UInt16(telemetryHistory.count % 65536)
        handleTelemetry(payload: telemetry.serialize(), sequence: sequence)
    }
}

// MARK: - LT Code Decoder

class LTCodeDecoder {
    private let numSourceSymbols: Int
    private let symbolSize: Int
    private let totalSize: Int
    
    private var decoded: [Int: Data] = [:]
    private var encoded: [UInt32: (Data, Set<Int>)] = [:]
    
    init(numSourceSymbols: Int, symbolSize: Int, totalSize: Int) {
        self.numSourceSymbols = numSourceSymbols
        self.symbolSize = symbolSize
        self.totalSize = totalSize
    }
    
    func addSymbol(symbolId: UInt32, data: Data) -> Bool {
        guard !encoded.keys.contains(symbolId) else {
            return isComplete
        }
        
        // Get neighbors using PRNG seeded with symbol ID
        let neighbors = getNeighbors(symbolId)
        
        // XOR out already decoded symbols
        var workingData = Data(data)
        var remaining = Set<Int>()
        
        for srcId in neighbors {
            if let decodedSym = decoded[srcId] {
                workingData = xorData(workingData, decodedSym)
            } else {
                remaining.insert(srcId)
            }
        }
        
        if remaining.isEmpty {
            // Redundant symbol
            return isComplete
        } else if remaining.count == 1 {
            // Degree 1 - decode immediately
            let srcId = remaining.first!
            decodeSymbol(srcId, data: workingData)
        } else {
            // Store for later
            encoded[symbolId] = (workingData, remaining)
        }
        
        return isComplete
    }
    
    private func decodeSymbol(_ srcId: Int, data: Data) {
        guard decoded[srcId] == nil else { return }
        
        decoded[srcId] = data
        
        // Propagate to encoded symbols
        var toRemove: [UInt32] = []
        var toDecode: [(Int, Data)] = []
        
        for (encId, (encData, neighbors)) in encoded {
            if neighbors.contains(srcId) {
                let newData = xorData(encData, data)
                var newNeighbors = neighbors
                newNeighbors.remove(srcId)
                
                if newNeighbors.isEmpty {
                    toRemove.append(encId)
                } else if newNeighbors.count == 1 {
                    toRemove.append(encId)
                    toDecode.append((newNeighbors.first!, newData))
                } else {
                    encoded[encId] = (newData, newNeighbors)
                }
            }
        }
        
        for encId in toRemove {
            encoded.removeValue(forKey: encId)
        }
        
        for (nextSrc, nextData) in toDecode {
            decodeSymbol(nextSrc, data: nextData)
        }
    }
    
    private func getNeighbors(_ symbolId: UInt32) -> Set<Int> {
        // Use same PRNG algorithm as encoder
        var rng = SeededRandom(seed: UInt64(symbolId))
        
        let degree = sampleDegree(&rng)
        let actualDegree = min(degree, numSourceSymbols)
        
        var indices = Set<Int>()
        while indices.count < actualDegree {
            indices.insert(Int(rng.next() % UInt64(numSourceSymbols)))
        }
        
        return indices
    }
    
    private func sampleDegree(_ rng: inout SeededRandom) -> Int {
        // Robust Soliton distribution (simplified)
        let k = Double(numSourceSymbols)
        let c = 0.1
        let delta = 0.5
        _ = c * log(k / delta) * sqrt(k)  // R for full implementation
        
        let random = Double(rng.next()) / Double(UInt64.max)
        
        // Simplified degree selection
        if random < 1.0 / k {
            return 1
        } else {
            let d = Int(1.0 / random)
            return max(1, min(d, numSourceSymbols))
        }
    }
    
    private func xorData(_ a: Data, _ b: Data) -> Data {
        let length = max(a.count, b.count)
        var result = Data(count: length)
        
        for i in 0..<length {
            let aByte = i < a.count ? a[i] : 0
            let bByte = i < b.count ? b[i] : 0
            result[i] = aByte ^ bByte
        }
        
        return result
    }
    
    var isComplete: Bool {
        return decoded.count >= numSourceSymbols
    }
    
    func getDecodedData() -> Data? {
        guard isComplete else { return nil }
        
        var result = Data()
        for i in 0..<numSourceSymbols {
            if let sym = decoded[i] {
                result.append(sym)
            } else {
                return nil
            }
        }
        
        // Trim to actual size
        if result.count > totalSize {
            result = result.prefix(totalSize)
        }
        
        return result
    }
}

// MARK: - Seeded Random

struct SeededRandom {
    private var state: UInt64
    
    init(seed: UInt64) {
        state = seed == 0 ? 1 : seed
    }
    
    mutating func next() -> UInt64 {
        // xorshift64
        var x = state
        x ^= x << 13
        x ^= x >> 7
        x ^= x << 17
        state = x
        return x
    }
}

// MARK: - TelemetryPayload Serialization Extension

extension TelemetryPayload {
    func serialize() -> Data {
        var data = Data()
        
        // Helper to append big-endian values
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
