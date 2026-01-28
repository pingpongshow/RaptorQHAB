//
//  MissionManager.swift
//  RaptorHabMobile
//
//  Manages mission data, session recording, and historical data
//  Ported from Mac version for iOS
//

import Foundation
import UIKit

// MARK: - Mission Model

struct Mission: Identifiable, Codable, Hashable {
    let id: UUID
    var name: String
    let createdAt: Date
    var launchTime: Date?
    var landingTime: Date?
    var maxAltitude: Double
    var totalDistance: Double
    var burstAltitude: Double?
    var launchLocation: Coordinate?
    var landingLocation: Coordinate?
    var burstLocation: Coordinate?
    var telemetryCount: Int
    var imageCount: Int
    var notes: String
    
    struct Coordinate: Codable, Hashable {
        let latitude: Double
        let longitude: Double
        let altitude: Double
    }
    
    var folderName: String {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd_HH-mm"
        return "\(formatter.string(from: createdAt))_\(name.replacingOccurrences(of: " ", with: "_"))"
    }
    
    var duration: TimeInterval? {
        guard let launch = launchTime, let landing = landingTime else { return nil }
        return landing.timeIntervalSince(launch)
    }
    
    var durationFormatted: String {
        guard let dur = duration else { return "N/A" }
        let hours = Int(dur) / 3600
        let minutes = (Int(dur) % 3600) / 60
        let seconds = Int(dur) % 60
        if hours > 0 {
            return String(format: "%d:%02d:%02d", hours, minutes, seconds)
        } else {
            return String(format: "%d:%02d", minutes, seconds)
        }
    }
    
    func hash(into hasher: inout Hasher) {
        hasher.combine(id)
    }
    
    static func == (lhs: Mission, rhs: Mission) -> Bool {
        lhs.id == rhs.id
    }
}

// MARK: - Recorded Image

struct RecordedImage: Identifiable, Codable {
    let id: UUID
    let imageId: UInt16
    let timestamp: Date
    let filename: String
    var latitude: Double?
    var longitude: Double?
    var altitude: Double?
}

// MARK: - Mission Manager

class MissionManager: ObservableObject {
    static let shared = MissionManager()
    
    // Current session
    @Published var isRecording = false
    @Published var isAutoRecording = false
    @Published var currentMission: Mission?
    @Published var recordedTelemetry: [TelemetryPoint] = []
    @Published var recordedImages: [RecordedImage] = []
    
    // Settings
    @Published var autoRecord: Bool {
        didSet { UserDefaults.standard.set(autoRecord, forKey: "MissionAutoRecord") }
    }
    
    // Historical missions
    @Published var missions: [Mission] = []
    
    // Playback state
    @Published var isPlaying = false
    @Published var playbackMission: Mission?
    @Published var playbackTelemetry: [TelemetryPoint] = []
    @Published var playbackIndex: Int = 0
    @Published var playbackSpeed: Double = 1.0
    
    private var playbackTimer: Timer?
    private let missionsFolder: URL
    
    var hasUnsavedRecording: Bool {
        isRecording && !recordedTelemetry.isEmpty
    }
    
    private init() {
        // Setup missions folder in Documents
        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first!
        missionsFolder = documents.appendingPathComponent("RaptorHabMobile/Missions")
        try? FileManager.default.createDirectory(at: missionsFolder, withIntermediateDirectories: true)
        
        // Load settings
        autoRecord = UserDefaults.standard.bool(forKey: "MissionAutoRecord")
        
        // Load missions
        loadMissions()
    }
    
    // MARK: - Recording Control
    
    func startRecording(name: String = "Mission", isAuto: Bool = false) {
        guard !isRecording else { return }
        
        let mission = Mission(
            id: UUID(),
            name: name,
            createdAt: Date(),
            launchTime: nil,
            landingTime: nil,
            maxAltitude: 0,
            totalDistance: 0,
            burstAltitude: nil,
            launchLocation: nil,
            landingLocation: nil,
            burstLocation: nil,
            telemetryCount: 0,
            imageCount: 0,
            notes: ""
        )
        
        currentMission = mission
        recordedTelemetry.removeAll()
        recordedImages.removeAll()
        isRecording = true
        isAutoRecording = isAuto
        
        // Create mission folder
        let missionFolder = missionsFolder.appendingPathComponent(mission.folderName)
        try? FileManager.default.createDirectory(at: missionFolder, withIntermediateDirectories: true)
        try? FileManager.default.createDirectory(at: missionFolder.appendingPathComponent("images"), withIntermediateDirectories: true)
    }
    
