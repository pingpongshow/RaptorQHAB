//
//  MissionManager.swift
//  RaptorHabGS
//
//  Manages mission data, session recording, and historical data
//

import Foundation
import AppKit
import UniformTypeIdentifiers

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
        // Remove characters that are problematic in file paths (colons, slashes, etc.)
        let safeName = name
            .replacingOccurrences(of: " ", with: "_")
            .replacingOccurrences(of: ":", with: "-")
            .replacingOccurrences(of: "/", with: "-")
        return "\(formatter.string(from: createdAt))_\(safeName)"
    }
    
    var duration: TimeInterval? {
        guard let launch = launchTime, let landing = landingTime else { return nil }
        return landing.timeIntervalSince(launch)
    }
    
    // Hashable conformance based on id
    func hash(into hasher: inout Hasher) {
        hasher.combine(id)
    }
    
    static func == (lhs: Mission, rhs: Mission) -> Bool {
        lhs.id == rhs.id
    }
}

// MARK: - Mission Manager

class MissionManager: ObservableObject {
    static let shared = MissionManager()
    
    // UI State
    @Published var showRecordingSettings = false

    // Current session
    @Published var isRecording = false
    @Published var isAutoRecording = false  // Track if recording was auto-started
    @Published var currentMission: Mission?
    @Published var recordedTelemetry: [TelemetryPoint] = []
    @Published var recordedImages: [RecordedImage] = []
    
    // Settings
    @Published var missionsFolder: URL? {
        didSet {
            if let folder = missionsFolder {
                UserDefaults.standard.set(folder.path, forKey: "MissionsFolder")
            }
        }
    }
    
    // Historical missions
    @Published var missions: [Mission] = []
    
    private var recordingTimer: Timer?
    private let defaultFolder: URL
    
    struct RecordedImage: Identifiable, Codable {
        let id: UUID
        let imageId: UInt16
        let timestamp: Date
        let filename: String
        var latitude: Double?
        var longitude: Double?
        var altitude: Double?
    }
    
    private init() {
        // Setup default folder
        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first!
        defaultFolder = documents.appendingPathComponent("RaptorHabGS/Missions")
        try? FileManager.default.createDirectory(at: defaultFolder, withIntermediateDirectories: true)

        // Load settings
        if let path = UserDefaults.standard.string(forKey: "MissionsFolder") {
            missionsFolder = URL(fileURLWithPath: path)
        }

        // Load missions
        loadMissions()
    }
    
    var activeMissionsFolder: URL {
        missionsFolder ?? defaultFolder
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
        let missionFolder = activeMissionsFolder.appendingPathComponent(mission.folderName)
        try? FileManager.default.createDirectory(at: missionFolder, withIntermediateDirectories: true)
        try? FileManager.default.createDirectory(at: missionFolder.appendingPathComponent("images"), withIntermediateDirectories: true)
        
    }
    
    func stopRecording() {
        guard isRecording, var mission = currentMission else {
            print("stopRecording called but not recording or no current mission")
            return
        }

        print("Stopping recording for mission: \(mission.name)")
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
        
        // Delete the mission folder that was created
        let missionFolder = activeMissionsFolder.appendingPathComponent(mission.folderName)
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
        // Always start recording on first telemetry if not already recording
        if !isRecording {
            let formatter = DateFormatter()
            formatter.dateFormat = "yyyy-MM-dd HH:mm"
            let name = "Mission \(formatter.string(from: Date()))"
            startRecording(name: name, isAuto: true)
            AudioAlertManager.shared.playAlert(.telemetryReceived, message: "Mission recording started")
        }

        recordedTelemetry.append(telemetry)

        // Auto-save periodically (every 100 points)
        if recordedTelemetry.count % 100 == 0 {
            saveTelemetryIncremental()
        }
    }
    
    func recordImage(imageId: UInt16, data: Data, telemetry: TelemetryPoint?) {
        // Start recording if not already (image received before telemetry)
        if !isRecording {
            let formatter = DateFormatter()
            formatter.dateFormat = "yyyy-MM-dd HH:mm"
            let name = "Mission \(formatter.string(from: Date()))"
            startRecording(name: name, isAuto: true)
        }

        guard let mission = currentMission else { return }

        let filename = "image_\(imageId)_\(Int(Date().timeIntervalSince1970)).jpg"
        let imagePath = activeMissionsFolder
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
        let missionFolder = activeMissionsFolder.appendingPathComponent(mission.folderName)
        print("Saving mission to: \(missionFolder.path)")

        // Ensure mission folder exists
        do {
            try FileManager.default.createDirectory(at: missionFolder, withIntermediateDirectories: true)
        } catch {
            print("Error creating mission folder: \(error)")
        }

        // Save mission metadata
        let metaPath = missionFolder.appendingPathComponent("mission.json")
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = .prettyPrinted

        do {
            let data = try encoder.encode(mission)
            try data.write(to: metaPath)
            print("Saved mission.json successfully")
        } catch {
            print("Error saving mission.json: \(error)")
        }

        // Save telemetry
        let telemetryPath = missionFolder.appendingPathComponent("telemetry.json")
        do {
            let data = try encoder.encode(recordedTelemetry)
            try data.write(to: telemetryPath)
            print("Saved telemetry.json with \(recordedTelemetry.count) points")
        } catch {
            print("Error saving telemetry.json: \(error)")
        }

        // Save image index
        let imagesPath = missionFolder.appendingPathComponent("images.json")
        do {
            let data = try encoder.encode(recordedImages)
            try data.write(to: imagesPath)
            print("Saved images.json with \(recordedImages.count) images")
        } catch {
            print("Error saving images.json: \(error)")
        }

        // Save telemetry CSV for easy viewing
        saveTelemetryCSV(missionFolder: missionFolder)
    }
    
