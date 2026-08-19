//
//  OfflineMapSettingsView.swift
//  RaptorHabGS
//
//  UI for managing offline map downloads
//

import SwiftUI
import MapKit

struct OfflineMapSettingsView: View {
    @ObservedObject var mapManager = OfflineMapManager.shared
    @Environment(\.dismiss) var dismiss
    
    // Download settings
    @State private var centerLatitude: String = "38.5"
    @State private var centerLongitude: String = "-121.5"
    @State private var radiusKm: Double = 50
    @State private var minZoom: Int = 5
    @State private var maxZoom: Int = 14
    
    // Preset locations
    private let presets: [(name: String, lat: Double, lon: Double)] = [
        ("Sacramento, CA", 38.58, -121.49),
        ("Denver, CO", 39.74, -104.99),
        ("Phoenix, AZ", 33.45, -112.07),
        ("Dallas, TX", 32.78, -96.80),
        ("Current Location", 0, 0)
    ]
    
    var estimatedTiles: Int {
        var total = 0
        for z in minZoom...maxZoom {
            let metersPerTile = 156543.03 * cos(38.5 * .pi / 180) / pow(2.0, Double(z)) * 256
            let tilesRadius = Int(ceil(radiusKm * 1000 / metersPerTile))
            let tilesPerSide = tilesRadius * 2 + 1
            total += tilesPerSide * tilesPerSide
        }
        return total
    }
    
    var estimatedSize: String {
        // Average OSM tile is ~15KB
        let bytes = Int64(estimatedTiles * 15_000)
        return ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }
    
    var body: some View {
        VStack(spacing: 0) {
            // Header
            HStack {
                Text("Offline Maps")
                    .font(.title2.bold())
                Spacer()
                Button("Done") { dismiss() }
            }
            .padding()
            
            Divider()
            
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    
                    // Current cache status
                    GroupBox("Cache Status") {
                        HStack {
                            VStack(alignment: .leading) {
                                Text("\(mapManager.tileCount) tiles")
                                    .font(.title3.bold())
                                Text(mapManager.cacheSize)
                                    .foregroundColor(.secondary)
                            }
                            
                            Spacer()
                            
                            Button(role: .destructive) {
                                mapManager.clearCache()
                            } label: {
                                Label("Clear", systemImage: "trash")
                            }
                            .disabled(mapManager.tileCount == 0)
                        }
                        .padding(.vertical, 4)
                    }
                    
                    // Download progress
                    if mapManager.isDownloading {
                        GroupBox("Downloading...") {
                            VStack(spacing: 12) {
                                ProgressView(value: mapManager.downloadProgress)
                                
                                HStack {
                                    Text("\(Int(mapManager.downloadProgress * 100))%")
                                        .font(.caption)
                                        .foregroundColor(.secondary)
                                    
                                    Spacer()
                                    
                                    Button("Cancel") {
                                        mapManager.cancelDownload()
                                    }
                                    .buttonStyle(.bordered)
                                }
                            }
                            .padding(.vertical, 4)
                        }
                    }
                    
                    // Download settings
                    GroupBox("Download Region") {
                        VStack(alignment: .leading, spacing: 16) {
                            
                            // Preset locations
                            HStack {
                                Text("Preset:")
                                Picker("Preset", selection: Binding(
                                    get: { "" },
                                    set: { name in
                                        if let preset = presets.first(where: { $0.name == name }) {
                                            if preset.lat != 0 {
                                                centerLatitude = String(format: "%.4f", preset.lat)
                                                centerLongitude = String(format: "%.4f", preset.lon)
                                            }
                                        }
                                    }
                                )) {
                                    Text("Select...").tag("")
                                    ForEach(presets, id: \.name) { preset in
                                        Text(preset.name).tag(preset.name)
                                    }
                                }
                            }
                            
                            // Center coordinates
                            HStack {
                                VStack(alignment: .leading) {
                                    Text("Latitude")
                                        .font(.caption)
                                        .foregroundColor(.secondary)
                                    TextField("Latitude", text: $centerLatitude)
                                        .textFieldStyle(.roundedBorder)
                                }
                                
                                VStack(alignment: .leading) {
                                    Text("Longitude")
                                        .font(.caption)
                                        .foregroundColor(.secondary)
                                    TextField("Longitude", text: $centerLongitude)
                                        .textFieldStyle(.roundedBorder)
                                }
                            }
                            
                            // Radius
                            VStack(alignment: .leading) {
                                Text("Radius: \(Int(radiusKm)) km (\(Int(radiusKm * 0.621)) mi)")
                                    .font(.caption)
                                Slider(value: $radiusKm, in: 10...200, step: 10)
                            }
                            
                            // Zoom levels
                            HStack {
                                VStack(alignment: .leading) {
                                    Text("Min Zoom: \(minZoom)")
                                        .font(.caption)
                                    Stepper("", value: $minZoom, in: 1...maxZoom-1)
                                        .labelsHidden()
                                }
                                
                                VStack(alignment: .leading) {
                                    Text("Max Zoom: \(maxZoom)")
                                        .font(.caption)
                                    Stepper("", value: $maxZoom, in: minZoom+1...16)
                                        .labelsHidden()
                                }
                            }
                            
                            // Estimate
                            HStack {
                                Image(systemName: "info.circle")
                                    .foregroundColor(.blue)
                                Text("~\(estimatedTiles) tiles, ~\(estimatedSize)")
                                    .font(.caption)
                                    .foregroundColor(.secondary)
                            }
                            
                            // Download button
                            Button {
                                startDownload()
                            } label: {
                                HStack {
                                    Image(systemName: "arrow.down.circle.fill")
                                    Text("Download Region")
                                }
                                .frame(maxWidth: .infinity)
                            }
                            .buttonStyle(.borderedProminent)
                            .disabled(mapManager.isDownloading)
                        }
                        .padding(.vertical, 4)
                    }
                    
                    // Info
                    GroupBox("About Offline Maps") {
                        VStack(alignment: .leading, spacing: 8) {
                            Label("Maps are cached automatically as you browse", systemImage: "checkmark.circle")
                            Label("Downloaded maps work without internet", systemImage: "wifi.slash")
                            Label("Uses OpenStreetMap data", systemImage: "map")
                            Label("Higher zoom = more detail but more tiles", systemImage: "magnifyingglass")
                        }
                        .font(.caption)
                        .foregroundColor(.secondary)
                        .padding(.vertical, 4)
                    }
                }
                .padding()
            }
        }
        .frame(width: 450, height: 600)
        .onAppear {
            mapManager.updateStats()
        }
    }
    
    private func startDownload() {
        guard let lat = Double(centerLatitude),
              let lon = Double(centerLongitude) else {
            return
        }
        
        let center = CLLocationCoordinate2D(latitude: lat, longitude: lon)
        mapManager.downloadRegion(center: center, radiusKm: radiusKm, minZoom: minZoom, maxZoom: maxZoom)
    }
}

#Preview {
    OfflineMapSettingsView()
}
