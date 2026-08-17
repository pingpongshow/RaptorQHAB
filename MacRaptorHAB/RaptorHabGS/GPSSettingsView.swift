//
//  GPSSettingsView.swift
//  RaptorHabGS
//
//  UI for configuring and monitoring external GPS
//

import SwiftUI

// MARK: - GPS Signal Indicator

struct GPSSignalIndicator: View {
    @ObservedObject var gpsManager = GPSManager.shared
    @State private var refreshTrigger = false
    
    var satellites: Int {
        gpsManager.currentPosition?.satellites ?? 0
    }
    
    var isConnected: Bool {
        gpsManager.isConnected
    }
    
    var isReceivingData: Bool {
        gpsManager.isReceivingData
    }
    
    var hasValidFix: Bool {
        gpsManager.currentPosition?.isValid ?? false
    }
    
    var statusColor: Color {
        if !isConnected {
            return .gray
        } else if !isReceivingData {
            return .red  // Connected but no NMEA data
        } else if hasValidFix {
            return .green  // Receiving data with fix
        } else {
            return .orange  // Receiving data but no fix yet
        }
    }
    
    var body: some View {
        HStack(spacing: 2) {
            // Satellite icon with activity indicator
            Image(systemName: isReceivingData ? "location.fill" : "location.slash")
                .font(.caption2)
                .foregroundColor(statusColor)
            
            // Signal bars (max 5 bars for 10+ satellites)
            HStack(spacing: 1) {
                ForEach(0..<5, id: \.self) { i in
                    RoundedRectangle(cornerRadius: 1)
                        .fill(barColor(for: i))
                        .frame(width: 3, height: CGFloat(4 + i * 2))
                }
            }
            .frame(height: 14, alignment: .bottom)
            
            // Satellite count
            if isConnected && satellites > 0 {
                Text("\(satellites)")
                    .font(.caption2.monospacedDigit())
                    .foregroundColor(.secondary)
            }
        }
        .help(statusTooltip)
        .onAppear {
            // Refresh periodically to update isReceivingData status
            Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { _ in
                refreshTrigger.toggle()
            }
        }
        .id(refreshTrigger) // Force refresh
    }
    
    private func barColor(for index: Int) -> Color {
        guard isConnected && isReceivingData else { return .gray.opacity(0.3) }
        
        // Each bar represents ~2 satellites
        // 0-1 sats = 0 bars, 2-3 = 1 bar, 4-5 = 2 bars, etc.
        let barsToShow = min(5, (satellites + 1) / 2)
        
        if index < barsToShow {
            return statusColor
        } else {
            return .gray.opacity(0.3)
        }
    }
    
    private var statusTooltip: String {
        if !isConnected {
            return "GPS: Not connected"
        } else if !isReceivingData {
            return "GPS: No data received"
        } else if hasValidFix {
            return "GPS: \(satellites) satellites, valid fix"
        } else {
            return "GPS: Acquiring fix..."
        }
    }
}

// MARK: - Compact GPS Indicator (for toolbar)

struct GPSToolbarIndicator: View {
    @ObservedObject var gpsManager = GPSManager.shared
    
    var body: some View {
        if gpsManager.isConnected {
            GPSSignalIndicator()
                .padding(.horizontal, 4)
        }
    }
}

struct GPSSettingsView: View {
    @ObservedObject var gpsManager = GPSManager.shared
    
