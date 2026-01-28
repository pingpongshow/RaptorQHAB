//
//  StatusViews.swift
//  RaptorHabMobile
//
//  Status display views for modem, GPS, and system statistics
//

import SwiftUI

// MARK: - Status View

struct StatusView: View {
    @EnvironmentObject var groundStation: GroundStationManager
    @EnvironmentObject var bleManager: BLESerialManager
    @EnvironmentObject var locationManager: LocationManager
    
    var body: some View {
        ScrollView {
            VStack(spacing: 16) {
                // Bluetooth Status
                BLEStatusCard()
                
                // GPS Status
                GPSStatusCard()
                
                // Receiver Statistics
                ReceiverStatsCard()
                
                // Actions
                ActionsCard()
            }
            .padding()
        }
        .navigationTitle("System Status")
        .navigationBarTitleDisplayMode(.inline)
    }
}

// MARK: - BLE Status Card

struct BLEStatusCard: View {
    @EnvironmentObject var bleManager: BLESerialManager
    @EnvironmentObject var groundStation: GroundStationManager
    
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Label("Bluetooth", systemImage: "antenna.radiowaves.left.and.right")
                    .font(.headline)
                Spacer()
                Circle()
                    .fill(bleManager.isConnected ? Color.green : Color.red)
                    .frame(width: 10, height: 10)
            }
            
            Divider()
            
            StatusRow(icon: "link", label: "Connection", 
                     value: bleManager.connectionStatus,
                     color: bleManager.isConnected ? .green : .secondary)
            
            if bleManager.isConnected {
                StatusRow(icon: "wave.3.right", label: "Device", 
                         value: "RaptorModem")
                
                StatusRow(icon: "antenna.radiowaves.left.and.right", label: "Modem RSSI", 
                         value: "\(Int(bleManager.lastRSSI)) dBm",
                         color: bleManager.lastRSSI > -80 ? .green : 
                                (bleManager.lastRSSI > -100 ? .orange : .red))
                
                StatusRow(icon: "waveform", label: "Modem SNR", 
                         value: String(format: "%.1f dB", bleManager.lastSNR),
                         color: bleManager.lastSNR > 5 ? .green : 
                                (bleManager.lastSNR > 0 ? .orange : .red))
                
                StatusRow(icon: "gearshape", label: "Configured", 
                         value: bleManager.isConfigured ? "Yes" : "No",
                         color: bleManager.isConfigured ? .green : .orange)
            }
            
            if bleManager.isScanning {
                HStack {
                    ProgressView()
                        .scaleEffect(0.8)
                    Text("Scanning for RaptorModem...")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
            }
            
            // Connection button
            if !bleManager.isConnected {
                Button {
                    if bleManager.isScanning {
                        bleManager.stopScanning()
                    } else {
                        bleManager.autoConnect()
                    }
                } label: {
                    HStack {
                        Spacer()
                        if bleManager.isScanning {
                            Text("Stop Scanning")
                        } else {
                            Image(systemName: "magnifyingglass")
                            Text("Scan for Modem")
                        }
                        Spacer()
                    }
                }
                .buttonStyle(.bordered)
                .padding(.top, 4)
            }
        }
        .padding()
        .background(Color(.systemBackground))
        .cornerRadius(12)
        .shadow(radius: 2)
    }
}

// MARK: - GPS Status Card

