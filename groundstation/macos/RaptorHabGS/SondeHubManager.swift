//
//  SondeHubManager.swift
//  RaptorHab Ground Station
//
//  SondeHub Amateur Telemetry Upload
//  Compatible with sondehub Python library format
//

import Foundation
import CoreLocation

// MARK: - SondeHub Telemetry Model

struct SondeHubTelemetry: Codable {
    let software_name: String
    let software_version: String
    let uploader_callsign: String
    let uploader_position: [Double]?
    let uploader_antenna: String?
    let time_received: String
    let payload_callsign: String
    let datetime: String
    let lat: Double
    let lon: Double
    let alt: Double
    let frame: Int?
    let sats: Int?
    let heading: Double?
    let vel_h: Double?
    let vel_v: Double?
    let temp: Double?
    let batt: Double?
    let snr: Double?
    let rssi: Double?
    let frequency: Double?
    let modulation: String?
    let comment: String?
}

// MARK: - SondeHub Configuration

struct SondeHubConfig: Codable {
    var enabled: Bool = false
    var uploaderCallsign: String = ""
    var payloadCallsign: String = ""
    var uploadTelemetry: Bool = true
    var uploadImages: Bool = false
    var uploadInterval: Double = 5.0
    var includeComment: Bool = false
    var comment: String = ""

    /// Shown on the SondeHub map beside the station. Empty fields make a
    /// receiver look abandoned; filling them is how other chasers know what
    /// they are looking at and whether to trust its coverage.
    var uploaderRadio: String = ""
    var uploaderAntenna: String = ""
    var contactEmail: String = ""
    /// A station in a chase car moves. SondeHub treats mobile listeners
    /// differently on the map, and a car reporting as a fixed site drags a
    /// trail of stale positions behind it.
    var mobile: Bool = false
    
    var isValid: Bool {
        enabled && !uploaderCallsign.isEmpty && !payloadCallsign.isEmpty
    }
}

// MARK: - SondeHub Manager

class SondeHubManager: ObservableObject {
    
    static let shared = SondeHubManager()
    
    // Configuration
    @Published var config: SondeHubConfig {
        didSet { saveConfig() }
    }
    
    /// Re-announce the station this often. An hour is well inside the
    /// window SondeHub ages listeners out at, and costs one small request.
    static let stationUploadInterval: TimeInterval = 3600

    private var lastStationUpload: Date?

    // UI State
    @Published var showSettings = false
    
    // Status
    @Published var isUploading = false
    @Published var lastUploadTime: Date?
    @Published var lastUploadStatus: String = "Not started"
    @Published var uploadCount: Int = 0
    @Published var errorCount: Int = 0
    
    // API endpoints
    private let telemetryURL = URL(string: "https://api.v2.sondehub.org/amateur/telemetry")!
    private let listenersURL = URL(string: "https://api.v2.sondehub.org/amateur/listeners")!
    
    // Rate limiting
    private var lastTelemetryUpload: Date?
    private let uploadQueue = DispatchQueue(label: "com.raptorhabgs.sondehub")
    
    // Ground station position (from GPS)
    var groundStationPosition: CLLocationCoordinate2D?
    var groundStationAltitude: Double?
    
    private let configKey = "SondeHubConfig"
    
    private init() {
        // Load saved config
        if let data = UserDefaults.standard.data(forKey: configKey),
           let saved = try? JSONDecoder().decode(SondeHubConfig.self, from: data) {
            config = saved
        } else {
            config = SondeHubConfig()
        }
    }
    
    private func saveConfig() {
        if let data = try? JSONEncoder().encode(config) {
            UserDefaults.standard.set(data, forKey: configKey)
        }
    }
    
    func resetStats() {
        uploadCount = 0
        errorCount = 0
        lastUploadStatus = "Reset"
    }
    
    // MARK: - Date Formatting
    
    /// Format date in SondeHub expected format: %Y-%m-%dT%H:%M:%S.%fZ
    private func formatDateTime(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSSSSS'Z'"
        formatter.timeZone = TimeZone(identifier: "UTC")
        return formatter.string(from: date)
    }
    
