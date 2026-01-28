//
//  LandingPrediction.swift
//  RaptorHabMobile
//
//  Predicts landing location based on current position, descent rate, and wind
//  Ported from Mac version for iOS
//

import Foundation
import CoreLocation

// MARK: - Prediction Configuration

struct PredictionConfig: Codable {
    var windSpeed: Double = 5.0         // m/s (fallback when no profile)
    var windDirection: Double = 270     // degrees (direction wind is coming FROM)
    var useAutoWind: Bool = false       // Auto-calculate from drift
    var useWindProfile: Bool = true     // Use multi-altitude wind data when available
    var descentRateOverride: Double?    // Manual descent rate override
    var burstAltitude: Double = 30000   // Expected burst altitude in meters
    var seaLevelTarget: Double = 0      // Target landing altitude (sea level default)
    
    // Descent rate model (typical for 600g balloon + 1kg payload)
    var ascentRate: Double = 5.0        // m/s typical ascent
    var descentRateAtBurst: Double = 30 // m/s right after burst
    var descentRateAtLanding: Double = 5 // m/s near ground (parachute)
}

// MARK: - Wind Profile

struct LiveWindProfile {
    var layers: [LiveWindLayer]
    var fetchTime: Date
    var isValid: Bool { !layers.isEmpty && Date().timeIntervalSince(fetchTime) < 3600 } // Valid for 1 hour
    
    // Interpolate wind at a given altitude
    func windAt(altitude: Double) -> (speed: Double, direction: Double)? {
        guard !layers.isEmpty else { return nil }
        
        let sorted = layers.sorted { $0.altitude < $1.altitude }
        
        // Below lowest layer
        if altitude <= sorted.first!.altitude {
            return (sorted.first!.speed, sorted.first!.direction)
        }
        
        // Above highest layer
        if altitude >= sorted.last!.altitude {
            return (sorted.last!.speed, sorted.last!.direction)
        }
        
        // Find surrounding layers and interpolate
        for i in 0..<(sorted.count - 1) {
            let lower = sorted[i]
            let upper = sorted[i + 1]
            
            if altitude >= lower.altitude && altitude <= upper.altitude {
                let factor = (altitude - lower.altitude) / (upper.altitude - lower.altitude)
                
                let speed = lower.speed + (upper.speed - lower.speed) * factor
                
                // Interpolate direction (handle wrap-around)
                var dirDiff = upper.direction - lower.direction
                if dirDiff > 180 { dirDiff -= 360 }
                if dirDiff < -180 { dirDiff += 360 }
                var direction = lower.direction + dirDiff * factor
                if direction < 0 { direction += 360 }
                if direction >= 360 { direction -= 360 }
                
                return (speed, direction)
            }
        }
        
        return nil
    }
}

struct LiveWindLayer {
    var speed: Double
    var direction: Double
    var altitude: Double
}

// MARK: - Prediction Result

struct LandingPrediction: Equatable, Identifiable {
    let id = UUID()
    let predictedLat: Double
    let predictedLon: Double
    let timeToLanding: TimeInterval     // seconds
    let distanceToLanding: Double       // meters from current position
    let bearingToLanding: Double        // degrees
    let confidence: PredictionConfidence
    let descentRate: Double             // current calculated descent rate
    let phase: FlightPhase
    let timestamp: Date
    let usedWindProfile: Bool           // Whether prediction used multi-altitude wind
    
    var predictedCoordinate: CLLocationCoordinate2D {
        CLLocationCoordinate2D(latitude: predictedLat, longitude: predictedLon)
    }
    
    static func == (lhs: LandingPrediction, rhs: LandingPrediction) -> Bool {
        lhs.predictedLat == rhs.predictedLat && lhs.predictedLon == rhs.predictedLon
    }
}

enum PredictionConfidence: String, Codable {
    case high = "High"
    case medium = "Medium"
    case low = "Low"
    case veryLow = "Very Low"
    
    var color: String {
        switch self {
        case .high: return "green"
        case .medium: return "yellow"
        case .low: return "orange"
        case .veryLow: return "red"
        }
    }
}

enum FlightPhase: String, Codable {
    case prelaunch = "Pre-Launch"
    case ascending = "Ascending"
    case floating = "Floating"
    case descending = "Descending"
    case landed = "Landed"
}

