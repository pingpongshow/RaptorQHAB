//
//  MissionsView.swift
//  RaptorHabMobile
//
//  View for managing and viewing historical mission data
//

import SwiftUI
import MapKit

struct MissionsView: View {
    @ObservedObject var missionManager = MissionManager.shared
    @State private var selectedMission: Mission?
    @State private var showDeleteConfirm = false
    @State private var missionToDelete: Mission?
    
    var body: some View {
        List {
            // Current Recording Section
            if missionManager.isRecording, let current = missionManager.currentMission {
                Section {
                    MissionRowView(mission: current, isRecording: true, 
                                   telemetryCount: missionManager.recordedTelemetry.count,
                                   imageCount: missionManager.recordedImages.count)
                        .onTapGesture {
                            selectedMission = current
                        }
                } header: {
                    HStack {
                        Circle().fill(Color.red).frame(width: 8, height: 8)
                        Text("Recording")
                    }
                }
            }
            
            // Saved Missions Section
            Section {
                if missionManager.missions.isEmpty {
                    Text("No saved missions")
                        .foregroundColor(.secondary)
                        .frame(maxWidth: .infinity, alignment: .center)
                } else {
                    ForEach(missionManager.missions) { mission in
                        MissionRowView(mission: mission)
                            .onTapGesture {
                                selectedMission = mission
                            }
                            .swipeActions(edge: .trailing) {
                                Button(role: .destructive) {
                                    missionToDelete = mission
                                    showDeleteConfirm = true
                                } label: {
                                    Label("Delete", systemImage: "trash")
                                }
                            }
                    }
                }
            } header: {
                HStack {
                    Text("Saved Missions")
                    Spacer()
                    Button {
                        missionManager.loadMissions()
                    } label: {
                        Image(systemName: "arrow.clockwise")
                    }
                }
            }
            
            // Settings Section
            Section {
                Toggle("Auto-Record Flights", isOn: $missionManager.autoRecord)
            } header: {
                Text("Settings")
            } footer: {
                Text("Automatically start recording when altitude exceeds 100m")
            }
        }
        .navigationTitle("Missions")
        .navigationBarTitleDisplayMode(.inline)
        .sheet(item: $selectedMission) { mission in
            MissionDetailView(mission: mission)
        }
        .alert("Delete Mission?", isPresented: $showDeleteConfirm) {
            Button("Cancel", role: .cancel) { }
            Button("Delete", role: .destructive) {
                if let mission = missionToDelete {
                    missionManager.deleteMission(mission)
                }
            }
        } message: {
            Text("This will permanently delete the mission and all associated data.")
        }
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                if missionManager.isRecording {
                    Button("Stop") {
                        missionManager.stopRecording()
                    }
                    .tint(.red)
                } else {
                    Button("Record") {
                        missionManager.startRecording(name: "Manual Mission")
                    }
                }
            }
        }
    }
}

// MARK: - Mission Row View

struct MissionRowView: View {
    let mission: Mission
    var isRecording: Bool = false
    var telemetryCount: Int?
    var imageCount: Int?
    
    var body: some View {
        HStack {
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text(mission.name)
                        .font(.headline)
                    if isRecording {
                        Circle()
                            .fill(Color.red)
                            .frame(width: 8, height: 8)
                    }
                }
                
                Text(mission.createdAt, style: .date)
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
            
            Spacer()
            
            VStack(alignment: .trailing, spacing: 4) {
                Text(formatAltitude(mission.maxAltitude))
                    .font(.subheadline)
                    .fontWeight(.medium)
                
                HStack(spacing: 8) {
                    Label("\(telemetryCount ?? mission.telemetryCount)", systemImage: "antenna.radiowaves.left.and.right")
                    Label("\(imageCount ?? mission.imageCount)", systemImage: "photo")
                }
                .font(.caption)
                .foregroundColor(.secondary)
            }
        }
        .padding(.vertical, 4)
    }
    
    private func formatAltitude(_ meters: Double) -> String {
        if meters >= 1000 {
            return String(format: "%.1f km", meters / 1000)
        } else {
            return String(format: "%.0f m", meters)
        }
    }
}

// MARK: - Mission Detail View

struct MissionDetailView: View {
    let mission: Mission
    @ObservedObject var missionManager = MissionManager.shared
    @Environment(\.dismiss) var dismiss
    
    @State private var telemetry: [TelemetryPoint] = []
    @State private var images: [RecordedImage] = []
    @State private var selectedTab = 0
    @State private var notes: String = ""
    @State private var showExportSheet = false
    @State private var exportURL: URL?
    
    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                // Mission Info Header
                MissionInfoHeader(mission: mission)
                
