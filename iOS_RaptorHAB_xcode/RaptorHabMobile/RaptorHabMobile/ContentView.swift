//
//  ContentView.swift
//  RaptorHabMobile
//
//  Main content view with adaptive layout for iPhone and iPad
//

import SwiftUI
import MapKit

struct ContentView: View {
    @EnvironmentObject var groundStation: GroundStationManager
    @EnvironmentObject var locationManager: LocationManager
    @EnvironmentObject var bleManager: BLESerialManager
    @ObservedObject var missionManager = MissionManager.shared
    @State private var selectedTab: Int? = 0
    @State private var showStopRecordingAlert = false
    @Environment(\.horizontalSizeClass) var horizontalSizeClass
    
    var body: some View {
        Group {
            if horizontalSizeClass == .regular {
                iPadLayout
            } else {
                iPhoneLayout
            }
        }
        .sheet(isPresented: $groundStation.showSettings) {
            SettingsView()
        }
        .alert("Error", isPresented: .constant(groundStation.errorMessage != nil)) {
            Button("OK") { groundStation.errorMessage = nil }
        } message: {
            if let error = groundStation.errorMessage { Text(error) }
        }
        .alert("Save Recording?", isPresented: $showStopRecordingAlert) {
            Button("Save") { missionManager.stopRecording(); groundStation.stopReceiving() }
            Button("Discard", role: .destructive) { missionManager.discardRecording(); groundStation.stopReceiving() }
            Button("Cancel", role: .cancel) { }
        } message: {
            Text("Recording in progress with \(missionManager.recordedTelemetry.count) points. Save it?")
        }
    }
    
    // MARK: - iPad Layout
    
    private var iPadLayout: some View {
        NavigationSplitView {
            List {
                Section("Tracking") {
                    sidebarButton("Telemetry", icon: "gauge.with.dots.needle.33percent", tag: 0)
                    sidebarButton("Map", icon: "map", tag: 1)
                    sidebarButton("Graphs", icon: "chart.line.uptrend.xyaxis", tag: 2)
                    sidebarButton("Predictions", icon: "location.magnifyingglass", tag: 3)
                }
                Section("Data") {
                    sidebarButton("Images", icon: "photo.stack", tag: 4)
                    sidebarButton("Messages", icon: "message", tag: 5)
                    sidebarButton("Missions", icon: "folder.badge.gearshape", tag: 6)
                }
                Section("System") {
                    sidebarButton("Packets", icon: "doc.text", tag: 7)
                    sidebarButton("Status", icon: "info.circle", tag: 8)
                }
                Section("Settings") {
                    sidebarButton("SondeHub", icon: "cloud.fill", tag: 10)
                    sidebarButton("Alerts", icon: "bell", tag: 11)
                    sidebarButton("Offline Maps", icon: "map.fill", tag: 12)
                }
            }
            .listStyle(.sidebar)
            .navigationTitle("RaptorHab")
            .toolbar {
                ToolbarItem(placement: .bottomBar) { connectionStatusView }
            }
        } detail: {
            NavigationStack {
                detailView
                    .toolbar {
                        ToolbarItem(placement: .primaryAction) { controlButtons }
                    }
            }
        }
    }
    
    private func sidebarButton(_ title: String, icon: String, tag: Int) -> some View {
        Button {
            selectedTab = tag
        } label: {
            Label(title, systemImage: icon)
        }
        .listRowBackground(selectedTab == tag ? Color.accentColor.opacity(0.2) : Color.clear)
        .foregroundColor(selectedTab == tag ? .accentColor : .primary)
    }
    
    // MARK: - iPhone Layout
    
    private var iPhoneLayout: some View {
        NavigationStack {
            TabView(selection: Binding(
                get: { selectedTab ?? 0 },
                set: { selectedTab = $0 }
            )) {
                TelemetryView()
                    .tabItem { Label("Telemetry", systemImage: "gauge.with.dots.needle.33percent") }
                    .tag(0)
                MapView()
                    .tabItem { Label("Map", systemImage: "map") }
                    .tag(1)
                GraphsView()
                    .tabItem { Label("Graphs", systemImage: "chart.line.uptrend.xyaxis") }
                    .tag(2)
                ImagesView()
                    .tabItem { Label("Images", systemImage: "photo.stack") }
                    .tag(4)
                MoreView()
                    .tabItem { Label("More", systemImage: "ellipsis.circle") }
                    .tag(9)
            }
            .toolbar {
                ToolbarItem(placement: .navigationBarLeading) { bleStatusIndicator }
                ToolbarItem(placement: .navigationBarTrailing) { controlButtons }
            }
        }
    }
    
