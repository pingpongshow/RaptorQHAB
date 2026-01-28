//
//  GraphsView.swift
//  RaptorHabMobile
//
//  Telemetry graphs showing altitude, speed, battery, and temperature over time
//

import SwiftUI
import Charts

struct GraphsView: View {
    @EnvironmentObject var groundStation: GroundStationManager
    @State private var selectedGraph: GraphType = .altitude
    
    enum GraphType: String, CaseIterable {
        case altitude = "Altitude"
        case speed = "Speed"
        case battery = "Battery"
        case temperature = "Temperature"
        case rssi = "Signal"
    }
    
    var body: some View {
        VStack(spacing: 0) {
            // Graph selector
            Picker("Graph", selection: $selectedGraph) {
                ForEach(GraphType.allCases, id: \.self) { type in
                    Text(type.rawValue).tag(type)
                }
            }
            .pickerStyle(.segmented)
            .padding()
            
            // Graph view
            if groundStation.telemetryHistory.isEmpty {
                Spacer()
                VStack(spacing: 16) {
                    Image(systemName: "chart.line.uptrend.xyaxis")
                        .font(.system(size: 48))
                        .foregroundColor(.secondary)
                    Text("No Data")
                        .font(.headline)
                    Text("Telemetry data will appear here once received")
                        .font(.subheadline)
                        .foregroundColor(.secondary)
                        .multilineTextAlignment(.center)
                }
                .padding(40)
                Spacer()
            } else {
                graphContent
                    .padding()
            }
        }
        .navigationTitle("Graphs")
    }
    
    @ViewBuilder
    private var graphContent: some View {
        switch selectedGraph {
        case .altitude:
            AltitudeChart(data: groundStation.telemetryHistory)
        case .speed:
            SpeedChart(data: groundStation.telemetryHistory)
        case .battery:
            BatteryChart(data: groundStation.telemetryHistory)
        case .temperature:
            TemperatureChart(data: groundStation.telemetryHistory)
        case .rssi:
            RSSIChart(data: groundStation.telemetryHistory)
        }
    }
}

// MARK: - Altitude Chart

struct AltitudeChart: View {
    let data: [TelemetryPoint]
    
    var body: some View {
        VStack(alignment: .leading) {
            Text("Altitude over Time")
                .font(.headline)
            
            if let max = data.map({ $0.altitude }).max(),
               let min = data.map({ $0.altitude }).min() {
                HStack {
                    Text("Max: \(String(format: "%.0f m", max))")
                    Spacer()
                    Text("Min: \(String(format: "%.0f m", min))")
                }
                .font(.caption)
                .foregroundColor(.secondary)
            }
            
            Chart(data) { point in
                LineMark(
                    x: .value("Time", point.timestamp),
                    y: .value("Altitude", point.altitude)
                )
                .foregroundStyle(.blue)
                
                AreaMark(
                    x: .value("Time", point.timestamp),
                    y: .value("Altitude", point.altitude)
                )
                .foregroundStyle(.blue.opacity(0.1))
            }
            .chartYAxisLabel("Meters")
            .chartXAxis {
                AxisMarks(values: .automatic) { value in
                    AxisGridLine()
                    AxisValueLabel(format: .dateTime.hour().minute())
                }
            }
        }
    }
}

// MARK: - Speed Chart

struct SpeedChart: View {
    let data: [TelemetryPoint]
    
    var body: some View {
        VStack(alignment: .leading) {
            Text("Speed over Time")
                .font(.headline)
            
            if let max = data.map({ $0.speed }).max() {
                Text("Max: \(String(format: "%.1f m/s", max))")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
            
            Chart(data) { point in
                LineMark(
                    x: .value("Time", point.timestamp),
                    y: .value("Speed", point.speed)
                )
                .foregroundStyle(.green)
            }
            .chartYAxisLabel("m/s")
            .chartXAxis {
                AxisMarks(values: .automatic) { value in
                    AxisGridLine()
                    AxisValueLabel(format: .dateTime.hour().minute())
                }
            }
        }
    }
}

// MARK: - Battery Chart

struct BatteryChart: View {
    let data: [TelemetryPoint]
    
    var body: some View {
        VStack(alignment: .leading) {
            Text("Battery Voltage over Time")
                .font(.headline)
            
            if let current = data.last?.batteryVoltage {
                Text("Current: \(String(format: "%.2f V", current))")
                    .font(.caption)
                    .foregroundColor(batteryColor(current))
            }
            
            Chart(data) { point in
                LineMark(
                    x: .value("Time", point.timestamp),
                    y: .value("Voltage", point.batteryVoltage)
                )
                .foregroundStyle(batteryGradient)
            }
            .chartYScale(domain: 3.0...4.5)
            .chartYAxisLabel("Volts")
            .chartXAxis {
                AxisMarks(values: .automatic) { value in
                    AxisGridLine()
                    AxisValueLabel(format: .dateTime.hour().minute())
                }
            }
        }
    }
    
    private var batteryGradient: LinearGradient {
        LinearGradient(
            colors: [.red, .yellow, .green],
            startPoint: .bottom,
            endPoint: .top
        )
    }
    
    private func batteryColor(_ voltage: Double) -> Color {
        if voltage > 3.8 { return .green }
        else if voltage > 3.5 { return .yellow }
        else { return .red }
    }
}

// MARK: - Temperature Chart

struct TemperatureChart: View {
    let data: [TelemetryPoint]
    
    var body: some View {
        VStack(alignment: .leading) {
            Text("Temperature over Time")
                .font(.headline)
            
            HStack {
                Circle().fill(.orange).frame(width: 8, height: 8)
                Text("CPU")
                Circle().fill(.purple).frame(width: 8, height: 8)
                Text("Radio")
            }
            .font(.caption)
            
            Chart {
                ForEach(data) { point in
                    LineMark(
                        x: .value("Time", point.timestamp),
                        y: .value("Temp", point.cpuTemp),
                        series: .value("Sensor", "CPU")
                    )
                    .foregroundStyle(.orange)
                    
                    LineMark(
                        x: .value("Time", point.timestamp),
                        y: .value("Temp", point.radioTemp),
                        series: .value("Sensor", "Radio")
                    )
                    .foregroundStyle(.purple)
                }
            }
            .chartYAxisLabel("°C")
            .chartXAxis {
                AxisMarks(values: .automatic) { value in
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
    
    var body: some View {
        VStack(alignment: .leading) {
            Text("Signal Strength over Time")
                .font(.headline)
            
            HStack {
                Circle().fill(.blue).frame(width: 8, height: 8)
                Text("RSSI")
                Circle().fill(.cyan).frame(width: 8, height: 8)
                Text("SNR")
            }
            .font(.caption)
            
            Chart {
                ForEach(data) { point in
                    LineMark(
                        x: .value("Time", point.timestamp),
                        y: .value("RSSI", point.rssi),
                        series: .value("Metric", "RSSI")
                    )
                    .foregroundStyle(.blue)
                }
            }
            .chartYAxisLabel("dBm")
            .chartXAxis {
                AxisMarks(values: .automatic) { value in
                    AxisGridLine()
                    AxisValueLabel(format: .dateTime.hour().minute())
                }
            }
        }
    }
}

#Preview {
    GraphsView()
        .environmentObject(GroundStationManager())
}