    private func saveTelemetryIncremental() {
        guard let mission = currentMission else { return }
        let missionFolder = activeMissionsFolder.appendingPathComponent(mission.folderName)
        let telemetryPath = missionFolder.appendingPathComponent("telemetry.json")
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        if let data = try? encoder.encode(recordedTelemetry) {
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
                String(point.fixType),
                String(point.batteryMv),
                String(point.rssi)
            ].joined(separator: ",")
            csv += line + "\n"
        }
        
        try? csv.write(to: csvPath, atomically: true, encoding: .utf8)
    }
    
    func loadMissions() {
        missions.removeAll()
        print("Loading missions from: \(activeMissionsFolder.path)")

        guard let contents = try? FileManager.default.contentsOfDirectory(
            at: activeMissionsFolder,
            includingPropertiesForKeys: [.isDirectoryKey],
            options: .skipsHiddenFiles
        ) else {
            print("Could not read missions folder")
            return
        }

        print("Found \(contents.count) items in missions folder")
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601

        for folder in contents {
            var isDir: ObjCBool = false
            guard FileManager.default.fileExists(atPath: folder.path, isDirectory: &isDir),
                  isDir.boolValue else { continue }

            let metaPath = folder.appendingPathComponent("mission.json")
            print("Looking for mission.json at: \(metaPath.path)")

            if let data = try? Data(contentsOf: metaPath) {
                do {
                    let mission = try decoder.decode(Mission.self, from: data)
                    missions.append(mission)
                    print("Loaded mission: \(mission.name)")
                } catch {
                    print("Error decoding mission.json: \(error)")
                }
            } else {
                print("No mission.json found in \(folder.lastPathComponent)")
            }
        }

        // Sort by date (newest first)
        missions.sort { $0.createdAt > $1.createdAt }
        print("Loaded \(missions.count) missions total")
    }
    
    // MARK: - Mission Operations
    
    func getMissionFolder(_ mission: Mission) -> URL {
        activeMissionsFolder.appendingPathComponent(mission.folderName)
    }
    
    func loadMissionTelemetry(_ mission: Mission) -> [TelemetryPoint] {
        let path = getMissionFolder(mission).appendingPathComponent("telemetry.json")
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        guard let data = try? Data(contentsOf: path),
              let telemetry = try? decoder.decode([TelemetryPoint].self, from: data) else {
            return []
        }
        return telemetry
    }

    func loadMissionImages(_ mission: Mission) -> [RecordedImage] {
        let path = getMissionFolder(mission).appendingPathComponent("images.json")
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        guard let data = try? Data(contentsOf: path),
              let images = try? decoder.decode([RecordedImage].self, from: data) else {
            return []
        }
        return images
    }
    
    func deleteMission(_ mission: Mission) {
        let folder = getMissionFolder(mission)
        try? FileManager.default.removeItem(at: folder)
        missions.removeAll { $0.id == mission.id }
    }
    
    func deleteMissionImages(_ mission: Mission) {
        let imagesFolder = getMissionFolder(mission).appendingPathComponent("images")
        try? FileManager.default.removeItem(at: imagesFolder)
        try? FileManager.default.createDirectory(at: imagesFolder, withIntermediateDirectories: true)
        
        // Update images.json
        let imagesPath = getMissionFolder(mission).appendingPathComponent("images.json")
        try? "[]".write(to: imagesPath, atomically: true, encoding: .utf8)
        
        // Update mission
        if let index = missions.firstIndex(where: { $0.id == mission.id }) {
            missions[index].imageCount = 0
        }
    }
    
    func deleteMissionTelemetry(_ mission: Mission) {
        let telemetryPath = getMissionFolder(mission).appendingPathComponent("telemetry.json")
        try? "[]".write(to: telemetryPath, atomically: true, encoding: .utf8)
        
        let csvPath = getMissionFolder(mission).appendingPathComponent("telemetry.csv")
        try? FileManager.default.removeItem(at: csvPath)
        
        // Update mission
        if let index = missions.firstIndex(where: { $0.id == mission.id }) {
            missions[index].telemetryCount = 0
        }
    }
    
    func exportMission(_ mission: Mission, completion: @escaping (URL?) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            let missionFolder = self.getMissionFolder(mission)
            let zipName = "\(mission.folderName).zip"
            let tempZip = FileManager.default.temporaryDirectory.appendingPathComponent(zipName)
            
            // Remove existing temp file
            try? FileManager.default.removeItem(at: tempZip)
            
            // Create zip using ditto command (macOS)
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/bin/ditto")
            process.arguments = ["-c", "-k", "--keepParent", missionFolder.path, tempZip.path]
            
            do {
                try process.run()
                process.waitUntilExit()
                
                if process.terminationStatus == 0 {
                    DispatchQueue.main.async {
                        completion(tempZip)
                    }
                } else {
                    DispatchQueue.main.async {
                        completion(nil)
                    }
                }
            } catch {
                DispatchQueue.main.async {
                    completion(nil)
                }
            }
        }
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
