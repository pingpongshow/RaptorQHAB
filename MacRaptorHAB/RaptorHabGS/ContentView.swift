//
//  ContentView.swift
//  RaptorHabGS
//
//  Main content view with telemetry display, map, and controls
//

import SwiftUI
import MapKit
import UniformTypeIdentifiers

struct ContentView: View {
    @EnvironmentObject var groundStation: GroundStationManager
    @ObservedObject var missionManager = MissionManager.shared
    @State private var selectedTab = 0
    
    var body: some View {
        NavigationSplitView {
            // Sidebar
            SidebarView()
                .frame(minWidth: 220, idealWidth: 260, maxWidth: 300)
        } detail: {
            // Main content
            TabView(selection: $selectedTab) {
                TelemetryView()
                    .tabItem {
                        Label("Telemetry", systemImage: "chart.xyaxis.line")
                    }
                    .tag(0)
                
                MapDisplayView()
                    .tabItem {
                        Label("Map", systemImage: "map")
                    }
                    .tag(1)
                
                FlightGraphsView()
                    .tabItem {
                        Label("Graphs", systemImage: "chart.line.uptrend.xyaxis")
                    }
                    .tag(2)
                
                LandingPredictionsView()
                    .tabItem {
                        Label("Predictions", systemImage: "location.magnifyingglass")
                    }
                    .tag(3)
                
                ImagesView()
                    .tabItem {
                        Label("Images", systemImage: "photo.stack")
                    }
                    .tag(4)
                
                MissionsView()
                    .tabItem {
                        Label("Missions", systemImage: "folder.badge.gearshape")
                    }
                    .tag(5)
                
                PacketLogView()
                    .tabItem {
                        Label("Packets", systemImage: "doc.text")
                    }
                    .tag(6)

                MeshtasticView()
                    .tabItem {
                        Label("Meshtastic", systemImage: "point.3.connected.trianglepath.dotted")
                    }
                    .tag(7)

                PayloadConfigView()
                    .tabItem {
                        Label("Config", systemImage: "slider.horizontal.3")
                    }
                    .tag(8)

                PayloadConsoleView()
                    .tabItem {
                        Label("Console", systemImage: "terminal")
                    }
                    .tag(9)
            }

                CardView()
                    .tabItem {
                        Label("SD Card", systemImage: "sdcard")
                    }
                    .tag(10)
        }
        .navigationSplitViewColumnWidth(min: 220, ideal: 260, max: 300)
        .navigationTitle("RaptorHab Ground Station")
        .toolbar {
            ToolbarItemGroup(placement: .navigation) {
                // Start/Stop button
                Button {
                    if groundStation.isReceiving {
                        groundStation.stopReceiving()
                        // Auto-save any recording that's in progress
                        if missionManager.isRecording {
                            missionManager.stopRecording()
                        }
                    } else {
                        groundStation.startReceiving()
                    }
                } label: {
                    Label(
                        groundStation.isReceiving ? "Stop" : "Start",
                        systemImage: groundStation.isReceiving ? "stop.fill" : "play.fill"
                    )
                }
                .tint(groundStation.isReceiving ? .red : .green)

                // Settings
                Button {
                    groundStation.showRadioConfig = true
                } label: {
                    Label("Settings", systemImage: "gear")
                }

                Divider()

                // Signal strength indicator - mode aware
                if groundStation.inputMode == .rtlsdr {
                    SignalIndicator(strength: groundStation.signalStrength)
                } else if groundStation.isSerialConnected {
                    // Show RSSI for serial mode
                    HStack(spacing: 4) {
                        Image(systemName: rssiIcon(groundStation.serialRSSI))
                            .foregroundColor(rssiColor(groundStation.serialRSSI))
                        Text(String(format: "%.0f dBm", groundStation.serialRSSI))
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                }
            }
        }
        .sheet(isPresented: $groundStation.showRadioConfig) {
            RadioConfigView()
        }
        .alert("Error", isPresented: .constant(groundStation.errorMessage != nil)) {
            Button("OK") {
                groundStation.errorMessage = nil
            }
        } message: {
            if let error = groundStation.errorMessage {
                Text(error)
            }
        }
    }
    
    // MARK: - RSSI Helpers
    
    private func rssiIcon(_ rssi: Float) -> String {
        if rssi > -70 {
            return "antenna.radiowaves.left.and.right"
        } else if rssi > -90 {
            return "antenna.radiowaves.left.and.right"
        } else if rssi > -110 {
            return "antenna.radiowaves.left.and.right"
        } else {
            return "antenna.radiowaves.left.and.right.slash"
        }
    }
    
    private func rssiColor(_ rssi: Float) -> Color {
        if rssi > -70 {
            return .green
        } else if rssi > -90 {
            return .yellow
        } else if rssi > -110 {
            return .orange
        } else {
            return .red
        }
    }
}

// MARK: - Sidebar

struct SidebarView: View {
    @EnvironmentObject var groundStation: GroundStationManager
    @ObservedObject var sondeHub = SondeHubManager.shared
    @ObservedObject var alertManager = AudioAlertManager.shared
    @ObservedObject var missionManager = MissionManager.shared
    @ObservedObject var landingPredictor = LandingPredictionManager.shared
    @ObservedObject var rotator = RotatorManager.shared
    
    // Throttle expensive operations to max once per second
    static var lastThrottledUpdate: Date = .distantPast
    
