//
//  LandingPredictionsView.swift
//  RaptorHabGS
//
//  Ground planning landing predictions with manual or live wind data
//

import SwiftUI
import MapKit

// MARK: - Wind Data Model

struct WindLayer: Codable, Identifiable {
    var id: Double { altitude }
    var speed: Double       // m/s
    var direction: Double   // degrees (direction wind is coming FROM)
    var altitude: Double    // meters
    var pressureLevel: Int  // hPa
}

struct WindProfile: Codable {
    var layers: [WindLayer]
    var fetchTime: Date
    var source: WindSource
    
    enum WindSource: String, Codable {
        case manual = "Manual"
        case api = "Weather API"
        case telemetry = "Telemetry"
    }
    
    // Interpolate wind at a given altitude
    func windAt(altitude: Double) -> (speed: Double, direction: Double) {
        guard !layers.isEmpty else { return (5, 270) }
        
        // Sort layers by altitude
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
                
                // Interpolate speed linearly
                let speed = lower.speed + (upper.speed - lower.speed) * factor
                
                // Interpolate direction (handle wrap-around at 360°)
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

// MARK: - Descent Model

struct DescentModel: Codable {
    var ascentRate: Double = 5.0           // m/s
    var burstAltitude: Double = 30000      // meters
    var descentRateAtBurst: Double = 30    // m/s (initial fast descent)
    var descentRateAtGround: Double = 5    // m/s (parachute near ground)
    var useParachute: Bool = true
    var parachuteDragCoeff: Double = 1.5
    var payloadMass: Double = 1.0          // kg
    
    // Calculate descent rate at given altitude (simple linear model)
    func descentRate(at altitude: Double) -> Double {
        guard burstAltitude > 0 else { return descentRateAtGround }
        let factor = altitude / burstAltitude
        return descentRateAtGround + (descentRateAtBurst - descentRateAtGround) * factor
    }
}

// MARK: - Balloon Parameters

enum BalloonType: String, CaseIterable, Codable, Identifiable {
    case hwoyee200 = "Hwoyee 200g"
    case hwoyee350 = "Hwoyee 350g"
    case hwoyee600 = "Hwoyee 600g"
    case hwoyee800 = "Hwoyee 800g"
    case hwoyee1000 = "Hwoyee 1000g"
    case hwoyee1200 = "Hwoyee 1200g"
    case hwoyee1600 = "Hwoyee 1600g"
    case hwoyee2000 = "Hwoyee 2000g"
    case hwoyee3000 = "Hwoyee 3000g"
    case totex100 = "Totex 100g"
    case totex200 = "Totex 200g"
    case totex350 = "Totex 350g"
    case totex600 = "Totex 600g"
    case totex800 = "Totex 800g"
    case totex1000 = "Totex 1000g"
    case totex1200 = "Totex 1200g"
    case totex1500 = "Totex 1500g"
    case totex2000 = "Totex 2000g"
    case totex3000 = "Totex 3000g"
    case custom = "Custom"
    
    var id: String { rawValue }
    
    // Burst diameter in meters (typical values)
    var burstDiameter: Double {
        switch self {
        case .hwoyee200, .totex200: return 3.0
        case .hwoyee350, .totex350: return 4.0
        case .hwoyee600, .totex600: return 6.0
        case .hwoyee800, .totex800: return 7.0
        case .hwoyee1000, .totex1000: return 7.86
        case .hwoyee1200, .totex1200: return 8.63
        case .hwoyee1600, .totex1500: return 9.44
        case .hwoyee2000, .totex2000: return 10.54
        case .hwoyee3000, .totex3000: return 13.0
        case .totex100: return 2.0
        case .custom: return 7.0
        }
    }
    
    // Balloon mass in kg
    var mass: Double {
        switch self {
        case .hwoyee200, .totex200: return 0.2
        case .hwoyee350, .totex350: return 0.35
        case .hwoyee600, .totex600: return 0.6
        case .hwoyee800, .totex800: return 0.8
        case .hwoyee1000, .totex1000: return 1.0
        case .hwoyee1200, .totex1200: return 1.2
        case .hwoyee1600: return 1.6
        case .totex1500: return 1.5
        case .hwoyee2000, .totex2000: return 2.0
        case .hwoyee3000, .totex3000: return 3.0
        case .totex100: return 0.1
        case .custom: return 1.0
        }
    }
    
    // Typical CD*A for descent (drag coefficient * area)
    var dragCoefficientArea: Double {
        switch self {
        case .totex100, .hwoyee200, .totex200: return 0.25
        case .hwoyee350, .totex350: return 0.3
        case .hwoyee600, .totex600: return 0.35
        case .hwoyee800, .totex800: return 0.4
        case .hwoyee1000, .totex1000: return 0.45
        case .hwoyee1200, .totex1200: return 0.5
        case .hwoyee1600, .totex1500: return 0.55
        case .hwoyee2000, .totex2000: return 0.6
        case .hwoyee3000, .totex3000: return 0.7
        case .custom: return 0.45
        }
    }
}

enum GasType: String, CaseIterable, Codable {
    case helium = "Helium"
    case hydrogen = "Hydrogen"
    
    // Lift per cubic meter at sea level (kg/m³)
    var liftPerCubicMeter: Double {
        switch self {
        case .helium: return 1.05    // ~1.05 kg/m³ net lift
        case .hydrogen: return 1.10  // ~1.10 kg/m³ net lift
        }
    }
    
    // Gas density at STP (kg/m³)
    var density: Double {
        switch self {
        case .helium: return 0.1664
        case .hydrogen: return 0.0838
        }
    }
}

struct BalloonParameters: Codable {
    var balloonType: BalloonType = .hwoyee1000
    var customBurstDiameter: Double = 7.0      // meters
    var customBalloonMass: Double = 1.0        // kg
    var payloadMass: Double = 1.0              // kg (includes parachute, payload, etc.)
    var neckLift: Double = 1.5                 // kg (total lift at neck)
    var gasType: GasType = .helium
    var useBalloonCalculation: Bool = false    // Whether to calculate burst from balloon params
    
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
    
    // Calculate burst altitude based on balloon expansion
    // Uses ideal gas law and atmospheric model
    func calculateBurstAltitude() -> Double {
        let burstRadius = burstDiameter / 2.0
        let burstVolume = (4.0 / 3.0) * .pi * pow(burstRadius, 3)
        
        // Calculate initial fill volume at sea level
        // Net lift = (air_density - gas_density) * volume - balloon_mass
        // Rearranging: volume = (net_lift + balloon_mass) / (air_density - gas_density)
        let airDensitySL = 1.225  // kg/m³ at sea level
        let gasDensity = gasType.density
        
        let densityDiff = airDensitySL - gasDensity
        let initialVolume = (neckLift) / densityDiff
        
        // The balloon expands as pressure decreases
        // At burst: P_burst * V_burst = P_0 * V_0 (isothermal approximation)
        // P_burst / P_0 = V_0 / V_burst
        let pressureRatio = initialVolume / burstVolume
        
        // Use barometric formula to find altitude where pressure ratio is achieved
        // P/P0 = exp(-altitude / H) where H ≈ 8500m (scale height)
        let scaleHeight = 8500.0  // meters
        let burstAltitude = -scaleHeight * log(pressureRatio)
        
        // Clamp to reasonable values
        return max(5000, min(45000, burstAltitude))
    }
    
    // Calculate expected ascent rate
    func calculateAscentRate() -> Double {
        // Simplified model: ascent rate based on free lift and drag
        // v = sqrt(2 * g * free_lift / (rho * Cd * A))
        let g = 9.81
        let rho = 1.225  // sea level air density
        let balloonRadius = burstDiameter / 4.0  // Approximate initial radius (half of burst)
        let area = .pi * pow(balloonRadius, 2)
        let cd = 0.3  // Drag coefficient for sphere
        
        guard freeLift > 0, area > 0 else { return 5.0 }
        
        let ascentRate = sqrt(2 * g * freeLift / (rho * cd * area))
        return min(8.0, max(2.0, ascentRate))  // Clamp to reasonable range
    }
}

// MARK: - Prediction Result

struct PredictionPath: Identifiable {
    let id = UUID()
    let points: [PredictionPathPoint]
    let landingCoordinate: CLLocationCoordinate2D
    let timeToLanding: TimeInterval
    let distanceFromLaunch: Double
    let timestamp: Date
}

struct PredictionPathPoint: Identifiable {
    let id = UUID()
    let coordinate: CLLocationCoordinate2D
    let altitude: Double
    let time: TimeInterval  // seconds from start
    let phase: FlightPhase
    let windSpeed: Double
    let windDirection: Double
}

// MARK: - Landing Predictions View

struct LandingPredictionsView: View {
    @StateObject private var viewModel = LandingPredictionsViewModel()
    
    var body: some View {
        HSplitView {
            // Left panel - Configuration
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    // Launch Site
                    GroupBox("Launch Site") {
                        VStack(spacing: 12) {
                            HStack {
                                Text("Latitude")
                                Spacer()
                                TextField("", value: $viewModel.launchLat, format: .number.precision(.fractionLength(6)))
                                    .textFieldStyle(.roundedBorder)
                                    .frame(width: 140)
                            }
                            HStack {
                                Text("Longitude")
                                Spacer()
                                TextField("", value: $viewModel.launchLon, format: .number.precision(.fractionLength(6)))
                                    .textFieldStyle(.roundedBorder)
                                    .frame(width: 140)
                            }
                            HStack {
                                Text("Altitude")
                                Spacer()
                                TextField("", value: $viewModel.launchAlt, format: .number)
                                    .textFieldStyle(.roundedBorder)
                                    .frame(width: 100)
                                Text("m")
                                    .foregroundColor(.secondary)
                            }
                            
                            Button("Use Current GPS") {
                                viewModel.useCurrentGPS()
                            }
                            .disabled(GPSManager.shared.currentPosition == nil)
                        }
                    }
                    
                    // Wind Data
                    GroupBox("Wind Data") {
                        VStack(spacing: 12) {
                            Picker("Source", selection: $viewModel.windSource) {
                                Text("Manual").tag(WindProfile.WindSource.manual)
                                Text("Weather API").tag(WindProfile.WindSource.api)
                            }
                            .pickerStyle(.segmented)
                            
                            if viewModel.windSource == .manual {
                                // Manual wind entry for different altitudes
                                Text("Surface Wind")
                                    .font(.caption)
                                    .foregroundColor(.secondary)
                                HStack {
                                    Text("Speed")
                                    Spacer()
                                    TextField("", value: $viewModel.manualWindSpeed, format: .number)
                                        .textFieldStyle(.roundedBorder)
                                        .frame(width: 80)
                                    Text("m/s")
                                        .foregroundColor(.secondary)
                                }
                                HStack {
                                    Text("Direction")
                                    Spacer()
                                    TextField("", value: $viewModel.manualWindDirection, format: .number)
                                        .textFieldStyle(.roundedBorder)
                                        .frame(width: 80)
                                    Text("° from")
                                        .foregroundColor(.secondary)
                                }
                                
                                Text("Note: Manual mode uses uniform wind. Use API for altitude-varying winds.")
                                    .font(.caption2)
                                    .foregroundColor(.orange)
                            } else {
                                HStack {
                                    if viewModel.isLoadingWeather {
                                        ProgressView()
                                            .scaleEffect(0.7)
                                        Text("Loading wind profile...")
                                            .foregroundColor(.secondary)
                                    } else if let error = viewModel.weatherError {
                                        Image(systemName: "exclamationmark.triangle")
                                            .foregroundColor(.orange)
                                        Text(error)
                                            .font(.caption)
                                            .foregroundColor(.orange)
                                    } else if !viewModel.windProfile.layers.isEmpty {
                                        Image(systemName: "checkmark.circle")
                                            .foregroundColor(.green)
                                        Text("\(viewModel.windProfile.layers.count) altitude layers")
                                    }
                                    Spacer()
                                    Button("Fetch") {
                                        viewModel.fetchWeatherData()
                                    }
                                }
                                
                                // Show wind profile summary
                                if !viewModel.windProfile.layers.isEmpty {
                                    WindProfileSummaryView(profile: viewModel.windProfile)
                                }
                            }
                        }
                    }
                    
                    // Balloon Parameters
                    GroupBox("Balloon Parameters") {
                        VStack(spacing: 12) {
                            Toggle("Calculate burst from balloon", isOn: $viewModel.balloonParams.useBalloonCalculation)
                            
                            if viewModel.balloonParams.useBalloonCalculation {
                                // Balloon type picker
                                HStack {
                                    Text("Balloon")
                                    Spacer()
                                    Picker("", selection: $viewModel.balloonParams.balloonType) {
                                        ForEach(BalloonType.allCases) { type in
                                            Text(type.rawValue).tag(type)
                                        }
                                    }
                                    .frame(width: 150)
                                }
                                
                                // Custom balloon parameters
                                if viewModel.balloonParams.balloonType == .custom {
                                    HStack {
                                        Text("Burst Diameter")
                                        Spacer()
                                        TextField("", value: $viewModel.balloonParams.customBurstDiameter, format: .number.precision(.fractionLength(1)))
                                            .textFieldStyle(.roundedBorder)
                                            .frame(width: 70)
                                        Text("m")
                                            .foregroundColor(.secondary)
                                    }
                                    HStack {
                                        Text("Balloon Mass")
                                        Spacer()
                                        TextField("", value: $viewModel.balloonParams.customBalloonMass, format: .number.precision(.fractionLength(2)))
                                            .textFieldStyle(.roundedBorder)
                                            .frame(width: 70)
                                        Text("kg")
                                            .foregroundColor(.secondary)
                                    }
                                }
                                
                                // Gas type
                                HStack {
                                    Text("Gas")
                                    Spacer()
                                    Picker("", selection: $viewModel.balloonParams.gasType) {
                                        ForEach(GasType.allCases, id: \.self) { gas in
                                            Text(gas.rawValue).tag(gas)
                                        }
                                    }
                                    .frame(width: 120)
                                }
                                
                                // Payload mass
                                HStack {
                                    Text("Payload Mass")
                                    Spacer()
                                    TextField("", value: $viewModel.balloonParams.payloadMass, format: .number.precision(.fractionLength(2)))
                                        .textFieldStyle(.roundedBorder)
                                        .frame(width: 70)
                                    Text("kg")
                                        .foregroundColor(.secondary)
                                }
                                
                                // Neck lift
                                HStack {
                                    Text("Neck Lift")
                                    Spacer()
                                    TextField("", value: $viewModel.balloonParams.neckLift, format: .number.precision(.fractionLength(2)))
                                        .textFieldStyle(.roundedBorder)
                                        .frame(width: 70)
                                    Text("kg")
                                        .foregroundColor(.secondary)
                                }
                                
                                Divider()
                                
                                // Calculated values
                                VStack(alignment: .leading, spacing: 6) {
                                    HStack {
                                        Text("Free Lift:")
                                            .foregroundColor(.secondary)
                                        Spacer()
                                        Text(String(format: "%.2f kg", viewModel.balloonParams.freeLift))
                                            .foregroundColor(viewModel.balloonParams.freeLift > 0 ? .green : .red)
                                    }
                                    .font(.caption)
                                    
                                    HStack {
                                        Text("Est. Ascent Rate:")
                                            .foregroundColor(.secondary)
                                        Spacer()
                                        Text(String(format: "%.1f m/s", viewModel.balloonParams.calculateAscentRate()))
                                    }
                                    .font(.caption)
                                    
                                    HStack {
                                        Text("Est. Burst Altitude:")
                                            .foregroundColor(.secondary)
                                        Spacer()
                                        Text(String(format: "%.0f m (%.0f ft)", 
                                                   viewModel.balloonParams.calculateBurstAltitude(),
                                                   viewModel.balloonParams.calculateBurstAltitude() * 3.28084))
                                            .foregroundColor(.blue)
                                    }
                                    .font(.caption)
                                }
                                .padding(.vertical, 4)
                                
                                Button("Apply to Descent Model") {
                                    viewModel.applyBalloonCalculations()
                                }
                                .buttonStyle(.bordered)
                                
                                Text("Burst calculation is an estimate. Actual burst altitude varies with balloon batch, fill technique, and conditions.")
                                    .font(.caption2)
                                    .foregroundColor(.secondary)
                            }
                        }
                    }
                    
                    // Descent Model
                    GroupBox("Descent Model") {
                        VStack(spacing: 12) {
                            HStack {
                                Text("Ascent Rate")
                                Spacer()
                                TextField("", value: $viewModel.descentModel.ascentRate, format: .number)
                                    .textFieldStyle(.roundedBorder)
                                    .frame(width: 80)
                                Text("m/s")
                                    .foregroundColor(.secondary)
                            }
                            HStack {
                                Text("Burst Altitude")
                                Spacer()
                                TextField("", value: $viewModel.descentModel.burstAltitude, format: .number)
                                    .textFieldStyle(.roundedBorder)
                                    .frame(width: 100)
                                Text("m")
                                    .foregroundColor(.secondary)
                                
                                if viewModel.balloonParams.useBalloonCalculation {
                                    Image(systemName: "balloon.fill")
                                        .foregroundColor(.blue)
                                        .help("Calculated from balloon parameters")
                                }
                            }
                            HStack {
                                Text("Descent @ Burst")
                                Spacer()
                                TextField("", value: $viewModel.descentModel.descentRateAtBurst, format: .number)
                                    .textFieldStyle(.roundedBorder)
                                    .frame(width: 80)
                                Text("m/s")
                                    .foregroundColor(.secondary)
                            }
                            HStack {
                                Text("Descent @ Ground")
                                Spacer()
                                TextField("", value: $viewModel.descentModel.descentRateAtGround, format: .number)
                                    .textFieldStyle(.roundedBorder)
                                    .frame(width: 80)
                                Text("m/s")
                                    .foregroundColor(.secondary)
                            }
                        }
                    }
                    
                    // Calculate Button
                    Button(action: viewModel.calculatePrediction) {
                        HStack {
                            Image(systemName: "location.circle")
                            Text("Calculate Landing")
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
                    
                    // Results
                    if let prediction = viewModel.prediction {
                        GroupBox("Prediction Results") {
                            VStack(alignment: .leading, spacing: 8) {
                                LabeledContent("Landing Location") {
                                    Text(String(format: "%.5f, %.5f",
                                               prediction.landingCoordinate.latitude,
                                               prediction.landingCoordinate.longitude))
                                        .font(.caption.monospacedDigit())
                                }
                                LabeledContent("Flight Time") {
                                    Text(formatTime(prediction.timeToLanding))
                                }
                                LabeledContent("Distance") {
                                    Text(formatDistance(prediction.distanceFromLaunch))
                                }
                                LabeledContent("Calculated") {
                                    Text(prediction.timestamp, style: .time)
                                }
                            }
                        }
                    }
                    
                    Spacer()
                }
                .padding()
            }
            .frame(minWidth: 300, maxWidth: 350)
            
            // Right panel - Map
            PredictionMapView(
                launchLat: viewModel.launchLat,
                launchLon: viewModel.launchLon,
                prediction: viewModel.prediction
            )
        }
    }
    
    private func formatTime(_ seconds: TimeInterval) -> String {
        let hours = Int(seconds) / 3600
        let minutes = (Int(seconds) % 3600) / 60
        if hours > 0 {
            return "\(hours)h \(minutes)m"
        }
        return "\(minutes) min"
    }
    
    private func formatDistance(_ meters: Double) -> String {
        if meters >= 1000 {
            return String(format: "%.1f km", meters / 1000)
        }
        return String(format: "%.0f m", meters)
    }
}

// MARK: - Wind Profile Summary

struct WindProfileSummaryView: View {
    let profile: WindProfile
    
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Wind Profile")
                .font(.caption.bold())
            
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(profile.layers.sorted { $0.altitude < $1.altitude }) { layer in
                        VStack(spacing: 2) {
                            Text(formatAltitude(layer.altitude))
                                .font(.caption2)
                            Image(systemName: "arrow.down")
                                .rotationEffect(.degrees(layer.direction))
                                .font(.caption2)
                            Text(String(format: "%.0f", layer.speed))
                                .font(.caption2.monospacedDigit())
                        }
                        .frame(width: 45)
                        .padding(4)
                        .background(Color.blue.opacity(0.1))
                        .cornerRadius(4)
                    }
                }
            }
            