    @ViewBuilder
    private var detailView: some View {
        switch selectedTab ?? 0 {
        case 0: TelemetryView()
        case 1: MapView()
        case 2: GraphsView()
        case 3: PredictionsView()
        case 4: ImagesView()
        case 5: MessagesListView()
        case 6: MissionsView()
        case 7: PacketLogView()
        case 8: StatusInfoView()
        case 10: SondeHubSettingsView()
        case 11: AlertSettingsView()
        case 12: OfflineMapSettingsView()
        default: TelemetryView()
        }
    }
    
    private var controlButtons: some View {
        HStack(spacing: 16) {
            if missionManager.isRecording {
                HStack(spacing: 4) {
                    Circle().fill(Color.red).frame(width: 8, height: 8)
                    Text("REC").font(.caption).foregroundColor(.red)
                }
            }
            Button {
                if groundStation.isReceiving {
                    if missionManager.hasUnsavedRecording { showStopRecordingAlert = true }
                    else { groundStation.stopReceiving() }
                } else {
                    groundStation.startReceiving()
                }
            } label: {
                Image(systemName: groundStation.isReceiving ? "stop.fill" : "play.fill")
                    .foregroundColor(groundStation.isReceiving ? .red : .green)
            }
            Button { groundStation.showSettings = true } label: { Image(systemName: "gear") }
        }
    }
    
    private var bleStatusIndicator: some View {
        HStack(spacing: 4) {
            Image(systemName: bleManager.isConnected ? "antenna.radiowaves.left.and.right" : "antenna.radiowaves.left.and.right.slash")
                .foregroundColor(bleManager.isConnected ? .green : .red)
            if bleManager.isScanning { ProgressView().scaleEffect(0.7) }
            if bleManager.isConnected {
                Text("\(groundStation.statistics.lastRSSI) dBm").font(.caption).foregroundColor(.secondary)
            }
        }
        .onTapGesture { if !bleManager.isConnected { bleManager.autoConnect() } }
    }
    
    private var connectionStatusView: some View {
        HStack {
            HStack(spacing: 4) {
                Circle().fill(bleManager.isConnected ? Color.green : Color.red).frame(width: 8, height: 8)
                Text(bleManager.connectionStatus).font(.caption).foregroundColor(.secondary)
            }
            Spacer()
            if groundStation.isReceiving {
                HStack(spacing: 4) {
                    Circle().fill(Color.green).frame(width: 6, height: 6)
                    Text("Receiving").font(.caption).foregroundColor(.green)
                }
            }
            if missionManager.isRecording {
                HStack(spacing: 4) {
                    Circle().fill(Color.red).frame(width: 6, height: 6)
                    Text("Recording").font(.caption).foregroundColor(.red)
                }
            }
        }
    }
}

// MARK: - More View

struct MoreView: View {
    var body: some View {
        List {
            NavigationLink { PredictionsView() } label: { Label("Predictions", systemImage: "location.magnifyingglass") }
            NavigationLink { MissionsView() } label: { Label("Missions", systemImage: "folder.badge.gearshape") }
            NavigationLink { MessagesListView() } label: { Label("Messages", systemImage: "message") }
            NavigationLink { PacketLogView() } label: { Label("Packet Log", systemImage: "doc.text") }
            NavigationLink { StatusInfoView() } label: { Label("Status", systemImage: "info.circle") }
            NavigationLink { SondeHubSettingsView() } label: { Label("SondeHub", systemImage: "cloud.fill") }
            NavigationLink { AlertSettingsView() } label: { Label("Alerts", systemImage: "bell") }
            NavigationLink { OfflineMapSettingsView() } label: { Label("Offline Maps", systemImage: "map.fill") }
        }
        .navigationTitle("More")
    }
}

// MARK: - Telemetry View