    var body: some View {
        List {
            Section("Input Mode") {
                Picker("Mode", selection: $groundStation.inputMode) {
                    ForEach(InputMode.allCases) { mode in
                        Text(mode.rawValue).tag(mode)
                    }
                }
                .pickerStyle(.segmented)
                .disabled(groundStation.isReceiving)
                
                // Mode-specific status
                if groundStation.inputMode == .rtlsdr {
                    StatusRow(
                        title: "RTL-SDR",
                        value: groundStation.isRTLSDRConnected ? "Connected" : "Disconnected",
                        color: groundStation.isRTLSDRConnected ? .green : .red
                    )
                    StatusRow(
                        title: "Frequency",
                        value: String(format: "%.3f MHz", groundStation.radioConfig.frequencyMHz),
                        color: .blue
                    )
                } else {
                    // Serial port selection
                    Picker("Port", selection: $groundStation.selectedSerialPort) {
                        Text("Select Port...").tag("")
                        ForEach(groundStation.availableSerialPorts, id: \.self) { port in
                            Text(port.components(separatedBy: "/").last ?? port)
                                .tag(port)
                        }
                    }
                    .disabled(groundStation.isReceiving)
                    
                    HStack {
                        Button("Refresh") {
                            groundStation.refreshSerialPorts()
                        }
                        .buttonStyle(.borderless)
                        .disabled(groundStation.isReceiving)
                        
                        Spacer()
                        
                        Button("Auto") {
                            _ = groundStation.autoConnectSerial()
                        }
                        .buttonStyle(.borderless)
                        .disabled(groundStation.isReceiving)
                    }
                    
                    StatusRow(
                        title: "Serial",
                        value: groundStation.isSerialConnected ? "Connected" : "Disconnected",
                        color: groundStation.isSerialConnected ? .green : .red
                    )
                    
                    if groundStation.isSerialConnected {
                        StatusRow(
                            title: "RSSI",
                            value: String(format: "%.1f dBm", groundStation.serialRSSI),
                            color: groundStation.serialRSSI > -100 ? .green : .orange
                        )
                        StatusRow(
                            title: "SNR",
                            value: String(format: "%.1f dB", groundStation.serialSNR),
                            color: groundStation.serialSNR > 0 ? .green : .orange
                        )
                    }
                }
            }
            
            // GPS Section
            GPSSettingsView()
            
            // Landing Prediction
            LandingPredictionSidebarView()
            
            // Recording Section
            RecordingSidebarSection()
            
            // Alerts Section
            AlertsSidebarSection()
            
            // Antenna Rotator Section
            RotatorSidebarSection()
            
            // SondeHub Section
            SondeHubSidebarSection()

            // Position Source Section
            Section {
                PositionSourcePanel()
            }
        }
        .listStyle(.sidebar)
        .frame(minWidth: 250)
        .onAppear {
            groundStation.refreshSerialPorts()
            GPSManager.shared.refreshPorts()
        }
        .onChange(of: groundStation.latestTelemetry) { _, newTelem in
            // Update bearing calculation when telemetry updates
            guard let telem = newTelem else { return }
            
            // Immediate UI updates (bearing affects map display)
            GPSManager.shared.updateBearing(
                toLatitude: telem.latitude,
                toLongitude: telem.longitude,
                toAltitude: telem.altitude
            )
            
            // Update SondeHub ground station position (cheap operation)
            if let gsPos = GPSManager.shared.currentPosition, gsPos.isValid {
                SondeHubManager.shared.groundStationPosition = gsPos.coordinate
                SondeHubManager.shared.groundStationAltitude = gsPos.altitude
            }
            
            // Throttled operations - run max once per second
            let now = Date()
            let shouldRunThrottled = now.timeIntervalSince(Self.lastThrottledUpdate) >= 1.0

            if shouldRunThrottled {
                Self.lastThrottledUpdate = now

                // Upload to SondeHub (has its own rate limiting)
                SondeHubManager.shared.uploadTelemetry(
                    telem,
                    rssi: groundStation.serialRSSI,
                    snr: groundStation.serialSNR
                )

                // Update landing prediction (expensive)
                LandingPredictionManager.shared.updatePrediction(from: groundStation.telemetryHistory)

                // Update burst detection
                BurstDetectionManager.shared.update(with: telem)

                // Update audio alerts
                AudioAlertManager.shared.updateWithTelemetry(telem)
            }

            // Note: Mission recording is handled in GroundStationManager.saveTelemetry()
            // with 10-second throttling
        }
        // All sheets attached here at the SidebarView level, outside the List
        .sheet(isPresented: $sondeHub.showSettings) {
            SondeHubSettingsView()
        }
        .sheet(isPresented: $alertManager.showSettings) {
            AlertSettingsView()
        }
        .sheet(isPresented: $missionManager.showRecordingSettings) {
            RecordingSettingsView()
        }
        .sheet(isPresented: $landingPredictor.showSettings) {
            PredictionSettingsView()
        }
        .sheet(isPresented: $rotator.showSettings) {
            RotatorSettingsView()
        }
    }
}

// MARK: - Telemetry View

struct TelemetryView: View {
    @EnvironmentObject var groundStation: GroundStationManager
    
    var body: some View {
        ScrollView {
            VStack(spacing: 20) {
                // Current telemetry cards
                if let telem = groundStation.latestTelemetry {
                    LazyVGrid(columns: [
                        GridItem(.flexible()),
                        GridItem(.flexible()),
                        GridItem(.flexible()),
                        GridItem(.flexible())
                    ], spacing: 16) {
                        TelemetryCard(
                            title: "Altitude",
                            value: String(format: "%.0f", telem.altitude),
                            unit: "m",
                            icon: "arrow.up.circle",
                            color: .blue
                        )
                        
                        TelemetryCard(
                            title: "Speed",
                            value: String(format: "%.1f", telem.speed),
                            unit: "m/s",
                            icon: "speedometer",
                            color: .green
                        )
                        
                        TelemetryCard(
                            title: "Battery",
                            value: String(format: "%.2f", telem.batteryVoltage),
                            unit: "V",
                            icon: "battery.100",
                            color: telem.batteryMV > 3600 ? .green : .orange
                        )
                        
                        TelemetryCard(
                            title: "RSSI",
                            value: "\(telem.rssi)",
                            unit: "dBm",
                            icon: "antenna.radiowaves.left.and.right",
                            color: .purple
                        )
                        
                        TelemetryCard(
                            title: "CPU Temp",
                            value: String(format: "%.1f", telem.cpuTemp),
                            unit: "°C",
                            icon: "thermometer",
                            color: telem.cpuTemp > 60 ? .red : .orange
                        )
                        
                        TelemetryCard(
                            title: "Satellites",
                            value: "\(telem.satellites)",
                            unit: telem.fixType,
                            icon: "location.circle",
                            color: telem.satellites >= 6 ? .green : .yellow
                        )
                        
                        TelemetryCard(
                            title: "Heading",
                            value: String(format: "%.0f", telem.heading),
                            unit: "°",
                            icon: "safari",
                            color: .cyan
                        )
                        
                        TelemetryCard(
                            title: "Sequence",
                            value: "\(telem.sequence)",
                            unit: "",
                            icon: "number",
                            color: .gray
                        )
                    }
                    .padding()
                } else {
                    ContentUnavailableView(
                        "No Telemetry",
                        systemImage: "antenna.radiowaves.left.and.right.slash",
                        description: Text("Start receiving to see telemetry data")
                    )
                }
                
                // Altitude chart
                if groundStation.telemetryHistory.count > 1 {
                    GroupBox("Altitude History") {
                        AltitudeChartView(data: groundStation.telemetryHistory)
                            .frame(height: 200)
                    }
                    .padding(.horizontal)
                }
                
                // History table
                GroupBox("Telemetry History") {
                    TelemetryTableView()
                }
                .padding(.horizontal)
            }
            .padding(.vertical)
        }
    }
}

// MARK: - Telemetry Card

struct TelemetryCard: View {
    let title: String
    let value: String
    let unit: String
    let icon: String
    let color: Color
    
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Image(systemName: icon)
                    .foregroundColor(color)
                Text(title)
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
            
            HStack(alignment: .lastTextBaseline, spacing: 4) {
                Text(value)
                    .font(.title)
                    .fontWeight(.semibold)
                Text(unit)
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding()
        .background(Color(nsColor: .controlBackgroundColor))
        .cornerRadius(12)
    }
}

// MARK: - Altitude Chart (Simple)

struct AltitudeChartView: View {
    let data: [TelemetryPoint]
    
    var body: some View {
        GeometryReader { geometry in
            let maxAlt = data.map(\.altitude).max() ?? 1
            let minAlt = data.map(\.altitude).min() ?? 0
            let range = maxAlt - minAlt > 0 ? maxAlt - minAlt : 1
            
            Path { path in
                guard data.count > 1 else { return }
                
                let xStep = geometry.size.width / CGFloat(data.count - 1)
                let yScale = geometry.size.height / CGFloat(range)
                
                for (index, point) in data.enumerated() {
                    let x = CGFloat(index) * xStep
                    let y = geometry.size.height - CGFloat(point.altitude - minAlt) * yScale
                    
                    if index == 0 {
                        path.move(to: CGPoint(x: x, y: y))
                    } else {
                        path.addLine(to: CGPoint(x: x, y: y))
                    }
                }
            }
            .stroke(Color.blue, lineWidth: 2)
            
            // Y-axis labels
            VStack {
                Text(String(format: "%.0f m", maxAlt))
                    .font(.caption2)
                    .foregroundColor(.secondary)
                Spacer()
                Text(String(format: "%.0f m", minAlt))
                    .font(.caption2)
                    .foregroundColor(.secondary)
            }
            .frame(width: 50)
        }
    }
}

// MARK: - Telemetry Table

struct TelemetryTableView: View {
    @EnvironmentObject var groundStation: GroundStationManager
    