struct GPSStatusCard: View {
    @EnvironmentObject var locationManager: LocationManager
    
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Label("GPS (Internal)", systemImage: "location.fill")
                    .font(.headline)
                Spacer()
                Circle()
                    .fill(locationManager.hasValidFix ? Color.green : Color.orange)
                    .frame(width: 10, height: 10)
            }
            
            Divider()
            
            StatusRow(icon: "checkmark.shield", label: "Permission", 
                     value: permissionText,
                     color: locationManager.authorizationStatus == .authorizedWhenInUse || 
                            locationManager.authorizationStatus == .authorizedAlways ? .green : .orange)
            
            StatusRow(icon: "antenna.radiowaves.left.and.right", label: "Status", 
                     value: locationManager.isUpdating ? "Active" : "Inactive",
                     color: locationManager.isUpdating ? .green : .secondary)
            
            if let location = locationManager.currentLocation {
                StatusRow(icon: "location", label: "Latitude", 
                         value: String(format: "%.6f°", location.coordinate.latitude))
                
                StatusRow(icon: "location", label: "Longitude", 
                         value: String(format: "%.6f°", location.coordinate.longitude))
                
                StatusRow(icon: "arrow.up", label: "Altitude", 
                         value: String(format: "%.1f m", location.altitude))
                
                StatusRow(icon: "scope", label: "Accuracy", 
                         value: String(format: "%.1f m", location.horizontalAccuracy),
                         color: location.horizontalAccuracy < 10 ? .green : 
                                (location.horizontalAccuracy < 50 ? .orange : .red))
            }
            
            if let error = locationManager.errorMessage {
                HStack {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundColor(.orange)
                    Text(error)
                        .font(.caption)
                        .foregroundColor(.orange)
                }
            }
            
            // Permission button
            if locationManager.authorizationStatus == .notDetermined {
                Button {
                    locationManager.requestPermission()
                } label: {
                    HStack {
                        Spacer()
                        Text("Request Location Permission")
                        Spacer()
                    }
                }
                .buttonStyle(.bordered)
                .padding(.top, 4)
            }
        }
        .padding()
        .background(Color(.systemBackground))
        .cornerRadius(12)
        .shadow(radius: 2)
    }
    
    private var permissionText: String {
        switch locationManager.authorizationStatus {
        case .notDetermined: return "Not Determined"
        case .restricted: return "Restricted"
        case .denied: return "Denied"
        case .authorizedWhenInUse: return "When In Use"
        case .authorizedAlways: return "Always"
        @unknown default: return "Unknown"
        }
    }
}

// MARK: - Receiver Stats Card

struct ReceiverStatsCard: View {
    @EnvironmentObject var groundStation: GroundStationManager
    @EnvironmentObject var bleManager: BLESerialManager
    
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("Receiver Statistics", systemImage: "chart.bar.fill")
                .font(.headline)
            
            Divider()
            
            // BLE stats
            Group {
                StatusRow(icon: "arrow.down.circle", label: "Bytes Received", 
                         value: formatBytes(bleManager.bytesReceived))
                
                StatusRow(icon: "tray.full", label: "Frames Extracted", 
                         value: "\(bleManager.framesExtracted)")
            }
            
            Divider()
            
            // Packet stats
            Group {
                StatusRow(icon: "envelope", label: "Packets Received", 
                         value: "\(groundStation.statistics.packetsReceived)")
                
                StatusRow(icon: "checkmark.circle", label: "Valid Packets", 
                         value: "\(groundStation.statistics.packetsValid)")
                
                StatusRow(icon: "xmark.circle", label: "Invalid Packets", 
                         value: "\(groundStation.statistics.packetsInvalid)",
                         color: groundStation.statistics.packetsInvalid > 0 ? .orange : .primary)
                
                StatusRow(icon: "percent", label: "Success Rate", 
                         value: String(format: "%.1f%%", groundStation.statistics.successRate),
                         color: groundStation.statistics.successRate > 90 ? .green : 
                                (groundStation.statistics.successRate > 70 ? .orange : .red))
            }
            
            Divider()
            
            // Packet type breakdown
            Group {
                StatusRow(icon: "gauge", label: "Telemetry", 
                         value: "\(groundStation.statistics.telemetryPackets)")
                
                StatusRow(icon: "photo", label: "Image Data", 
                         value: "\(groundStation.statistics.imageDataPackets)")
                
                StatusRow(icon: "doc.text", label: "Image Meta", 
                         value: "\(groundStation.statistics.imageMetaPackets)")
                
                StatusRow(icon: "message", label: "Text Messages", 
                         value: "\(groundStation.statistics.textPackets)")
            }
            