struct TelemetryView: View {
    @EnvironmentObject var groundStation: GroundStationManager
    @EnvironmentObject var locationManager: LocationManager
    @ObservedObject var burstManager = BurstDetectionManager.shared
    
    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                if burstManager.flightPhase != .prelaunch {
                    FlightPhaseBanner(phase: burstManager.flightPhase, burstDetected: burstManager.burstDetected)
                }
                if burstManager.burstDetected, let burst = burstManager.burstPoint {
                    BurstAlertCard(burstPoint: burst)
                }
                PositionCard(telemetry: groundStation.latestTelemetry)
                FlightDataCard(telemetry: groundStation.latestTelemetry, burstManager: burstManager)
                SystemStatusCard(telemetry: groundStation.latestTelemetry, groundStation: groundStation)
                GroundStationCard(location: locationManager.currentLocation, payloadLocation: groundStation.latestTelemetry)
                if let prediction = LandingPredictionManager.shared.currentPrediction {
                    PredictionSummaryCard(prediction: prediction)
                }
            }
            .padding()
        }
        .navigationTitle("Telemetry")
        .navigationBarTitleDisplayMode(.inline)
    }
}

// MARK: - Supporting Views

struct FlightPhaseBanner: View {
    let phase: FlightPhase
    let burstDetected: Bool
    
    var body: some View {
        HStack {
            Image(systemName: phaseIcon)
            Text(phase.rawValue).fontWeight(.semibold)
            if burstDetected && phase == .descending {
                Spacer()
                Text("BURST").font(.caption).fontWeight(.bold).foregroundColor(.white)
                    .padding(.horizontal, 8).padding(.vertical, 4).background(Color.orange).cornerRadius(4)
            }
        }
        .frame(maxWidth: .infinity).padding()
        .background(phaseColor.opacity(0.2)).cornerRadius(8)
    }
    
    private var phaseIcon: String {
        switch phase {
        case .prelaunch: return "clock"
        case .ascending: return "arrow.up"
        case .floating: return "minus"
        case .descending: return "arrow.down"
        case .landed: return "checkmark.circle"
        }
    }
    
    private var phaseColor: Color {
        switch phase {
        case .prelaunch: return .gray
        case .ascending: return .blue
        case .floating: return .purple
        case .descending: return .orange
        case .landed: return .green
        }
    }
}

struct BurstAlertCard: View {
    let burstPoint: BurstPoint
    
    var body: some View {
        GroupBox {
            VStack(spacing: 8) {
                HStack {
                    Image(systemName: "exclamationmark.triangle.fill").foregroundColor(.orange)
                    Text("Balloon Burst Detected").font(.headline)
                }
                HStack {
                    VStack(alignment: .leading) {
                        Text("Max Altitude").font(.caption).foregroundColor(.secondary)
                        Text(String(format: "%.0f m", burstPoint.maxAltitude)).font(.title3).fontWeight(.bold)
                    }
                    Spacer()
                    VStack(alignment: .trailing) {
                        Text("Burst Time").font(.caption).foregroundColor(.secondary)
                        Text(burstPoint.timestamp, style: .time).font(.title3).fontWeight(.bold)
                    }
                }
            }
        }
        .backgroundStyle(Color.orange.opacity(0.1))
    }
}

struct PositionCard: View {
    let telemetry: TelemetryPoint?
    
    var body: some View {
        GroupBox {
            if let t = telemetry {
                VStack(spacing: 12) {
                    HStack {
                        VStack(alignment: .leading) {
                            Text("Latitude").font(.caption).foregroundColor(.secondary)
                            Text(String(format: "%.6f°", t.latitude)).font(.title3).fontWeight(.semibold)
                        }
                        Spacer()
                        VStack(alignment: .trailing) {
                            Text("Longitude").font(.caption).foregroundColor(.secondary)
                            Text(String(format: "%.6f°", t.longitude)).font(.title3).fontWeight(.semibold)
                        }
                    }
                    Divider()
                    HStack {
                        StatBlock(title: "Altitude", value: String(format: "%.0f m", t.altitude), icon: "arrow.up")
                        StatBlock(title: "Speed", value: String(format: "%.1f m/s", t.speed), icon: "speedometer")
                        StatBlock(title: "Heading", value: String(format: "%.0f°", t.heading), icon: "location.north")
                    }
                }
            } else {
                Text("No telemetry data").foregroundColor(.secondary)
            }
        } label: {
            Label("Position", systemImage: "location")
        }
    }
}