    var body: some View {
        Table(groundStation.telemetryHistory.suffix(100).reversed()) {
            TableColumn("Time") { point in
                Text(point.timestamp, style: .time)
                    .font(.caption)
            }
            .width(80)
            
            TableColumn("Seq") { point in
                Text("\(point.sequence)")
                    .font(.caption.monospacedDigit())
            }
            .width(50)
            
            TableColumn("Lat") { point in
                Text(String(format: "%.5f", point.latitude))
                    .font(.caption.monospacedDigit())
            }
            .width(90)
            
            TableColumn("Lon") { point in
                Text(String(format: "%.5f", point.longitude))
                    .font(.caption.monospacedDigit())
            }
            .width(90)
            
            TableColumn("Alt (m)") { point in
                Text(String(format: "%.0f", point.altitude))
                    .font(.caption.monospacedDigit())
            }
            .width(70)
            
            TableColumn("Speed") { point in
                Text(String(format: "%.1f", point.speed))
                    .font(.caption.monospacedDigit())
            }
            .width(60)
            
            TableColumn("Sats") { point in
                Text("\(point.satellites)")
                    .font(.caption.monospacedDigit())
            }
            .width(40)
            
            TableColumn("RSSI") { point in
                Text("\(point.rssi)")
                    .font(.caption.monospacedDigit())
            }
            .width(50)
        }
        .frame(minHeight: 200)
    }
}

// MARK: - Map View

struct MapDisplayView: View {
    @EnvironmentObject var groundStation: GroundStationManager
    @ObservedObject var gpsManager = GPSManager.shared
    @ObservedObject var predictor = LandingPredictionManager.shared
    @ObservedObject var burstDetector = BurstDetectionManager.shared
    @State private var cameraPosition: MapCameraPosition = .automatic
    @State private var showOfflineSettings = false
    @State private var useOfflineMap = false
    
    var body: some View {
        ZStack {
            if useOfflineMap {
                // Offline map with OSM tiles
                OfflineMapView(
                    telemetryHistory: groundStation.telemetryHistory,
                    latestTelemetry: groundStation.latestTelemetry,
                    groundStationPosition: gpsManager.currentPosition
                )
            } else {
                // Standard Apple Maps
                Map(position: $cameraPosition) {
                    // Ground station marker
                    if let gsPos = gpsManager.currentPosition, gsPos.isValid {
                        Annotation("Ground Station", coordinate: gsPos.coordinate) {
                            ZStack {
                                Circle()
                                    .fill(.green)
                                    .frame(width: 24, height: 24)
                                Image(systemName: "antenna.radiowaves.left.and.right")
                                    .foregroundColor(.white)
                                    .font(.caption2)
                            }
                        }
                    }
                    
                    // Burst point marker (if detected)
                    if let burst = burstDetector.burstPoint {
                        Annotation("Burst", coordinate: burst.coordinate) {
                            ZStack {
                                Circle()
                                    .fill(.orange)
                                    .frame(width: 28, height: 28)
                                Image(systemName: "burst.fill")
                                    .foregroundColor(.white)
                                    .font(.caption)
                            }
                        }
                    }
                    
                    // Current position marker (payload)
                    if let latest = groundStation.latestTelemetry {
                        Annotation("Payload", coordinate: CLLocationCoordinate2D(
                            latitude: latest.latitude,
                            longitude: latest.longitude
                        )) {
                            ZStack {
                                Circle()
                                    .fill(.red)
                                    .frame(width: 24, height: 24)
                                Image(systemName: "balloon.fill")
                                    .foregroundColor(.white)
                                    .font(.caption)
                            }
                        }
                    }
                    
                    // Predicted landing marker
                    if let prediction = predictor.currentPrediction {
                        // Landing zone circle (uncertainty)
                        MapCircle(center: prediction.predictedCoordinate, radius: uncertaintyRadius(prediction))
                            .foregroundStyle(predictionColor(prediction).opacity(0.2))
                            .stroke(predictionColor(prediction), lineWidth: 2)
                        
                        // Landing marker
                        Annotation("Predicted Landing", coordinate: prediction.predictedCoordinate) {
                            ZStack {
                                Circle()
                                    .fill(predictionColor(prediction))
                                    .frame(width: 24, height: 24)
                                Image(systemName: "mappin.and.ellipse")
                                    .foregroundColor(.white)
                                    .font(.caption2)
                            }
                        }
                        
                        // Line from current position to predicted landing
                        if let latest = groundStation.latestTelemetry {
                            MapPolyline(coordinates: [
                                CLLocationCoordinate2D(latitude: latest.latitude, longitude: latest.longitude),
                                prediction.predictedCoordinate
                            ])
                            .stroke(predictionColor(prediction), style: StrokeStyle(lineWidth: 2, dash: [8, 4]))
                        }
                    }
                    
                    // Line from ground station to payload with bearing indicator
                    if let gsPos = gpsManager.currentPosition, gsPos.isValid,
                       let payload = groundStation.latestTelemetry {
                        let payloadCoord = CLLocationCoordinate2D(latitude: payload.latitude, longitude: payload.longitude)
                        
                        // Main bearing line
                        MapPolyline(coordinates: [gsPos.coordinate, payloadCoord])
                            .stroke(.orange, style: StrokeStyle(lineWidth: 3, dash: [10, 5]))
                        
                        // Bearing label annotation at midpoint
                        if let bearing = gpsManager.bearingToPayload {
                            let midLat = (gsPos.latitude + payload.latitude) / 2
                            let midLon = (gsPos.longitude + payload.longitude) / 2
                            let midCoord = CLLocationCoordinate2D(latitude: midLat, longitude: midLon)
                            
                            Annotation("", coordinate: midCoord) {
                                BearingDistanceLabel(bearing: bearing)
                            }
                        }
                    }
                    
                    // Flight path
                    if groundStation.telemetryHistory.count > 1 {
                        MapPolyline(coordinates: groundStation.telemetryHistory.map {
                            CLLocationCoordinate2D(latitude: $0.latitude, longitude: $0.longitude)
                        })
                        .stroke(.blue, lineWidth: 3)
                    }
                }
                .mapStyle(.hybrid(elevation: .realistic))
            }
            
            // Large bearing/heading overlay at bottom center
            if let bearing = gpsManager.bearingToPayload, groundStation.latestTelemetry != nil {
                VStack {
                    Spacer()
                    BearingHeadingOverlay(bearing: bearing)
                        .padding(.bottom, 20)
                }
            }
        }
        .overlay(alignment: .topLeading) {
            // Which source the displayed position came from, and how old it
            // is. A fused position is only trustworthy if you can see what
            // produced it.
            PositionSourceBadge()
                .padding(10)
        }
        .overlay(alignment: .topTrailing) {
            VStack(alignment: .trailing, spacing: 8) {
                // Payload position info overlay
                if let latest = groundStation.latestTelemetry {
                    VStack(alignment: .trailing, spacing: 4) {
                        HStack {
                            Image(systemName: "balloon.fill")
                                .foregroundColor(.red)
                                .font(.caption)
                            Text("Payload")
                                .font(.caption.bold())
                        }
                        Text(String(format: "%.5f, %.5f", latest.latitude, latest.longitude))
                            .font(.caption.monospacedDigit())
                        Text(String(format: "Alt: %.0f m (%.0f ft)", latest.altitude, latest.altitude * 3.28084))
                            .font(.caption.monospacedDigit())
                    }
                    .padding(8)
                    .background(.ultraThinMaterial)
                    .cornerRadius(8)
                }
                
                // Ground station info
                if let gsPos = gpsManager.currentPosition, gsPos.isValid {
                    VStack(alignment: .trailing, spacing: 4) {
                        HStack {
                            Image(systemName: "antenna.radiowaves.left.and.right")
                                .foregroundColor(.green)
                                .font(.caption)
                            Text("Ground Station")
                                .font(.caption.bold())
                        }
                        Text(String(format: "%.5f, %.5f", gsPos.latitude, gsPos.longitude))
                            .font(.caption.monospacedDigit())
                        Text(String(format: "Alt: %.0f m", gsPos.altitude))
                            .font(.caption.monospacedDigit())
                    }
                    .padding(8)
                    .background(.ultraThinMaterial)
                    .cornerRadius(8)
                }
                
                // Map controls
                VStack(spacing: 8) {
                    // Toggle offline mode
                    Button {
                        useOfflineMap.toggle()
                    } label: {
                        Image(systemName: useOfflineMap ? "map.fill" : "map")
                            .foregroundColor(useOfflineMap ? .blue : .secondary)
                    }
                    .help(useOfflineMap ? "Using offline map" : "Using Apple Maps")
                    
                    // Offline settings
                    Button {
                        showOfflineSettings = true
                    } label: {
                        Image(systemName: "arrow.down.circle")
                    }
                    .help("Offline map settings")
                }
                .padding(8)
                .background(.ultraThinMaterial)
                .cornerRadius(8)
            }
            .padding()
        }
        .sheet(isPresented: $showOfflineSettings) {
            OfflineMapSettingsView()
        }
    }
    
