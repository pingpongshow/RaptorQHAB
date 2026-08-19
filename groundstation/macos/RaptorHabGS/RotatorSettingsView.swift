//
//  RotatorSettingsView.swift
//  RaptorHabGS
//
//  UI for antenna rotator control
//

import SwiftUI

// MARK: - Rotator Settings View (popup)

struct RotatorSettingsView: View {
    @ObservedObject var rotator = RotatorManager.shared
    @Environment(\.dismiss) var dismiss
    
    var body: some View {
        VStack(spacing: 0) {
            // Header
            HStack {
                Image(systemName: "antenna.radiowaves.left.and.right.circle")
                    .foregroundColor(.purple)
                Text("Antenna Rotator")
                    .font(.title2.bold())
                Spacer()
                Button("Done") { dismiss() }
            }
            .padding()
            
            Divider()
            
            Form {
                // Enable/Disable
                Section {
                    Toggle("Enable Rotator Control", isOn: $rotator.config.enabled)
                        .toggleStyle(.switch)
                        .onChange(of: rotator.config.enabled) { _, enabled in
                            if enabled {
                                rotator.connect()
                            } else {
                                rotator.disconnect()
                            }
                        }
                } footer: {
                    Text("Connect to rotctld server for antenna tracking")
                }
                
                // Connection settings
                Section("Connection") {
                    HStack {
                        Text("Host")
                        Spacer()
                        TextField("IP Address", text: $rotator.config.host)
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 150)
                    }
                    
                    HStack {
                        Text("Port")
                        Spacer()
                        TextField("Port", value: $rotator.config.port, format: .number)
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 80)
                    }
                    
                    HStack {
                        if rotator.isConnected {
                            Button("Disconnect") {
                                rotator.disconnect()
                            }
                            .buttonStyle(.borderedProminent)
                            .tint(.red)
                        } else {
                            Button("Connect") {
                                rotator.connect()
                            }
                            .buttonStyle(.borderedProminent)
                            .disabled(!rotator.config.enabled)
                        }
                        
                        Spacer()
                        
                        if let error = rotator.connectionError {
                            Text(error)
                                .font(.caption)
                                .foregroundColor(.red)
                        }
                    }
                }
                
                // Tracking settings
                Section("Tracking") {
                    Toggle("Auto-track payload", isOn: $rotator.config.autoTrack)
                    
                    HStack {
                        Text("Update interval")
                        Spacer()
                        Picker("", selection: $rotator.config.updateInterval) {
                            Text("1 sec").tag(1.0)
                            Text("2 sec").tag(2.0)
                            Text("5 sec").tag(5.0)
                            Text("10 sec").tag(10.0)
                        }
                        .frame(width: 100)
                    }
                    
                    HStack {
                        if rotator.config.autoTrack {
                            Button("Start Tracking") {
                                rotator.startTracking()
                            }
                            .disabled(!rotator.isConnected)
                            
                            Button("Stop Tracking") {
                                rotator.stopTracking()
                            }
                        }
                    }
                }
                
                // Manual control
                Section("Manual Control") {
                    if let pos = rotator.currentPosition {
                        HStack {
                            Text("Current:")
                            Spacer()
                            Text(String(format: "Az: %.1f° (%@)  El: %.1f°", 
                                       pos.azimuth, pos.azimuthCardinal, pos.elevation))
                                .font(.caption.monospacedDigit())
                        }
                    }
                    
                    if let target = rotator.targetPosition {
                        HStack {
                            Text("Target:")
                            Spacer()
                            Text(String(format: "Az: %.1f°  El: %.1f°", 
                                       target.azimuth, target.elevation))
                                .font(.caption.monospacedDigit())
                            if rotator.isMoving {
                                ProgressView()
                                    .scaleEffect(0.6)
                            }
                        }
                    }
                    
                    HStack {
                        Button("Stop") {
                            rotator.stop()
                        }
                        .buttonStyle(.borderedProminent)
                        .tint(.orange)
                        .disabled(!rotator.isConnected)
                        
                        Spacer()
                        
                        Button("Park") {
                            rotator.park()
                        }
                        .disabled(!rotator.isConnected)
                    }
                }
                
                // Park position
                Section("Park Position") {
                    HStack {
                        Text("Azimuth")
                        Spacer()
                        TextField("", value: $rotator.config.parkAzimuth, format: .number)
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 80)
                        Text("°")
                            .foregroundColor(.secondary)
                    }
                    
                    HStack {
                        Text("Elevation")
                        Spacer()
                        TextField("", value: $rotator.config.parkElevation, format: .number)
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 80)
                        Text("°")
                            .foregroundColor(.secondary)
                    }
                }
                
                // Status
                Section("Status") {
                    HStack {
                        Circle()
                            .fill(statusColor)
                            .frame(width: 10, height: 10)
                        Text(statusText)
                            .foregroundColor(.secondary)
                    }
                    
                    HStack {
                        Text("Commands sent:")
                        Spacer()
                        Text("\(rotator.commandsSent)")
                            .foregroundColor(.secondary)
                    }
                    
                    if let lastTime = rotator.lastCommandTime {
                        HStack {
                            Text("Last command:")
                            Spacer()
                            Text(lastTime, style: .relative)
                                .foregroundColor(.secondary)
                        }
                    }
                }
            }
            .formStyle(.grouped)
        }
        .frame(width: 450, height: 650)
    }
    
    private var statusColor: Color {
        if !rotator.config.enabled {
            return .gray
        } else if rotator.isConnected {
            return rotator.isMoving ? .orange : .green
        } else {
            return .red
        }
    }
    
    private var statusText: String {
        if !rotator.config.enabled {
            return "Disabled"
        } else if rotator.isConnected {
            return rotator.isMoving ? "Moving" : "Connected"
        } else {
            return "Disconnected"
        }
    }
}