    func stopRecording() {
        guard isRecording, var mission = currentMission else { return }
        
        isRecording = false
        isAutoRecording = false
        
        // Update mission stats
        mission.telemetryCount = recordedTelemetry.count
        mission.imageCount = recordedImages.count
        
        if let first = recordedTelemetry.first {
            mission.launchTime = first.timestamp
            mission.launchLocation = Mission.Coordinate(
                latitude: first.latitude,
                longitude: first.longitude,
                altitude: first.altitude
            )
        }
        
        if let last = recordedTelemetry.last {
            mission.landingTime = last.timestamp
            mission.landingLocation = Mission.Coordinate(
                latitude: last.latitude,
                longitude: last.longitude,
                altitude: last.altitude
            )
        }
        
        mission.maxAltitude = recordedTelemetry.map(\.altitude).max() ?? 0
        
        // Calculate total distance
        var distance: Double = 0
        for i in 1..<recordedTelemetry.count {
            distance += haversineDistance(
                lat1: recordedTelemetry[i-1].latitude, lon1: recordedTelemetry[i-1].longitude,
                lat2: recordedTelemetry[i].latitude, lon2: recordedTelemetry[i].longitude
            )
        }
        mission.totalDistance = distance
        
        // Get burst info if detected
        if let burst = BurstDetectionManager.shared.burstPoint {
            mission.burstAltitude = burst.altitude
            mission.burstLocation = Mission.Coordinate(
                latitude: burst.latitude,
                longitude: burst.longitude,
                altitude: burst.altitude
            )
        }
        
        currentMission = mission
        
        // Save mission data
        saveMission(mission)
        
        // Add to list
        missions.insert(mission, at: 0)
    }
    
    func discardRecording() {
        guard isRecording, let mission = currentMission else { return }
        
        // Delete the mission folder
        let missionFolder = missionsFolder.appendingPathComponent(mission.folderName)
        try? FileManager.default.removeItem(at: missionFolder)
        
        // Reset state
        isRecording = false
        isAutoRecording = false
        currentMission = nil
        recordedTelemetry.removeAll()
        recordedImages.removeAll()
    }
    
    // MARK: - Record Data
    
    func recordTelemetry(_ telemetry: TelemetryPoint) {
        // Auto-start recording if enabled and not already recording
        if autoRecord && !isRecording && telemetry.altitude > 100 {
            startRecording(name: "Auto Mission", isAuto: true)
        }
        
        guard isRecording else { return }
        
        recordedTelemetry.append(telemetry)
        
        // Auto-save periodically
        if recordedTelemetry.count % 100 == 0 {
            saveTelemetryIncremental()
        }
    }
    
    func recordImage(imageId: UInt16, data: Data, telemetry: TelemetryPoint?) {
        guard isRecording, let mission = currentMission else { return }
        
        let filename = "image_\(imageId)_\(Int(Date().timeIntervalSince1970)).jpg"
        let imagePath = missionsFolder
            .appendingPathComponent(mission.folderName)
            .appendingPathComponent("images")
            .appendingPathComponent(filename)
        
        try? data.write(to: imagePath)
        
        let recorded = RecordedImage(
            id: UUID(),
            imageId: imageId,
            timestamp: Date(),
            filename: filename,
            latitude: telemetry?.latitude,
            longitude: telemetry?.longitude,
            altitude: telemetry?.altitude
        )
        
        recordedImages.append(recorded)
    }
    
    // MARK: - Save/Load
    
    private func saveMission(_ mission: Mission) {
        let missionFolder = missionsFolder.appendingPathComponent(mission.folderName)
        
        // Save mission metadata
        let metaPath = missionFolder.appendingPathComponent("mission.json")
        if let data = try? JSONEncoder().encode(mission) {
            try? data.write(to: metaPath)
        }
        
        // Save telemetry
        let telemetryPath = missionFolder.appendingPathComponent("telemetry.json")
        if let data = try? JSONEncoder().encode(recordedTelemetry) {
            try? data.write(to: telemetryPath)
        }
        
        // Save image index
        let imagesPath = missionFolder.appendingPathComponent("images.json")
        if let data = try? JSONEncoder().encode(recordedImages) {
            try? data.write(to: imagesPath)
        }
        
        // Save telemetry CSV
        saveTelemetryCSV(missionFolder: missionFolder)
    }
    