    private func predictionColor(_ prediction: LandingPrediction) -> Color {
        switch prediction.confidence {
        case .high: return .green
        case .medium: return .yellow
        case .low: return .orange
        case .veryLow: return .red
        }
    }
    
    private func uncertaintyRadius(_ prediction: LandingPrediction) -> CLLocationDistance {
        // Uncertainty radius based on confidence and altitude
        switch prediction.confidence {
        case .high: return 200      // 200m radius
        case .medium: return 500    // 500m radius
        case .low: return 1000      // 1km radius
        case .veryLow: return 2000  // 2km radius
        }
    }
}

// MARK: - Bearing/Distance Label for Map

struct BearingDistanceLabel: View {
    let bearing: BearingDistance
    
    var body: some View {
        VStack(spacing: 2) {
            Text(String(format: "%.0f°", bearing.bearing))
                .font(.caption.bold().monospacedDigit())
            Text(bearing.distanceFormatted)
                .font(.caption2.monospacedDigit())
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(.orange)
        .foregroundColor(.white)
        .cornerRadius(6)
        .shadow(radius: 2)
    }
}

// MARK: - Large Bearing/Heading Overlay

struct BearingHeadingOverlay: View {
    let bearing: BearingDistance
    
    var body: some View {
        HStack(spacing: 20) {
            // Compass indicator
            ZStack {
                Circle()
                    .stroke(Color.orange.opacity(0.5), lineWidth: 3)
                    .frame(width: 60, height: 60)
                
                // Cardinal directions
                ForEach([0, 90, 180, 270], id: \.self) { angle in
                    Text(cardinalDirection(for: angle))
                        .font(.caption2.bold())
                        .foregroundColor(.secondary)
                        .offset(y: -22)
                        .rotationEffect(.degrees(Double(angle)))
                }
                
                // Bearing arrow
                Image(systemName: "location.north.fill")
                    .font(.title2)
                    .foregroundColor(.orange)
                    .rotationEffect(.degrees(bearing.bearing))
                
                // Center dot
                Circle()
                    .fill(.orange)
                    .frame(width: 8, height: 8)
            }
            
            // Bearing info
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 4) {
                    Text(String(format: "%.0f°", bearing.bearing))
                        .font(.title.bold().monospacedDigit())
                    Text(bearing.bearingCardinal)
                        .font(.title3.bold())
                        .foregroundColor(.orange)
                }
                
                Text(bearing.distanceFormatted)
                    .font(.title2.monospacedDigit())
                
                HStack(spacing: 12) {
                    Label(String(format: "%.1f°", bearing.elevation), systemImage: "arrow.up.right")
                        .font(.caption.monospacedDigit())
                    Text(bearing.distanceMiles)
                        .font(.caption.monospacedDigit())
                        .foregroundColor(.secondary)
                }
            }
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
        .background(.ultraThinMaterial)
        .cornerRadius(12)
        .shadow(radius: 4)
    }
    
    private func cardinalDirection(for angle: Int) -> String {
        switch angle {
        case 0: return "N"
        case 90: return "E"
        case 180: return "S"
        case 270: return "W"
        default: return ""
        }
    }
}

// MARK: - Offline Map View (NSViewRepresentable for MKMapView with tile overlay)

struct OfflineMapView: NSViewRepresentable {
    let telemetryHistory: [TelemetryPoint]
    let latestTelemetry: TelemetryPoint?
    let groundStationPosition: GPSPosition?
    
    func makeNSView(context: Context) -> MKMapView {
        let mapView = MKMapView()
        mapView.delegate = context.coordinator
        
        // Add offline tile overlay
        let overlay = OfflineMapManager.shared.tileOverlay
        mapView.addOverlay(overlay, level: .aboveLabels)
        
        // Configure map
        mapView.mapType = .mutedStandard
        mapView.showsCompass = true
        mapView.showsScale = true
        
        return mapView
    }
    
    func updateNSView(_ mapView: MKMapView, context: Context) {
        // Update annotations
        mapView.removeAnnotations(mapView.annotations)
        mapView.removeOverlays(mapView.overlays.filter { !($0 is MKTileOverlay) })
        
        // Add ground station position
        if let gsPos = groundStationPosition, gsPos.isValid {
            let gsAnnotation = MKPointAnnotation()
            gsAnnotation.coordinate = gsPos.coordinate
            gsAnnotation.title = "Ground Station"
            gsAnnotation.subtitle = String(format: "Alt: %.0f m", gsPos.altitude)
            mapView.addAnnotation(gsAnnotation)
            context.coordinator.groundStationCoordinate = gsPos.coordinate
        } else {
            context.coordinator.groundStationCoordinate = nil
        }
        
        // Add payload position
        if let latest = latestTelemetry {
            let annotation = MKPointAnnotation()
            annotation.coordinate = CLLocationCoordinate2D(latitude: latest.latitude, longitude: latest.longitude)
            annotation.title = "Payload"
            annotation.subtitle = String(format: "Alt: %.0f m", latest.altitude)
            mapView.addAnnotation(annotation)
            context.coordinator.payloadCoordinate = annotation.coordinate
            
            // Center on latest position
            let region = MKCoordinateRegion(
                center: annotation.coordinate,
                latitudinalMeters: 10000,
                longitudinalMeters: 10000
            )
            mapView.setRegion(region, animated: true)
        } else {
            context.coordinator.payloadCoordinate = nil
        }
        
        // Add line from GS to payload
        if let gsPos = groundStationPosition, gsPos.isValid,
           let payload = latestTelemetry {
            let coordinates = [
                gsPos.coordinate,
                CLLocationCoordinate2D(latitude: payload.latitude, longitude: payload.longitude)
            ]
            let bearingLine = MKPolyline(coordinates: coordinates, count: 2)
            bearingLine.title = "bearing"
            mapView.addOverlay(bearingLine)
        }
        
        // Add flight path
        if telemetryHistory.count > 1 {
            let coordinates = telemetryHistory.map {
                CLLocationCoordinate2D(latitude: $0.latitude, longitude: $0.longitude)
            }
            let polyline = MKPolyline(coordinates: coordinates, count: coordinates.count)
            polyline.title = "flightpath"
            mapView.addOverlay(polyline)
        }
    }
    
    func makeCoordinator() -> Coordinator {
        Coordinator()
    }
    
    class Coordinator: NSObject, MKMapViewDelegate {
        var groundStationCoordinate: CLLocationCoordinate2D?
        var payloadCoordinate: CLLocationCoordinate2D?
        
