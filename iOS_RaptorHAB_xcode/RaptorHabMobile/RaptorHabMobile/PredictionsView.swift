//
//  PredictionsView.swift
//  RaptorHabMobile
//
//  Landing predictions view with wind data and flight planning
//

import SwiftUI
import MapKit

// MARK: - Wind Data Models

struct WindLayer: Codable, Identifiable {
    var id: Double { altitude }
    var speed: Double
    var direction: Double
    var altitude: Double
    var pressureLevel: Int
}

struct WindProfile: Codable {
    var layers: [WindLayer]
    var fetchTime: Date
    var source: WindSource
    
    enum WindSource: String, Codable {
        case manual = "Manual"
        case api = "Weather API"
    }
    
    func windAt(altitude: Double) -> (speed: Double, direction: Double) {
        guard !layers.isEmpty else { return (5, 270) }
        let sorted = layers.sorted { $0.altitude < $1.altitude }
        
        if altitude <= sorted.first!.altitude { return (sorted.first!.speed, sorted.first!.direction) }
        if altitude >= sorted.last!.altitude { return (sorted.last!.speed, sorted.last!.direction) }
        
        for i in 0..<(sorted.count - 1) {
            let lower = sorted[i]
            let upper = sorted[i + 1]
            if altitude >= lower.altitude && altitude <= upper.altitude {
                let factor = (altitude - lower.altitude) / (upper.altitude - lower.altitude)
                let speed = lower.speed + (upper.speed - lower.speed) * factor
                var dirDiff = upper.direction - lower.direction
                if dirDiff > 180 { dirDiff -= 360 }
                if dirDiff < -180 { dirDiff += 360 }
                var direction = lower.direction + dirDiff * factor
                if direction < 0 { direction += 360 }
                if direction >= 360 { direction -= 360 }
                return (speed, direction)
            }
        }
        return (sorted.first!.speed, sorted.first!.direction)
    }
}

// MARK: - Balloon Types

enum BalloonType: String, CaseIterable, Codable, Identifiable {
    case hwoyee600 = "Hwoyee 600g"
    case hwoyee1000 = "Hwoyee 1000g"
    case hwoyee1200 = "Hwoyee 1200g"
    case hwoyee1600 = "Hwoyee 1600g"
    case totex1000 = "Totex 1000g"
    case custom = "Custom"
    
    var id: String { rawValue }
    var burstDiameter: Double {
        switch self {
        case .hwoyee600: return 6.0
        case .hwoyee1000, .totex1000: return 7.86
        case .hwoyee1200: return 8.63
        case .hwoyee1600: return 9.44
        case .custom: return 7.0
        }
    }
    var mass: Double {
        switch self {
        case .hwoyee600: return 0.6
        case .hwoyee1000, .totex1000: return 1.0
        case .hwoyee1200: return 1.2
        case .hwoyee1600: return 1.6
        case .custom: return 1.0
        }
    }
}

// MARK: - Gas Types

enum GasType: String, CaseIterable, Codable, Identifiable {
    case helium = "Helium"
    case hydrogen = "Hydrogen"
    
    var id: String { rawValue }
    
    var liftPerCubicMeter: Double {
        switch self {
        case .helium: return 1.05
        case .hydrogen: return 1.10
        }
    }
    
    var density: Double {
        switch self {
        case .helium: return 0.1664
        case .hydrogen: return 0.0838
        }
    }
}

// MARK: - Balloon Parameters

struct BalloonParameters {
    var balloonType: BalloonType = .hwoyee1000
    var customBurstDiameter: Double = 7.0
    var customBalloonMass: Double = 1.0
    var payloadMass: Double = 1.0
    var neckLift: Double = 1.5
    var gasType: GasType = .helium
    
    var totalMass: Double {
        let balloonMass = balloonType == .custom ? customBalloonMass : balloonType.mass
        return payloadMass + balloonMass
    }
    
    var freeLift: Double {
        return neckLift - totalMass
    }
    
    var burstDiameter: Double {
        balloonType == .custom ? customBurstDiameter : balloonType.burstDiameter
    }
    