            if let lastTime = groundStation.statistics.lastPacketTime {
                Divider()
                StatusRow(icon: "clock", label: "Last Packet", 
                         value: formatTimeAgo(lastTime))
            }
        }
        .padding()
        .background(Color(.systemBackground))
        .cornerRadius(12)
        .shadow(radius: 2)
    }
    
    private func formatBytes(_ bytes: Int) -> String {
        if bytes < 1024 {
            return "\(bytes) B"
        } else if bytes < 1024 * 1024 {
            return String(format: "%.1f KB", Double(bytes) / 1024)
        } else {
            return String(format: "%.2f MB", Double(bytes) / (1024 * 1024))
        }
    }
    
    private func formatTimeAgo(_ date: Date) -> String {
        let seconds = -date.timeIntervalSinceNow
        if seconds < 60 {
            return String(format: "%.0fs ago", seconds)
        } else if seconds < 3600 {
            return String(format: "%.0fm ago", seconds / 60)
        } else {
            return String(format: "%.1fh ago", seconds / 3600)
        }
    }
}

// MARK: - Actions Card

struct ActionsCard: View {
    @EnvironmentObject var groundStation: GroundStationManager
    @EnvironmentObject var bleManager: BLESerialManager
    
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("Actions", systemImage: "hammer.fill")
                .font(.headline)
            
            Divider()
            
            HStack(spacing: 12) {
                Button {
                    if let url = groundStation.exportTelemetryCSV() {
                        shareFile(url)
                    }
                } label: {
                    VStack {
                        Image(systemName: "square.and.arrow.up")
                        Text("Export CSV")
                            .font(.caption)
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
                .disabled(groundStation.telemetryHistory.isEmpty)
                
                Button {
                    groundStation.clearTelemetry()
                } label: {
                    VStack {
                        Image(systemName: "trash")
                        Text("Clear Data")
                            .font(.caption)
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(.bordered)
                .tint(.red)
                
                Button {
                    if bleManager.isConnected {
                        groundStation.startReceiving()
                    } else {
                        bleManager.autoConnect()
                    }
                } label: {
                    VStack {
                        Image(systemName: bleManager.isConnected ? "play.fill" : "antenna.radiowaves.left.and.right")
                        Text(bleManager.isConnected ? "Start" : "Connect")
                            .font(.caption)
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
            }
        }
        .padding()
        .background(Color(.systemBackground))
        .cornerRadius(12)
        .shadow(radius: 2)
    }
    
    private func shareFile(_ url: URL) {
        let activityVC = UIActivityViewController(activityItems: [url], applicationActivities: nil)
        
        if let windowScene = UIApplication.shared.connectedScenes.first as? UIWindowScene,
           let window = windowScene.windows.first,
           let rootVC = window.rootViewController {
            rootVC.present(activityVC, animated: true)
        }
    }
}

// MARK: - Messages View

struct MessagesView: View {
    @EnvironmentObject var groundStation: GroundStationManager
    
    var body: some View {
        List {
            if groundStation.textMessages.isEmpty {
                Text("No messages received")
                    .foregroundColor(.secondary)
                    .frame(maxWidth: .infinity, alignment: .center)
                    .listRowBackground(Color.clear)
            } else {
                ForEach(groundStation.textMessages.reversed(), id: \.0) { date, message in
                    VStack(alignment: .leading, spacing: 4) {
                        Text(message)
                            .font(.body)
                        Text(date, style: .time)
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                    .padding(.vertical, 4)
                }
            }
        }
        .navigationTitle("Messages")
        .navigationBarTitleDisplayMode(.inline)
    }
}

// MARK: - Status Row Helper

struct StatusRow: View {
    let icon: String
    let label: String
    let value: String
    var color: Color = .primary
    
    var body: some View {
        HStack {
            Image(systemName: icon)
                .foregroundColor(.secondary)
                .frame(width: 20)
            Text(label)
                .foregroundColor(.secondary)
            Spacer()
            Text(value)
                .foregroundColor(color)
        }
        .font(.subheadline)
    }
}

#Preview {
    NavigationStack {
        StatusView()
            .environmentObject(GroundStationManager())
            .environmentObject(BLESerialManager())
            .environmentObject(LocationManager())
    }
}