        func mapView(_ mapView: MKMapView, rendererFor overlay: MKOverlay) -> MKOverlayRenderer {
            if let tileOverlay = overlay as? MKTileOverlay {
                return MKTileOverlayRenderer(tileOverlay: tileOverlay)
            }
            
            if let polyline = overlay as? MKPolyline {
                let renderer = MKPolylineRenderer(polyline: polyline)
                
                if polyline.title == "bearing" {
                    renderer.strokeColor = .systemOrange
                    renderer.lineWidth = 2
                    renderer.lineDashPattern = [10, 5]
                } else {
                    renderer.strokeColor = .systemBlue
                    renderer.lineWidth = 3
                }
                return renderer
            }
            
            return MKOverlayRenderer(overlay: overlay)
        }
        
        func mapView(_ mapView: MKMapView, viewFor annotation: MKAnnotation) -> MKAnnotationView? {
            guard !(annotation is MKUserLocation) else { return nil }
            
            let isGroundStation = annotation.title == "Ground Station"
            let identifier = isGroundStation ? "GSMarker" : "PayloadMarker"
            
            var annotationView = mapView.dequeueReusableAnnotationView(withIdentifier: identifier) as? MKMarkerAnnotationView
            
            if annotationView == nil {
                annotationView = MKMarkerAnnotationView(annotation: annotation, reuseIdentifier: identifier)
                annotationView?.canShowCallout = true
            } else {
                annotationView?.annotation = annotation
            }
            
            if isGroundStation {
                annotationView?.markerTintColor = .systemGreen
                annotationView?.glyphImage = NSImage(systemSymbolName: "antenna.radiowaves.left.and.right", accessibilityDescription: nil)
            } else {
                annotationView?.markerTintColor = .systemRed
                annotationView?.glyphImage = NSImage(systemSymbolName: "balloon.fill", accessibilityDescription: nil)
            }
            
            return annotationView
        }
    }
}

// MARK: - Images View

// Wrapper for selected image data (avoids extending UInt16 with Identifiable)
struct SelectedImage: Identifiable {
    let id: UInt16
    let data: Data
    let nsImage: NSImage
}

struct ImagesView: View {
    @EnvironmentObject var groundStation: GroundStationManager
    @State private var selectedImage: SelectedImage? = nil

    /// The most recently decoded image ID
    private var latestImageId: UInt16? {
        groundStation.completedImages.keys.max()
    }

    /// The currently receiving image (highest ID pending image with metadata)
    private var receivingImage: PendingImage? {
        groundStation.pendingImages.values
            .filter { $0.metadata != nil && !groundStation.completedImages.keys.contains($0.id) }
            .max { $0.id < $1.id }
    }

    var body: some View {
        ZStack {
            GeometryReader { geometry in
                if let imageId = latestImageId,
                   let data = groundStation.completedImages[imageId],
                   let nsImage = NSImage(data: data) {
                    // Show the most recent completed image, expanded to fill space
                    VStack(spacing: 12) {
                        Image(nsImage: nsImage)
                            .resizable()
                            .aspectRatio(contentMode: .fit)
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                            .cornerRadius(12)
                            .shadow(radius: 4)
                            .onTapGesture {
                                selectedImage = SelectedImage(id: imageId, data: data, nsImage: nsImage)
                            }
                            .contentShape(Rectangle())
                            .help("Click to expand")

                        HStack {
                            Text("Image \(imageId)")
                                .font(.headline)

                            Spacer()

                            if groundStation.completedImages.count > 1 {
                                Text("\(groundStation.completedImages.count) images this session")
                                    .font(.caption)
                                    .foregroundColor(.secondary)
                            }
                        }
                    }
                    .padding()
                    .padding(.bottom, receivingImage != nil ? 50 : 0) // Make room for progress bar
                } else if let pending = receivingImage {
                    // Show pending image progress if no completed images yet
                    VStack(spacing: 16) {
                        Spacer()

                        ZStack {
                            RoundedRectangle(cornerRadius: 12)
                                .fill(Color.gray.opacity(0.15))
                                .aspectRatio(4/3, contentMode: .fit)
                                .frame(maxWidth: min(geometry.size.width - 40, 500))

                            VStack(spacing: 12) {
                                ProgressView(value: pending.progress / 100)
                                    .frame(width: 200)

                                Text(String(format: "%.0f%%", pending.progress))
                                    .font(.title2)
                                    .fontWeight(.medium)

                                if let meta = pending.metadata {
                                    Text("\(pending.symbols.count) / \(meta.numSourceSymbols) symbols")
                                        .font(.caption)
                                        .foregroundColor(.secondary)
                                }
                            }
                        }

                        Text("Receiving Image \(pending.id)")
                            .font(.headline)
                            .foregroundColor(.secondary)

                        Spacer()
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .padding()
                } else {
                    // No images at all
                    ContentUnavailableView(
                        "No Images",
                        systemImage: "photo.stack",
                        description: Text("The most recent image will appear here when received")
                    )
                }
            }

            // Live reception progress indicator at bottom center
            if let pending = receivingImage, latestImageId != nil {
                VStack {
                    Spacer()
                    ImageReceptionIndicator(pending: pending)
                        .padding(.bottom, 16)
                }
            }
        }
        .sheet(item: $selectedImage) { selected in
            ImageDetailView(imageId: selected.id, nsImage: selected.nsImage, imageData: selected.data)
        }
    }
}

/// Compact progress indicator for image reception
struct ImageReceptionIndicator: View {
    let pending: PendingImage

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: "photo.badge.arrow.down")
                .foregroundColor(.blue)

            VStack(alignment: .leading, spacing: 2) {
                Text("Receiving Image \(pending.id)")
                    .font(.caption)
                    .fontWeight(.medium)

                ProgressView(value: pending.progress / 100)
                    .frame(width: 120)
            }

            if let meta = pending.metadata {
                Text("\(pending.symbols.count)/\(meta.numSourceSymbols)")
                    .font(.caption.monospacedDigit())
                    .foregroundColor(.secondary)
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 10))
        .shadow(radius: 2)
    }
}

// MARK: - Image Detail View (Expandable & Zoomable)

struct ImageDetailView: View {
    let imageId: UInt16
    let nsImage: NSImage
    let imageData: Data
    
    @Environment(\.dismiss) var dismiss
    @State private var scale: CGFloat = 1.0
    @State private var lastScale: CGFloat = 1.0
    @State private var offset: CGSize = .zero
    @State private var lastOffset: CGSize = .zero
    