    func calculateBurstAltitude() -> Double {
        let burstRadius = burstDiameter / 2.0
        let burstVolume = (4.0 / 3.0) * .pi * pow(burstRadius, 3)
        let airDensitySL = 1.225
        let gasDensity = gasType.density
        let densityDiff = airDensitySL - gasDensity
        let initialVolume = neckLift / densityDiff
        let pressureRatio = initialVolume / burstVolume
        let scaleHeight = 8500.0
        let burstAltitude = -scaleHeight * log(pressureRatio)
        return max(5000, min(45000, burstAltitude))
    }
    
    func calculateAscentRate() -> Double {
        let g = 9.81
        let rho = 1.225
        let balloonRadius = burstDiameter / 4.0
        let area = Double.pi * pow(balloonRadius, 2)
        let cd = 0.3
        guard freeLift > 0, area > 0 else { return 5.0 }
        let ascentRate = sqrt(2 * g * freeLift / (rho * cd * area))
        return min(8.0, max(2.0, ascentRate))
    }
}

// MARK: - Prediction Path

struct PredictionPath: Identifiable {
    let id = UUID()
    let points: [PredictionPathPoint]
    let landingCoordinate: CLLocationCoordinate2D
    let timeToLanding: TimeInterval
    let distanceFromLaunch: Double
}

struct PredictionPathPoint: Identifiable {
    let id = UUID()
    let coordinate: CLLocationCoordinate2D
    let altitude: Double
    let time: TimeInterval
    let phase: FlightPhase
}

// MARK: - Predictions View

struct PredictionsView: View {
    @EnvironmentObject var groundStation: GroundStationManager
    @EnvironmentObject var locationManager: LocationManager
    @ObservedObject var predictionManager = LandingPredictionManager.shared
    
    @State private var launchLat: Double = 0
    @State private var launchLon: Double = 0
    @State private var launchAlt: Double = 0
    @State private var burstAltitude: Double = 30000
    @State private var ascentRate: Double = 5.0
    @State private var descentRateHigh: Double = 30.0
    @State private var descentRateLow: Double = 5.0
    
    // Balloon Calculator
    @State private var useBalloonCalculator = false
    @State private var balloonParams = BalloonParameters()
    
    @State private var windSource: WindProfile.WindSource = .api
    @State private var manualWindSpeed: Double = 5.0
    @State private var manualWindDirection: Double = 270
    
    @State private var isLoadingWind = false
    @State private var windError: String?
    @State private var windProfile = WindProfile(layers: [], fetchTime: Date(), source: .manual)
    @State private var prediction: PredictionPath?
    @State private var showWindProfile = false
    
    private let pressureLevels: [(hPa: Int, altitudeM: Double)] = [
        (1000, 100), (925, 750), (850, 1500), (700, 3000),
        (600, 4200), (500, 5500), (400, 7200), (300, 9000),
        (250, 10500), (200, 12000), (150, 13500), (100, 16000)
    ]
    
    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                // Live Prediction
                if let livePred = predictionManager.currentPrediction {
                    LivePredictionCard(prediction: livePred)
                }
                
                // Launch Site
                GroupBox {
                    VStack(spacing: 12) {
                        HStack {
                            Text("Latitude")
                            Spacer()
                            TextField("Lat", value: $launchLat, format: .number.precision(.fractionLength(6)))
                                .textFieldStyle(.roundedBorder)
                                .keyboardType(.decimalPad)
                                .frame(width: 140)
                        }
                        HStack {
                            Text("Longitude")
                            Spacer()
                            TextField("Lon", value: $launchLon, format: .number.precision(.fractionLength(6)))
                                .textFieldStyle(.roundedBorder)
                                .keyboardType(.decimalPad)
                                .frame(width: 140)
                        }
                        HStack {
                            Text("Altitude")
                            Spacer()
                            TextField("m", value: $launchAlt, format: .number)
                                .textFieldStyle(.roundedBorder)
                                .keyboardType(.decimalPad)
                                .frame(width: 100)
                            Text("m").foregroundColor(.secondary)
                        }
                        Button("Use Current Location") {
                            if let loc = locationManager.currentLocation {
                                launchLat = loc.coordinate.latitude
                                launchLon = loc.coordinate.longitude
                                launchAlt = loc.altitude
                            }
                        }
                        .disabled(locationManager.currentLocation == nil)
                    }
                } label: {
                    Label("Launch Site", systemImage: "mappin.and.ellipse")
                }
                