            if let fetchTime = profile.fetchTime as Date? {
                Text("Updated: \(fetchTime, style: .time)")
                    .font(.caption2)
                    .foregroundColor(.secondary)
            }
        }
    }
    
    private func formatAltitude(_ meters: Double) -> String {
        if meters >= 1000 {
            return String(format: "%.0fk", meters / 1000)
        }
        return String(format: "%.0f", meters)
    }
}

// MARK: - Prediction Map View

struct PredictionMapView: View {
    let launchLat: Double
    let launchLon: Double
    let prediction: PredictionPath?
    
    @State private var cameraPosition: MapCameraPosition = .automatic
    
    var body: some View {
        Map(position: $cameraPosition) {
            // Launch marker
            if launchLat != 0 && launchLon != 0 {
                Annotation("Launch", coordinate: CLLocationCoordinate2D(latitude: launchLat, longitude: launchLon)) {
                    ZStack {
                        Circle().fill(.green).frame(width: 28, height: 28)
                        Image(systemName: "arrow.up").foregroundColor(.white).font(.caption)
                    }
                }
            }
            
            if let prediction = prediction {
                // Predicted path
                if prediction.points.count > 1 {
                    // Ascent path (green)
                    let ascentPoints = prediction.points.filter { $0.phase == .ascending }
                    if ascentPoints.count > 1 {
                        MapPolyline(coordinates: ascentPoints.map { $0.coordinate })
                            .stroke(.green, lineWidth: 3)
                    }
                    
                    // Descent path (orange)
                    let descentPoints = prediction.points.filter { $0.phase == .descending }
                    if descentPoints.count > 1 {
                        MapPolyline(coordinates: descentPoints.map { $0.coordinate })
                            .stroke(.orange, lineWidth: 3)
                    }
                }
                
                // Burst marker
                if let burstPoint = prediction.points.first(where: { $0.phase == .descending }) {
                    Annotation("Burst", coordinate: burstPoint.coordinate) {
                        ZStack {
                            Circle().fill(.orange).frame(width: 24, height: 24)
                            Image(systemName: "burst").foregroundColor(.white).font(.caption2)
                        }
                    }
                }
                
                // Landing zone circle
                MapCircle(center: prediction.landingCoordinate, radius: 500)
                    .foregroundStyle(.red.opacity(0.2))
                    .stroke(.red, lineWidth: 2)
                
                // Landing marker
                Annotation("Landing", coordinate: prediction.landingCoordinate) {
                    ZStack {
                        Circle().fill(.red).frame(width: 28, height: 28)
                        Image(systemName: "mappin").foregroundColor(.white).font(.caption)
                    }
                }
            }
        }
        .mapStyle(.hybrid)
    }
}