struct FlightDataCard: View {
    let telemetry: TelemetryPoint?
    let burstManager: BurstDetectionManager
    
    var body: some View {
        GroupBox {
            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 12) {
                DataItem(label: "Max Altitude", value: String(format: "%.0f m", burstManager.maxAltitudeReached))
                DataItem(label: "Vertical Speed", value: String(format: "%.1f m/s", burstManager.currentVerticalSpeed))
                DataItem(label: "Satellites", value: "\(telemetry?.satellites ?? 0)")
                DataItem(label: "Fix Type", value: telemetry?.fixType ?? "N/A")
            }
        } label: {
            Label("Flight Data", systemImage: "airplane")
        }
    }
}

struct SystemStatusCard: View {
    let telemetry: TelemetryPoint?
    let groundStation: GroundStationManager
    
    var body: some View {
        GroupBox {
            LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible())], spacing: 12) {
                DataItem(label: "Battery", value: telemetry != nil ? String(format: "%.2f V", Double(telemetry!.batteryMV) / 1000) : "N/A")
                DataItem(label: "RSSI", value: "\(groundStation.statistics.lastRSSI) dBm")
                DataItem(label: "SNR", value: String(format: "%.1f dB", groundStation.statistics.lastSNR))
                DataItem(label: "Packets", value: "\(groundStation.statistics.packetsReceived)")
            }
        } label: {
            Label("System Status", systemImage: "cpu")
        }
    }
}

struct GroundStationCard: View {
    let location: CLLocation?
    let payloadLocation: TelemetryPoint?
    
    var body: some View {
        GroupBox {
            if let loc = location {
                VStack(spacing: 8) {
                    HStack {
                        VStack(alignment: .leading) {
                            Text("Your Position").font(.caption).foregroundColor(.secondary)
                            Text(String(format: "%.5f, %.5f", loc.coordinate.latitude, loc.coordinate.longitude)).font(.subheadline)
                        }
                        Spacer()
                        Text(String(format: "%.0f m", loc.altitude)).font(.subheadline)
                    }
                    if let payload = payloadLocation {
                        Divider()
                        let distance = CLLocation(latitude: loc.coordinate.latitude, longitude: loc.coordinate.longitude)
                            .distance(from: CLLocation(latitude: payload.latitude, longitude: payload.longitude))
                        let bearing = calculateBearing(from: loc.coordinate, to: CLLocationCoordinate2D(latitude: payload.latitude, longitude: payload.longitude))
                        HStack {
                            VStack(alignment: .leading) {
                                Text("Distance").font(.caption).foregroundColor(.secondary)
                                Text(distance >= 1000 ? String(format: "%.2f km", distance/1000) : String(format: "%.0f m", distance))
                                    .font(.title3).fontWeight(.semibold)
                            }
                            Spacer()
                            VStack(alignment: .trailing) {
                                Text("Bearing").font(.caption).foregroundColor(.secondary)
                                Text(String(format: "%.0f°", bearing)).font(.title3).fontWeight(.semibold)
                            }
                        }
                    }
                }
            } else {
                Text("Location not available").foregroundColor(.secondary)
            }
        } label: {
            Label("Ground Station", systemImage: "antenna.radiowaves.left.and.right")
        }
    }
    
    private func calculateBearing(from: CLLocationCoordinate2D, to: CLLocationCoordinate2D) -> Double {
        let lat1 = from.latitude * .pi / 180
        let lat2 = to.latitude * .pi / 180
        let dLon = (to.longitude - from.longitude) * .pi / 180
        let y = sin(dLon) * cos(lat2)
        let x = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dLon)
        return (atan2(y, x) * 180 / .pi + 360).truncatingRemainder(dividingBy: 360)
    }
}

struct PredictionSummaryCard: View {
    let prediction: LandingPrediction
    