    var body: some View {
        Section("Ground Station GPS") {
            // Signal indicator
            HStack {
                GPSSignalIndicator()
                Spacer()
                Text(gpsManager.statusMessage)
                    .font(.caption)
                    .foregroundColor(.secondary)
                    .lineLimit(1)
            }
            
            // Port selection
            Picker("Port", selection: $gpsManager.selectedPort) {
                Text("Select...").tag("")
                ForEach(gpsManager.availablePorts, id: \.self) { port in
                    Text(port.components(separatedBy: "/").last ?? port)
                        .tag(port)
                }
            }
            .disabled(gpsManager.isConnected)
            
            // Baud rate
            Picker("Baud", selection: $gpsManager.baudRate) {
                Text("4800").tag(4800)
                Text("9600").tag(9600)
                Text("19200").tag(19200)
                Text("38400").tag(38400)
                Text("57600").tag(57600)
                Text("115200").tag(115200)
            }
            .pickerStyle(.menu)
            .disabled(gpsManager.isConnected)
            
            // Connect/Disconnect buttons
            HStack {
                Button("Refresh") {
                    gpsManager.refreshPorts()
                }
                .buttonStyle(.borderless)
                .disabled(gpsManager.isConnected)
                
                Spacer()
                
                if gpsManager.isConnected {
                    Button("Disconnect") {
                        gpsManager.disconnect()
                    }
                    .buttonStyle(.borderless)
                    .foregroundColor(.red)
                } else {
                    Button("Connect") {
                        gpsManager.connect()
                    }
                    .buttonStyle(.borderless)
                    .disabled(gpsManager.selectedPort.isEmpty)
                }
            }
            
            // Position display
            if let pos = gpsManager.currentPosition, pos.isValid {
                Divider()
                
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Image(systemName: "location.fill")
                            .foregroundColor(.blue)
                        Text(String(format: "%.6f, %.6f", pos.latitude, pos.longitude))
                            .font(.caption.monospacedDigit())
                    }
                    
                    HStack {
                        Image(systemName: "arrow.up")
                            .foregroundColor(.green)
                        Text(String(format: "%.1f m", pos.altitude))
                            .font(.caption.monospacedDigit())
                        
                        Spacer()
                        
                        Image(systemName: "satellite.fill")
                            .foregroundColor(.orange)
                        Text("\(pos.satellites)")
                            .font(.caption.monospacedDigit())
                    }
                    
                    HStack {
                        Text(pos.fixQuality.description)
                            .font(.caption2)
                            .foregroundColor(.secondary)
                        
                        Spacer()
                        
                        Text(String(format: "HDOP: %.1f", pos.hdop))
                            .font(.caption2)
                            .foregroundColor(.secondary)
                    }
                }
            }
        }
    }
}

// MARK: - Bearing/Distance Display

struct BearingDistanceView: View {
    @ObservedObject var gpsManager = GPSManager.shared
    
    var body: some View {
        if let bearing = gpsManager.bearingToPayload {
            Section("To Payload") {
                // Compass bearing
                HStack {
                    CompassView(bearing: bearing.bearing)
                        .frame(width: 50, height: 50)
                    
                    VStack(alignment: .leading, spacing: 4) {
                        HStack {
                            Text(String(format: "%.0f°", bearing.bearing))
                                .font(.title2.monospacedDigit().bold())
                            Text(bearing.bearingCardinal)
                                .font(.caption)
                                .foregroundColor(.secondary)
                        }
                        
                        Text(bearing.distanceFormatted)
                            .font(.subheadline.monospacedDigit())
                        
                        Text(bearing.distanceMiles)
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                    
                    Spacer()
                    
                    VStack(alignment: .trailing, spacing: 4) {
                        HStack {
                            Image(systemName: bearing.elevation >= 0 ? "arrow.up.right" : "arrow.down.right")
                                .foregroundColor(bearing.elevation >= 0 ? .green : .orange)
                            Text(String(format: "%.1f°", bearing.elevation))
                                .font(.caption.monospacedDigit())
                        }
                        
                        Text(String(format: "%+.0f m", bearing.altitudeDiff))
                            .font(.caption.monospacedDigit())
                            .foregroundColor(.secondary)
                    }
                }
            }
        }
    }
}

// MARK: - Compass View

struct CompassView: View {
    let bearing: Double
    
    var body: some View {
        ZStack {
            // Compass ring
            Circle()
                .stroke(Color.gray.opacity(0.3), lineWidth: 2)
            
            // Cardinal points
            ForEach([0, 90, 180, 270], id: \.self) { angle in
                let label = ["N", "E", "S", "W"][angle / 90]
                Text(label)
                    .font(.system(size: 8, weight: .bold))
                    .foregroundColor(angle == 0 ? .red : .secondary)
                    .offset(y: -18)
                    .rotationEffect(.degrees(Double(angle)))
            }
            
            // Bearing arrow
            VStack(spacing: 0) {
                Triangle()
                    .fill(Color.red)
                    .frame(width: 10, height: 15)
                
                Rectangle()
                    .fill(Color.red)
                    .frame(width: 3, height: 10)
            }
            .offset(y: -5)
            .rotationEffect(.degrees(bearing))
            
            // Center dot
            Circle()
                .fill(Color.primary)
                .frame(width: 4, height: 4)
        }
    }
}

struct Triangle: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        path.move(to: CGPoint(x: rect.midX, y: rect.minY))
        path.addLine(to: CGPoint(x: rect.maxX, y: rect.maxY))
        path.addLine(to: CGPoint(x: rect.minX, y: rect.maxY))
        path.closeSubpath()
        return path
    }
}

#Preview {
    List {
        GPSSettingsView()
        BearingDistanceView()
    }
    .frame(width: 250)
}