                // Wind Data
                GroupBox {
                    VStack(spacing: 12) {
                        Picker("Source", selection: $windSource) {
                            Text("Manual").tag(WindProfile.WindSource.manual)
                            Text("Weather API").tag(WindProfile.WindSource.api)
                        }
                        .pickerStyle(.segmented)
                        
                        if windSource == .manual {
                            HStack {
                                Text("Wind Speed")
                                Spacer()
                                TextField("m/s", value: $manualWindSpeed, format: .number)
                                    .textFieldStyle(.roundedBorder)
                                    .keyboardType(.decimalPad)
                                    .frame(width: 80)
                                Text("m/s").foregroundColor(.secondary)
                            }
                            HStack {
                                Text("Direction FROM")
                                Spacer()
                                TextField("°", value: $manualWindDirection, format: .number)
                                    .textFieldStyle(.roundedBorder)
                                    .keyboardType(.decimalPad)
                                    .frame(width: 80)
                                Text("°").foregroundColor(.secondary)
                            }
                        } else {
                            HStack {
                                if isLoadingWind {
                                    ProgressView().scaleEffect(0.8)
                                    Text("Loading...").foregroundColor(.secondary)
                                } else if let error = windError {
                                    Image(systemName: "exclamationmark.triangle").foregroundColor(.orange)
                                    Text(error).font(.caption).foregroundColor(.orange)
                                } else if !windProfile.layers.isEmpty {
                                    Image(systemName: "checkmark.circle").foregroundColor(.green)
                                    Text("\(windProfile.layers.count) layers")
                                }
                                Spacer()
                                Button("Fetch") { fetchWindData() }
                                    .disabled(launchLat == 0 && launchLon == 0)
                            }
                            if !windProfile.layers.isEmpty {
                                Button("Show Wind Profile") { showWindProfile = true }
                                    .font(.caption)
                            }
                        }
                    }
                } label: {
                    Label("Wind Data", systemImage: "wind")
                }
                
                // Flight Parameters
                GroupBox {
                    VStack(spacing: 12) {
                        HStack {
                            Text("Burst Altitude")
                            Spacer()
                            TextField("m", value: $burstAltitude, format: .number)
                                .textFieldStyle(.roundedBorder)
                                .keyboardType(.decimalPad)
                                .frame(width: 100)
                            Text("m").foregroundColor(.secondary)
                        }
                        HStack {
                            Text("Ascent Rate")
                            Spacer()
                            TextField("m/s", value: $ascentRate, format: .number.precision(.fractionLength(1)))
                                .textFieldStyle(.roundedBorder)
                                .keyboardType(.decimalPad)
                                .frame(width: 80)
                            Text("m/s").foregroundColor(.secondary)
                        }
                        HStack {
                            Text("Descent (burst)")
                            Spacer()
                            TextField("m/s", value: $descentRateHigh, format: .number)
                                .textFieldStyle(.roundedBorder)
                                .keyboardType(.decimalPad)
                                .frame(width: 80)
                            Text("m/s").foregroundColor(.secondary)
                        }
                        HStack {
                            Text("Descent (landing)")
                            Spacer()
                            TextField("m/s", value: $descentRateLow, format: .number)
                                .textFieldStyle(.roundedBorder)
                                .keyboardType(.decimalPad)
                                .frame(width: 80)
                            Text("m/s").foregroundColor(.secondary)
                        }
                    }
                } label: {
                    Label("Flight Parameters", systemImage: "arrow.up.arrow.down")
                }
                