// MARK: - View Model

class LandingPredictionsViewModel: ObservableObject {
    // Launch site
    @Published var launchLat: Double = 0
    @Published var launchLon: Double = 0
    @Published var launchAlt: Double = 0
    
    // Wind
    @Published var windSource: WindProfile.WindSource = .api
    @Published var manualWindSpeed: Double = 5
    @Published var manualWindDirection: Double = 270
    @Published var windProfile = WindProfile(layers: [], fetchTime: Date(), source: .manual)
    @Published var isLoadingWeather = false
    @Published var weatherError: String?
    
    // Balloon parameters
    @Published var balloonParams = BalloonParameters()
    
    // Descent model
    @Published var descentModel = DescentModel()
    
    // Result
    @Published var prediction: PredictionPath?
    
    // Pressure levels and approximate altitudes (meters)
    private let pressureLevels: [(hPa: Int, altitudeM: Double)] = [
        (1000, 100),
        (925, 750),
        (850, 1500),
        (700, 3000),
        (600, 4200),
        (500, 5500),
        (400, 7200),
        (300, 9000),
        (250, 10500),
        (200, 12000),
        (150, 13500),
        (100, 16000),
        (70, 18500),
        (50, 20500),
        (30, 24000),
        (20, 26500),
        (10, 31000)
    ]
    
