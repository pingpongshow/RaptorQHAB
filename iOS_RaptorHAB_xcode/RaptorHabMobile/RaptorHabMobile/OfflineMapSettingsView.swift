//
//  OfflineMapSettingsView.swift
//  RaptorHabMobile
//
//  UI for managing offline map downloads
//

import SwiftUI
import MapKit

struct OfflineMapSettingsView: View {
    @ObservedObject var mapManager = OfflineMapManager.shared
    @EnvironmentObject var locationManager: LocationManager
    @Environment(\.dismiss) var dismiss
    
    // Download settings
    @State private var centerLatitude: String = "38.5"
    @State private var centerLongitude: String = "-121.5"
    @State private var radiusKm: Double = 50
    @State private var minZoom: Int = 5
    @State private var maxZoom: Int = 14
    @State private var showConfirmClear = false
    
    // Preset locations
    private let presets: [(name: String, lat: Double, lon: Double)] = [
        ("Sacramento, CA", 38.58, -121.49),
        ("Denver, CO", 39.74, -104.99),
        ("Phoenix, AZ", 33.45, -112.07),
        ("Dallas, TX", 32.78, -96.80),
        ("Kansas City, MO", 39.10, -94.58),
        ("Salt Lake City, UT", 40.76, -111.89),
    ]
    
    var estimatedTiles: Int {
        mapManager.estimateTiles(radiusKm: radiusKm, minZoom: minZoom, maxZoom: maxZoom)
    }
    
    var estimatedSize: String {
        mapManager.estimateSize(tileCount: estimatedTiles)
    }
    
    var body: some View {
        NavigationStack {
            Form {
                // Current cache status
                Section {
                    HStack {
                        VStack(alignment: .leading) {
                            Text("\(mapManager.tileCount) tiles")
                                .font(.title3.bold())
                            Text(mapManager.cacheSize)
                                .foregroundColor(.secondary)
                        }
                        
                        Spacer()
                        
                        Button(role: .destructive) {
                            showConfirmClear = true
                        } label: {
                            Label("Clear", systemImage: "trash")
                        }
                        .disabled(mapManager.tileCount == 0)
                    }
                } header: {
                    Text("Cache Status")
                }
                
                // Download progress
                if mapManager.isDownloading {
                    Section {
                        VStack(spacing: 12) {
                            ProgressView(value: mapManager.downloadProgress)
                            
                            HStack {
                                Text("\(mapManager.downloadedTiles) / \(mapManager.totalTilesToDownload) tiles")
                                    .font(.caption)
                                    .foregroundColor(.secondary)
                                
                                Spacer()
                                
                                Text("\(Int(mapManager.downloadProgress * 100))%")
                                    .font(.caption)
                                    .foregroundColor(.secondary)
                            }
                            
                            Button("Cancel Download") {
                                mapManager.cancelDownload()
                            }
                            .foregroundColor(.red)
                        }
                    } header: {
                        HStack {
                            Text("Downloading")
                            Spacer()
                            ProgressView()
                                .scaleEffect(0.7)
                        }
                    }
                }
                
                // Download region settings
                Section {
                    // Preset picker
                    Picker("Preset Location", selection: Binding(
                        get: { "" },
                        set: { name in
                            if let preset = presets.first(where: { $0.name == name }) {
                                centerLatitude = String(format: "%.4f", preset.lat)
                                centerLongitude = String(format: "%.4f", preset.lon)
                            }
                        }
                    )) {
                        Text("Select...").tag("")
                        ForEach(presets, id: \.name) { preset in
                            Text(preset.name).tag(preset.name)
                        }
                    }
                    
                    // Use current location
                    Button {
                        if let location = locationManager.currentLocation {
                            centerLatitude = String(format: "%.4f", location.coordinate.latitude)
                            centerLongitude = String(format: "%.4f", location.coordinate.longitude)
                        }
                    } label: {
                        Label("Use Current Location", systemImage: "location.fill")
                    }
                    .disabled(locationManager.currentLocation == nil)
                    
                    // Manual coordinates
                    HStack {
                        Text("Latitude")
                        Spacer()
                        TextField("Lat", text: $centerLatitude)
                            .keyboardType(.decimalPad)
                            .multilineTextAlignment(.trailing)
                            .frame(width: 100)
                    }
                    
                    HStack {
                        Text("Longitude")
                        Spacer()
                        TextField("Lon", text: $centerLongitude)
                            .keyboardType(.decimalPad)
                            .multilineTextAlignment(.trailing)
                            .frame(width: 100)
                    }
                } header: {
                    Text("Center Location")
                }
                
                // Coverage settings
                Section {
                    VStack(alignment: .leading) {
                        HStack {
                            Text("Radius")
                            Spacer()
                            Text("\(Int(radiusKm)) km (\(Int(radiusKm * 0.621)) mi)")
                                .foregroundColor(.secondary)
                        }
                        Slider(value: $radiusKm, in: 10...200, step: 10)
                    }
                    
                    Stepper("Min Zoom: \(minZoom)", value: $minZoom, in: 1...maxZoom-1)
                    Stepper("Max Zoom: \(maxZoom)", value: $maxZoom, in: minZoom+1...16)
                    
                    HStack {
                        Image(systemName: "info.circle")
                            .foregroundColor(.blue)
                        Text("~\(estimatedTiles) tiles, ~\(estimatedSize)")
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                } header: {
                    Text("Coverage")
                } footer: {
                    Text("Higher zoom levels provide more detail but require more storage space.")
                }
                
                // Download button
                Section {
                    Button {
                        startDownload()
                    } label: {
                        HStack {
                            Image(systemName: "arrow.down.circle.fill")
                            Text("Download Region")
                        }
                        .frame(maxWidth: .infinity)
                    }
                    .disabled(mapManager.isDownloading || !isValidCoordinates)
                }
                
                // Info section
                Section {
                    Label("Maps cache automatically as you browse", systemImage: "checkmark.circle")
                    Label("Downloaded maps work offline", systemImage: "wifi.slash")
                    Label("Uses OpenStreetMap data", systemImage: "map")
                    Label("Higher zoom = more detail, more tiles", systemImage: "magnifyingglass")
                } header: {
                    Text("About Offline Maps")
                }
            }
            .navigationTitle("Offline Maps")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
            .onAppear {
                mapManager.updateStats()
            }
            .alert("Clear Cache?", isPresented: $showConfirmClear) {
                Button("Cancel", role: .cancel) { }
                Button("Clear", role: .destructive) {
                    mapManager.clearCache()
                }
            } message: {
                Text("This will delete all \(mapManager.tileCount) cached map tiles. You'll need to download them again for offline use.")
            }
        }
    }
    
    private var isValidCoordinates: Bool {
        guard let lat = Double(centerLatitude),
              let lon = Double(centerLongitude) else {
            return false
        }
        return lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180
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
        .environmentObject(LocationManager())
}