    private func saveTelemetryIncremental() {
        guard let mission = currentMission else { return }
        let missionFolder = missionsFolder.appendingPathComponent(mission.folderName)
        let telemetryPath = missionFolder.appendingPathComponent("telemetry.json")
        if let data = try? JSONEncoder().encode(recordedTelemetry) {
            try? data.write(to: telemetryPath)
        }
    }
    
    private func saveTelemetryCSV(missionFolder: URL) {
        let csvPath = missionFolder.appendingPathComponent("telemetry.csv")
        
        var csv = "timestamp,sequence,latitude,longitude,altitude_m,speed_ms,heading,satellites,fix_type,battery_mv,rssi\n"
        
        let isoFormatter = ISO8601DateFormatter()
        
        for point in recordedTelemetry {
            let line = [
                isoFormatter.string(from: point.timestamp),
                String(point.sequence),
                String(format: "%.7f", point.latitude),
                String(format: "%.7f", point.longitude),
                String(format: "%.1f", point.altitude),
                String(format: "%.1f", point.speed),
                String(format: "%.0f", point.heading),
                String(point.satellites),
                point.fixType,
                String(point.batteryMV),
                String(point.rssi)
            ].joined(separator: ",")
            csv += line + "\n"
        }
        
        try? csv.write(to: csvPath, atomically: true, encoding: .utf8)
    }
    
    func loadMissions() {
        missions.removeAll()
        
        guard let contents = try? FileManager.default.contentsOfDirectory(
            at: missionsFolder,
            includingPropertiesForKeys: [.isDirectoryKey],
            options: .skipsHiddenFiles
        ) else { return }
        
        for folder in contents {
            var isDir: ObjCBool = false
            guard FileManager.default.fileExists(atPath: folder.path, isDirectory: &isDir),
                  isDir.boolValue else { continue }
            
            let metaPath = folder.appendingPathComponent("mission.json")
            guard let data = try? Data(contentsOf: metaPath),
                  let mission = try? JSONDecoder().decode(Mission.self, from: data) else { continue }
            
            missions.append(mission)
        }
        
        // Sort by date (newest first)
        missions.sort { $0.createdAt > $1.createdAt }
    }
    
    // MARK: - Mission Operations
    
    func getMissionFolder(_ mission: Mission) -> URL {
        missionsFolder.appendingPathComponent(mission.folderName)
    }
    
    func loadMissionTelemetry(_ mission: Mission) -> [TelemetryPoint] {
        let path = getMissionFolder(mission).appendingPathComponent("telemetry.json")
        guard let data = try? Data(contentsOf: path),
              let telemetry = try? JSONDecoder().decode([TelemetryPoint].self, from: data) else {
            return []
        }
        return telemetry
    }
    
    func loadMissionImages(_ mission: Mission) -> [RecordedImage] {
        let path = getMissionFolder(mission).appendingPathComponent("images.json")
        guard let data = try? Data(contentsOf: path),
              let images = try? JSONDecoder().decode([RecordedImage].self, from: data) else {
            return []
        }
        return images
    }
    
    func getImageData(mission: Mission, image: RecordedImage) -> Data? {
        let imagePath = getMissionFolder(mission)
            .appendingPathComponent("images")
            .appendingPathComponent(image.filename)
        return try? Data(contentsOf: imagePath)
    }
    
    func deleteMission(_ mission: Mission) {
        let folder = getMissionFolder(mission)
        try? FileManager.default.removeItem(at: folder)
        missions.removeAll { $0.id == mission.id }
    }
    
    func updateMissionNotes(_ mission: Mission, notes: String) {
        guard let index = missions.firstIndex(where: { $0.id == mission.id }) else { return }
        
        missions[index].notes = notes
        
        // Save updated mission metadata
        let metaPath = getMissionFolder(mission).appendingPathComponent("mission.json")
        if let data = try? JSONEncoder().encode(missions[index]) {
            try? data.write(to: metaPath)
        }
    }
    
    // MARK: - Playback
    