                // Tab Picker
                Picker("View", selection: $selectedTab) {
                    Text("Map").tag(0)
                    Text("Telemetry").tag(1)
                    Text("Images").tag(2)
                    Text("Info").tag(3)
                }
                .pickerStyle(.segmented)
                .padding()
                
                // Content
                TabView(selection: $selectedTab) {
                    MissionMapView(telemetry: telemetry, mission: mission)
                        .tag(0)
                    
                    MissionTelemetryList(telemetry: telemetry)
                        .tag(1)
                    
                    MissionImagesGrid(images: images, mission: mission)
                        .tag(2)
                    
                    MissionInfoView(mission: mission, notes: $notes, onSaveNotes: saveNotes)
                        .tag(3)
                }
                .tabViewStyle(.page(indexDisplayMode: .never))
            }
            .navigationTitle(mission.name)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Done") { dismiss() }
                }
                ToolbarItem(placement: .primaryAction) {
                    Menu {
                        Button {
                            exportCSV()
                        } label: {
                            Label("Export CSV", systemImage: "square.and.arrow.up")
                        }
                        
                        if !missionManager.isPlaying {
                            Button {
                                missionManager.startPlayback(mission: mission)
                            } label: {
                                Label("Playback", systemImage: "play")
                            }
                        } else {
                            Button {
                                missionManager.stopPlayback()
                            } label: {
                                Label("Stop Playback", systemImage: "stop")
                            }
                        }
                    } label: {
                        Image(systemName: "ellipsis.circle")
                    }
                }
            }
            .onAppear {
                loadMissionData()
            }
            .sheet(isPresented: $showExportSheet) {
                if let url = exportURL {
                    ShareSheet(items: [url])
                }
            }
        }
    }
    
    private func loadMissionData() {
        telemetry = missionManager.loadMissionTelemetry(mission)
        images = missionManager.loadMissionImages(mission)
        notes = mission.notes
    }
    
    private func saveNotes() {
        missionManager.updateMissionNotes(mission, notes: notes)
    }
    
    private func exportCSV() {
        if let url = missionManager.exportMissionCSV(mission) {
            exportURL = url
            showExportSheet = true
        }
    }
}

// MARK: - Mission Info Header

struct MissionInfoHeader: View {
    let mission: Mission
    
    var body: some View {
        HStack(spacing: 16) {
            StatBox(title: "Max Alt", value: formatAltitude(mission.maxAltitude))
            StatBox(title: "Distance", value: formatDistance(mission.totalDistance))
            StatBox(title: "Duration", value: mission.durationFormatted)
            StatBox(title: "Points", value: "\(mission.telemetryCount)")
        }
        .padding()
        .background(Color(.secondarySystemBackground))
    }
    
    private func formatAltitude(_ meters: Double) -> String {
        if meters >= 1000 {
            return String(format: "%.1f km", meters / 1000)
        } else {
            return String(format: "%.0f m", meters)
        }
    }
    
    private func formatDistance(_ meters: Double) -> String {
        if meters >= 1000 {
            return String(format: "%.1f km", meters / 1000)
        } else {
            return String(format: "%.0f m", meters)
        }
    }
}

struct StatBox: View {
    let title: String
    let value: String
    
    var body: some View {
        VStack(spacing: 2) {
            Text(value)
                .font(.headline)
            Text(title)
                .font(.caption)
                .foregroundColor(.secondary)
        }
        .frame(maxWidth: .infinity)
    }
}

// MARK: - Mission Map View

struct MissionMapView: View {
    let telemetry: [TelemetryPoint]
    let mission: Mission
    
    var body: some View {
        Map {
            // Flight path
            if telemetry.count >= 2 {
                MapPolyline(coordinates: telemetry.map { 
                    CLLocationCoordinate2D(latitude: $0.latitude, longitude: $0.longitude)
                })
                .stroke(.red, lineWidth: 2)
            }
            
            // Launch marker
            if let launch = mission.launchLocation {
                Marker("Launch", coordinate: CLLocationCoordinate2D(latitude: launch.latitude, longitude: launch.longitude))
                    .tint(.green)
            }
            
            // Landing marker
            if let landing = mission.landingLocation {
                Marker("Landing", coordinate: CLLocationCoordinate2D(latitude: landing.latitude, longitude: landing.longitude))
                    .tint(.red)
            }
            
            // Burst marker
            if let burst = mission.burstLocation {
                Marker("Burst", coordinate: CLLocationCoordinate2D(latitude: burst.latitude, longitude: burst.longitude))
                    .tint(.orange)
            }
        }
    }
}