                // Balloon Calculator
                GroupBox {
                    VStack(spacing: 12) {
                        Toggle("Use Balloon Calculator", isOn: $useBalloonCalculator)
                        
                        if useBalloonCalculator {
                            Picker("Balloon", selection: $balloonParams.balloonType) {
                                ForEach(BalloonType.allCases) { type in
                                    Text(type.rawValue).tag(type)
                                }
                            }
                            
                            if balloonParams.balloonType == .custom {
                                HStack {
                                    Text("Burst Diameter")
                                    Spacer()
                                    TextField("m", value: $balloonParams.customBurstDiameter, format: .number.precision(.fractionLength(2)))
                                        .textFieldStyle(.roundedBorder)
                                        .keyboardType(.decimalPad)
                                        .frame(width: 80)
                                    Text("m").foregroundColor(.secondary)
                                }
                                HStack {
                                    Text("Balloon Mass")
                                    Spacer()
                                    TextField("kg", value: $balloonParams.customBalloonMass, format: .number.precision(.fractionLength(2)))
                                        .textFieldStyle(.roundedBorder)
                                        .keyboardType(.decimalPad)
                                        .frame(width: 80)
                                    Text("kg").foregroundColor(.secondary)
                                }
                            }
                            
                            Picker("Gas", selection: $balloonParams.gasType) {
                                ForEach(GasType.allCases) { gas in
                                    Text(gas.rawValue).tag(gas)
                                }
                            }
                            .pickerStyle(.segmented)
                            
                            HStack {
                                Text("Payload Mass")
                                Spacer()
                                TextField("kg", value: $balloonParams.payloadMass, format: .number.precision(.fractionLength(2)))
                                    .textFieldStyle(.roundedBorder)
                                    .keyboardType(.decimalPad)
                                    .frame(width: 80)
                                Text("kg").foregroundColor(.secondary)
                            }
                            
                            HStack {
                                Text("Neck Lift")
                                Spacer()
                                TextField("kg", value: $balloonParams.neckLift, format: .number.precision(.fractionLength(2)))
                                    .textFieldStyle(.roundedBorder)
                                    .keyboardType(.decimalPad)
                                    .frame(width: 80)
                                Text("kg").foregroundColor(.secondary)
                            }
                            
                            Divider()
                            
                            // Calculated values
                            HStack {
                                Text("Free Lift")
                                Spacer()
                                Text(String(format: "%.2f kg", balloonParams.freeLift))
                                    .foregroundColor(balloonParams.freeLift > 0 ? .green : .red)
                            }
                            
                            HStack {
                                Text("Calculated Burst Alt")
                                Spacer()
                                Text(String(format: "%.0f m", balloonParams.calculateBurstAltitude()))
                                    .fontWeight(.semibold)
                            }
                            
                            HStack {
                                Text("Est. Ascent Rate")
                                Spacer()
                                Text(String(format: "%.1f m/s", balloonParams.calculateAscentRate()))
                                    .fontWeight(.semibold)
                            }
                            
                            Button("Apply to Flight Parameters") {
                                burstAltitude = balloonParams.calculateBurstAltitude()
                                ascentRate = balloonParams.calculateAscentRate()
                            }
                            .buttonStyle(.bordered)
                            
                            Text("Burst calculation is an estimate. Actual burst altitude varies with balloon batch, fill technique, and conditions.")
                                .font(.caption2)
                                .foregroundColor(.secondary)
                        }
                    }
                } label: {
                    Label("Balloon Calculator", systemImage: "circle.circle")
                }
                