    func startPlayback(mission: Mission) {
        stopPlayback()
        
        playbackMission = mission
        playbackTelemetry = loadMissionTelemetry(mission)
        playbackIndex = 0
        isPlaying = true
        
        guard !playbackTelemetry.isEmpty else {
            isPlaying = false
            return
        }
        
        // Calculate time between first two points to set initial interval
        var interval: TimeInterval = 1.0
        if playbackTelemetry.count >= 2 {
            interval = playbackTelemetry[1].timestamp.timeIntervalSince(playbackTelemetry[0].timestamp)
            interval = max(0.1, interval / playbackSpeed)
        }
        
        scheduleNextPlaybackPoint(interval: interval)
    }
    
    func stopPlayback() {
        playbackTimer?.invalidate()
        playbackTimer = nil
        isPlaying = false
        playbackIndex = 0
    }
    
    func pausePlayback() {
        playbackTimer?.invalidate()
        playbackTimer = nil
        isPlaying = false
    }
    
    func resumePlayback() {
        guard playbackMission != nil, playbackIndex < playbackTelemetry.count else { return }
        isPlaying = true
        scheduleNextPlaybackPoint(interval: 0.1)
    }
    
    func seekPlayback(to index: Int) {
        playbackIndex = min(max(0, index), playbackTelemetry.count - 1)
        
        // Post current telemetry
        if playbackIndex < playbackTelemetry.count {
            NotificationCenter.default.post(
                name: .playbackTelemetryUpdate,
                object: playbackTelemetry[playbackIndex]
            )
        }
    }
    
    private func scheduleNextPlaybackPoint(interval: TimeInterval) {
        playbackTimer = Timer.scheduledTimer(withTimeInterval: interval, repeats: false) { [weak self] _ in
            self?.advancePlayback()
        }
    }
    
    private func advancePlayback() {
        guard isPlaying, playbackIndex < playbackTelemetry.count else {
            stopPlayback()
            return
        }
        
        let currentPoint = playbackTelemetry[playbackIndex]
        
        // Post telemetry update
        NotificationCenter.default.post(
            name: .playbackTelemetryUpdate,
            object: currentPoint
        )
        
        playbackIndex += 1
        
        // Schedule next point
        if playbackIndex < playbackTelemetry.count {
            let nextPoint = playbackTelemetry[playbackIndex]
            var interval = nextPoint.timestamp.timeIntervalSince(currentPoint.timestamp)
            interval = max(0.05, interval / playbackSpeed)
            scheduleNextPlaybackPoint(interval: interval)
        } else {
            stopPlayback()
        }
    }
    
    // MARK: - Export
    
    func exportMissionCSV(_ mission: Mission) -> URL? {
        let telemetry = loadMissionTelemetry(mission)
        guard !telemetry.isEmpty else { return nil }
        
        let tempURL = FileManager.default.temporaryDirectory
            .appendingPathComponent("\(mission.folderName).csv")
        
        var csv = "timestamp,sequence,latitude,longitude,altitude_m,speed_ms,heading,satellites,battery_mv,rssi,snr\n"
        
        let isoFormatter = ISO8601DateFormatter()
        
        for point in telemetry {
            let line = [
                isoFormatter.string(from: point.timestamp),
                String(point.sequence),
                String(format: "%.7f", point.latitude),
                String(format: "%.7f", point.longitude),
                String(format: "%.1f", point.altitude),
                String(format: "%.1f", point.speed),
                String(format: "%.0f", point.heading),
                String(point.satellites),
                String(point.batteryMV),
                String(point.rssi),
                String(format: "%.1f", point.snr)
            ].joined(separator: ",")
            csv += line + "\n"
        }
        
        try? csv.write(to: tempURL, atomically: true, encoding: .utf8)
        return tempURL
    }
    
    // MARK: - Helpers
    
    private func haversineDistance(lat1: Double, lon1: Double, lat2: Double, lon2: Double) -> Double {
        let R = 6371000.0
        let dLat = (lat2 - lat1) * .pi / 180
        let dLon = (lon2 - lon1) * .pi / 180
        let a = sin(dLat/2) * sin(dLat/2) +
                cos(lat1 * .pi / 180) * cos(lat2 * .pi / 180) *
                sin(dLon/2) * sin(dLon/2)
        let c = 2 * atan2(sqrt(a), sqrt(1-a))
        return R * c
    }
}

// MARK: - Notification Names

extension Notification.Name {
    static let playbackTelemetryUpdate = Notification.Name("playbackTelemetryUpdate")
}