    init() {
        loadSettings()
    }
    
    func useCurrentGPS() {
        if let pos = GPSManager.shared.currentPosition, pos.isValid {
            launchLat = pos.latitude
            launchLon = pos.longitude
            launchAlt = pos.altitude
        }
    }
    
    func applyBalloonCalculations() {
        if balloonParams.useBalloonCalculation {
            descentModel.burstAltitude = balloonParams.calculateBurstAltitude()
            descentModel.ascentRate = balloonParams.calculateAscentRate()
        }
    }
    
    func fetchWeatherData() {
        guard launchLat != 0, launchLon != 0 else {
            weatherError = "Set launch location first"
            return
        }
        
        isLoadingWeather = true
        weatherError = nil
        
        // Build pressure level parameters for Open-Meteo
        let pressureLevelParams = pressureLevels.map { "wind_speed_\($0.hPa)hPa,wind_direction_\($0.hPa)hPa" }.joined(separator: ",")
        
        let urlString = "https://api.open-meteo.com/v1/forecast?latitude=\(launchLat)&longitude=\(launchLon)&current=wind_speed_10m,wind_direction_10m&hourly=\(pressureLevelParams)&wind_speed_unit=ms&forecast_days=1"
        
        guard let url = URL(string: urlString) else {
            weatherError = "Invalid URL"
            isLoadingWeather = false
            return
        }
        
        URLSession.shared.dataTask(with: url) { [weak self] data, response, error in
            DispatchQueue.main.async {
                self?.isLoadingWeather = false
                
                if let error = error {
                    self?.weatherError = error.localizedDescription
                    return
                }
                
                guard let data = data else {
                    self?.weatherError = "No data received"
                    return
                }
                
                self?.parseWindProfile(data: data)
            }
        }.resume()
    }
    