    var body: some View {
        VStack(spacing: 0) {
            // Header
            HStack {
                Text("Image \(imageId)")
                    .font(.title2.bold())
                
                Spacer()
                
                Text("\(Int(nsImage.size.width)) × \(Int(nsImage.size.height))")
                    .foregroundColor(.secondary)
                
                Text("•")
                    .foregroundColor(.secondary)
                
                Text(ByteCountFormatter.string(fromByteCount: Int64(imageData.count), countStyle: .file))
                    .foregroundColor(.secondary)
                
                Spacer()
                
                // Zoom controls
                Button {
                    withAnimation { scale = max(0.5, scale - 0.25) }
                } label: {
                    Image(systemName: "minus.magnifyingglass")
                }
                .buttonStyle(.borderless)
                
                Text(String(format: "%.0f%%", scale * 100))
                    .frame(width: 50)
                    .foregroundColor(.secondary)
                
                Button {
                    withAnimation { scale = min(5.0, scale + 0.25) }
                } label: {
                    Image(systemName: "plus.magnifyingglass")
                }
                .buttonStyle(.borderless)
                
                Button {
                    withAnimation {
                        scale = 1.0
                        offset = .zero
                    }
                } label: {
                    Image(systemName: "arrow.counterclockwise")
                }
                .buttonStyle(.borderless)
                .help("Reset zoom")
                
                Divider()
                    .frame(height: 20)
                    .padding(.horizontal, 8)
                
                // Export menu
                Menu {
                    Button("Save as WebP (Original)...") {
                        saveImage(format: .webp)
                    }
                    Button("Save as JPEG...") {
                        saveImage(format: .jpeg)
                    }
                    Button("Save as PNG...") {
                        saveImage(format: .png)
                    }
                    Button("Save as TIFF...") {
                        saveImage(format: .tiff)
                    }
                } label: {
                    Image(systemName: "square.and.arrow.down")
                }
                .menuStyle(.borderlessButton)
                .help("Save image")
                
                Button {
                    dismiss()
                } label: {
                    Image(systemName: "xmark.circle.fill")
                        .foregroundColor(.secondary)
                }
                .buttonStyle(.borderless)
                .keyboardShortcut(.escape)
            }
            .padding()
            .background(Color(NSColor.windowBackgroundColor))
            
            Divider()
            
            // Image with zoom and pan
            GeometryReader { geometry in
                ScrollView([.horizontal, .vertical], showsIndicators: true) {
                    Image(nsImage: nsImage)
                        .resizable()
                        .aspectRatio(contentMode: .fit)
                        .scaleEffect(scale)
                        .offset(offset)
                        .frame(
                            width: max(geometry.size.width, nsImage.size.width * scale),
                            height: max(geometry.size.height, nsImage.size.height * scale)
                        )
                        .gesture(
                            MagnificationGesture()
                                .onChanged { value in
                                    let newScale = lastScale * value
                                    scale = min(max(newScale, 0.5), 5.0)
                                }
                                .onEnded { _ in
                                    lastScale = scale
                                }
                        )
                        .gesture(
                            DragGesture()
                                .onChanged { value in
                                    offset = CGSize(
                                        width: lastOffset.width + value.translation.width,
                                        height: lastOffset.height + value.translation.height
                                    )
                                }
                                .onEnded { _ in
                                    lastOffset = offset
                                }
                        )
                        .onTapGesture(count: 2) {
                            withAnimation {
                                if scale > 1.0 {
                                    scale = 1.0
                                    offset = .zero
                                    lastScale = 1.0
                                    lastOffset = .zero
                                } else {
                                    scale = 2.0
                                    lastScale = 2.0
                                }
                            }
                        }
                }
                .background(Color.black.opacity(0.9))
            }
        }
        .frame(minWidth: 600, idealWidth: 1000, maxWidth: .infinity,
               minHeight: 500, idealHeight: 800, maxHeight: .infinity)
    }
    
    private enum ImageFormat {
        case webp, jpeg, png, tiff
        
        var fileExtension: String {
            switch self {
            case .webp: return "webp"
            case .jpeg: return "jpg"
            case .png: return "png"
            case .tiff: return "tiff"
            }
        }
        
        var utType: UTType {
            switch self {
            case .webp: return UTType(filenameExtension: "webp") ?? .data
            case .jpeg: return .jpeg
            case .png: return .png
            case .tiff: return .tiff
            }
        }
    }
    
    private func saveImage(format: ImageFormat) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "image_\(imageId).\(format.fileExtension)"
        panel.allowedContentTypes = [format.utType]
        
        guard panel.runModal() == .OK, let url = panel.url else { return }
        
        do {
            // For WebP, save the original data
            if format == .webp {
                try imageData.write(to: url)
                NSWorkspace.shared.activateFileViewerSelecting([url])
                return
            }
            
            // For other formats, convert the image
            guard let tiffData = nsImage.tiffRepresentation,
                  let bitmap = NSBitmapImageRep(data: tiffData) else {
                print("Failed to create bitmap representation")
                return
            }
            
            let convertedData: Data?
            switch format {
            case .jpeg:
                convertedData = bitmap.representation(using: .jpeg, properties: [.compressionFactor: 0.9])
            case .png:
                convertedData = bitmap.representation(using: .png, properties: [:])
            case .tiff:
                convertedData = bitmap.representation(using: .tiff, properties: [:])
            case .webp:
                convertedData = nil // Handled above
            }
            
            if let data = convertedData {
                try data.write(to: url)
                NSWorkspace.shared.activateFileViewerSelecting([url])
            }
        } catch {
            print("Failed to save image: \(error)")
        }
    }
}

// MARK: - Packet Log View

struct PacketLogView: View {
    @EnvironmentObject var groundStation: GroundStationManager
    
    var body: some View {
        VStack {
            // Messages
            if !groundStation.textMessages.isEmpty {
                GroupBox("Text Messages") {
                    List(groundStation.textMessages.reversed(), id: \.0) { (date, message) in
                        HStack {
                            Text(date, style: .time)
                                .font(.caption)
                                .foregroundColor(.secondary)
                            Text(message)
                                .font(.body)
                        }
                    }
                    .frame(maxHeight: 200)
                }
                .padding()
            }
            
            // Statistics detail
            GroupBox("Detailed Statistics") {
                Form {
                    LabeledContent("Packets Received", value: "\(groundStation.statistics.packetsReceived)")
                    LabeledContent("Packets Valid", value: "\(groundStation.statistics.packetsValid)")
                    LabeledContent("Packets Invalid", value: "\(groundStation.statistics.packetsInvalid)")
                    LabeledContent("Success Rate", value: String(format: "%.1f%%", groundStation.statistics.successRate))
                    
                    Divider()
                    
                    LabeledContent("Telemetry Packets", value: "\(groundStation.statistics.telemetryPackets)")
                    LabeledContent("Image Meta Packets", value: "\(groundStation.statistics.imageMetaPackets)")
                    LabeledContent("Image Data Packets", value: "\(groundStation.statistics.imageDataPackets)")
                    LabeledContent("Text Packets", value: "\(groundStation.statistics.textPackets)")
                    
                    Divider()
                    
                    if let lastTime = groundStation.statistics.lastPacketTime {
                        LabeledContent("Last Packet", value: lastTime, format: .dateTime)
                    }
                    LabeledContent("Last RSSI", value: "\(groundStation.statistics.lastRSSI) dBm")
                }
            }
            .padding()
            
            Spacer()
            
            // Export button
            HStack {
                Spacer()
                Button("Export Telemetry CSV") {
                    if let url = groundStation.exportTelemetryCSV() {
                        NSWorkspace.shared.activateFileViewerSelecting([url])
                    }
                }
                .padding()
            }
        }
    }
}

// MARK: - Radio Config View

struct RadioConfigView: View {
    @EnvironmentObject var groundStation: GroundStationManager
    @Environment(\.dismiss) var dismiss
    
    // RTL-SDR settings
    @State private var frequency: Double = 915.0
    @State private var bitrate: Int = 96000
    @State private var freqDev: Int = 50000
    @State private var gain: Int = 40
    @State private var sampleRate: Int = 1000000
    
    // Modem RF settings
    @State private var modemFrequency: Double = 915.0
    @State private var modemBitrate: Double = 96.0
    @State private var modemDeviation: Double = 50.0
    @State private var modemBandwidth: Double = 234.3
    @State private var modemPreamble: Int = 32
    
    @State private var selectedTab = 0
    
