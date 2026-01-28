//
//  GroundStationManager.swift
//  RaptorHabMobile
//
//  Main coordinator for ground station operations on iOS
//  Manages BLE modem, packet processing, and data storage
//

import Foundation
import Combine
import SwiftUI
import UIKit

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
    var lastSNR: Float = 0
    var lastPacketTime: Date?
    
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
        return symbols.count >= Int(meta.numSourceSymbols)
    }
}

// MARK: - Ground Station Manager

@MainActor
class GroundStationManager: ObservableObject {
    
    // MARK: - Published Properties
    
    @Published var isReceiving = false
    @Published var errorMessage: String?
    @Published var showSettings = false
    
    // BLE/Modem state
    @Published var isModemConnected = false
    @Published var modemRSSI: Float = 0
    @Published var modemSNR: Float = 0
    
    // Modem RF configuration
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
    @Published var maxHistorySize = 500
    
    // Images
    @Published var pendingImages: [UInt16: PendingImage] = [:]
    @Published var completedImages: [UInt16: Data] = [:]
    @Published var latestImageId: UInt16?
    
    // Messages
    @Published var textMessages: [(Date, String)] = []
    
    // Statistics
    @Published var statistics = ReceiverStatistics()
    
    // MARK: - External References
    
    var bleManager: BLESerialManager? {
        didSet {
            setupBLECallbacks()
            setupBindings()
        }
    }
    
    var locationManager: LocationManager?
    
    // MARK: - Private Properties
    
    private var cancellables = Set<AnyCancellable>()
    private let dataDirectory: URL
    
    // MARK: - Initialization
    
    init() {
        // Create data directory
        let documentsPath = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first!
        dataDirectory = documentsPath.appendingPathComponent("RaptorHabMobile")
        try? FileManager.default.createDirectory(at: dataDirectory, withIntermediateDirectories: true)
        
        // Load modem config
        loadModemConfig()
    }
    
    // MARK: - Setup
    
    private func setupBLECallbacks() {
        guard let bleManager = bleManager else { return }
        
        bleManager.onPacketReceived = { [weak self] packet, rssi, snr in
            Task { @MainActor in
                self?.processPacket(packet, rssi: rssi, snr: snr)
            }
        }
        
        bleManager.onError = { [weak self] error in
            Task { @MainActor in
                self?.errorMessage = error
            }
        }
    }
    
    private func setupBindings() {
        guard let bleManager = bleManager else { return }
        
        cancellables.removeAll()
        
        // Bind BLE manager state
        bleManager.$isConnected
            .receive(on: DispatchQueue.main)
            .assign(to: &$isModemConnected)
        
        bleManager.$lastRSSI
            .receive(on: DispatchQueue.main)
            .assign(to: &$modemRSSI)
        
        bleManager.$lastSNR
            .receive(on: DispatchQueue.main)
            .assign(to: &$modemSNR)
        
        bleManager.$isConfigured
            .receive(on: DispatchQueue.main)
            .assign(to: &$isModemConfigured)
        
        // Update bearing when telemetry changes
        $latestTelemetry
            .sink { [weak self] telemetry in
                if let telemetry = telemetry {
                    self?.locationManager?.updateBearing(
                        toLatitude: telemetry.latitude,
                        toLongitude: telemetry.longitude,
                        toAltitude: telemetry.altitude
                    )
                }
            }
            .store(in: &cancellables)
    }
    
    // MARK: - Modem Config Persistence
    
    private func saveModemConfig() {
        if let encoded = try? JSONEncoder().encode(modemConfig) {
            UserDefaults.standard.set(encoded, forKey: "ModemConfig")
        }
    }
    
    private func loadModemConfig() {
        if let data = UserDefaults.standard.data(forKey: "ModemConfig"),
           let config = try? JSONDecoder().decode(ModemConfig.self, from: data) {
            modemConfig = config
        }
    }
    
    // MARK: - Connection Control
    