                // Calculate Button
                Button {
                    calculatePrediction()
                } label: {
                    HStack {
                        Image(systemName: "location.magnifyingglass")
                        Text("Calculate Prediction")
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .disabled(launchLat == 0 && launchLon == 0)
                
                // Result
                if let pred = prediction {
                    PredictionResultCard(prediction: pred, launchLat: launchLat, launchLon: launchLon)
                }
            }
            .padding()
        }
        .navigationTitle("Predictions")
        .navigationBarTitleDisplayMode(.inline)
        .sheet(isPresented: $showWindProfile) {
            WindProfileSheet(profile: windProfile)
        }
    }
    
    private func fetchWindData() {
        guard launchLat != 0 || launchLon != 0 else { return }
        isLoadingWind = true
        windError = nil
        
        let pressureParams = pressureLevels.map { "wind_speed_\($0.hPa)hPa,wind_direction_\($0.hPa)hPa" }.joined(separator: ",")
        let urlString = "https://api.open-meteo.com/v1/forecast?latitude=\(launchLat)&longitude=\(launchLon)&current=wind_speed_10m,wind_direction_10m&hourly=\(pressureParams)&wind_speed_unit=ms&forecast_days=1"
        
        guard let url = URL(string: urlString) else {
            windError = "Invalid URL"
            isLoadingWind = false
            return
        }
        
        URLSession.shared.dataTask(with: url) { data, response, error in
            DispatchQueue.main.async {
                isLoadingWind = false
                if let error = error { windError = error.localizedDescription; return }
                guard let data = data else { windError = "No data"; return }
                parseWindData(data)
            }
        }.resume()
    }
    
    private func parseWindData(_ data: Data) {
        do {
            guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                windError = "Invalid JSON"; return
            }
            var layers: [WindLayer] = []
            if let current = json["current"] as? [String: Any],
               let speed = current["wind_speed_10m"] as? Double,
               let dir = current["wind_direction_10m"] as? Double {
                layers.append(WindLayer(speed: speed, direction: dir, altitude: 10, pressureLevel: 1013))
            }
            if let hourly = json["hourly"] as? [String: Any] {
                for (hPa, altitudeM) in pressureLevels {
                    if let speeds = hourly["wind_speed_\(hPa)hPa"] as? [Double],
                       let dirs = hourly["wind_direction_\(hPa)hPa"] as? [Double],
                       let speed = speeds.first, let dir = dirs.first {
                        layers.append(WindLayer(speed: speed, direction: dir, altitude: altitudeM, pressureLevel: hPa))
                    }
                }
            }
            if layers.isEmpty { windError = "No wind data"; return }
            layers.sort { $0.altitude < $1.altitude }
            windProfile = WindProfile(layers: layers, fetchTime: Date(), source: .api)
            windError = nil
        } catch {
            windError = error.localizedDescription
        }
    }
    
    private func calculatePrediction() {
        var points: [PredictionPathPoint] = []
        var currentLat = launchLat
        var currentLon = launchLon
        var currentAlt = launchAlt
        var time: TimeInterval = 0
        let timeStep: TimeInterval = 10
        let useProfile = windSource == .api && !windProfile.layers.isEmpty
        
        // Ascent
        while currentAlt < burstAltitude {
            let (windSpeed, windDir) = useProfile ? windProfile.windAt(altitude: currentAlt) : (manualWindSpeed, manualWindDirection)
            let driftDir = (windDir + 180).truncatingRemainder(dividingBy: 360) * .pi / 180
            points.append(PredictionPathPoint(coordinate: CLLocationCoordinate2D(latitude: currentLat, longitude: currentLon), altitude: currentAlt, time: time, phase: .ascending))
            currentAlt += ascentRate * timeStep
            let drift = windSpeed * timeStep
            currentLat += drift * cos(driftDir) / 111320
            currentLon += drift * sin(driftDir) / (111320 * cos(currentLat * .pi / 180))
            time += timeStep
        }
        
        // Descent
        while currentAlt > launchAlt {
            let (windSpeed, windDir) = useProfile ? windProfile.windAt(altitude: currentAlt) : (manualWindSpeed, manualWindDirection)
            let driftDir = (windDir + 180).truncatingRemainder(dividingBy: 360) * .pi / 180
            points.append(PredictionPathPoint(coordinate: CLLocationCoordinate2D(latitude: currentLat, longitude: currentLon), altitude: currentAlt, time: time, phase: .descending))
            let altFactor = currentAlt / burstAltitude
            let descentRate = descentRateLow + (descentRateHigh - descentRateLow) * altFactor
            currentAlt -= descentRate * timeStep
            let drift = windSpeed * timeStep
            currentLat += drift * cos(driftDir) / 111320
            currentLon += drift * sin(driftDir) / (111320 * cos(currentLat * .pi / 180))
            time += timeStep
            if time > 86400 { break }
        }
        
        let distance = haversineDistance(lat1: launchLat, lon1: launchLon, lat2: currentLat, lon2: currentLon)
        prediction = PredictionPath(points: points, landingCoordinate: CLLocationCoordinate2D(latitude: currentLat, longitude: currentLon), timeToLanding: time, distanceFromLaunch: distance)
    }
    
    private func haversineDistance(lat1: Double, lon1: Double, lat2: Double, lon2: Double) -> Double {
        let R = 6371000.0
        let dLat = (lat2 - lat1) * .pi / 180
        let dLon = (lon2 - lon1) * .pi / 180
        let a = sin(dLat/2) * sin(dLat/2) + cos(lat1 * .pi / 180) * cos(lat2 * .pi / 180) * sin(dLon/2) * sin(dLon/2)
        return R * 2 * atan2(sqrt(a), sqrt(1-a))
    }
}

// MARK: - Supporting Views

struct LivePredictionCard: View {
    let prediction: LandingPrediction
    
