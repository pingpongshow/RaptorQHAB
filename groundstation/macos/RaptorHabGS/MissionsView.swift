//
//  MissionsView.swift
//  RaptorHabGS
//
//  View for managing and viewing historical mission data
//

import SwiftUI
import MapKit
import UniformTypeIdentifiers

struct MissionsView: View {
    @ObservedObject var missionManager = MissionManager.shared
    @State private var selectedMission: Mission?
    @State private var showDeleteConfirm = false
    @State private var missionToDelete: Mission?
    @State private var showExportPanel = false
    
    // Combined list: current recording mission (if any) + saved missions
    private var allMissions: [Mission] {
        var missions: [Mission] = []
        if missionManager.isRecording, let current = missionManager.currentMission {
            missions.append(current)
        }
        missions.append(contentsOf: missionManager.missions)
        return missions
    }
    
    private func isRecordingMission(_ mission: Mission) -> Bool {
        missionManager.isRecording && missionManager.currentMission?.id == mission.id
    }
    
    var body: some View {
        HSplitView {
            // Mission List
            VStack(spacing: 0) {
                // Toolbar
                HStack {
                    Menu {
                        Button("Open Missions Folder") {
                            NSWorkspace.shared.open(missionManager.activeMissionsFolder)
                        }
                        Button("Change Missions Folder...") {
                            // Show folder picker
                        }
                    } label: {
                        Image(systemName: "folder")
                    }
                    .menuStyle(.borderlessButton)
                    
                    Spacer()
                    
                    Button {
                        missionManager.loadMissions()
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                    .buttonStyle(.borderless)
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                
                Divider()
                
                List(selection: $selectedMission) {
                    // Active recording section
                    if missionManager.isRecording, let current = missionManager.currentMission {
                        Section {
                            MissionRowView(
                                mission: current,
                                isRecording: true,
                                liveImageCount: missionManager.recordedImages.count,
                                liveTelemetryCount: missionManager.recordedTelemetry.count
                            )
                            .tag(current)
                        } header: {
                            HStack {
                                Circle()
                                    .fill(Color.red)
                                    .frame(width: 8, height: 8)
                                Text("Recording")
                            }
                        }
                    }
                    
                    // Saved missions section
                    Section {
                        ForEach(missionManager.missions) { mission in
                            MissionRowView(mission: mission)
                                .tag(mission)
                                .contextMenu {
                                    Button("Export as ZIP...") {
                                        exportMission(mission)
                                    }
                                    Divider()
                                    Button("Delete Mission", role: .destructive) {
                                        missionToDelete = mission
                                        showDeleteConfirm = true
                                    }
                                }
                        }
                    } header: {
                        Text("Saved Missions (\(missionManager.missions.count))")
                    }
                }
                .listStyle(.sidebar)
            }
            .frame(minWidth: 250, idealWidth: 280, maxWidth: 350)
            
            // Detail View
            Group {
                if let mission = selectedMission {
                    MissionDetailView(mission: mission)
                } else {
                    VStack(spacing: 16) {
                        Image(systemName: "folder.badge.questionmark")
                            .font(.system(size: 64))
                            .foregroundColor(.secondary)
                        Text("Select a Mission")
                            .font(.title2)
                            .foregroundColor(.secondary)
                        Text("Choose a mission from the list to view its data")
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
            .frame(minWidth: 400)
        }
        .alert("Delete Mission?", isPresented: $showDeleteConfirm, presenting: missionToDelete) { mission in
            Button("Cancel", role: .cancel) {}
            Button("Delete", role: .destructive) {
                missionManager.deleteMission(mission)
                if selectedMission?.id == mission.id {
                    selectedMission = nil
                }
            }
        } message: { mission in
            Text("This will permanently delete '\(mission.name)' and all its data including telemetry and images.")
        }
    }
    
    private func exportMission(_ mission: Mission) {
        missionManager.exportMission(mission) { zipURL in
            guard let zipURL = zipURL else { return }
            
            let panel = NSSavePanel()
            panel.nameFieldStringValue = zipURL.lastPathComponent
            panel.allowedContentTypes = [.zip]
            
            if panel.runModal() == .OK, let destination = panel.url {
                try? FileManager.default.copyItem(at: zipURL, to: destination)
            }
        }
    }
}

// MARK: - Mission Row

struct MissionRowView: View {
    let mission: Mission
    var isRecording: Bool = false
    var liveImageCount: Int? = nil
    var liveTelemetryCount: Int? = nil
    
    private var displayImageCount: Int {
        liveImageCount ?? mission.imageCount
    }
    
    private var displayTelemetryCount: Int {
        liveTelemetryCount ?? mission.telemetryCount
    }
    
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(mission.name)
                    .font(.headline)
                if isRecording {
                    Circle()
                        .fill(Color.red)
                        .frame(width: 8, height: 8)
                }
                Spacer()
                if mission.maxAltitude > 0 {
                    Text(formatAltitude(mission.maxAltitude))
                        .font(.caption)
                        .foregroundColor(.blue)
                }
            }
            
            HStack {
                Text(mission.createdAt, style: .date)
                Text("•")
                Text(mission.createdAt, style: .time)
            }
            .font(.caption)
            .foregroundColor(.secondary)
            
            HStack(spacing: 12) {
                Label("\(displayTelemetryCount)", systemImage: "chart.line.uptrend.xyaxis")
                Label("\(displayImageCount)", systemImage: "photo")
                if let duration = mission.duration {
                    Label(formatDuration(duration), systemImage: "clock")
                } else if isRecording {
                    Label("Recording...", systemImage: "clock")
                }
            }
            .font(.caption2)
            .foregroundColor(.secondary)
        }
        .padding(.vertical, 4)
    }
    
    private func formatAltitude(_ meters: Double) -> String {
        if meters >= 1000 {
            return String(format: "%.1f km", meters / 1000)
        }
        return String(format: "%.0f m", meters)
    }
    
    private func formatDuration(_ seconds: TimeInterval) -> String {
        let hours = Int(seconds) / 3600
        let minutes = (Int(seconds) % 3600) / 60
        if hours > 0 {
            return "\(hours)h \(minutes)m"
        }
        return "\(minutes)m"
    }
}

// MARK: - Mission Detail View

struct MissionDetailView: View {
    let mission: Mission
    @ObservedObject var missionManager = MissionManager.shared
    
    @State private var telemetry: [TelemetryPoint] = []
    @State private var images: [MissionManager.RecordedImage] = []
    @State private var selectedTab = 0
    @State private var showDeleteImagesConfirm = false
    @State private var showDeleteTelemetryConfirm = false
    @State private var isExporting = false
    @State private var showReplay = false
    
    // Check if this is the currently recording mission
    private var isActiveMission: Bool {
        missionManager.isRecording && missionManager.currentMission?.id == mission.id
    }
    
    // Use live data for active mission, loaded data for past missions
    private var displayTelemetry: [TelemetryPoint] {
        isActiveMission ? missionManager.recordedTelemetry : telemetry
    }
    
    private var displayImages: [MissionManager.RecordedImage] {
        isActiveMission ? missionManager.recordedImages : images
    }
    
    // Live counts for header
    private var displayImageCount: Int {
        isActiveMission ? missionManager.recordedImages.count : mission.imageCount
    }
    
    private var displayTelemetryCount: Int {
        isActiveMission ? missionManager.recordedTelemetry.count : mission.telemetryCount
    }
    
    var body: some View {
        VStack(spacing: 0) {
            // Header - use live counts for active mission
            MissionHeaderView(
                mission: mission,
                imageCount: displayImageCount,
                telemetryCount: displayTelemetryCount,
                isActive: isActiveMission
            )
            
            // Tab selector - placed prominently below header
            HStack {
                Picker("View", selection: $selectedTab) {
                    Label("Overview", systemImage: "info.circle").tag(0)
                    Label("Map", systemImage: "map").tag(1)
                    Label("Telemetry", systemImage: "chart.xyaxis.line").tag(2)
                    Label("Images", systemImage: "photo.stack").tag(3)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                
                Spacer()
                
                // Action buttons
                Button {
                    NSWorkspace.shared.open(missionManager.getMissionFolder(mission))
                } label: {
                    Image(systemName: "folder")
                }
                .buttonStyle(.borderless)
                .help("Open mission folder")
                
                Button {
                    showReplay = true
                } label: {
                    Image(systemName: "play.rectangle")
                }
                .buttonStyle(.borderless)
                .disabled(telemetry.isEmpty)
                .help("Replay this flight: scrub the map, imagery and "
                      + "telemetry against one time cursor")

                Menu {
                    Button("Export Mission...") {
                        exportMission()
                    }
                    Divider()
                    Button("Delete Images...", role: .destructive) {
                        showDeleteImagesConfirm = true
                    }
                    Button("Delete Telemetry...", role: .destructive) {
                        showDeleteTelemetryConfirm = true
                    }
                } label: {
                    Image(systemName: "ellipsis.circle")
                }
                .menuStyle(.borderlessButton)
            }
            .padding(.horizontal)
            .padding(.vertical, 8)
            .background(Color(NSColor.controlBackgroundColor))
            
            Divider()
            
            // Tab content
            Group {
                switch selectedTab {
                case 0:
                    MissionOverviewTab(mission: mission, telemetry: displayTelemetry)
                case 1:
                    MissionMapTab(mission: mission, telemetry: displayTelemetry, isActiveMission: isActiveMission)
                case 2:
                    MissionTelemetryTab(telemetry: displayTelemetry)
                case 3:
                    MissionImagesTab(mission: mission, images: displayImages, isActiveMission: isActiveMission)
                default:
                    MissionOverviewTab(mission: mission, telemetry: displayTelemetry)
                }
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .sheet(isPresented: $showReplay) {
            MissionReplayView(mission: mission, telemetry: telemetry, images: images)
        }
        .onAppear {
            loadData()
        }
        .onChange(of: mission.id) { _, _ in
            loadData()
        }
        .alert("Delete All Images?", isPresented: $showDeleteImagesConfirm) {
            Button("Cancel", role: .cancel) {}
            Button("Delete", role: .destructive) {
                missionManager.deleteMissionImages(mission)
                images.removeAll()
            }
        }
        .alert("Delete Telemetry?", isPresented: $showDeleteTelemetryConfirm) {
            Button("Cancel", role: .cancel) {}
            Button("Delete", role: .destructive) {
                missionManager.deleteMissionTelemetry(mission)
                telemetry.removeAll()
            }
        }
    }
    
    private func loadData() {
        // Only load from disk for past missions
        if !isActiveMission {
            telemetry = missionManager.loadMissionTelemetry(mission)
            images = missionManager.loadMissionImages(mission)
        }
    }
    
    private func exportMission() {
        isExporting = true
        missionManager.exportMission(mission) { zipURL in
            isExporting = false
            guard let zipURL = zipURL else { return }
            
            let panel = NSSavePanel()
            panel.nameFieldStringValue = zipURL.lastPathComponent
            panel.allowedContentTypes = [.zip]
            
            if panel.runModal() == .OK, let destination = panel.url {
                try? FileManager.default.moveItem(at: zipURL, to: destination)
            }
        }
    }
}

// MARK: - Mission Header

struct MissionHeaderView: View {
    let mission: Mission
    var imageCount: Int? = nil
    var telemetryCount: Int? = nil
    var isActive: Bool = false
    
    // Use provided counts or fall back to mission values
    private var displayImageCount: Int {
        imageCount ?? mission.imageCount
    }
    
    private var displayTelemetryCount: Int {
        telemetryCount ?? mission.telemetryCount
    }
    
    var body: some View {
        HStack(spacing: 20) {
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(mission.name)
                        .font(.title.bold())
                    if isActive {
                        Text("RECORDING")
                            .font(.caption.bold())
                            .foregroundColor(.white)
                            .padding(.horizontal, 8)
                            .padding(.vertical, 2)
                            .background(Color.red)
                            .cornerRadius(4)
                    }
                }
                Text(mission.createdAt, style: .date) + Text(" at ") + Text(mission.createdAt, style: .time)
                    .font(.subheadline)
                    .foregroundColor(.secondary)
            }
            
            Spacer()
            
            // Stats
            HStack(spacing: 24) {
                StatItem(title: "Max Alt", value: formatAltitude(mission.maxAltitude), icon: "arrow.up")
                StatItem(title: "Distance", value: formatDistance(mission.totalDistance), icon: "point.topleft.down.curvedto.point.bottomright.up")
                StatItem(title: "Duration", value: formatDuration(mission.duration), icon: "clock")
                StatItem(title: "Points", value: "\(displayTelemetryCount)", icon: "chart.dots.scatter")
                StatItem(title: "Images", value: "\(displayImageCount)", icon: "photo")
            }
        }
        .padding()
        .background(Color(NSColor.controlBackgroundColor))
    }
    
    private func formatAltitude(_ meters: Double) -> String {
        if meters >= 1000 {
            return String(format: "%.1f km", meters / 1000)
        }
        return String(format: "%.0f m", meters)
    }
    
    private func formatDistance(_ meters: Double) -> String {
        if meters >= 1000 {
            return String(format: "%.1f km", meters / 1000)
        }
        return String(format: "%.0f m", meters)
    }
    
    private func formatDuration(_ seconds: TimeInterval?) -> String {
        guard let seconds = seconds else { return "N/A" }
        let hours = Int(seconds) / 3600
        let minutes = (Int(seconds) % 3600) / 60
        if hours > 0 {
            return "\(hours)h \(minutes)m"
        }
        return "\(minutes)m"
    }
}

struct StatItem: View {
    let title: String
    let value: String
    let icon: String
    
    var body: some View {
        VStack(spacing: 4) {
            Image(systemName: icon)
                .font(.title2)
                .foregroundColor(.blue)
            Text(value)
                .font(.headline.monospacedDigit())
            Text(title)
                .font(.caption)
                .foregroundColor(.secondary)
        }
    }
}

// MARK: - Mission Overview Tab

struct MissionOverviewTab: View {
    let mission: Mission
    let telemetry: [TelemetryPoint]
    
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                // Launch Info
                if let launch = mission.launchLocation {
                    GroupBox("Launch") {
                        VStack(alignment: .leading, spacing: 8) {
                            if let time = mission.launchTime {
                                LabeledContent("Time", value: time, format: .dateTime)
                            }
                            LabeledContent("Location", value: String(format: "%.5f, %.5f", launch.latitude, launch.longitude))
                            LabeledContent("Altitude", value: String(format: "%.0f m", launch.altitude))
                        }
                    }
                }
                
                // Burst Info
                if let burst = mission.burstLocation {
                    GroupBox("Burst") {
                        VStack(alignment: .leading, spacing: 8) {
                            LabeledContent("Altitude", value: String(format: "%.0f m (%.0f ft)", burst.altitude, burst.altitude * 3.28084))
                            LabeledContent("Location", value: String(format: "%.5f, %.5f", burst.latitude, burst.longitude))
                        }
                    }
                }
                
                // Landing Info
                if let landing = mission.landingLocation {
                    GroupBox("Landing") {
                        VStack(alignment: .leading, spacing: 8) {
                            if let time = mission.landingTime {
                                LabeledContent("Time", value: time, format: .dateTime)
                            }
                            LabeledContent("Location", value: String(format: "%.5f, %.5f", landing.latitude, landing.longitude))
                            LabeledContent("Altitude", value: String(format: "%.0f m", landing.altitude))
                        }
                    }
                }
                
                // Notes
                if !mission.notes.isEmpty {
                    GroupBox("Notes") {
                        Text(mission.notes)
                    }
                }
                
                Spacer()
            }
            .padding()
        }
    }
}

