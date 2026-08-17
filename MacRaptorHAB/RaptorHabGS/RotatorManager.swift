//
//  RotatorManager.swift
//  RaptorHabGS
//
//  Antenna rotator control via rotctld TCP protocol
//

import Foundation
import Network

// MARK: - Rotator Configuration

struct RotatorConfig: Codable {
    var enabled: Bool = false
    var host: String = "127.0.0.1"
    var port: Int = 4533
    var autoTrack: Bool = true
    var updateInterval: Double = 2.0  // seconds between position updates
    var parkAzimuth: Double = 0
    var parkElevation: Double = 0
}

// MARK: - Rotator Position

struct RotatorPosition {
    var azimuth: Double
    var elevation: Double
    
    var azimuthCardinal: String {
        let directions = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                          "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
        let index = Int((azimuth + 11.25) / 22.5) % 16
        return directions[index]
    }
}

// MARK: - Rotator Manager

class RotatorManager: ObservableObject {
    
    static let shared = RotatorManager()
    
    // Configuration
    @Published var config: RotatorConfig {
        didSet { saveConfig() }
    }
    
    // UI State
    @Published var showSettings = false
    
    // Connection state
    @Published var isConnected = false
    @Published var connectionError: String?
    
    // Current position
    @Published var currentPosition: RotatorPosition?
    @Published var targetPosition: RotatorPosition?
    @Published var isMoving = false
    
    // Statistics
    @Published var commandsSent: Int = 0
    @Published var lastCommandTime: Date?
    
    private var connection: NWConnection?
    private var trackingTimer: Timer?
    private var pollingTimer: Timer?
    private let configKey = "RotatorConfig"
    
    private init() {
        if let data = UserDefaults.standard.data(forKey: configKey),
           let saved = try? JSONDecoder().decode(RotatorConfig.self, from: data) {
            config = saved
        } else {
            config = RotatorConfig()
        }
    }
    
    private func saveConfig() {
        if let data = try? JSONEncoder().encode(config) {
            UserDefaults.standard.set(data, forKey: configKey)
        }
    }
    
    // MARK: - Connection Management
    
    func connect() {
        guard config.enabled else { return }
        
        disconnect()
        
        let host = NWEndpoint.Host(config.host)
        let port = NWEndpoint.Port(integerLiteral: UInt16(config.port))
        
        connection = NWConnection(host: host, port: port, using: .tcp)
        
        connection?.stateUpdateHandler = { [weak self] state in
            DispatchQueue.main.async {
                switch state {
                case .ready:
                    self?.isConnected = true
                    self?.connectionError = nil
                    self?.startPolling()
                    
                case .failed(let error):
                    self?.isConnected = false
                    self?.connectionError = error.localizedDescription
                    
                case .cancelled:
                    self?.isConnected = false
                    
                default:
                    break
                }
            }
        }
        
        connection?.start(queue: .global(qos: .userInitiated))
    }
    
    func disconnect() {
        stopTracking()
        stopPolling()
        connection?.cancel()
        connection = nil
        isConnected = false
        currentPosition = nil
        targetPosition = nil
    }
    
    // MARK: - Polling
    
    private func startPolling() {
        pollingTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            self?.getPosition()
        }
    }
    
    private func stopPolling() {
        pollingTimer?.invalidate()
        pollingTimer = nil
    }
    
    // MARK: - Auto Tracking
    
    func startTracking() {
        guard config.autoTrack else { return }
        
        trackingTimer = Timer.scheduledTimer(withTimeInterval: config.updateInterval, repeats: true) { [weak self] _ in
            self?.updateTrackingPosition()
        }
        
        // Immediate update
        updateTrackingPosition()
    }
    
    func stopTracking() {
        trackingTimer?.invalidate()
        trackingTimer = nil
    }
    
    private func updateTrackingPosition() {
        guard config.autoTrack, isConnected else { return }
        
        // Get bearing from GPS manager
        if let bearing = GPSManager.shared.bearingToPayload {
            setPosition(azimuth: bearing.bearing, elevation: max(0, bearing.elevation))
        }
    }
    
    // MARK: - rotctld Commands
    
    func getPosition() {
        sendCommand("p") { [weak self] response in
            // Response format: "AZ\nEL\n" or "AZ EL"
            let parts = response
                .replacingOccurrences(of: "\n", with: " ")
                .split(separator: " ")
                .compactMap { Double($0) }
            
            if parts.count >= 2 {
                DispatchQueue.main.async {
                    self?.currentPosition = RotatorPosition(azimuth: parts[0], elevation: parts[1])
                    
                    // Check if we're still moving
                    if let target = self?.targetPosition {
                        let azDiff = abs(target.azimuth - parts[0])
                        let elDiff = abs(target.elevation - parts[1])
                        self?.isMoving = azDiff > 2 || elDiff > 2
                    }
                }
            }
        }
    }
    
    func setPosition(azimuth: Double, elevation: Double) {
        let az = max(0, min(360, azimuth))
        let el = max(0, min(90, elevation))
        
        targetPosition = RotatorPosition(azimuth: az, elevation: el)
        isMoving = true
        
        // rotctld command: P <azimuth> <elevation>
        sendCommand(String(format: "P %.1f %.1f", az, el)) { [weak self] response in
            if response.contains("RPRT 0") || response.isEmpty {
            } else {
            }
            DispatchQueue.main.async {
                self?.commandsSent += 1
                self?.lastCommandTime = Date()
            }
        }
    }
    
    func stop() {
        sendCommand("S") { response in
        }
        isMoving = false
    }
    
    func park() {
        setPosition(azimuth: config.parkAzimuth, elevation: config.parkElevation)
    }
    
    private func sendCommand(_ command: String, completion: @escaping (String) -> Void) {
        guard let connection = connection, isConnected else {
            completion("")
            return
        }
        
        let data = (command + "\n").data(using: .utf8)!
        
        connection.send(content: data, completion: .contentProcessed { [weak self] sendError in
            if sendError != nil {
                completion("")
                return
            }
            
            // Read response
            self?.connection?.receive(minimumIncompleteLength: 1, maximumLength: 1024) { data, _, _, _ in
                if let data = data, let response = String(data: data, encoding: .utf8) {
                    completion(response.trimmingCharacters(in: .whitespacesAndNewlines))
                } else {
                    completion("")
                }
            }
        })
    }
    
    // MARK: - Reset
    
    func resetStats() {
        commandsSent = 0
        lastCommandTime = nil
    }
}
