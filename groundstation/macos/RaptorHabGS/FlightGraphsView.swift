//
//  FlightGraphsView.swift
//  RaptorHabGS
//
//  Real-time flight graphs for altitude, speed, vertical speed, and RSSI
//

import SwiftUI
import Charts

struct FlightGraphsView: View {
    @EnvironmentObject var groundStation: GroundStationManager
    @State private var timeWindow: TimeInterval = 600  // 10 minutes default
    
    var body: some View {
        ScrollView {
            VStack(spacing: 20) {
                // Time window selector
                HStack {
                    Text("Time Window:")
                        .font(.headline)
                    Picker("", selection: $timeWindow) {
                        Text("5 min").tag(TimeInterval(300))
                        Text("10 min").tag(TimeInterval(600))
                        Text("30 min").tag(TimeInterval(1800))
                        Text("1 hour").tag(TimeInterval(3600))
                        Text("All").tag(TimeInterval(0))
                    }
                    .pickerStyle(.segmented)
                    .frame(width: 400)
                    
                    Spacer()
                    
                    if let latest = groundStation.latestTelemetry {
                        VStack(alignment: .trailing) {
                            Text("Latest: \(latest.timestamp, style: .time)")
                                .font(.caption)
                            Text("\(groundStation.telemetryHistory.count) points")
                                .font(.caption)
                                .foregroundColor(.secondary)
                        }
                    }
                }
                .padding(.horizontal)
                
                // Altitude chart
                GraphCard(title: "Altitude", unit: "m", color: .blue) {
                    AltitudeChart(data: filteredData, timeWindow: timeWindow)
                }
                
                // Speed chart
                GraphCard(title: "Ground Speed", unit: "m/s", color: .green) {
                    SpeedChart(data: filteredData, timeWindow: timeWindow)
                }
                
                // Vertical Speed chart
                GraphCard(title: "Vertical Speed", unit: "m/s", color: .orange) {
                    VerticalSpeedChart(data: filteredData, timeWindow: timeWindow)
                }
                
                // RSSI chart
                GraphCard(title: "Signal Strength (RSSI)", unit: "dBm", color: .purple) {
                    RSSIChart(data: filteredData, timeWindow: timeWindow)
                }
                
                // SNR chart
                GraphCard(title: "Signal-to-Noise Ratio (SNR)", unit: "dB", color: .cyan) {
                    SNRChart(data: filteredData, timeWindow: timeWindow)
                }
            }
            .padding()
        }
    }
    
    var filteredData: [TelemetryPoint] {
        guard timeWindow > 0 else { return groundStation.telemetryHistory }
        
        let cutoff = Date().addingTimeInterval(-timeWindow)
        return groundStation.telemetryHistory.filter { $0.timestamp >= cutoff }
    }
}

// MARK: - Graph Card Container

struct GraphCard<Content: View>: View {
    let title: String
    let unit: String
    let color: Color
    @ViewBuilder let content: () -> Content
    
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Circle()
                    .fill(color)
                    .frame(width: 10, height: 10)
                Text(title)
                    .font(.headline)
                Text("(\(unit))")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
            
            content()
                .frame(height: 200)
        }
        .padding()
        .background(Color(NSColor.controlBackgroundColor))
        .cornerRadius(12)
    }
}

// MARK: - Altitude Chart

struct AltitudeChart: View {
    let data: [TelemetryPoint]
    let timeWindow: TimeInterval
    
    var body: some View {
        if data.isEmpty {
            Text("No data")
                .foregroundColor(.secondary)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            Chart {
                ForEach(data, id: \.timestamp) { point in
                    LineMark(
                        x: .value("Time", point.timestamp),
                        y: .value("Altitude", point.altitude)
                    )
                    .foregroundStyle(.blue)
                    .interpolationMethod(.catmullRom)
                    
                    AreaMark(
                        x: .value("Time", point.timestamp),
                        y: .value("Altitude", point.altitude)
                    )
                    .foregroundStyle(.blue.opacity(0.1))
                    .interpolationMethod(.catmullRom)
                }
            }
            .chartYAxis {
                AxisMarks(position: .leading)
            }
            .chartXAxis {
                AxisMarks(values: .automatic(desiredCount: 6)) { value in
                    AxisGridLine()
                    AxisValueLabel(format: .dateTime.hour().minute())
                }
            }
            .chartYScale(domain: altitudeRange)
        }
    }
    