// MARK: - Landing Prediction Manager

class LandingPredictionManager: ObservableObject {
    
    static let shared = LandingPredictionManager()
    
    @Published var config: PredictionConfig {
        didSet { saveConfig() }
    }
    @Published var currentPrediction: LandingPrediction?
    @Published var predictionHistory: [LandingPrediction] = []
    
    // Auto-calculated wind from drift
    @Published var calculatedWindSpeed: Double = 0
    @Published var calculatedWindDirection: Double = 0
    
    // Wind profile for altitude-specific wind data
    @Published var windProfile: LiveWindProfile?
    @Published var isLoadingWindProfile = false
    @Published var windProfileError: String?
    
    private let configKey = "LandingPredictionConfig"
    
    // Pressure levels and approximate altitudes
    private let pressureLevels: [(hPa: Int, altitudeM: Double)] = [
        (1000, 100), (925, 750), (850, 1500), (700, 3000),
        (600, 4200), (500, 5500), (400, 7200), (300, 9000),
        (250, 10500), (200, 12000), (150, 13500), (100, 16000),
        (70, 18500), (50, 20500), (30, 24000)
    ]
    
    private init() {
        if let data = UserDefaults.standard.data(forKey: configKey),
           let saved = try? JSONDecoder().decode(PredictionConfig.self, from: data) {
            config = saved
        } else {
            config = PredictionConfig()
        }
    }
    
    private func saveConfig() {
        if let data = try? JSONEncoder().encode(config) {
            UserDefaults.standard.set(data, forKey: configKey)
        }
    }
    
    // MARK: - Fetch Wind Profile
    
    func fetchWindProfile(latitude: Double, longitude: Double) {
        guard latitude != 0, longitude != 0 else { return }
        
        isLoadingWindProfile = true
        windProfileError = nil
        
        let pressureParams = pressureLevels.map { "wind_speed_\($0.hPa)hPa,wind_direction_\($0.hPa)hPa" }.joined(separator: ",")
        let urlString = "https://api.open-meteo.com/v1/forecast?latitude=\(latitude)&longitude=\(longitude)&current=wind_speed_10m,wind_direction_10m&hourly=\(pressureParams)&wind_speed_unit=ms&forecast_days=1"
        
        guard let url = URL(string: urlString) else {
            windProfileError = "Invalid URL"
            isLoadingWindProfile = false
            return
        }
        
        URLSession.shared.dataTask(with: url) { [weak self] data, response, error in
            DispatchQueue.main.async {
                self?.isLoadingWindProfile = false
                
                if let error = error {
                    self?.windProfileError = error.localizedDescription
                    return
                }
                
                guard let data = data else {
                    self?.windProfileError = "No data"
                    return
                }
                
                self?.parseWindProfile(data: data)
            }
        }.resume()
    }
    
