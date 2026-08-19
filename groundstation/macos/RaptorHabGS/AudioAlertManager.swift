//
//  AudioAlertManager.swift
//  RaptorHabGS
//
//  Manages audio alerts for various flight events
//

import Foundation
import AVFoundation
import AppKit

enum AlertType: String, CaseIterable, Codable {
    case telemetryReceived = "Telemetry Received"
    case burst = "Burst Detected"
    case landing = "Landing Detected"
    case signalLost = "Signal Lost"
    case signalRestored = "Signal Restored"
    case altitudeMilestone = "Altitude Milestone"
    case lowBattery = "Low Battery"
    case imageReceived = "Image Received"
    
    var defaultEnabled: Bool {
        switch self {
        case .burst, .landing, .signalLost, .lowBattery:
            return true
        case .telemetryReceived, .signalRestored, .altitudeMilestone, .imageReceived:
            return false
        }
    }
    
    var systemSound: NSSound.Name? {
        switch self {
        case .telemetryReceived: return NSSound.Name("Tink")
        case .burst: return NSSound.Name("Sosumi")
        case .landing: return NSSound.Name("Glass")
        case .signalLost: return NSSound.Name("Basso")
        case .signalRestored: return NSSound.Name("Ping")
        case .altitudeMilestone: return NSSound.Name("Pop")
        case .lowBattery: return NSSound.Name("Funk")
        case .imageReceived: return NSSound.Name("Submarine")
        }
    }
}

class AudioAlertManager: ObservableObject {
    static let shared = AudioAlertManager()
    
    // UI State
    @Published var showSettings = false
    
    // Global enable/disable
    @Published var alertsEnabled: Bool {
        didSet { saveSettings() }
    }
    
    // Per-alert enable/disable
    @Published var enabledAlerts: [AlertType: Bool] {
        didSet { saveSettings() }
    }
    
    // Alert volume (0.0 - 1.0)
    @Published var volume: Float {
        didSet { saveSettings() }
    }
    
    // Altitude milestones
    @Published var altitudeMilestones: [Double] {
        didSet { saveSettings() }
    }
    private var reachedMilestones: Set<Double> = []
    
    // Signal loss tracking
    private var lastTelemetryTime: Date?
    @Published var signalLostTimeout: TimeInterval = 30  // seconds
    private var signalLostTimer: Timer?
    @Published var isSignalLost = false
    
    // Speech synthesis for announcements
    private let synthesizer = AVSpeechSynthesizer()
    @Published var speakAlerts: Bool {
        didSet { saveSettings() }
    }
    
    private let settingsKey = "AudioAlertSettings"
    
    private init() {
        // Load saved settings or use defaults
        if let data = UserDefaults.standard.data(forKey: settingsKey),
           let settings = try? JSONDecoder().decode(AlertSettings.self, from: data) {
            alertsEnabled = settings.alertsEnabled
            enabledAlerts = settings.enabledAlerts
            volume = settings.volume
            altitudeMilestones = settings.altitudeMilestones
            speakAlerts = settings.speakAlerts
            signalLostTimeout = settings.signalLostTimeout
        } else {
            // Defaults
            alertsEnabled = true
            enabledAlerts = Dictionary(uniqueKeysWithValues: AlertType.allCases.map { ($0, $0.defaultEnabled) })
            volume = 0.7
            altitudeMilestones = [1000, 5000, 10000, 20000, 30000]  // meters
            speakAlerts = true
            signalLostTimeout = 30
        }
        
        // Start signal monitoring
        startSignalMonitoring()
    }
    
    private func saveSettings() {
        let settings = AlertSettings(
            alertsEnabled: alertsEnabled,
            enabledAlerts: enabledAlerts,
            volume: volume,
            altitudeMilestones: altitudeMilestones,
            speakAlerts: speakAlerts,
            signalLostTimeout: signalLostTimeout
        )
        if let data = try? JSONEncoder().encode(settings) {
            UserDefaults.standard.set(data, forKey: settingsKey)
        }
    }
    
    // MARK: - Play Alerts
    
    func playAlert(_ type: AlertType, message: String? = nil) {
        guard alertsEnabled else { return }
        guard enabledAlerts[type] == true else { return }
        
        // Play sound
        if let soundName = type.systemSound, let sound = NSSound(named: soundName) {
            sound.volume = volume
            sound.play()
        }
        
        // Speak message if enabled
        if speakAlerts, let announcement = message ?? defaultAnnouncement(for: type) {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                let utterance = AVSpeechUtterance(string: announcement)
                utterance.volume = self.volume
                utterance.rate = AVSpeechUtteranceDefaultSpeechRate
                self.synthesizer.speak(utterance)
            }
        }
    }
    
    private func defaultAnnouncement(for type: AlertType) -> String? {
        switch type {
        case .burst: return "Burst detected"
        case .landing: return "Landing detected"
        case .signalLost: return "Signal lost"
        case .signalRestored: return "Signal restored"
        case .lowBattery: return "Low battery warning"
        default: return nil
        }
    }
    
    // MARK: - Telemetry Updates
    
    func updateWithTelemetry(_ telemetry: TelemetryPoint) {
        lastTelemetryTime = Date()
        
        // Check if signal was lost and is now restored
        if isSignalLost {
            isSignalLost = false
            playAlert(.signalRestored)
        }
        
        // Check altitude milestones
        for milestone in altitudeMilestones {
            if telemetry.altitude >= milestone && !reachedMilestones.contains(milestone) {
                reachedMilestones.insert(milestone)
                let altStr = milestone >= 1000 ? "\(Int(milestone/1000)) kilometers" : "\(Int(milestone)) meters"
                playAlert(.altitudeMilestone, message: "Altitude milestone: \(altStr)")
            }
        }
        
        // Check battery (if available)
        if telemetry.batteryMv > 0 && telemetry.batteryMv < 3300 {
            playAlert(.lowBattery, message: "Battery low: \(telemetry.batteryMv) millivolts")
        }
    }
    
    // MARK: - Signal Monitoring
    
    private func startSignalMonitoring() {
        signalLostTimer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            self?.checkSignalStatus()
        }
    }
    
    private func checkSignalStatus() {
        guard let lastTime = lastTelemetryTime else { return }
        
        let elapsed = Date().timeIntervalSince(lastTime)
        if elapsed > signalLostTimeout && !isSignalLost {
            isSignalLost = true
            playAlert(.signalLost, message: "Signal lost for \(Int(elapsed)) seconds")
        }
    }
    
    // MARK: - Reset
    
    func resetForNewFlight() {
        reachedMilestones.removeAll()
        lastTelemetryTime = nil
        isSignalLost = false
    }
}

// MARK: - Settings Model

struct AlertSettings: Codable {
    let alertsEnabled: Bool
    let enabledAlerts: [AlertType: Bool]
    let volume: Float
    let altitudeMilestones: [Double]
    let speakAlerts: Bool
    let signalLostTimeout: TimeInterval
}