    var altitudeRange: ClosedRange<Double> {
        guard !data.isEmpty else { return 0...1000 }
        let minAlt = max(0, (data.map(\.altitude).min() ?? 0) - 100)
        let maxAlt = (data.map(\.altitude).max() ?? 1000) + 100
        return minAlt...maxAlt
    }
}

// MARK: - Speed Chart

struct SpeedChart: View {
    let data: [TelemetryPoint]
    let timeWindow: TimeInterval
    
    var body: some View {
        if data.isEmpty {
            Text("No data")
                .foregroundColor(.secondary)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            Chart {
                ForEach(data, id: \.timestamp) { point in
                    LineMark(
                        x: .value("Time", point.timestamp),
                        y: .value("Speed", point.speed)
                    )
                    .foregroundStyle(.green)
                    .interpolationMethod(.catmullRom)
                }
            }
            .chartYAxis {
                AxisMarks(position: .leading)
            }
            .chartXAxis {
                AxisMarks(values: .automatic(desiredCount: 6)) { value in
                    AxisGridLine()
                    AxisValueLabel(format: .dateTime.hour().minute())
                }
            }
        }
    }
}

// MARK: - Vertical Speed Chart

struct VerticalSpeedChart: View {
    let data: [TelemetryPoint]
    let timeWindow: TimeInterval
    
    var verticalSpeeds: [(Date, Double)] {
        guard data.count >= 2 else { return [] }
        
        var speeds: [(Date, Double)] = []
        for i in 1..<data.count {
            let dt = data[i].timestamp.timeIntervalSince(data[i-1].timestamp)
            guard dt > 0 else { continue }
            
            let dAlt = data[i].altitude - data[i-1].altitude
            let vSpeed = dAlt / dt
            speeds.append((data[i].timestamp, vSpeed))
        }
        return speeds
    }
    
    var body: some View {
        let speeds = verticalSpeeds
        
        if speeds.isEmpty {
            Text("Need more data")
                .foregroundColor(.secondary)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            Chart {
                // Zero reference line
                RuleMark(y: .value("Zero", 0))
                    .foregroundStyle(.gray.opacity(0.5))
                    .lineStyle(StrokeStyle(dash: [5, 5]))
                
                ForEach(speeds, id: \.0) { timestamp, speed in
                    LineMark(
                        x: .value("Time", timestamp),
                        y: .value("V Speed", speed)
                    )
                    .foregroundStyle(speed >= 0 ? .green : .orange)
                    .interpolationMethod(.catmullRom)
                }
            }
            .chartYAxis {
                AxisMarks(position: .leading)
            }
            .chartXAxis {
                AxisMarks(values: .automatic(desiredCount: 6)) { value in
                    AxisGridLine()
                    AxisValueLabel(format: .dateTime.hour().minute())
                }
            }
        }
    }
}

// MARK: - RSSI Chart

struct RSSIChart: View {
    let data: [TelemetryPoint]
    let timeWindow: TimeInterval
    