    private func parseWindProfile(data: Data) {
        do {
            guard let json = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                weatherError = "Invalid JSON"
                return
            }
            
            var layers: [WindLayer] = []
            
            // Get surface wind from current data
            if let current = json["current"] as? [String: Any],
               let surfaceSpeed = current["wind_speed_10m"] as? Double,
               let surfaceDir = current["wind_direction_10m"] as? Double {
                layers.append(WindLayer(
                    speed: surfaceSpeed,
                    direction: surfaceDir,
                    altitude: 10,
                    pressureLevel: 1013
                ))
            }
            
            // Get hourly pressure level data (use first hour / current conditions)
            if let hourly = json["hourly"] as? [String: Any] {
                for (hPa, altitudeM) in pressureLevels {
                    let speedKey = "wind_speed_\(hPa)hPa"
                    let dirKey = "wind_direction_\(hPa)hPa"
                    
                    if let speeds = hourly[speedKey] as? [Double],
                       let directions = hourly[dirKey] as? [Double],
                       let speed = speeds.first,
                       let direction = directions.first {
                        layers.append(WindLayer(
                            speed: speed,
                            direction: direction,
                            altitude: altitudeM,
                            pressureLevel: hPa
                        ))
                    }
                }
            }
            
            if layers.isEmpty {
                weatherError = "No wind data in response"
                return
            }
            
            // Sort by altitude
            layers.sort { $0.altitude < $1.altitude }
            
            windProfile = WindProfile(
                layers: layers,
                fetchTime: Date(),
                source: .api
            )
            
            weatherError = nil
            
        } catch {
            weatherError = "Parse error: \(error.localizedDescription)"
        }
    }
    
    func calculatePrediction() {
        guard launchLat != 0, launchLon != 0 else { return }
        
        // Apply balloon calculations if enabled
        if balloonParams.useBalloonCalculation {
            applyBalloonCalculations()
        }
        
        var points: [PredictionPathPoint] = []
        var currentLat = launchLat
        var currentLon = launchLon
        var currentAlt = launchAlt
        var time: TimeInterval = 0
        let timeStep: TimeInterval = 10  // seconds
        
        // Use manual wind or profile
        let useProfile = windSource == .api && !windProfile.layers.isEmpty
        
        // Ascent phase
        while currentAlt < descentModel.burstAltitude {
            // Get wind at current altitude
            let (windSpeed, windDir) = useProfile ?
                windProfile.windAt(altitude: currentAlt) :
                (manualWindSpeed, manualWindDirection)
            
            // Wind direction is where wind is coming FROM, drift is opposite
            let driftDirection = (windDir + 180).truncatingRemainder(dividingBy: 360)
            let driftDirectionRad = driftDirection * .pi / 180
            
            points.append(PredictionPathPoint(
                coordinate: CLLocationCoordinate2D(latitude: currentLat, longitude: currentLon),
                altitude: currentAlt,
                time: time,
                phase: .ascending,
                windSpeed: windSpeed,
                windDirection: windDir
            ))
            
            // Ascend
            currentAlt += descentModel.ascentRate * timeStep
            
            // Wind drift
            let drift = windSpeed * timeStep
            let dLat = drift * cos(driftDirectionRad) / 111320
            let dLon = drift * sin(driftDirectionRad) / (111320 * cos(currentLat * .pi / 180))
            currentLat += dLat
            currentLon += dLon
            
            time += timeStep
        }
        
        // Descent phase
        while currentAlt > launchAlt {
            // Get wind at current altitude
            let (windSpeed, windDir) = useProfile ?
                windProfile.windAt(altitude: currentAlt) :
                (manualWindSpeed, manualWindDirection)
            
            let driftDirection = (windDir + 180).truncatingRemainder(dividingBy: 360)
            let driftDirectionRad = driftDirection * .pi / 180
            
            points.append(PredictionPathPoint(
                coordinate: CLLocationCoordinate2D(latitude: currentLat, longitude: currentLon),
                altitude: currentAlt,
                time: time,
                phase: .descending,
                windSpeed: windSpeed,
                windDirection: windDir
            ))
            
            // Descend
            let descentRate = descentModel.descentRate(at: currentAlt)
            currentAlt -= descentRate * timeStep
            
            // Wind drift
            let drift = windSpeed * timeStep
            let dLat = drift * cos(driftDirectionRad) / 111320
            let dLon = drift * sin(driftDirectionRad) / (111320 * cos(currentLat * .pi / 180))
            currentLat += dLat
            currentLon += dLon
            
            time += timeStep
            
            // Prevent infinite loop
            if time > 86400 { break }
        }
        
        // Final landing point
        let landingCoord = CLLocationCoordinate2D(latitude: currentLat, longitude: currentLon)
        
        // Calculate distance from launch
        let distance = haversineDistance(
            lat1: launchLat, lon1: launchLon,
            lat2: currentLat, lon2: currentLon
        )
        
        prediction = PredictionPath(
            points: points,
            landingCoordinate: landingCoord,
            timeToLanding: time,
            distanceFromLaunch: distance,
            timestamp: Date()
        )
        
        // Save settings
        saveSettings()
    }
    
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
    
    private func saveSettings() {
        UserDefaults.standard.set(launchLat, forKey: "PredictionLaunchLat")
        UserDefaults.standard.set(launchLon, forKey: "PredictionLaunchLon")
        UserDefaults.standard.set(launchAlt, forKey: "PredictionLaunchAlt")
        UserDefaults.standard.set(manualWindSpeed, forKey: "PredictionWindSpeed")
        UserDefaults.standard.set(manualWindDirection, forKey: "PredictionWindDirection")
        
        if let data = try? JSONEncoder().encode(descentModel) {
            UserDefaults.standard.set(data, forKey: "PredictionDescentModel")
        }
        
        if let data = try? JSONEncoder().encode(balloonParams) {
            UserDefaults.standard.set(data, forKey: "PredictionBalloonParams")
        }
    }
    
    private func loadSettings() {
        launchLat = UserDefaults.standard.double(forKey: "PredictionLaunchLat")
        launchLon = UserDefaults.standard.double(forKey: "PredictionLaunchLon")
        launchAlt = UserDefaults.standard.double(forKey: "PredictionLaunchAlt")
        manualWindSpeed = UserDefaults.standard.double(forKey: "PredictionWindSpeed")
        if manualWindSpeed == 0 { manualWindSpeed = 5 }
        manualWindDirection = UserDefaults.standard.double(forKey: "PredictionWindDirection")
        if manualWindDirection == 0 { manualWindDirection = 270 }
        
        if let data = UserDefaults.standard.data(forKey: "PredictionDescentModel"),
           let model = try? JSONDecoder().decode(DescentModel.self, from: data) {
            descentModel = model
        }
        
        if let data = UserDefaults.standard.data(forKey: "PredictionBalloonParams"),
           let params = try? JSONDecoder().decode(BalloonParameters.self, from: data) {
            balloonParams = params
        }
    }
}

#Preview {
    LandingPredictionsView()
}