    var body: some View {
        VStack(spacing: 20) {
            Text("Radio Configuration")
                .font(.title2)
            
            TabView(selection: $selectedTab) {
                // Modem RF Configuration Tab
                Form {
                    Section {
                        HStack {
                            Image(systemName: "exclamationmark.triangle.fill")
                                .foregroundColor(.orange)
                            Text("Configure these settings BEFORE starting. To change settings after connecting, unplug the modem and reconnect.")
                                .font(.caption)
                                .foregroundColor(.secondary)
                        }
                        .padding(.vertical, 4)
                    }
                    
                    Section("RF Parameters") {
                        HStack {
                            Text("Frequency")
                            Spacer()
                            TextField("", value: $modemFrequency, format: .number.precision(.fractionLength(1)))
                                .textFieldStyle(.roundedBorder)
                                .frame(width: 100)
                            Text("MHz")
                                .foregroundColor(.secondary)
                        }
                        
                        HStack {
                            Text("Bit Rate")
                            Spacer()
                            TextField("", value: $modemBitrate, format: .number.precision(.fractionLength(1)))
                                .textFieldStyle(.roundedBorder)
                                .frame(width: 100)
                            Text("kbps")
                                .foregroundColor(.secondary)
                        }
                        
                        HStack {
                            Text("Deviation")
                            Spacer()
                            TextField("", value: $modemDeviation, format: .number.precision(.fractionLength(1)))
                                .textFieldStyle(.roundedBorder)
                                .frame(width: 100)
                            Text("kHz")
                                .foregroundColor(.secondary)
                        }
                        
                        HStack {
                            Text("RX Bandwidth")
                            Spacer()
                            TextField("", value: $modemBandwidth, format: .number.precision(.fractionLength(1)))
                                .textFieldStyle(.roundedBorder)
                                .frame(width: 100)
                            Text("kHz")
                                .foregroundColor(.secondary)
                        }
                        
                        HStack {
                            Text("Preamble")
                            Spacer()
                            TextField("", value: $modemPreamble, format: .number)
                                .textFieldStyle(.roundedBorder)
                                .frame(width: 100)
                            Text("bits")
                                .foregroundColor(.secondary)
                        }
                    }
                    
                    Section("Presets") {
                        Button("RaptorHab Default (96kbps)") {
                            modemFrequency = 915.0
                            modemBitrate = 96.0
                            modemDeviation = 50.0
                            modemBandwidth = 234.3
                            modemPreamble = 32
                        }
                        
                        Button("Long Range (9.6kbps)") {
                            modemFrequency = 915.0
                            modemBitrate = 9.6
                            modemDeviation = 5.0
                            modemBandwidth = 50.0
                            modemPreamble = 64
                        }
                    }
                    
                    Section {
                        if groundStation.isModemConfigured {
                            HStack {
                                Image(systemName: "checkmark.circle.fill")
                                    .foregroundColor(.green)
                                Text("Modem configured")
                            }
                        } else if let error = groundStation.modemConfigError {
                            HStack {
                                Image(systemName: "xmark.circle.fill")
                                    .foregroundColor(.red)
                                Text(error)
                                    .font(.caption)
                            }
                        }
                    }
                }
                .formStyle(.grouped)
                .tabItem {
                    Label("Modem RF", systemImage: "antenna.radiowaves.left.and.right")
                }
                .tag(0)
                
                // RTL-SDR Configuration Tab
                Form {
                    Section("RTL-SDR Device") {
                        Picker("Device", selection: .constant(0)) {
                            if groundStation.availableDevices.isEmpty {
                                Text("No devices found")
                                    .tag(0)
                            } else {
                                ForEach(groundStation.availableDevices) { device in
                                    Text(device.name)
                                        .tag(Int(device.id))
                                }
                            }
                        }
                        
                        Button("Scan Devices") {
                            groundStation.scanDevices()
                        }
                    }
                    
                    Section("Frequency") {
                        TextField("Center Frequency (MHz)", value: $frequency, format: .number)
                        Text("RaptorHab uses 915 MHz")
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                    
                    Section("Modulation") {
                        TextField("Bit Rate (bps)", value: $bitrate, format: .number)
                        TextField("Frequency Deviation (Hz)", value: $freqDev, format: .number)
                        Text("RaptorHab: 96000 bps, 50000 Hz deviation")
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                    
                    Section("Receiver") {
                        Picker("Sample Rate", selection: $sampleRate) {
                            Text("1.0 MHz").tag(1000000)
                            Text("1.4 MHz").tag(1400000)
                            Text("2.0 MHz").tag(2000000)
                            Text("2.4 MHz").tag(2400000)
                        }
                        
                        Stepper("Gain: \(gain) dB", value: $gain, in: 0...50)
                        Text("0 = Auto gain")
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                }
                .formStyle(.grouped)
                .tabItem {
                    Label("RTL-SDR", systemImage: "sdcard")
                }
                .tag(1)
            }
            .frame(width: 450, height: 400)
            
            HStack {
                Button("Cancel") {
                    dismiss()
                }
                .keyboardShortcut(.escape)
                
                Spacer()
                
                Button("Apply") {
                    // Apply modem config
                    var modemCfg = ModemConfig()
                    modemCfg.frequencyMHz = modemFrequency
                    modemCfg.bitrateKbps = modemBitrate
                    modemCfg.deviationKHz = modemDeviation
                    modemCfg.bandwidthKHz = modemBandwidth
                    modemCfg.preambleBits = modemPreamble
                    groundStation.modemConfig = modemCfg
                    
                    // Apply RTL-SDR config
                    var config = RadioConfig()
                    config.frequencyMHz = frequency
                    config.bitrateBPS = bitrate
                    config.frequencyDevHz = freqDev
                    config.gain = gain
                    config.sampleRate = sampleRate
                    groundStation.updateRadioConfig(config)
                    
                    dismiss()
                }
                .keyboardShortcut(.return)
                .buttonStyle(.borderedProminent)
            }
            .padding()
        }
        .padding()
        .frame(minWidth: 500, minHeight: 550)
        .onAppear {
            // Load RTL-SDR settings
            frequency = groundStation.radioConfig.frequencyMHz
            bitrate = groundStation.radioConfig.bitrateBPS
            freqDev = groundStation.radioConfig.frequencyDevHz
            gain = groundStation.radioConfig.gain
            sampleRate = groundStation.radioConfig.sampleRate
            
            // Load modem settings
            modemFrequency = groundStation.modemConfig.frequencyMHz
            modemBitrate = groundStation.modemConfig.bitrateKbps
            modemDeviation = groundStation.modemConfig.deviationKHz
            modemBandwidth = groundStation.modemConfig.bandwidthKHz
            modemPreamble = groundStation.modemConfig.preambleBits
        }
    }
}

// MARK: - Settings View

struct SettingsView: View {
    @EnvironmentObject var groundStation: GroundStationManager

    var body: some View {
        TabView {
            RadioConfigView()
                .tabItem {
                    Label("Radio", systemImage: "antenna.radiowaves.left.and.right")
                }
            
            Form {
                Section("Data Storage") {
                    LabeledContent("Max History") {
                        TextField("", value: $groundStation.maxHistorySize, format: .number)
                            .frame(width: 100)
                    }
                }

                Section("Debug") {
                    Button("Inject Test Telemetry") {
                        groundStation.injectSimulatedTelemetry()
                    }
                }
            }
            .formStyle(.grouped)
            .tabItem {
                Label("General", systemImage: "gear")
            }
        }
        .frame(width: 500, height: 450)
    }
}

// MARK: - Helper Views

struct StatusRow: View {
    let title: String
    let value: String
    let color: Color
    
    var body: some View {
        HStack {
            Circle()
                .fill(color)
                .frame(width: 8, height: 8)
            Text(title)
            Spacer()
            Text(value)
                .foregroundColor(.secondary)
        }
    }
}

struct StatRow: View {
    let label: String
    let value: String
    
    var body: some View {
        HStack {
            Text(label)
            Spacer()
            Text(value)
                .foregroundColor(.secondary)
                .font(.caption.monospacedDigit())
        }
    }
}

struct TelemetrySummaryRow: View {
    let label: String
    let value: String
    
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label)
                .font(.caption)
                .foregroundColor(.secondary)
            Text(value)
                .font(.caption.monospacedDigit())
        }
    }
}

struct SignalIndicator: View {
    let strength: Double
    
    var bars: Int {
        switch strength {
        case 0..<20: return 1
        case 20..<40: return 2
        case 40..<60: return 3
        case 60..<80: return 4
        default: return 5
        }
    }
    