    var body: some View {
        if data.isEmpty {
            Text("No data")
                .foregroundColor(.secondary)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            Chart {
                ForEach(data, id: \.timestamp) { point in
                    LineMark(
                        x: .value("Time", point.timestamp),
                        y: .value("RSSI", point.rssi)
                    )
                    .foregroundStyle(.purple)
                    .interpolationMethod(.catmullRom)
                }
                
                // Signal quality bands
                RuleMark(y: .value("Good", -80))
                    .foregroundStyle(.green.opacity(0.3))
                    .lineStyle(StrokeStyle(dash: [5, 5]))
                    .annotation(position: .trailing) {
                        Text("Good")
                            .font(.caption2)
                            .foregroundColor(.green)
                    }
                
                RuleMark(y: .value("Fair", -100))
                    .foregroundStyle(.orange.opacity(0.3))
                    .lineStyle(StrokeStyle(dash: [5, 5]))
                    .annotation(position: .trailing) {
                        Text("Fair")
                            .font(.caption2)
                            .foregroundColor(.orange)
                    }
            }
            .chartYAxis {
                AxisMarks(position: .leading)
            }
            .chartXAxis {
                AxisMarks(values: .automatic(desiredCount: 6)) { value in
                    AxisGridLine()
                    AxisValueLabel(format: .dateTime.hour().minute())
                }
            }
            .chartYScale(domain: rssiRange)
        }
    }
    
    var rssiRange: ClosedRange<Double> {
        guard !data.isEmpty else { return -130...(-50) }
        let minRSSI = min(-130, (data.map { Double($0.rssi) }.min() ?? -130) - 10)
        let maxRSSI = max(-50, (data.map { Double($0.rssi) }.max() ?? -50) + 10)
        return minRSSI...maxRSSI
    }
}

// MARK: - SNR Chart

struct SNRChart: View {
    let data: [TelemetryPoint]
    let timeWindow: TimeInterval
    
    var body: some View {
        if data.isEmpty {
            Text("No data")
                .foregroundColor(.secondary)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            Chart {
                ForEach(data, id: \.timestamp) { point in
                    LineMark(
                        x: .value("Time", point.timestamp),
                        y: .value("SNR", point.snr)
                    )
                    .foregroundStyle(.cyan)
                    .interpolationMethod(.catmullRom)
                }
                
                // SNR quality bands
                RuleMark(y: .value("Good", 10))
                    .foregroundStyle(.green.opacity(0.3))
                    .lineStyle(StrokeStyle(dash: [5, 5]))
                    .annotation(position: .trailing) {
                        Text("Good")
                            .font(.caption2)
                            .foregroundColor(.green)
                    }
                
                RuleMark(y: .value("Fair", 5))
                    .foregroundStyle(.orange.opacity(0.3))
                    .lineStyle(StrokeStyle(dash: [5, 5]))
                    .annotation(position: .trailing) {
                        Text("Fair")
                            .font(.caption2)
                            .foregroundColor(.orange)
                    }
                
                // Zero line
                RuleMark(y: .value("Zero", 0))
                    .foregroundStyle(.gray.opacity(0.5))
                    .lineStyle(StrokeStyle(dash: [3, 3]))
            }
            .chartYAxis {
                AxisMarks(position: .leading)
            }
            .chartXAxis {
                AxisMarks(values: .automatic(desiredCount: 6)) { value in
                    AxisGridLine()
                    AxisValueLabel(format: .dateTime.hour().minute())
                }
            }
            .chartYScale(domain: snrRange)
        }
    }
    
    var snrRange: ClosedRange<Double> {
        guard !data.isEmpty else { return -10...20 }
        let minSNR = min(-10, (data.map { Double($0.snr) }.min() ?? -10) - 2)
        let maxSNR = max(20, (data.map { Double($0.snr) }.max() ?? 20) + 2)
        return minSNR...maxSNR
    }
}

// MARK: - Flight Statistics

struct FlightStatisticsView: View {
    @EnvironmentObject var groundStation: GroundStationManager
    
    var stats: FlightStats {
        FlightStats(from: groundStation.telemetryHistory)
    }
    
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Flight Statistics")
                .font(.headline)
            
