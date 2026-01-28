//
//  BurstDetection.swift
//  RaptorHabMobile
//
//  Detects balloon burst based on descent rate changes
//  Ported from Mac version for iOS
//

import Foundation
import CoreLocation
import UIKit

// MARK: - Burst Point

struct BurstPoint: Codable {
    let timestamp: Date
    let latitude: Double
    let longitude: Double
    let altitude: Double
    let maxAltitude: Double
    
    var coordinate: CLLocationCoordinate2D {
        CLLocationCoordinate2D(latitude: latitude, longitude: longitude)
    }
}

// MARK: - Burst Detection Manager

class BurstDetectionManager: ObservableObject {
    static let shared = BurstDetectionManager()
    
    @Published var burstDetected = false
    @Published var burstPoint: BurstPoint?
    @Published var maxAltitudeReached: Double = 0
    @Published var currentVerticalSpeed: Double = 0
    @Published var flightPhase: FlightPhase = .prelaunch
    
    // Detection parameters
    @Published var burstThreshold: Double = -5.0  // m/s descent rate to trigger burst
    @Published var confirmationSamples: Int = 3    // Number of samples to confirm burst
    
    private var descentSamples: [Double] = []
    private var previousAltitudes: [(Date, Double)] = []
    
    private init() {}
    
    // MARK: - Update with Telemetry
    
    func update(with telemetry: TelemetryPoint) {
        // Track max altitude
        if telemetry.altitude > maxAltitudeReached {
            maxAltitudeReached = telemetry.altitude
        }
        
        // Calculate vertical speed
        previousAltitudes.append((telemetry.timestamp, telemetry.altitude))
        
        // Keep last 10 samples
        if previousAltitudes.count > 10 {
            previousAltitudes.removeFirst()
        }
        
        // Calculate average vertical speed over recent samples
        if previousAltitudes.count >= 2 {
            let recent = Array(previousAltitudes.suffix(5))
            var totalSpeed: Double = 0
            var count = 0
            
            for i in 1..<recent.count {
                let dt = recent[i].0.timeIntervalSince(recent[i-1].0)
                guard dt > 0 else { continue }
                let dAlt = recent[i].1 - recent[i-1].1
                totalSpeed += dAlt / dt
                count += 1
            }
            
            if count > 0 {
                currentVerticalSpeed = totalSpeed / Double(count)
            }
        }
        
        // Update flight phase
        updateFlightPhase(altitude: telemetry.altitude)
        
        // Check for burst (only if not already detected)
        if !burstDetected {
            checkForBurst(telemetry: telemetry)
        }
    }
    
    private func updateFlightPhase(altitude: Double) {
        if burstDetected {
            if altitude < 100 && abs(currentVerticalSpeed) < 1 {
                flightPhase = .landed
            } else {
                flightPhase = .descending
            }
        } else if altitude < 100 && abs(currentVerticalSpeed) < 1 {
            flightPhase = .prelaunch
        } else if currentVerticalSpeed > 1.0 {
            flightPhase = .ascending
        } else if currentVerticalSpeed < -1.0 {
            // Might be burst, but wait for confirmation
            flightPhase = .ascending
        } else {
            flightPhase = .floating
        }
    }
    
    private func checkForBurst(telemetry: TelemetryPoint) {
        // Need to be at significant altitude to detect burst
        guard maxAltitudeReached > 1000 else { return }
        
        // Check if descending fast enough
        if currentVerticalSpeed < burstThreshold {
            descentSamples.append(currentVerticalSpeed)
            
            // Confirm burst after multiple samples
            if descentSamples.count >= confirmationSamples {
                let avgDescent = descentSamples.reduce(0, +) / Double(descentSamples.count)
                if avgDescent < burstThreshold {
                    triggerBurst(telemetry: telemetry)
                }
            }
        } else {
            // Reset if not consistently descending
            descentSamples.removeAll()
        }
    }
    
    private func triggerBurst(telemetry: TelemetryPoint) {
        burstDetected = true
        flightPhase = .descending
        
        burstPoint = BurstPoint(
            timestamp: telemetry.timestamp,
            latitude: telemetry.latitude,
            longitude: telemetry.longitude,
            altitude: telemetry.altitude,
            maxAltitude: maxAltitudeReached
        )
        
        // Trigger haptic feedback
        let generator = UINotificationFeedbackGenerator()
        generator.notificationOccurred(.warning)
        
        // Play system sound for burst
        AudioAlertManager.shared.playAlert(.burst)
        
        // Post notification
        NotificationCenter.default.post(name: .burstDetected, object: burstPoint)
    }
    
    // MARK: - Reset
    
    func reset() {
        burstDetected = false
        burstPoint = nil
        maxAltitudeReached = 0
        currentVerticalSpeed = 0
        flightPhase = .prelaunch
        descentSamples.removeAll()
        previousAltitudes.removeAll()
    }
}

// MARK: - Notification Names

extension Notification.Name {
    static let burstDetected = Notification.Name("burstDetected")
    static let landingDetected = Notification.Name("landingDetected")
    static let altitudeMilestone = Notification.Name("altitudeMilestone")
    static let signalLost = Notification.Name("signalLost")
    static let signalRestored = Notification.Name("signalRestored")
}
