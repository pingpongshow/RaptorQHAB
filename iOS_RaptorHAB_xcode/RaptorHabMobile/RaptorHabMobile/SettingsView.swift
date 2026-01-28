//
//  SettingsView.swift
//  RaptorHabMobile
//
//  Settings for Bluetooth connection and RF configuration
//

import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var groundStation: GroundStationManager
    @EnvironmentObject var bleManager: BLESerialManager
    @EnvironmentObject var locationManager: LocationManager
    @Environment(\.dismiss) var dismiss
    
    @State private var frequency: String = ""
    @State private var bitrate: String = ""
    @State private var deviation: String = ""
    @State private var bandwidth: String = ""
    @State private var preamble: String = ""
    @State private var isConfiguring = false
    @State private var configResult: String?
    
    var body: some View {
        NavigationStack {
            Form {
                // Bluetooth Section
                Section {
                    HStack {
                        Label("Status", systemImage: "antenna.radiowaves.left.and.right")
                        Spacer()
                        if bleManager.isScanning {
                            ProgressView()
                                .padding(.trailing, 8)
                        }
                        Text(bleManager.connectionStatus)
                            .foregroundColor(bleManager.isConnected ? .green : .secondary)
                    }
                    
                    if bleManager.isConnected {
                        HStack {
                            Text("Device")
                            Spacer()
                            Text("RaptorModem")
                                .foregroundColor(.secondary)
                        }
                        
                        HStack {
                            Text("Signal")
                            Spacer()
                            Text("\(Int(bleManager.lastRSSI)) dBm")
                                .foregroundColor(.secondary)
                        }
                        
                        Button("Disconnect", role: .destructive) {
                            bleManager.disconnect()
                        }
                    } else {
                        if bleManager.isScanning {
                            Button("Stop Scanning") {
                                bleManager.stopScanning()
                            }
                        } else {
                            Button("Scan for RaptorModem") {
                                bleManager.startScanning()
                            }
                        }
                    }
                    
                    // Discovered devices
                    if !bleManager.discoveredDevices.isEmpty {
                        ForEach(bleManager.discoveredDevices) { device in
                            Button {
                                bleManager.connect(to: device)
                            } label: {
                                HStack {
                                    Image(systemName: "wave.3.right")
                                        .foregroundColor(.blue)
                                    Text(device.name)
                                    Spacer()
                                    Text("\(device.rssi) dBm")
                                        .foregroundColor(.secondary)
                                        .font(.caption)
                                    Image(systemName: "chevron.right")
                                        .foregroundColor(.secondary)
                                }
                            }
                            .foregroundColor(.primary)
                        }
                    }
                } header: {
                    Text("Bluetooth Connection")
                } footer: {
                    Text("Connect to RaptorModem via Bluetooth LE. No pairing PIN required.")
                }
                
                // RF Configuration Section
                Section {
                    HStack {
                        Text("Frequency (MHz)")
                        Spacer()
                        TextField("915.0", text: $frequency)
                            .keyboardType(.decimalPad)
                            .multilineTextAlignment(.trailing)
                            .frame(width: 100)
                    }
                    
                    HStack {
                        Text("Bitrate (kbps)")
                        Spacer()
                        TextField("96.0", text: $bitrate)
                            .keyboardType(.decimalPad)
                            .multilineTextAlignment(.trailing)
                            .frame(width: 100)
                    }
                    
                    HStack {
                        Text("Deviation (kHz)")
                        Spacer()
                        TextField("50.0", text: $deviation)
                            .keyboardType(.decimalPad)
                            .multilineTextAlignment(.trailing)
                            .frame(width: 100)
                    }
                    
                    HStack {
                        Text("Bandwidth (kHz)")
                        Spacer()
                        TextField("467.0", text: $bandwidth)
                            .keyboardType(.decimalPad)
                            .multilineTextAlignment(.trailing)
                            .frame(width: 100)
                    }
                    
                    HStack {
                        Text("Preamble (bits)")
                        Spacer()
                        TextField("32", text: $preamble)
                            .keyboardType(.numberPad)
                            .multilineTextAlignment(.trailing)
                            .frame(width: 100)
                    }
                    
                    // Presets
                    Menu {
                        Button("Default (915 MHz, 96 kbps)") {
                            applyPreset(freq: 915.0, bitrate: 96.0, dev: 50.0, bw: 467.0, pre: 32)
                        }
                        Button("Low Rate (915 MHz, 48 kbps)") {
                            applyPreset(freq: 915.0, bitrate: 48.0, dev: 25.0, bw: 234.0, pre: 32)
                        }
                        Button("EU Band (868 MHz, 96 kbps)") {
                            applyPreset(freq: 868.0, bitrate: 96.0, dev: 50.0, bw: 467.0, pre: 32)
                        }
                    } label: {
                        Label("Load Preset", systemImage: "list.bullet")
                    }
                    
                    // Configure button
                    Button {
                        configureModem()
                    } label: {
                        HStack {
                            Spacer()
                            if isConfiguring {
                                ProgressView()
                                    .padding(.trailing, 8)
                            }
                            Text("Apply Configuration")
                            Spacer()
                        }
                    }
                    .disabled(!bleManager.isConnected || isConfiguring)
                    
                    if let result = configResult {
                        HStack {
                            Image(systemName: result.contains("OK") ? "checkmark.circle.fill" : "xmark.circle.fill")
                                .foregroundColor(result.contains("OK") ? .green : .red)
                            Text(result)
                                .font(.caption)
                        }
                    }
                } header: {
                    Text("RF Configuration")
                } footer: {
                    Text("Configure the modem's RF parameters. These must match the airborne unit settings.")
                }
                
                // GPS Section
                Section {
                    HStack {
                        Text("Permission")
                        Spacer()
                        Text(locationStatusText)
                            .foregroundColor(locationManager.authorizationStatus == .authorizedWhenInUse || 
                                           locationManager.authorizationStatus == .authorizedAlways ? .green : .secondary)
                    }
                    
                    if locationManager.authorizationStatus == .notDetermined {
                        Button("Request Location Permission") {
                            locationManager.requestPermission()
                        }
                    }
                    
                    HStack {
                        Text("GPS Status")
                        Spacer()
                        Text(locationManager.isUpdating ? "Active" : "Inactive")
                            .foregroundColor(locationManager.isUpdating ? .green : .secondary)
                    }
                    
                    if locationManager.authorizationStatus == .authorizedWhenInUse || 
                       locationManager.authorizationStatus == .authorizedAlways {
                        Toggle("Location Updates", isOn: Binding(
                            get: { locationManager.isUpdating },
                            set: { newValue in
                                if newValue {
                                    locationManager.startUpdating()
                                } else {
                                    locationManager.stopUpdating()
                                }
                            }
                        ))
                    }
                } header: {
                    Text("GPS (Internal)")
                } footer: {
                    Text("Uses the device's built-in GPS for ground station position.")
                }
                
                // Data Management
                Section {
                    Button("Export Telemetry CSV") {
                        if let url = groundStation.exportTelemetryCSV() {
                            // Share the file
                            let activityVC = UIActivityViewController(activityItems: [url], applicationActivities: nil)
                            
                            if let windowScene = UIApplication.shared.connectedScenes.first as? UIWindowScene,
                               let window = windowScene.windows.first,
                               let rootVC = window.rootViewController {
                                rootVC.present(activityVC, animated: true)
                            }
                        }
                    }
                    .disabled(groundStation.telemetryHistory.isEmpty)
                    
                    Button("Clear Telemetry", role: .destructive) {
                        groundStation.clearTelemetry()
                    }
                    .disabled(groundStation.telemetryHistory.isEmpty)
                    
                    Button("Clear Images", role: .destructive) {
                        groundStation.clearImages()
                    }
                    .disabled(groundStation.completedImages.isEmpty && groundStation.pendingImages.isEmpty)
                } header: {
                    Text("Data Management")
                }
                
                // About Section
                Section {
                    HStack {
                        Text("Version")
                        Spacer()
                        Text("1.0 (BLE)")
                            .foregroundColor(.secondary)
                    }
                    
                    HStack {
                        Text("Protocol")
                        Spacer()
                        Text("RaptorHab v1.0")
                            .foregroundColor(.secondary)
                    }
                } header: {
                    Text("About")
                }
            }
            .navigationTitle("Settings")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") {
                        dismiss()
                    }
                }
            }
            .onAppear {
                loadCurrentConfig()
            }
        }
    }
    
    private var locationStatusText: String {
        switch locationManager.authorizationStatus {
        case .notDetermined: return "Not Determined"
        case .restricted: return "Restricted"
        case .denied: return "Denied"
        case .authorizedWhenInUse: return "When In Use"
        case .authorizedAlways: return "Always"
        @unknown default: return "Unknown"
        }
    }
    
    private func loadCurrentConfig() {
        let config = groundStation.modemConfig
        frequency = String(format: "%.1f", config.frequencyMHz)
        bitrate = String(format: "%.1f", config.bitrateKbps)
        deviation = String(format: "%.1f", config.deviationKHz)
        bandwidth = String(format: "%.1f", config.bandwidthKHz)
        preamble = String(config.preambleBits)
    }
    
    private func applyPreset(freq: Double, bitrate: Double, dev: Double, bw: Double, pre: Int) {
        self.frequency = String(format: "%.1f", freq)
        self.bitrate = String(format: "%.1f", bitrate)
        self.deviation = String(format: "%.1f", dev)
        self.bandwidth = String(format: "%.1f", bw)
        self.preamble = String(pre)
    }
    
    private func configureModem() {
        guard let freq = Double(frequency),
              let br = Double(bitrate),
              let dev = Double(deviation),
              let bw = Double(bandwidth),
              let pre = Int(preamble) else {
            configResult = "Invalid parameter values"
            return
        }
        
        let config = ModemConfig(
            frequencyMHz: freq,
            bitrateKbps: br,
            deviationKHz: dev,
            bandwidthKHz: bw,
            preambleBits: pre
        )
        
        isConfiguring = true
        configResult = nil
        
        bleManager.configureModem(config) { success, error in
            Task { @MainActor in
                isConfiguring = false
                if success {
                    configResult = "OK - Configuration applied"
                    groundStation.modemConfig = config
                } else {
                    configResult = error ?? "Configuration failed"
                }
            }
        }
    }
}

#Preview {
    SettingsView()
        .environmentObject(GroundStationManager())
        .environmentObject(BLESerialManager())
        .environmentObject(LocationManager())
}