// MARK: - Mission Telemetry List

struct MissionTelemetryList: View {
    let telemetry: [TelemetryPoint]
    
    var body: some View {
        List {
            ForEach(Array(telemetry.enumerated()), id: \.offset) { index, point in
                HStack {
                    Text("#\(index + 1)")
                        .font(.caption)
                        .foregroundColor(.secondary)
                        .frame(width: 40, alignment: .leading)
                    
                    VStack(alignment: .leading, spacing: 2) {
                        Text(String(format: "%.5f, %.5f", point.latitude, point.longitude))
                            .font(.caption)
                        Text(point.timestamp, style: .time)
                            .font(.caption2)
                            .foregroundColor(.secondary)
                    }
                    
                    Spacer()
                    
                    VStack(alignment: .trailing, spacing: 2) {
                        Text(String(format: "%.0f m", point.altitude))
                            .font(.subheadline)
                            .fontWeight(.medium)
                        Text("\(point.rssi) dBm")
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                }
            }
        }
    }
}

// MARK: - Mission Images Grid

struct MissionImagesGrid: View {
    let images: [RecordedImage]
    let mission: Mission
    @ObservedObject var missionManager = MissionManager.shared
    
    let columns = [GridItem(.adaptive(minimum: 100))]
    
    var body: some View {
        ScrollView {
            if images.isEmpty {
                Text("No images in this mission")
                    .foregroundColor(.secondary)
                    .padding()
            } else {
                LazyVGrid(columns: columns, spacing: 8) {
                    ForEach(images) { image in
                        if let data = missionManager.getImageData(mission: mission, image: image),
                           let uiImage = UIImage(data: data) {
                            Image(uiImage: uiImage)
                                .resizable()
                                .aspectRatio(contentMode: .fill)
                                .frame(width: 100, height: 100)
                                .clipped()
                                .cornerRadius(8)
                        } else {
                            Rectangle()
                                .fill(Color.gray.opacity(0.3))
                                .frame(width: 100, height: 100)
                                .cornerRadius(8)
                                .overlay {
                                    Image(systemName: "photo")
                                        .foregroundColor(.gray)
                                }
                        }
                    }
                }
                .padding()
            }
        }
    }
}

// MARK: - Mission Info View

struct MissionInfoView: View {
    let mission: Mission
    @Binding var notes: String
    let onSaveNotes: () -> Void
    
    var body: some View {
        Form {
            Section("Flight Details") {
                InfoRow(label: "Created", value: mission.createdAt.formatted())
                if let launch = mission.launchTime {
                    InfoRow(label: "Launch", value: launch.formatted(date: .omitted, time: .shortened))
                }
                if let landing = mission.landingTime {
                    InfoRow(label: "Landing", value: landing.formatted(date: .omitted, time: .shortened))
                }
                InfoRow(label: "Duration", value: mission.durationFormatted)
            }
            
            Section("Statistics") {
                InfoRow(label: "Max Altitude", value: String(format: "%.0f m", mission.maxAltitude))
                InfoRow(label: "Total Distance", value: String(format: "%.1f km", mission.totalDistance / 1000))
                InfoRow(label: "Telemetry Points", value: "\(mission.telemetryCount)")
                InfoRow(label: "Images", value: "\(mission.imageCount)")
                if let burst = mission.burstAltitude {
                    InfoRow(label: "Burst Altitude", value: String(format: "%.0f m", burst))
                }
            }
            
            Section("Notes") {
                TextEditor(text: $notes)
                    .frame(minHeight: 100)
                
                Button("Save Notes") {
                    onSaveNotes()
                }
            }
        }
    }
}

struct InfoRow: View {
    let label: String
    let value: String
    
    var body: some View {
        HStack {
            Text(label)
                .foregroundColor(.secondary)
            Spacer()
            Text(value)
        }
    }
}

// MARK: - Share Sheet

struct ShareSheet: UIViewControllerRepresentable {
    let items: [Any]
    
    func makeUIViewController(context: Context) -> UIActivityViewController {
        UIActivityViewController(activityItems: items, applicationActivities: nil)
    }
    
    func updateUIViewController(_ uiViewController: UIActivityViewController, context: Context) {}
}

#Preview {
    NavigationStack {
        MissionsView()
    }
}