// MARK: - Sidebar Section

struct RotatorSidebarSection: View {
    @ObservedObject var rotator = RotatorManager.shared
    
    var body: some View {
        Section("Antenna Rotator") {
            // Connection status and settings button
            HStack {
                Circle()
                    .fill(statusColor)
                    .frame(width: 8, height: 8)
                
                Text(statusText)
                    .font(.caption)
                
                Spacer()
                
                Button {
                    rotator.showSettings = true
                } label: {
                    Image(systemName: "gear")
                }
                .buttonStyle(.borderless)
            }
            
            // Position display when connected
            if rotator.isConnected {
                if let pos = rotator.currentPosition {
                    HStack {
                        // Compass indicator
                        ZStack {
                            Circle()
                                .stroke(Color.secondary.opacity(0.3), lineWidth: 1)
                                .frame(width: 32, height: 32)
                            
                            // Direction indicator
                            Image(systemName: "arrowtriangle.up.fill")
                                .font(.caption2)
                                .foregroundColor(.purple)
                                .rotationEffect(.degrees(pos.azimuth))
                        }
                        
                        VStack(alignment: .leading, spacing: 2) {
                            Text(String(format: "Az: %.0f° %@", pos.azimuth, pos.azimuthCardinal))
                                .font(.caption.monospacedDigit())
                            Text(String(format: "El: %.0f°", pos.elevation))
                                .font(.caption.monospacedDigit())
                        }
                        
                        Spacer()
                        
                        if rotator.isMoving {
                            ProgressView()
                                .scaleEffect(0.6)
                        }
                    }
                }
                
                // Quick controls
                HStack {
                    Toggle("Track", isOn: Binding(
                        get: { rotator.config.autoTrack },
                        set: { enabled in
                            rotator.config.autoTrack = enabled
                            if enabled {
                                rotator.startTracking()
                            } else {
                                rotator.stopTracking()
                            }
                        }
                    ))
                    .toggleStyle(.switch)
                    .controlSize(.small)
                    
                    Spacer()
                    
                    Button("Park") {
                        rotator.park()
                    }
                    .buttonStyle(.borderless)
                    .font(.caption)
                }
            }
        }
    }
    
    private var statusColor: Color {
        if !rotator.config.enabled {
            return .gray
        } else if rotator.isConnected {
            return rotator.isMoving ? .orange : .green
        } else {
            return .red
        }
    }
    
    private var statusText: String {
        if !rotator.config.enabled {
            return "Disabled"
        } else if rotator.isConnected {
            if let pos = rotator.currentPosition {
                return String(format: "%.0f° %@", pos.azimuth, pos.azimuthCardinal)
            }
            return "Connected"
        } else {
            return "Not connected"
        }
    }
}

#Preview {
    RotatorSettingsView()
}