    var body: some View {
        GroupBox {
            VStack(spacing: 8) {
                HStack {
                    VStack(alignment: .leading) {
                        Text("Phase: \(prediction.phase.rawValue)").font(.headline)
                        Text("Confidence: \(prediction.confidence.rawValue)")
                            .foregroundColor(prediction.confidence == .high ? .green : prediction.confidence == .medium ? .yellow : .orange)
                    }
                    Spacer()
                    VStack(alignment: .trailing) {
                        Text(prediction.distanceToLanding > 1000 ? String(format: "%.1f km", prediction.distanceToLanding/1000) : String(format: "%.0f m", prediction.distanceToLanding)).font(.title2).fontWeight(.bold)
                        Text(String(format: "%.0f min", prediction.timeToLanding/60)).foregroundColor(.secondary)
                    }
                }
                HStack {
                    Label(String(format: "%.5f", prediction.predictedLat), systemImage: "location")
                    Spacer()
                    Label(String(format: "%.5f", prediction.predictedLon), systemImage: "location")
                }.font(.caption)
            }
        } label: {
            Label("Live Prediction", systemImage: "scope")
        }
    }
}

struct PredictionResultCard: View {
    let prediction: PredictionPath
    let launchLat: Double
    let launchLon: Double
    
    var body: some View {
        GroupBox {
            VStack(spacing: 12) {
                Map {
                    Marker("Launch", coordinate: CLLocationCoordinate2D(latitude: launchLat, longitude: launchLon)).tint(.green)
                    Marker("Landing", coordinate: prediction.landingCoordinate).tint(.red)
                    MapPolyline(coordinates: prediction.points.map { $0.coordinate }).stroke(.blue, lineWidth: 2)
                }
                .frame(height: 200)
                .cornerRadius(8)
                
                LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 8) {
                    VStack(alignment: .leading) {
                        Text("Landing Lat").font(.caption).foregroundColor(.secondary)
                        Text(String(format: "%.5f°", prediction.landingCoordinate.latitude)).font(.subheadline).fontWeight(.medium)
                    }
                    VStack(alignment: .leading) {
                        Text("Landing Lon").font(.caption).foregroundColor(.secondary)
                        Text(String(format: "%.5f°", prediction.landingCoordinate.longitude)).font(.subheadline).fontWeight(.medium)
                    }
                    VStack(alignment: .leading) {
                        Text("Distance").font(.caption).foregroundColor(.secondary)
                        Text(prediction.distanceFromLaunch > 1000 ? String(format: "%.1f km", prediction.distanceFromLaunch/1000) : String(format: "%.0f m", prediction.distanceFromLaunch)).font(.subheadline).fontWeight(.medium)
                    }
                    VStack(alignment: .leading) {
                        Text("Flight Time").font(.caption).foregroundColor(.secondary)
                        let h = Int(prediction.timeToLanding) / 3600
                        let m = (Int(prediction.timeToLanding) % 3600) / 60
                        Text(h > 0 ? String(format: "%d:%02d:%02d", h, m, Int(prediction.timeToLanding) % 60) : String(format: "%d:%02d", m, Int(prediction.timeToLanding) % 60)).font(.subheadline).fontWeight(.medium)
                    }
                }
            }
        } label: {
            Label("Predicted Landing", systemImage: "mappin.circle")
        }
    }
}

struct WindProfileSheet: View {
    let profile: WindProfile
    @Environment(\.dismiss) var dismiss
    
    var body: some View {
        NavigationStack {
            List {
                ForEach(profile.layers.sorted { $0.altitude > $1.altitude }) { layer in
                    HStack {
                        VStack(alignment: .leading) {
                            Text(layer.altitude >= 1000 ? String(format: "%.1f km", layer.altitude/1000) : String(format: "%.0f m", layer.altitude)).font(.headline)
                            Text("\(layer.pressureLevel) hPa").font(.caption).foregroundColor(.secondary)
                        }
                        Spacer()
                        VStack(alignment: .trailing) {
                            Text(String(format: "%.1f m/s", layer.speed))
                            Text(String(format: "%.0f°", layer.direction)).foregroundColor(.secondary)
                        }
                        Image(systemName: "arrow.up").rotationEffect(.degrees(layer.direction)).foregroundColor(.blue)
                    }
                }
            }
            .navigationTitle("Wind Profile")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
        }
    }
}