    var body: some View {
        GroupBox {
            HStack {
                VStack(alignment: .leading) {
                    Text("Predicted Landing").font(.caption).foregroundColor(.secondary)
                    Text(String(format: "%.5f, %.5f", prediction.predictedLat, prediction.predictedLon)).font(.subheadline)
                }
                Spacer()
                VStack(alignment: .trailing) {
                    Text(prediction.distanceToLanding >= 1000 ? String(format: "%.1f km", prediction.distanceToLanding/1000) : String(format: "%.0f m", prediction.distanceToLanding))
                        .font(.title3).fontWeight(.bold)
                    Text(String(format: "%.0f min", prediction.timeToLanding/60)).font(.caption).foregroundColor(.secondary)
                }
            }
        } label: {
            HStack {
                Label("Landing Prediction", systemImage: "scope")
                Spacer()
                Text(prediction.confidence.rawValue).font(.caption).padding(.horizontal, 6).padding(.vertical, 2)
                    .background(confidenceColor.opacity(0.2)).cornerRadius(4)
            }
        }
    }
    
    private var confidenceColor: Color {
        switch prediction.confidence {
        case .high: return .green
        case .medium: return .yellow
        case .low: return .orange
        case .veryLow: return .red
        }
    }
}

struct StatBlock: View {
    let title: String
    let value: String
    let icon: String
    
    var body: some View {
        VStack(spacing: 4) {
            Image(systemName: icon).font(.caption).foregroundColor(.secondary)
            Text(value).font(.headline)
            Text(title).font(.caption2).foregroundColor(.secondary)
        }
        .frame(maxWidth: .infinity)
    }
}

struct DataItem: View {
    let label: String
    let value: String
    
    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).font(.caption).foregroundColor(.secondary)
            Text(value).font(.subheadline).fontWeight(.medium)
        }
    }
}

// MARK: - Messages List View (renamed to avoid conflict)

struct MessagesListView: View {
    @EnvironmentObject var groundStation: GroundStationManager
    
    var body: some View {
        List {
            if groundStation.textMessages.isEmpty {
                Text("No messages received").foregroundColor(.secondary)
            } else {
                ForEach(Array(groundStation.textMessages.enumerated()), id: \.offset) { index, message in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(message.1).font(.body)
                        Text(message.0, style: .time).font(.caption).foregroundColor(.secondary)
                    }
                    .padding(.vertical, 4)
                }
            }
        }
        .navigationTitle("Messages")
        .navigationBarTitleDisplayMode(.inline)
    }
}

// MARK: - Status Info View (renamed to avoid conflict)

struct StatusInfoView: View {
    @EnvironmentObject var groundStation: GroundStationManager
    @EnvironmentObject var bleManager: BLESerialManager
    @ObservedObject var burstManager = BurstDetectionManager.shared
    @ObservedObject var missionManager = MissionManager.shared
    @ObservedObject var sondeHub = SondeHubManager.shared
    
    var body: some View {
        List {
            Section("Connection") {
                HStack { Text("BLE Status"); Spacer(); Text(bleManager.connectionStatus).foregroundColor(bleManager.isConnected ? .green : .secondary) }
                HStack { Text("RSSI"); Spacer(); Text("\(groundStation.statistics.lastRSSI) dBm").foregroundColor(.secondary) }
                HStack { Text("SNR"); Spacer(); Text(String(format: "%.1f dB", groundStation.statistics.lastSNR)).foregroundColor(.secondary) }
            }
            Section("Receiver") {
                HStack { Text("Packets Received"); Spacer(); Text("\(groundStation.statistics.packetsReceived)") }
                HStack { Text("Valid Packets"); Spacer(); Text("\(groundStation.statistics.packetsValid)") }
                HStack { Text("Invalid Packets"); Spacer(); Text("\(groundStation.statistics.packetsInvalid)").foregroundColor(groundStation.statistics.packetsInvalid > 0 ? .red : .primary) }
                HStack { Text("Success Rate"); Spacer(); Text(String(format: "%.1f%%", groundStation.statistics.successRate)).foregroundColor(groundStation.statistics.successRate > 90 ? .green : .orange) }
            }
            Section("Flight Status") {
                HStack { Text("Phase"); Spacer(); Text(burstManager.flightPhase.rawValue) }
                HStack { Text("Max Altitude"); Spacer(); Text(String(format: "%.0f m", burstManager.maxAltitudeReached)) }
                HStack { Text("Vertical Speed"); Spacer(); Text(String(format: "%.1f m/s", burstManager.currentVerticalSpeed)) }
                HStack { Text("Burst Detected"); Spacer(); Text(burstManager.burstDetected ? "Yes" : "No").foregroundColor(burstManager.burstDetected ? .orange : .secondary) }
            }
            Section("Mission Recording") {
                HStack { Text("Recording"); Spacer(); Text(missionManager.isRecording ? "Active" : "Inactive").foregroundColor(missionManager.isRecording ? .red : .secondary) }
                if missionManager.isRecording {
                    HStack { Text("Telemetry Points"); Spacer(); Text("\(missionManager.recordedTelemetry.count)") }
                    HStack { Text("Images"); Spacer(); Text("\(missionManager.recordedImages.count)") }
                }
                HStack { Text("Saved Missions"); Spacer(); Text("\(missionManager.missions.count)") }
            }
            Section("SondeHub") {
                HStack { Text("Status"); Spacer(); Text(sondeHub.config.enabled ? "Enabled" : "Disabled").foregroundColor(sondeHub.config.enabled ? .green : .secondary) }
                if sondeHub.config.enabled {
                    HStack { Text("Uploads"); Spacer(); Text("\(sondeHub.uploadCount)") }
                    HStack { Text("Errors"); Spacer(); Text("\(sondeHub.errorCount)").foregroundColor(sondeHub.errorCount > 0 ? .red : .primary) }
                }
            }
        }
        .navigationTitle("Status")
        .navigationBarTitleDisplayMode(.inline)
    }
}

