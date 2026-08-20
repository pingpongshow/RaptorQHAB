//
//  SondeHubSettingsView.swift
//  RaptorHabGS
//
//  Settings UI for SondeHub integration
//

import SwiftUI

// MARK: - SondeHub Settings View (popup)

struct SondeHubSettingsView: View {
    @ObservedObject var sondeHub = SondeHubManager.shared
    @Environment(\.dismiss) var dismiss
    
    var body: some View {
        VStack(spacing: 0) {
            // Header
            HStack {
                Image(systemName: "globe")
                    .foregroundColor(.blue)
                Text("SondeHub Integration")
                    .font(.title2.bold())
                Spacer()
                Button("Done") { dismiss() }
            }
            .padding()
            
            Divider()
            
            Form {
                // Enable/Disable
                Section {
                    Toggle("Enable SondeHub Upload", isOn: $sondeHub.config.enabled)
                        .toggleStyle(.switch)
                } footer: {
                    Text("Upload telemetry to SondeHub Amateur tracking platform for public tracking.")
                }
                
                // Callsigns
                Section("Identification") {
                    TextField("Your Callsign", text: $sondeHub.config.uploaderCallsign)
                        .textFieldStyle(.roundedBorder)
                        .help("Your amateur radio callsign (e.g., KD2NDR)")
                    
                    TextField("Payload Callsign", text: $sondeHub.config.payloadCallsign)
                        .textFieldStyle(.roundedBorder)
                        .help("Payload identifier (e.g., RAPTORQ-1)")
                }

                // Shown beside the station on the SondeHub map. Optional, but
                // an empty receiver looks abandoned to anyone deciding whether
                // its coverage means anything.
                Section("Station") {
                    TextField("Radio", text: $sondeHub.config.uploaderRadio)
                        .textFieldStyle(.roundedBorder)
                        .help("e.g. SX1262 ground station modem")

                    TextField("Antenna", text: $sondeHub.config.uploaderAntenna)
                        .textFieldStyle(.roundedBorder)
                        .help("e.g. 1/4 wave ground plane at 10 m")

                    TextField("Contact e-mail", text: $sondeHub.config.contactEmail)
                        .textFieldStyle(.roundedBorder)
                        .help("Optional. Shown to other chasers who want to reach you.")

                    Toggle("Mobile station (chase car)", isOn: $sondeHub.config.mobile)
                        .help("A car reporting as a fixed site drags a trail of "
                              + "stale positions across the map.")
                }
                
                // Upload Options
                Section("Upload Options") {
                    Toggle("Upload Telemetry", isOn: $sondeHub.config.uploadTelemetry)
                    
                    Toggle("Upload Images", isOn: $sondeHub.config.uploadImages)
                    
                    HStack {
                        Text("Upload Interval")
                        Spacer()
                        Picker("", selection: $sondeHub.config.uploadInterval) {
                            Text("1 sec").tag(1.0)
                            Text("5 sec").tag(5.0)
                            Text("10 sec").tag(10.0)
                            Text("30 sec").tag(30.0)
                            Text("60 sec").tag(60.0)
                        }
                        .frame(width: 100)
                    }
                }
                
                // Comment
                Section("Comment") {
                    Toggle("Include Comment", isOn: $sondeHub.config.includeComment)
                    
                    if sondeHub.config.includeComment {
                        TextField("Comment", text: $sondeHub.config.comment)
                            .textFieldStyle(.roundedBorder)
                    }
                }
                
                // Status
                Section("Status") {
                    HStack {
                        Circle()
                            .fill(statusColor)
                            .frame(width: 10, height: 10)
                        Text(sondeHub.config.isValid ? "Ready" : "Not configured")
                            .foregroundColor(.secondary)
                    }
                    
                    if sondeHub.config.enabled {
                        HStack {
                            Text("Last Upload:")
                            Spacer()
                            if let time = sondeHub.lastUploadTime {
                                Text(time, style: .relative)
                                    .foregroundColor(.secondary)
                            } else {
                                Text("Never")
                                    .foregroundColor(.secondary)
                            }
                        }
                        
                        HStack {
                            Text("Status:")
                            Spacer()
                            Text(sondeHub.lastUploadStatus)
                                .foregroundColor(.secondary)
                                .lineLimit(1)
                        }
                        
                        HStack {
                            Text("Uploads:")
                            Spacer()
                            Text("\(sondeHub.uploadCount)")
                                .foregroundColor(.green)
                            Text("Errors:")
                            Text("\(sondeHub.errorCount)")
                                .foregroundColor(sondeHub.errorCount > 0 ? .red : .secondary)
                        }
                        
                        Button("Reset Statistics") {
                            sondeHub.resetStats()
                        }
                        .buttonStyle(.borderless)
                    }
                }
                
                // Info
                Section("About SondeHub") {
                    VStack(alignment: .leading, spacing: 8) {
                        Label("Track your balloon at sondehub.org", systemImage: "safari")
                        Label("Share position with chase teams", systemImage: "person.2")
                        Label("Public tracking - no account needed", systemImage: "globe")
                        
                        Link(destination: URL(string: "https://amateur.sondehub.org")!) {
                            Label("Open SondeHub Amateur", systemImage: "arrow.up.right.square")
                        }
                    }
                    .font(.caption)
                }
            }
            .formStyle(.grouped)
        }
        .frame(width: 450, height: 650)
    }
    
    private var statusColor: Color {
        if !sondeHub.config.enabled {
            return .gray
        } else if sondeHub.config.isValid {
            return .green
        } else {
            return .orange
        }
    }
}

// MARK: - Sidebar Section

struct SondeHubSidebarSection: View {
    @ObservedObject var sondeHub = SondeHubManager.shared
    
    var body: some View {
        Section("SondeHub") {
            HStack {
                Circle()
                    .fill(statusColor)
                    .frame(width: 8, height: 8)
                
                Text(statusText)
                    .font(.caption)
                
                Spacer()
                
                Button {
                    sondeHub.showSettings = true
                } label: {
                    Image(systemName: "gear")
                }
                .buttonStyle(.borderless)
            }
            
            if sondeHub.config.enabled && sondeHub.config.isValid {
                HStack {
                    Image(systemName: "arrow.up.circle")
                        .foregroundColor(.green)
                        .font(.caption)
                    Text("\(sondeHub.uploadCount)")
                        .font(.caption.monospacedDigit())
                    
                    if sondeHub.errorCount > 0 {
                        Spacer()
                        Image(systemName: "exclamationmark.triangle")
                            .foregroundColor(.orange)
                            .font(.caption)
                        Text("\(sondeHub.errorCount)")
                            .font(.caption.monospacedDigit())
                            .foregroundColor(.orange)
                    }
                    
                    Spacer()
                    
                    if sondeHub.isUploading {
                        ProgressView()
                            .scaleEffect(0.6)
                    } else if let time = sondeHub.lastUploadTime {
                        Text(time, style: .relative)
                            .font(.caption2)
                            .foregroundColor(.secondary)
                    }
                }
            }
        }
    }
    
    private var statusColor: Color {
        if !sondeHub.config.enabled {
            return .gray
        } else if !sondeHub.config.isValid {
            return .orange
        } else if sondeHub.errorCount > 0 {
            return .yellow
        } else {
            return .green
        }
    }
    
    private var statusText: String {
        if !sondeHub.config.enabled {
            return "Disabled"
        } else if !sondeHub.config.isValid {
            return "Not configured"
        } else {
            return sondeHub.config.payloadCallsign
        }
    }
}

#Preview {
    SondeHubSettingsView()
}