// MARK: - Mission Map Tab

struct MissionMapTab: View {
    let mission: Mission
    let telemetry: [TelemetryPoint]
    var isActiveMission: Bool = false
    @State private var cameraPosition: MapCameraPosition = .automatic
    
    // Filter out invalid coordinates (0,0 or very small values)
    private var validTelemetry: [TelemetryPoint] {
        telemetry.filter { point in
            abs(point.latitude) > 0.001 && abs(point.longitude) > 0.001
        }
    }
    
    private var flightPathCoordinates: [CLLocationCoordinate2D] {
        validTelemetry.map {
            CLLocationCoordinate2D(latitude: $0.latitude, longitude: $0.longitude)
        }
    }
    
    var body: some View {
        Map(position: $cameraPosition) {
            // Flight path
            if flightPathCoordinates.count > 1 {
                MapPolyline(coordinates: flightPathCoordinates)
                    .stroke(.blue, lineWidth: 3)
            }
            
            // Launch marker - use first valid telemetry point if mission doesn't have launch location
            if let launch = mission.launchLocation {
                Annotation("Launch", coordinate: CLLocationCoordinate2D(latitude: launch.latitude, longitude: launch.longitude)) {
                    ZStack {
                        Circle().fill(.green).frame(width: 24, height: 24)
                        Image(systemName: "arrow.up").foregroundColor(.white).font(.caption2)
                    }
                }
            } else if let first = validTelemetry.first {
                Annotation("Start", coordinate: CLLocationCoordinate2D(latitude: first.latitude, longitude: first.longitude)) {
                    ZStack {
                        Circle().fill(.green).frame(width: 24, height: 24)
                        Image(systemName: "arrow.up").foregroundColor(.white).font(.caption2)
                    }
                }
            }
            
            // Burst marker
            if let burst = mission.burstLocation {
                Annotation("Burst", coordinate: CLLocationCoordinate2D(latitude: burst.latitude, longitude: burst.longitude)) {
                    ZStack {
                        Circle().fill(.orange).frame(width: 24, height: 24)
                        Image(systemName: "burst").foregroundColor(.white).font(.caption2)
                    }
                }
            }
            
            // Current position marker for active mission, or landing marker for completed missions
            if isActiveMission, let current = validTelemetry.last {
                Annotation("Current", coordinate: CLLocationCoordinate2D(latitude: current.latitude, longitude: current.longitude)) {
                    ZStack {
                        Circle().fill(.blue).frame(width: 28, height: 28)
                        Circle().fill(.white).frame(width: 20, height: 20)
                        Circle().fill(.blue).frame(width: 12, height: 12)
                    }
                }
            } else if let landing = mission.landingLocation {
                Annotation("Landing", coordinate: CLLocationCoordinate2D(latitude: landing.latitude, longitude: landing.longitude)) {
                    ZStack {
                        Circle().fill(.red).frame(width: 24, height: 24)
                        Image(systemName: "mappin").foregroundColor(.white).font(.caption2)
                    }
                }
            } else if !isActiveMission, let last = validTelemetry.last {
                // Show last known position for completed missions without landing location
                Annotation("Last Position", coordinate: CLLocationCoordinate2D(latitude: last.latitude, longitude: last.longitude)) {
                    ZStack {
                        Circle().fill(.red).frame(width: 24, height: 24)
                        Image(systemName: "mappin").foregroundColor(.white).font(.caption2)
                    }
                }
            }
        }
        .mapStyle(.hybrid)
        .overlay(alignment: .topLeading) {
            if validTelemetry.isEmpty && !telemetry.isEmpty {
                Text("No valid GPS coordinates")
                    .font(.caption)
                    .padding(8)
                    .background(.ultraThinMaterial)
                    .cornerRadius(8)
                    .padding()
            } else if telemetry.isEmpty {
                Text("No telemetry data")
                    .font(.caption)
                    .padding(8)
                    .background(.ultraThinMaterial)
                    .cornerRadius(8)
                    .padding()
            }
        }
    }
}