            LazyVGrid(columns: [
                GridItem(.flexible()),
                GridItem(.flexible()),
                GridItem(.flexible()),
                GridItem(.flexible())
            ], spacing: 12) {
                StatCard(title: "Max Altitude", value: String(format: "%.0f m", stats.maxAltitude), icon: "arrow.up")
                StatCard(title: "Max Speed", value: String(format: "%.1f m/s", stats.maxSpeed), icon: "speedometer")
                StatCard(title: "Max Ascent", value: String(format: "%.1f m/s", stats.maxAscentRate), icon: "arrow.up.right")
                StatCard(title: "Max Descent", value: String(format: "%.1f m/s", stats.maxDescentRate), icon: "arrow.down.right")
                StatCard(title: "Distance", value: String(format: "%.1f km", stats.totalDistance / 1000), icon: "point.topleft.down.curvedto.point.bottomright.up")
                StatCard(title: "Flight Time", value: formatDuration(stats.flightDuration), icon: "clock")
                StatCard(title: "Avg RSSI", value: String(format: "%.0f dBm", stats.avgRSSI), icon: "antenna.radiowaves.left.and.right")
                StatCard(title: "Points", value: "\(stats.pointCount)", icon: "chart.dots.scatter")
            }
        }
        .padding()
        .background(Color(NSColor.controlBackgroundColor))
        .cornerRadius(12)
    }
    
    func formatDuration(_ seconds: TimeInterval) -> String {
        let hours = Int(seconds) / 3600
        let minutes = (Int(seconds) % 3600) / 60
        if hours > 0 {
            return "\(hours)h \(minutes)m"
        } else {
            return "\(minutes)m"
        }
    }
}

struct StatCard: View {
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
        .frame(maxWidth: .infinity)
        .padding(.vertical, 8)
    }
}

struct FlightStats {
    let maxAltitude: Double
    let maxSpeed: Double
    let maxAscentRate: Double
    let maxDescentRate: Double
    let totalDistance: Double
    let flightDuration: TimeInterval
    let avgRSSI: Double
    let pointCount: Int
    
    init(from data: [TelemetryPoint]) {
        pointCount = data.count
        
        guard data.count >= 2 else {
            maxAltitude = 0
            maxSpeed = 0
            maxAscentRate = 0
            maxDescentRate = 0
            totalDistance = 0
            flightDuration = 0
            avgRSSI = 0
            return
        }
        
        maxAltitude = data.map(\.altitude).max() ?? 0
        maxSpeed = data.map(\.speed).max() ?? 0
        
        // Calculate vertical speeds and distance
        var ascents: [Double] = []
        var descents: [Double] = []
        var distance: Double = 0
        
        for i in 1..<data.count {
            let dt = data[i].timestamp.timeIntervalSince(data[i-1].timestamp)
            guard dt > 0 else { continue }
            
            let dAlt = data[i].altitude - data[i-1].altitude
            let vSpeed = dAlt / dt
            
            if vSpeed > 0 {
                ascents.append(vSpeed)
            } else {
                descents.append(abs(vSpeed))
            }
            
            // Haversine distance
            let lat1 = data[i-1].latitude * .pi / 180
            let lat2 = data[i].latitude * .pi / 180
            let dLat = lat2 - lat1
            let dLon = (data[i].longitude - data[i-1].longitude) * .pi / 180
            
            let a = sin(dLat/2) * sin(dLat/2) + cos(lat1) * cos(lat2) * sin(dLon/2) * sin(dLon/2)
            let c = 2 * atan2(sqrt(a), sqrt(1-a))
            distance += 6371000 * c
        }
        
        maxAscentRate = ascents.max() ?? 0
        maxDescentRate = descents.max() ?? 0
        totalDistance = distance
        
        if let first = data.first, let last = data.last {
            flightDuration = last.timestamp.timeIntervalSince(first.timestamp)
        } else {
            flightDuration = 0
        }
        
        avgRSSI = data.map { Double($0.rssi) }.reduce(0, +) / Double(data.count)
    }
}

#Preview {
    FlightGraphsView()
        .environmentObject(GroundStationManager())
        .frame(width: 800, height: 900)
}
