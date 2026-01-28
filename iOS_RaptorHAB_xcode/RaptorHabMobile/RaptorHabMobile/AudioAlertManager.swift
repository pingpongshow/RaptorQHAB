//
//  AudioAlertManager.swift
//  RaptorHabMobile
//
//  Manages audio alerts for various flight events
//  iOS version using AVFoundation and system sounds
//

import Foundation
import AVFoundation
import UIKit

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
    
    // iOS system sound IDs
    var systemSoundID: SystemSoundID {
        switch self {
        case .telemetryReceived: return 1057  // Tink
        case .burst: return 1005              // Alarm
        case .landing: return 1016            // Tweet
        case .signalLost: return 1073         // Low beep
        case .signalRestored: return 1054     // Ding
        case .altitudeMilestone: return 1104  // Pop
        case .lowBattery: return 1006         // Alarm 2
        case .imageReceived: return 1003      // Mail received
        }
    }
}

class AudioAlertManager: ObservableObject {
    static let shared = AudioAlertManager()
    
    // Global enable/disable
    @Published var alertsEnabled: Bool {
        didSet { saveSettings() }
    }
    
    // Per-alert enable/disable
    @Published var enabledAlerts: [AlertType: Bool] {
        didSet { saveSettings() }
    }
    
    // Use haptic feedback
    @Published var hapticsEnabled: Bool {
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
           let settings = try? JSONDecoder().decode(AlertSettingsiOS.self, from: data) {
            alertsEnabled = settings.alertsEnabled
            enabledAlerts = settings.enabledAlerts
            hapticsEnabled = settings.hapticsEnabled
            altitudeMilestones = settings.altitudeMilestones
            speakAlerts = settings.speakAlerts
            signalLostTimeout = settings.signalLostTimeout
        } else {
            // Defaults
            alertsEnabled = true
            enabledAlerts = Dictionary(uniqueKeysWithValues: AlertType.allCases.map { ($0, $0.defaultEnabled) })
            hapticsEnabled = true
            altitudeMilestones = [1000, 5000, 10000, 20000, 30000]  // meters
            speakAlerts = true
            signalLostTimeout = 30
        }
        
        // Configure audio session
        configureAudioSession()
        
        // Start signal monitoring
        startSignalMonitoring()
    }
    
    private func configureAudioSession() {
        do {
            try AVAudioSession.sharedInstance().setCategory(.playback, mode: .default, options: [.mixWithOthers])
            try AVAudioSession.sharedInstance().setActive(true)
        } catch {
            print("[Audio] Failed to configure audio session: \(error)")
        }
    }
    
    private func saveSettings() {
        let settings = AlertSettingsiOS(
            alertsEnabled: alertsEnabled,
            enabledAlerts: enabledAlerts,
            hapticsEnabled: hapticsEnabled,
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
        
        // Play system sound
        AudioServicesPlaySystemSound(type.systemSoundID)
        
        // Haptic feedback
        if hapticsEnabled {
            let generator: UINotificationFeedbackGenerator
            switch type {
            case .burst, .signalLost, .lowBattery:
                generator = UINotificationFeedbackGenerator()
                generator.notificationOccurred(.warning)
            case .landing, .imageReceived:
                generator = UINotificationFeedbackGenerator()
                generator.notificationOccurred(.success)
            default:
                let impactGenerator = UIImpactFeedbackGenerator(style: .light)
                impactGenerator.impactOccurred()
                return
            }
        }
        
        // Speak message if enabled
        if speakAlerts, let announcement = message ?? defaultAnnouncement(for: type) {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                let utterance = AVSpeechUtterance(string: announcement)
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
        if telemetry.batteryMV > 0 && telemetry.batteryMV < 3300 {
            playAlert(.lowBattery, message: "Battery low: \(telemetry.batteryMV) millivolts")
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

struct AlertSettingsiOS: Codable {
    let alertsEnabled: Bool
    let enabledAlerts: [AlertType: Bool]
    let hapticsEnabled: Bool
    let altitudeMilestones: [Double]
    let speakAlerts: Bool
    let signalLostTimeout: TimeInterval
}