// MARK: - Mission Telemetry Tab

struct MissionTelemetryTab: View {
    let telemetry: [TelemetryPoint]
    
    var body: some View {
        if telemetry.isEmpty {
            VStack {
                Image(systemName: "chart.xyaxis.line")
                    .font(.system(size: 48))
                    .foregroundColor(.secondary)
                Text("No telemetry data")
                    .foregroundColor(.secondary)
            }
        } else {
            Table(telemetry) {
                TableColumn("Time") { point in
                    Text(point.timestamp, style: .time)
                        .font(.caption.monospacedDigit())
                }
                .width(80)
                
                TableColumn("Lat") { point in
                    Text(String(format: "%.5f", point.latitude))
                        .font(.caption.monospacedDigit())
                }
                .width(90)
                
                TableColumn("Lon") { point in
                    Text(String(format: "%.5f", point.longitude))
                        .font(.caption.monospacedDigit())
                }
                .width(100)
                
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
                
                TableColumn("RSSI") { point in
                    Text(String(format: "%d", point.rssi))
                        .font(.caption.monospacedDigit())
                }
                .width(50)
            }
        }
    }
}

// MARK: - Mission Images Tab

struct MissionImagesTab: View {
    let mission: Mission
    let images: [MissionManager.RecordedImage]
    var isActiveMission: Bool = false
    @ObservedObject var missionManager = MissionManager.shared
    @State private var selectedImage: MissionManager.RecordedImage?
    