    func startReceiving() {
        guard !isReceiving else { return }
        guard let bleManager = bleManager else {
            errorMessage = "BLE Manager not available"
            return
        }
        
        if bleManager.isConnected {
            // Connected - configure modem
            configureModem()
        } else {
            // Start scanning for modem
            bleManager.autoConnect()
            
            // Wait for connection then configure
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.0) { [weak self] in
                if bleManager.isConnected {
                    self?.configureModem()
                } else {
                    self?.errorMessage = "No RaptorModem found. Enable Bluetooth and ensure modem is powered on."
                }
            }
        }
    }
    
    private func configureModem() {
        guard let bleManager = bleManager else { return }
        
        bleManager.configureModem(modemConfig) { [weak self] success, error in
            Task { @MainActor in
                if success {
                    self?.isReceiving = true
                    self?.errorMessage = nil
                    print("[GS] Modem configured successfully")
                } else {
                    self?.errorMessage = error ?? "Configuration failed"
                    self?.modemConfigError = error
                    print("[GS] Modem configuration failed: \(error ?? "unknown")")
                }
            }
        }
    }
    
    func stopReceiving() {
        isReceiving = false
        bleManager?.disconnect()
    }
    
    func scanForModem() {
        bleManager?.startScanning()
    }
    
    func connectToModem() {
        bleManager?.autoConnect()
    }
    
    // MARK: - Packet Processing
    
    private func processPacket(_ data: Data, rssi: Float, snr: Float) {
        statistics.packetsReceived += 1
        statistics.lastRSSI = Int(rssi)
        statistics.lastSNR = snr
        statistics.lastPacketTime = Date()
        
        guard let (type, sequence, _, payload) = PacketParser.parse(data) else {
            statistics.packetsInvalid += 1
            return
        }
        
        statistics.packetsValid += 1
        
        switch type {
        case .telemetry:
            statistics.telemetryPackets += 1
            handleTelemetry(payload: payload, sequence: sequence, rssi: Int(rssi), snr: snr)
            
        case .imageMeta:
            statistics.imageMetaPackets += 1
            handleImageMeta(payload: payload)
            
        case .imageData:
            statistics.imageDataPackets += 1
            handleImageData(payload: payload)
            
        case .textMessage:
            statistics.textPackets += 1
            handleTextMessage(payload: payload)
            
        default:
            break
        }
    }
    
    private func handleTelemetry(payload: Data, sequence: UInt16, rssi: Int, snr: Float) {
        guard let telemetry = TelemetryPayload.deserialize(from: payload) else { return }
        
        let point = TelemetryPoint(from: telemetry, sequence: sequence, rssi: rssi, snr: snr)
        
        latestTelemetry = point
        telemetryHistory.append(point)
        
        // Trim history
        if telemetryHistory.count > maxHistorySize {
            telemetryHistory.removeFirst(telemetryHistory.count - maxHistorySize)
        }
        
        // Update burst detection
        BurstDetectionManager.shared.update(with: point)
        
        // Update landing prediction
        LandingPredictionManager.shared.updatePrediction(from: telemetryHistory)
        
        // Record to mission if recording
        MissionManager.shared.recordTelemetry(point)
        
        // Audio alerts
        AudioAlertManager.shared.updateWithTelemetry(point)
        AudioAlertManager.shared.playAlert(.telemetryReceived)
        
        // SondeHub upload
        SondeHubManager.shared.uploadTelemetry(point, rssi: Float(rssi), snr: snr, frequency: modemConfig.frequencyMHz)
        
        // Log packet
        PacketLogManager.shared.addEntry(data: payload, type: .telemetry, sequence: sequence, rssi: rssi, snr: snr, isValid: true)
        
        // Provide haptic feedback on new telemetry
        let generator = UIImpactFeedbackGenerator(style: .light)
        generator.impactOccurred()
    }
    
    private func handleImageMeta(payload: Data) {
        guard let meta = ImageMetaPayload.deserialize(from: payload) else { return }
        
        if pendingImages[meta.imageId] == nil {
            pendingImages[meta.imageId] = PendingImage(id: meta.imageId)
        }
        pendingImages[meta.imageId]?.metadata = meta
        latestImageId = meta.imageId
    }
    
    private func handleImageData(payload: Data) {
        guard let imageData = ImageDataPayload.deserialize(from: payload) else { return }
        
        if pendingImages[imageData.imageId] == nil {
            pendingImages[imageData.imageId] = PendingImage(id: imageData.imageId)
        }
        
        pendingImages[imageData.imageId]?.symbols[imageData.symbolId] = imageData.symbolData
        pendingImages[imageData.imageId]?.lastReceived = Date()
        latestImageId = imageData.imageId
        
        // Check if we can decode
        if let pending = pendingImages[imageData.imageId], pending.isDecodable {
            tryDecodeImage(imageId: imageData.imageId)
        }
    }
    
    private func tryDecodeImage(imageId: UInt16) {
        guard let pending = pendingImages[imageId],
              let meta = pending.metadata else { return }
        
        // Simple concatenation decoder for now
        var imageData = Data()
        
        for i in 0..<Int(meta.numSourceSymbols) {
            if let symbolData = pending.symbols[UInt32(i)] {
                imageData.append(symbolData)
            } else {
                return // Missing symbol
            }
        }
        
        // Trim to size
        if imageData.count > Int(meta.totalSize) {
            imageData = imageData.prefix(Int(meta.totalSize))
        }
        
        // Verify CRC
        let calculatedCRC = CRC32.calculate(data: imageData)
        guard calculatedCRC == meta.crc32 else { return }
        
        // Save image
        completedImages[imageId] = imageData
        saveImage(imageId: imageId, data: imageData)
        
        // Record image to mission
        MissionManager.shared.recordImage(imageId: imageId, data: imageData, telemetry: latestTelemetry)
        
        // Audio alert
        AudioAlertManager.shared.playAlert(.imageReceived)
        
        // Notify user
        let generator = UINotificationFeedbackGenerator()
        generator.notificationOccurred(.success)
    }
    
    private func handleTextMessage(payload: Data) {
        guard let msg = TextMessagePayload.deserialize(from: payload) else { return }
        textMessages.append((Date(), msg.message))
        
        // Keep only last 100 messages
        if textMessages.count > 100 {
            textMessages.removeFirst(textMessages.count - 100)
        }
    }
    
    // MARK: - Data Storage
    
    private func saveImage(imageId: UInt16, data: Data) {
        let imagesDir = dataDirectory.appendingPathComponent("images")
        try? FileManager.default.createDirectory(at: imagesDir, withIntermediateDirectories: true)
        
        let dateFormatter = DateFormatter()
        dateFormatter.dateFormat = "yyyy-MM-dd_HH-mm-ss"
        let timestamp = dateFormatter.string(from: Date())
        
        let filename = "image_\(imageId)_\(timestamp).jpg"
        let fileURL = imagesDir.appendingPathComponent(filename)
        
        try? data.write(to: fileURL)
    }
    
    func exportTelemetryCSV() -> URL? {
        guard !telemetryHistory.isEmpty else { return nil }
        
        let dateFormatter = DateFormatter()
        dateFormatter.dateFormat = "yyyy-MM-dd_HH-mm-ss"
        let timestamp = dateFormatter.string(from: Date())
        
        let fileURL = dataDirectory.appendingPathComponent("telemetry_\(timestamp).csv")
        
        var csv = "timestamp,sequence,rssi,snr,latitude,longitude,altitude,speed,heading,satellites,fixType,batteryMV,cpuTemp,radioTemp,imageId,imageProgress\n"
        
        for point in telemetryHistory {
            let line = [
                ISO8601DateFormatter().string(from: point.timestamp),
                String(point.sequence),
                String(point.rssi),
                String(format: "%.1f", point.snr),
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
                String(point.imageId),
                String(point.imageProgress)
            ].joined(separator: ",")
            csv += line + "\n"
        }
        
        try? csv.write(to: fileURL, atomically: true, encoding: .utf8)
        return fileURL
    }
    
    // MARK: - Clear Data
    
    func clearTelemetry() {
        telemetryHistory.removeAll()
        latestTelemetry = nil
        statistics = ReceiverStatistics()
    }
    
    func clearImages() {
        pendingImages.removeAll()
        completedImages.removeAll()
        latestImageId = nil
    }
}