    private func parseWindProfile(data: Data) {
        do {
            guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                windProfileError = "Invalid JSON"
                return
            }
            
            var layers: [LiveWindLayer] = []
            
            // Surface wind
            if let current = json["current"] as? [String: Any],
               let speed = current["wind_speed_10m"] as? Double,
               let dir = current["wind_direction_10m"] as? Double {
                layers.append(LiveWindLayer(speed: speed, direction: dir, altitude: 10))
            }
            
            // Pressure level winds
            if let hourly = json["hourly"] as? [String: Any] {
                for (hPa, altitudeM) in pressureLevels {
                    if let speeds = hourly["wind_speed_\(hPa)hPa"] as? [Double],
                       let dirs = hourly["wind_direction_\(hPa)hPa"] as? [Double],
                       let speed = speeds.first, let dir = dirs.first {
                        layers.append(LiveWindLayer(speed: speed, direction: dir, altitude: altitudeM))
                    }
                }
            }
            
            if layers.isEmpty {
                windProfileError = "No wind data"
                return
            }
            
            windProfile = LiveWindProfile(layers: layers, fetchTime: Date())
            windProfileError = nil
            
        } catch {
            windProfileError = error.localizedDescription
        }
    }
    
    // MARK: - Get Wind at Altitude
    
    private func getWind(at altitude: Double) -> (speed: Double, direction: Double) {
        // Priority: 1. Wind profile, 2. Auto-calculated, 3. Manual config
        
        if config.useWindProfile, let profile = windProfile, profile.isValid,
           let wind = profile.windAt(altitude: altitude) {
            return wind
        }
        
        if config.useAutoWind {
            return (calculatedWindSpeed, calculatedWindDirection)
        }
        
        return (config.windSpeed, config.windDirection)
    }
    
    // MARK: - Prediction Calculation
    
    func updatePrediction(from telemetry: [TelemetryPoint]) {
        guard telemetry.count >= 3 else {
            currentPrediction = nil
            return
        }
        
        let recent = Array(telemetry.suffix(30))
        guard let latest = recent.last else { return }
        
        // Fetch wind profile if needed (every 30 minutes)
        if config.useWindProfile {
            if windProfile == nil || !windProfile!.isValid {
                fetchWindProfile(latitude: latest.latitude, longitude: latest.longitude)
            }
        }
        
        // Determine flight phase and calculate descent rate
        let (phase, descentRate) = calculatePhaseAndDescentRate(from: recent)
        
        // Calculate wind if auto mode
        if config.useAutoWind {
            calculateWindFromDrift(telemetry: recent)
        }
        
        // Calculate landing prediction
        let prediction: LandingPrediction?
        let usedProfile = config.useWindProfile && windProfile?.isValid == true
        
        switch phase {
        case .ascending:
            prediction = predictFromAscent(
                current: latest,
                ascentRate: abs(descentRate),
                usedWindProfile: usedProfile
            )
            
        case .descending:
            prediction = predictFromDescent(
                current: latest,
                descentRate: abs(descentRate),
                usedWindProfile: usedProfile
            )
            
        case .floating:
            prediction = predictFromFloat(
                current: latest,
                usedWindProfile: usedProfile
            )
            
        case .landed, .prelaunch:
            prediction = nil
        }
        
        if let pred = prediction {
            let finalPred = LandingPrediction(
                predictedLat: pred.predictedLat,
                predictedLon: pred.predictedLon,
                timeToLanding: pred.timeToLanding,
                distanceToLanding: pred.distanceToLanding,
                bearingToLanding: pred.bearingToLanding,
                confidence: pred.confidence,
                descentRate: descentRate,
                phase: phase,
                timestamp: Date(),
                usedWindProfile: usedProfile
            )
            
            currentPrediction = finalPred
            predictionHistory.append(finalPred)
            
            // Keep history manageable
            if predictionHistory.count > 100 {
                predictionHistory.removeFirst(predictionHistory.count - 100)
            }
        } else {
            currentPrediction = nil
        }
    }
    
    // MARK: - Phase Detection
    
    private func calculatePhaseAndDescentRate(from telemetry: [TelemetryPoint]) -> (FlightPhase, Double) {
        guard telemetry.count >= 3 else {
            return (.prelaunch, 0)
        }
        
        var verticalSpeeds: [Double] = []
        for i in 1..<telemetry.count {
            let dt = telemetry[i].timestamp.timeIntervalSince(telemetry[i-1].timestamp)
            guard dt > 0 else { continue }
            let dAlt = telemetry[i].altitude - telemetry[i-1].altitude
            verticalSpeeds.append(dAlt / dt)
        }
        
        guard !verticalSpeeds.isEmpty else {
            return (.prelaunch, 0)
        }
        
        let recentSpeeds = Array(verticalSpeeds.suffix(5))
        let recentAvg = recentSpeeds.reduce(0, +) / Double(recentSpeeds.count)
        let currentAlt = telemetry.last?.altitude ?? 0
        
        if currentAlt < 100 && abs(recentAvg) < 1 {
            return (.prelaunch, 0)
        } else if currentAlt < 50 && recentAvg < 0.5 {
            return (.landed, 0)
        } else if recentAvg > 1.0 {
            return (.ascending, recentAvg)
        } else if recentAvg < -1.0 {
            return (.descending, recentAvg)
        } else {
            return (.floating, recentAvg)
        }
    }
    
    // MARK: - Wind Calculation
    
    private func calculateWindFromDrift(telemetry: [TelemetryPoint]) {
        guard telemetry.count >= 10 else { return }
        
        let recent = Array(telemetry.suffix(20))
        
        var driftX: Double = 0
        var driftY: Double = 0
        var totalTime: Double = 0
        
        for i in 1..<recent.count {
            let dt = recent[i].timestamp.timeIntervalSince(recent[i-1].timestamp)
            guard dt > 0 else { continue }
            
            let lat1 = recent[i-1].latitude * .pi / 180
            let lat2 = recent[i].latitude * .pi / 180
            let lon1 = recent[i-1].longitude * .pi / 180
            let lon2 = recent[i].longitude * .pi / 180
            
            let dLat = (lat2 - lat1) * 6371000
            let dLon = (lon2 - lon1) * 6371000 * cos((lat1 + lat2) / 2)
            
            driftY += dLat
            driftX += dLon
            totalTime += dt
        }
        
        guard totalTime > 0 else { return }
        
        let windSpeedX = driftX / totalTime
        let windSpeedY = driftY / totalTime
        
        calculatedWindSpeed = sqrt(windSpeedX * windSpeedX + windSpeedY * windSpeedY)
        
        var direction = atan2(windSpeedX, windSpeedY) * 180 / .pi
        direction = (direction + 180).truncatingRemainder(dividingBy: 360)
        if direction < 0 { direction += 360 }
        calculatedWindDirection = direction
    }
    
    // MARK: - Prediction from Ascent
    
    private func predictFromAscent(
        current: TelemetryPoint,
        ascentRate: Double,
        usedWindProfile: Bool
    ) -> LandingPrediction? {
        
        let burstAlt = config.burstAltitude
        let targetAlt = config.seaLevelTarget
        
        guard current.altitude < burstAlt else { return nil }
        
        var currentLat = current.latitude
        var currentLon = current.longitude
        var currentAlt = current.altitude
        var time: TimeInterval = 0
        let timeStep: TimeInterval = 30
        
        // Ascent phase
        while currentAlt < burstAlt {
            let wind = getWind(at: currentAlt)
            let driftDir = (wind.direction + 180).truncatingRemainder(dividingBy: 360) * .pi / 180
            
            currentAlt += ascentRate * timeStep
            
            let drift = wind.speed * timeStep
            currentLat += drift * cos(driftDir) / 111320
            currentLon += drift * sin(driftDir) / (111320 * cos(currentLat * .pi / 180))
            
            time += timeStep
            if time > 86400 { break }
        }
        
        // Descent phase
        while currentAlt > targetAlt {
            let wind = getWind(at: currentAlt)
            let driftDir = (wind.direction + 180).truncatingRemainder(dividingBy: 360) * .pi / 180
            
            let altFactor = currentAlt / burstAlt
            let descentRate = config.descentRateAtLanding + (config.descentRateAtBurst - config.descentRateAtLanding) * altFactor
            
            currentAlt -= descentRate * timeStep
            
            let drift = wind.speed * timeStep
            currentLat += drift * cos(driftDir) / 111320
            currentLon += drift * sin(driftDir) / (111320 * cos(currentLat * .pi / 180))
            
            time += timeStep
            if time > 86400 { break }
        }
        
        let distance = haversineDistance(
            lat1: current.latitude, lon1: current.longitude,
            lat2: currentLat, lon2: currentLon
        )
        let bearing = calculateBearing(
            lat1: current.latitude, lon1: current.longitude,
            lat2: currentLat, lon2: currentLon
        )
        
        return LandingPrediction(
            predictedLat: currentLat,
            predictedLon: currentLon,
            timeToLanding: time,
            distanceToLanding: distance,
            bearingToLanding: bearing,
            confidence: .low,
            descentRate: ascentRate,
            phase: .ascending,
            timestamp: Date(),
            usedWindProfile: usedWindProfile
        )
    }
    
    // MARK: - Prediction from Descent
    
    private func predictFromDescent(
        current: TelemetryPoint,
        descentRate: Double,
        usedWindProfile: Bool
    ) -> LandingPrediction? {
        
        let targetAlt = config.seaLevelTarget
        guard current.altitude > targetAlt else { return nil }
        
        let effectiveDescentRate = config.descentRateOverride ?? descentRate
        guard effectiveDescentRate > 0 else { return nil }
        
        var currentLat = current.latitude
        var currentLon = current.longitude
        var currentAlt = current.altitude
        var time: TimeInterval = 0
        let timeStep: TimeInterval = 10
        
        while currentAlt > targetAlt {
            let wind = getWind(at: currentAlt)
            let driftDir = (wind.direction + 180).truncatingRemainder(dividingBy: 360) * .pi / 180
            
            currentAlt -= effectiveDescentRate * timeStep
            
            let drift = wind.speed * timeStep
            currentLat += drift * cos(driftDir) / 111320
            currentLon += drift * sin(driftDir) / (111320 * cos(currentLat * .pi / 180))
            
            time += timeStep
            if time > 86400 { break }
        }
        
        let distance = haversineDistance(
            lat1: current.latitude, lon1: current.longitude,
            lat2: currentLat, lon2: currentLon
        )
        let bearing = calculateBearing(
            lat1: current.latitude, lon1: current.longitude,
            lat2: currentLat, lon2: currentLon
        )
        
        let confidence: PredictionConfidence
        if current.altitude < 1000 {
            confidence = .high
        } else if current.altitude < 5000 {
            confidence = .medium
        } else if current.altitude < 15000 {
            confidence = .low
        } else {
            confidence = .veryLow
        }
        
        return LandingPrediction(
            predictedLat: currentLat,
            predictedLon: currentLon,
            timeToLanding: time,
            distanceToLanding: distance,
            bearingToLanding: bearing,
            confidence: confidence,
            descentRate: effectiveDescentRate,
            phase: .descending,
            timestamp: Date(),
            usedWindProfile: usedWindProfile
        )
    }
    
    // MARK: - Prediction from Float
    
    private func predictFromFloat(
        current: TelemetryPoint,
        usedWindProfile: Bool
    ) -> LandingPrediction? {
        let burstAlt = max(current.altitude, config.burstAltitude)
        
        var currentLat = current.latitude
        var currentLon = current.longitude
        var currentAlt = burstAlt
        var time: TimeInterval = 0
        let timeStep: TimeInterval = 30
        
        while currentAlt > config.seaLevelTarget {
            let wind = getWind(at: currentAlt)
            let driftDir = (wind.direction + 180).truncatingRemainder(dividingBy: 360) * .pi / 180
            
            let altFactor = currentAlt / burstAlt
            let descentRate = config.descentRateAtLanding + (config.descentRateAtBurst - config.descentRateAtLanding) * altFactor
            
            currentAlt -= descentRate * timeStep
            
            let drift = wind.speed * timeStep
            currentLat += drift * cos(driftDir) / 111320
            currentLon += drift * sin(driftDir) / (111320 * cos(currentLat * .pi / 180))
            
            time += timeStep
            if time > 86400 { break }
        }
        
        let distance = haversineDistance(
            lat1: current.latitude, lon1: current.longitude,
            lat2: currentLat, lon2: currentLon
        )
        let bearing = calculateBearing(
            lat1: current.latitude, lon1: current.longitude,
            lat2: currentLat, lon2: currentLon
        )
        
        return LandingPrediction(
            predictedLat: currentLat,
            predictedLon: currentLon,
            timeToLanding: time,
            distanceToLanding: distance,
            bearingToLanding: bearing,
            confidence: .veryLow,
            descentRate: 0,
            phase: .floating,
            timestamp: Date(),
            usedWindProfile: usedWindProfile
        )
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
    
    private func calculateBearing(lat1: Double, lon1: Double, lat2: Double, lon2: Double) -> Double {
        let lat1Rad = lat1 * .pi / 180
        let lat2Rad = lat2 * .pi / 180
        let dLon = (lon2 - lon1) * .pi / 180
        
        let y = sin(dLon) * cos(lat2Rad)
        let x = cos(lat1Rad) * sin(lat2Rad) - sin(lat1Rad) * cos(lat2Rad) * cos(dLon)
        var bearing = atan2(y, x) * 180 / .pi
        bearing = (bearing + 360).truncatingRemainder(dividingBy: 360)
        return bearing
    }
    
    func reset() {
        currentPrediction = nil
        predictionHistory.removeAll()
        calculatedWindSpeed = 0
        calculatedWindDirection = 0
    }
}