// MARK: - SondeHub Settings View

struct SondeHubSettingsView: View {
    @ObservedObject var sondeHub = SondeHubManager.shared
    
    var body: some View {
        Form {
            Section { Toggle("Enable Upload", isOn: $sondeHub.config.enabled) }
            Section("Callsigns") {
                HStack {
                    Text("Uploader")
                    Spacer()
                    TextField("Your callsign", text: $sondeHub.config.uploaderCallsign)
                        .multilineTextAlignment(.trailing)
                        .textInputAutocapitalization(.characters)
                }
                HStack {
                    Text("Payload")
                    Spacer()
                    TextField("Payload callsign", text: $sondeHub.config.payloadCallsign)
                        .multilineTextAlignment(.trailing)
                        .textInputAutocapitalization(.characters)
                }
            }
            Section("Options") {
                Toggle("Upload Telemetry", isOn: $sondeHub.config.uploadTelemetry)
                HStack {
                    Text("Upload Interval")
                    Spacer()
                    TextField("sec", value: $sondeHub.config.uploadInterval, format: .number)
                        .keyboardType(.decimalPad)
                        .multilineTextAlignment(.trailing)
                        .frame(width: 60)
                    Text("sec")
                }
            }
            Section("Status") {
                HStack { Text("Uploads"); Spacer(); Text("\(sondeHub.uploadCount)") }
                HStack { Text("Errors"); Spacer(); Text("\(sondeHub.errorCount)") }
                Button("Reset Statistics") { sondeHub.resetStats() }
            }
        }
        .navigationTitle("SondeHub")
        .navigationBarTitleDisplayMode(.inline)
    }
}

// MARK: - Alert Settings View

struct AlertSettingsView: View {
    @ObservedObject var alertManager = AudioAlertManager.shared
    
    var body: some View {
        Form {
            Section {
                Toggle("Enable Alerts", isOn: $alertManager.alertsEnabled)
                Toggle("Haptic Feedback", isOn: $alertManager.hapticsEnabled)
                Toggle("Voice Announcements", isOn: $alertManager.speakAlerts)
            }
            Section("Alert Types") {
                ForEach(AlertType.allCases, id: \.self) { type in
                    Toggle(type.rawValue, isOn: Binding(
                        get: { alertManager.enabledAlerts[type] ?? false },
                        set: { alertManager.enabledAlerts[type] = $0 }
                    ))
                }
            }
            Section("Signal Loss") {
                HStack {
                    Text("Timeout")
                    Spacer()
                    TextField("sec", value: $alertManager.signalLostTimeout, format: .number)
                        .keyboardType(.numberPad)
                        .multilineTextAlignment(.trailing)
                        .frame(width: 60)
                    Text("sec")
                }
            }
        }
        .navigationTitle("Alerts")
        .navigationBarTitleDisplayMode(.inline)
    }
}