    let columns = [GridItem(.adaptive(minimum: 150, maximum: 200))]
    
    // For active mission, use currentMission to ensure correct folder path
    private var effectiveMission: Mission {
        if isActiveMission, let current = missionManager.currentMission {
            return current
        }
        return mission
    }
    
    var body: some View {
        if images.isEmpty {
            VStack {
                Image(systemName: "photo.stack")
                    .font(.system(size: 48))
                    .foregroundColor(.secondary)
                Text("No images")
                    .foregroundColor(.secondary)
                if isActiveMission {
                    Text("Images will appear here as they are received")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
            }
        } else {
            ScrollView {
                LazyVGrid(columns: columns, spacing: 12) {
                    ForEach(images) { image in
                        MissionImageThumbnail(mission: effectiveMission, image: image)
                            .onTapGesture {
                                selectedImage = image
                            }
                    }
                }
                .padding()
            }
            .sheet(item: $selectedImage) { image in
                MissionImageDetailView(mission: effectiveMission, image: image)
            }
        }
    }
}

struct MissionImageThumbnail: View {
    let mission: Mission
    let image: MissionManager.RecordedImage
    
    var imageURL: URL {
        MissionManager.shared.getMissionFolder(mission)
            .appendingPathComponent("images")
            .appendingPathComponent(image.filename)
    }
    