    /// Format RFC 2822 date for Date header
    private func formatRFC2822Date() -> String {
        let formatter = DateFormatter()
        formatter.dateFormat = "EEE, dd MMM yyyy HH:mm:ss zzz"
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "GMT")
        return formatter.string(from: Date())
    }
    
    // MARK: - Telemetry Upload
    
    func uploadTelemetry(_ telemetry: TelemetryPoint, rssi: Float, snr: Float, frequency: Double = 915.0) {
        
        guard config.isValid && config.uploadTelemetry else {
            return
        }
        
        // Validation: Skip if lat/lon both 0 (no GPS fix)
        if telemetry.latitude == 0.0 && telemetry.longitude == 0.0 {
            return
        }
        
        // Validation: Skip if satellites = 0
        if telemetry.satellites == 0 {
            return
        }
        
        // Rate limiting
        if let last = lastTelemetryUpload,
           Date().timeIntervalSince(last) < config.uploadInterval {
            return
        }
        
        lastTelemetryUpload = Date()
        
        uploadQueue.async { [weak self] in
            self?.performTelemetryUpload(telemetry, rssi: rssi, snr: snr, frequency: frequency)
        }
    }
    
    private func performTelemetryUpload(_ telemetry: TelemetryPoint, rssi: Float, snr: Float, frequency: Double) {
        
        // Build uploader position if available
        var uploaderPosition: [Double]? = nil
        if let pos = groundStationPosition, let alt = groundStationAltitude {
            uploaderPosition = [pos.latitude, pos.longitude, alt]
        }
        
        let payload = SondeHubTelemetry(
            software_name: "RaptorHabGS",
            software_version: "1.0",
            uploader_callsign: config.uploaderCallsign,
            uploader_position: uploaderPosition,
            uploader_antenna: nil,
            time_received: formatDateTime(Date()),
            payload_callsign: config.payloadCallsign,
            datetime: formatDateTime(telemetry.gpsTime),
            lat: telemetry.latitude,
            lon: telemetry.longitude,
            alt: telemetry.altitude,
            frame: Int(telemetry.sequence),
            sats: Int(telemetry.satellites),
            heading: telemetry.heading,
            vel_h: telemetry.speed,
            vel_v: nil,
            temp: telemetry.cpuTemp,
            batt: Double(telemetry.batteryMV) / 1000.0,
            snr: Double(snr),
            rssi: Double(rssi),
            frequency: frequency,
            modulation: "FSK",
            comment: config.includeComment ? config.comment : nil
        )
        
        // SondeHub expects an array of telemetry objects
        let payloadArray = [payload]
        
        guard let jsonData = try? JSONEncoder().encode(payloadArray) else {
            DispatchQueue.main.async {
                self.errorCount += 1
            }
            updateStatus("JSON encode failed", isError: true)
            return
        }
        
        var request = URLRequest(url: telemetryURL)
        request.httpMethod = "PUT"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("RaptorHabGS-1.0", forHTTPHeaderField: "User-Agent")
        request.setValue(formatRFC2822Date(), forHTTPHeaderField: "Date")
        request.httpBody = jsonData
        request.timeoutInterval = 20
        
        DispatchQueue.main.async {
            self.isUploading = true
        }
        
        
        URLSession.shared.dataTask(with: request) { [weak self] data, response, error in
            DispatchQueue.main.async {
                self?.isUploading = false
                self?.lastUploadTime = Date()
                
                if let error = error {
                    self?.errorCount += 1
                    self?.updateStatus("Error: \(error.localizedDescription)", isError: true)
                    return
                }
                
                if let httpResponse = response as? HTTPURLResponse {
                    let bodyStr = data.flatMap { String(data: $0, encoding: .utf8) } ?? ""
                    
                    if httpResponse.statusCode == 200 {
                        self?.uploadCount += 1
                        self?.updateStatus("OK (\(self?.uploadCount ?? 0) uploads)", isError: false)
                    } else {
                        self?.errorCount += 1
                        self?.updateStatus("HTTP \(httpResponse.statusCode): \(bodyStr)", isError: true)
                    }
                }
            }
        }.resume()
    }
    
    private func updateStatus(_ status: String, isError: Bool) {
        DispatchQueue.main.async {
            self.lastUploadStatus = status
            // Note: errorCount is incremented by the caller when needed
        }
    }
    
    // MARK: - Station Position Upload
    
    func uploadStationPosition() {
        guard config.isValid else { return }
        guard let pos = groundStationPosition, let alt = groundStationAltitude else { return }
        
        let position: [String: Any] = [
            "software_name": "RaptorHabGS",
            "software_version": "1.0",
            "uploader_callsign": config.uploaderCallsign,
            "uploader_position": [pos.latitude, pos.longitude, alt],
            "uploader_radio": config.uploaderRadio,
            "uploader_antenna": config.uploaderAntenna,
            "uploader_contact_email": config.contactEmail,
            "mobile": config.mobile
        ]
        
        guard let jsonData = try? JSONSerialization.data(withJSONObject: position) else { return }
        
        var request = URLRequest(url: listenersURL)
        request.httpMethod = "PUT"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("RaptorHabGS-1.0", forHTTPHeaderField: "User-Agent")
        request.setValue(formatRFC2822Date(), forHTTPHeaderField: "Date")
        request.httpBody = jsonData
        
        URLSession.shared.dataTask(with: request) { [weak self] _, response, error in
            guard let self = self else { return }
            if let error = error {
                self.updateStatus("Station position failed: \(error.localizedDescription)",
                                  isError: true)
                return
            }
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            if (200...299).contains(code) {
                self.lastStationUpload = Date()
                self.updateStatus("Station position uploaded", isError: false)
            } else {
                // Worth surfacing rather than swallowing: a station that
                // silently fails to register looks identical to one nobody
                // is listening to, and the difference matters when you are
                // relying on other receivers to hear your balloon.
                self.updateStatus("Station position rejected (HTTP \(code))",
                                  isError: true)
            }
        }.resume()
    }

    /// Register the station if it has never been, or if it is due again.
    ///
    /// SondeHub expects listeners to re-announce themselves; a station that
    /// registered once and went quiet ages off the map. Called from the same
    /// telemetry path as everything else, so it needs no timer of its own.
    func uploadStationPositionIfDue() {
        guard config.isValid, groundStationPosition != nil else { return }
        let due = lastStationUpload.map {
            Date().timeIntervalSince($0) > Self.stationUploadInterval
        } ?? true
        if due { uploadStationPosition() }
    }
    
    // MARK: - Image Upload
    
    func uploadImage(_ imageData: Data, imageId: UInt16, telemetry: TelemetryPoint?) {
        guard config.isValid && config.uploadImages else { return }
        
        // SondeHub amateur doesn't have an official image API yet
        // This is a placeholder for future implementation
    }
}