    var body: some View {
        HStack(spacing: 2) {
            ForEach(0..<5) { i in
                Rectangle()
                    .fill(i < bars ? Color.green : Color.gray.opacity(0.3))
                    .frame(width: 4, height: CGFloat(8 + i * 4))
            }
        }
        .frame(height: 24)
    }
}

// MARK: - Recording Sidebar Section

struct RecordingSidebarSection: View {
    @ObservedObject var missionManager = MissionManager.shared

    var body: some View {
        Section("Mission") {
            if missionManager.isRecording {
                HStack {
                    Circle()
                        .fill(.red)
                        .frame(width: 10, height: 10)
                    Text("Recording")
                        .foregroundColor(.red)
                    Spacer()
                    Text("\(missionManager.recordedTelemetry.count) pts")
                        .font(.caption.monospacedDigit())
                        .foregroundColor(.secondary)
                }

                if let mission = missionManager.currentMission {
                    Text(mission.name)
                        .font(.caption)
                        .foregroundColor(.secondary)
                }

                if !missionManager.recordedImages.isEmpty {
                    Text("\(missionManager.recordedImages.count) images")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }

                Button("Stop & Save Mission") {
                    missionManager.stopRecording()
                    AudioAlertManager.shared.playAlert(.telemetryReceived, message: "Mission saved")
                }
                .buttonStyle(.borderedProminent)
                .tint(.orange)
            } else {
                HStack {
                    Circle()
                        .fill(.gray)
                        .frame(width: 10, height: 10)
                    Text("Waiting for telemetry...")
                        .foregroundColor(.secondary)
                    Spacer()
                    Button {
                        missionManager.showRecordingSettings = true
                    } label: {
                        Image(systemName: "gear")
                    }
                    .buttonStyle(.borderless)
                }

                Text("Recording starts automatically")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
        }
    }
}

struct RecordingSettingsView: View {
    @ObservedObject var missionManager = MissionManager.shared
    @Environment(\.dismiss) var dismiss
    @State private var showFolderPicker = false

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Image(systemName: "record.circle")
                    .foregroundColor(.red)
                Text("Mission Settings")
                    .font(.title2.bold())
                Spacer()
                Button("Done") { dismiss() }
            }
            .padding()

            Divider()

            Form {
                Section("Storage") {
                    HStack {
                        Text("Missions folder:")
                        Spacer()
                        Text(missionManager.activeMissionsFolder.path)
                            .font(.caption)
                            .foregroundColor(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }

                    HStack {
                        Button("Choose Folder...") {
                            showFolderPicker = true
                        }

                        Button("Open Missions Folder") {
                            NSWorkspace.shared.open(missionManager.activeMissionsFolder)
                        }
                    }
                }

                Section {
                    Text("Missions are recorded automatically when telemetry or images are received. All data is saved to the mission folder.")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
            }
            .formStyle(.grouped)
        }
        .frame(width: 450, height: 250)
        .fileImporter(isPresented: $showFolderPicker, allowedContentTypes: [.folder]) { result in
            if case .success(let url) = result {
                if url.startAccessingSecurityScopedResource() {
                    missionManager.missionsFolder = url
                }
            }
        }
    }
}

// MARK: - Alerts Sidebar Section

struct AlertsSidebarSection: View {
    @ObservedObject var alertManager = AudioAlertManager.shared
    @ObservedObject var burstDetector = BurstDetectionManager.shared
    
    var body: some View {
        Section("Alerts") {
            // Alerts toggle
            HStack {
                Toggle("Audio Alerts", isOn: $alertManager.alertsEnabled)
                    .toggleStyle(.switch)
                Spacer()
                Button {
                    alertManager.showSettings = true
                } label: {
                    Image(systemName: "gear")
                }
                .buttonStyle(.borderless)
            }
            
            // Flight phase indicator
            HStack {
                Image(systemName: phaseIcon(burstDetector.flightPhase))
                    .foregroundColor(phaseColor(burstDetector.flightPhase))
                Text(burstDetector.flightPhase.rawValue)
                    .font(.caption)
                Spacer()
                if burstDetector.maxAltitudeReached > 0 {
                    Text(String(format: "Max: %.0f m", burstDetector.maxAltitudeReached))
                        .font(.caption2)
                        .foregroundColor(.secondary)
                }
            }
            
            // Burst indicator
            if burstDetector.burstDetected {
                HStack {
                    Image(systemName: "burst.fill")
                        .foregroundColor(.orange)
                    Text("Burst Detected!")
                        .font(.caption.bold())
                        .foregroundColor(.orange)
                    if let burst = burstDetector.burstPoint {
                        Spacer()
                        Text(String(format: "%.0f m", burst.altitude))
                            .font(.caption2.monospacedDigit())
                    }
                }
            }
            
            // Vertical speed
            if burstDetector.currentVerticalSpeed != 0 {
                HStack {
                    Image(systemName: burstDetector.currentVerticalSpeed > 0 ? "arrow.up" : "arrow.down")
                        .foregroundColor(burstDetector.currentVerticalSpeed > 0 ? .green : .orange)
                    Text(String(format: "%.1f m/s", burstDetector.currentVerticalSpeed))
                        .font(.caption.monospacedDigit())
                }
            }
            
            // Signal lost warning
            if alertManager.isSignalLost {
                HStack {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundColor(.red)
                    Text("Signal Lost!")
                        .font(.caption.bold())
                        .foregroundColor(.red)
                }
            }
        }
    }
    
    private func phaseIcon(_ phase: FlightPhase) -> String {
        switch phase {
        case .prelaunch: return "circle"
        case .ascending: return "arrow.up.circle.fill"
        case .floating: return "arrow.left.arrow.right.circle.fill"
        case .descending: return "arrow.down.circle.fill"
        case .landed: return "checkmark.circle.fill"
        }
    }
    
    private func phaseColor(_ phase: FlightPhase) -> Color {
        switch phase {
        case .prelaunch: return .gray
        case .ascending: return .green
        case .floating: return .blue
        case .descending: return .orange
        case .landed: return .purple
        }
    }
}

struct AlertSettingsView: View {
    @ObservedObject var alertManager = AudioAlertManager.shared
    @Environment(\.dismiss) var dismiss
    
    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Image(systemName: "bell.badge")
                    .foregroundColor(.blue)
                Text("Alert Settings")
                    .font(.title2.bold())
                Spacer()
                Button("Done") { dismiss() }
            }
            .padding()
            
            Divider()
            
            Form {
                Section("General") {
                    Toggle("Enable Audio Alerts", isOn: $alertManager.alertsEnabled)
                    Toggle("Speak Alerts", isOn: $alertManager.speakAlerts)
                    
                    HStack {
                        Text("Volume")
                        Slider(value: $alertManager.volume, in: 0...1)
                        Text(String(format: "%.0f%%", alertManager.volume * 100))
                            .foregroundColor(.secondary)
                            .frame(width: 40)
                    }
                    
                    HStack {
                        Text("Signal Lost Timeout")
                        Spacer()
                        TextField("", value: $alertManager.signalLostTimeout, format: .number)
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 60)
                        Text("sec")
                            .foregroundColor(.secondary)
                    }
                }
                
                Section("Alert Types") {
                    ForEach(AlertType.allCases, id: \.self) { alertType in
                        Toggle(alertType.rawValue, isOn: Binding(
                            get: { alertManager.enabledAlerts[alertType] ?? false },
                            set: { alertManager.enabledAlerts[alertType] = $0 }
                        ))
                    }
                }
                
                Section("Test") {
                    Button("Test Alert Sound") {
                        alertManager.playAlert(.telemetryReceived, message: "Test alert")
                    }
                }
            }
            .formStyle(.grouped)
        }
        .frame(width: 400, height: 500)
    }
}

#Preview {
    ContentView()
        .environmentObject(GroundStationManager())
}