    var body: some View {
        VStack(spacing: 4) {
            if let nsImage = NSImage(contentsOf: imageURL) {
                Image(nsImage: nsImage)
                    .resizable()
                    .aspectRatio(contentMode: .fill)
                    .frame(width: 150, height: 100)
                    .clipped()
                    .cornerRadius(8)
            } else {
                Rectangle()
                    .fill(Color.gray.opacity(0.3))
                    .frame(width: 150, height: 100)
                    .cornerRadius(8)
                    .overlay {
                        Image(systemName: "photo")
                            .foregroundColor(.secondary)
                    }
            }
            
            Text(image.timestamp, style: .time)
                .font(.caption2)
                .foregroundColor(.secondary)
        }
    }
}

struct MissionImageDetailView: View {
    let mission: Mission
    let image: MissionManager.RecordedImage
    @Environment(\.dismiss) var dismiss
    @State private var showExportPanel = false
    
    var imageURL: URL {
        MissionManager.shared.getMissionFolder(mission)
            .appendingPathComponent("images")
            .appendingPathComponent(image.filename)
    }
    
    var body: some View {
        VStack(spacing: 0) {
            // Header
            HStack {
                Text("Image \(image.imageId)")
                    .font(.headline)
                
                Spacer()
                
                // Export menu
                Menu {
                    Button("Export as WebP (Original)...") {
                        exportImage(format: .webp)
                    }
                    Button("Export as JPEG...") {
                        exportImage(format: .jpeg)
                    }
                    Button("Export as PNG...") {
                        exportImage(format: .png)
                    }
                    Button("Export as TIFF...") {
                        exportImage(format: .tiff)
                    }
                    Divider()
                    Button("Show in Finder") {
                        NSWorkspace.shared.activateFileViewerSelecting([imageURL])
                    }
                } label: {
                    Label("Export", systemImage: "square.and.arrow.up")
                }
                .menuStyle(.borderlessButton)
                
                Button("Done") { dismiss() }
                    .keyboardShortcut(.escape, modifiers: [])
            }
            .padding()
            
            Divider()
            
            // Image
            if let nsImage = NSImage(contentsOf: imageURL) {
                Image(nsImage: nsImage)
                    .resizable()
                    .aspectRatio(contentMode: .fit)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .background(Color.black.opacity(0.05))
            } else {
                VStack {
                    Image(systemName: "photo")
                        .font(.system(size: 64))
                        .foregroundColor(.secondary)
                    Text("Unable to load image")
                        .foregroundColor(.secondary)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            
            Divider()
            
            // Footer with metadata
            HStack {
                if let lat = image.latitude, let lon = image.longitude {
                    Label(String(format: "%.5f, %.5f", lat, lon), systemImage: "location")
                        .font(.caption.monospacedDigit())
                }
                if let alt = image.altitude {
                    Label(String(format: "%.0f m", alt), systemImage: "arrow.up")
                        .font(.caption.monospacedDigit())
                }
                Spacer()
                Label(image.filename, systemImage: "doc")
                    .font(.caption)
                Spacer()
                Text(image.timestamp, style: .date)
                Text(image.timestamp, style: .time)
            }
            .font(.caption)
            .foregroundColor(.secondary)
            .padding()
        }
        .frame(minWidth: 500, idealWidth: 800, maxWidth: .infinity,
               minHeight: 400, idealHeight: 600, maxHeight: .infinity)
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
    
    private func exportImage(format: ImageFormat) {
        let panel = NSSavePanel()
        let baseName = (image.filename as NSString).deletingPathExtension
        panel.nameFieldStringValue = "\(baseName).\(format.fileExtension)"
        panel.allowedContentTypes = [format.utType]
        
        guard panel.runModal() == .OK, let destination = panel.url else { return }
        
        // For WebP, just copy the original file
        if format == .webp {
            do {
                if FileManager.default.fileExists(atPath: destination.path) {
                    try FileManager.default.removeItem(at: destination)
                }
                try FileManager.default.copyItem(at: imageURL, to: destination)
            } catch {
                print("Failed to export WebP: \(error)")
            }
            return
        }
        
        // For other formats, convert the image
        guard let nsImage = NSImage(contentsOf: imageURL),
              let tiffData = nsImage.tiffRepresentation,
              let bitmap = NSBitmapImageRep(data: tiffData) else {
            print("Failed to load image for conversion")
            return
        }
        
        let imageData: Data?
        switch format {
        case .jpeg:
            imageData = bitmap.representation(using: .jpeg, properties: [.compressionFactor: 0.9])
        case .png:
            imageData = bitmap.representation(using: .png, properties: [:])
        case .tiff:
            imageData = bitmap.representation(using: .tiff, properties: [:])
        case .webp:
            imageData = nil // Handled above
        }
        
        if let data = imageData {
            do {
                try data.write(to: destination)
            } catch {
                print("Failed to export image: \(error)")
            }
        }
    }
}

#Preview {
    MissionsView()
}
