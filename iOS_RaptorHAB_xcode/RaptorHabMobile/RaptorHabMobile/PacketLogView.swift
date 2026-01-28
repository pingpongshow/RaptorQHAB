//
//  PacketLogView.swift
//  RaptorHabMobile
//
//  Raw packet log view for debugging
//

import SwiftUI

// MARK: - Packet Log Entry

struct PacketLogEntry: Identifiable {
    let id = UUID()
    let timestamp: Date
    let type: PacketType
    let sequence: UInt16
    let size: Int
    let rssi: Int
    let snr: Float
    let rawHex: String
    let isValid: Bool
    let errorMessage: String?
    
    var typeName: String {
        switch type {
        case .telemetry: return "Telemetry"
        case .imageMeta: return "Image Meta"
        case .imageData: return "Image Data"
        case .textMessage: return "Text Message"
        case .commandAck: return "Command Ack"
        case .cmdPing: return "Ping"
        case .cmdSetParam: return "Set Param"
        case .cmdCapture: return "Capture"
        case .cmdReboot: return "Reboot"
        case .unknown: return "Unknown"
        }
    }
}

// MARK: - Packet Log Manager

class PacketLogManager: ObservableObject {
    static let shared = PacketLogManager()
    
    @Published var entries: [PacketLogEntry] = []
    @Published var maxEntries: Int = 500
    @Published var filterType: PacketType? = nil
    @Published var showOnlyErrors: Bool = false
    
    private init() {}
    
    func addEntry(data: Data, type: PacketType, sequence: UInt16, rssi: Int, snr: Float, isValid: Bool, error: String? = nil) {
        let entry = PacketLogEntry(
            timestamp: Date(),
            type: type,
            sequence: sequence,
            size: data.count,
            rssi: rssi,
            snr: snr,
            rawHex: data.prefix(64).map { String(format: "%02X", $0) }.joined(separator: " "),
            isValid: isValid,
            errorMessage: error
        )
        
        DispatchQueue.main.async {
            self.entries.insert(entry, at: 0)
            if self.entries.count > self.maxEntries {
                self.entries.removeLast(self.entries.count - self.maxEntries)
            }
        }
    }
    
    func clear() {
        entries.removeAll()
    }
    
    var filteredEntries: [PacketLogEntry] {
        var result = entries
        
        if let filter = filterType {
            result = result.filter { $0.type == filter }
        }
        
        if showOnlyErrors {
            result = result.filter { !$0.isValid }
        }
        
        return result
    }
    
    var statistics: (received: Int, valid: Int, invalid: Int) {
        let valid = entries.filter { $0.isValid }.count
        return (entries.count, valid, entries.count - valid)
    }
}

// MARK: - Packet Log View

struct PacketLogView: View {
    @ObservedObject var logManager = PacketLogManager.shared
    @State private var selectedEntry: PacketLogEntry?
    
    var body: some View {
        VStack(spacing: 0) {
            // Stats header
            statsHeader
            
            Divider()
            
            // Filter bar
            filterBar
            
            Divider()
            
            // Packet list
            if logManager.filteredEntries.isEmpty {
                VStack(spacing: 16) {
                    Image(systemName: "doc.text")
                        .font(.system(size: 48))
                        .foregroundColor(.secondary)
                    Text("No Packets")
                        .font(.headline)
                    Text("Received packets will appear here")
                        .font(.subheadline)
                        .foregroundColor(.secondary)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                List {
                    ForEach(logManager.filteredEntries) { entry in
                        PacketLogRow(entry: entry)
                            .onTapGesture {
                                selectedEntry = entry
                            }
                    }
                }
                .listStyle(.plain)
            }
        }
        .navigationTitle("Packet Log")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button("Clear") {
                    logManager.clear()
                }
                .disabled(logManager.entries.isEmpty)
            }
        }
        .sheet(item: $selectedEntry) { entry in
            PacketDetailView(entry: entry)
        }
    }
    
    private var statsHeader: some View {
        HStack(spacing: 20) {
            PacketStatBox(title: "Received", value: "\(logManager.statistics.received)", color: .blue)
            PacketStatBox(title: "Valid", value: "\(logManager.statistics.valid)", color: .green)
            PacketStatBox(title: "Invalid", value: "\(logManager.statistics.invalid)", color: .red)
            
            if logManager.statistics.received > 0 {
                let rate = Double(logManager.statistics.valid) / Double(logManager.statistics.received) * 100
                PacketStatBox(title: "Rate", value: String(format: "%.1f%%", rate), color: rate > 90 ? .green : .orange)
            }
        }
        .padding()
    }
    
    private var filterBar: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 12) {
                // Type filter
                Menu {
                    Button("All Types") { logManager.filterType = nil }
                    Divider()
                    Button("Telemetry") { logManager.filterType = .telemetry }
                    Button("Image Meta") { logManager.filterType = .imageMeta }
                    Button("Image Data") { logManager.filterType = .imageData }
                    Button("Text Message") { logManager.filterType = .textMessage }
                } label: {
                    HStack {
                        Image(systemName: "line.3.horizontal.decrease.circle")
                        Text(logManager.filterType == nil ? "All Types" : filterTypeName)
                    }
                    .padding(.horizontal, 12)
                    .padding(.vertical, 6)
                    .background(Color(.systemGray6))
                    .cornerRadius(8)
                }
                
                // Errors only toggle
                Toggle(isOn: $logManager.showOnlyErrors) {
                    HStack {
                        Image(systemName: "exclamationmark.triangle")
                        Text("Errors Only")
                    }
                }
                .toggleStyle(.button)
                .tint(logManager.showOnlyErrors ? .red : .gray)
            }
            .padding(.horizontal)
            .padding(.vertical, 8)
        }
    }
    
    private var filterTypeName: String {
        guard let type = logManager.filterType else { return "All Types" }
        switch type {
        case .telemetry: return "Telemetry"
        case .imageMeta: return "Image Meta"
        case .imageData: return "Image Data"
        case .textMessage: return "Text Message"
        default: return "Other"
        }
    }
}

// MARK: - Packet Stat Box

private struct PacketStatBox: View {
    let title: String
    let value: String
    let color: Color
    
    var body: some View {
        VStack(spacing: 2) {
            Text(value)
                .font(.headline)
                .foregroundColor(color)
            Text(title)
                .font(.caption2)
                .foregroundColor(.secondary)
        }
    }
}

// MARK: - Packet Log Row

struct PacketLogRow: View {
    let entry: PacketLogEntry
    
    var body: some View {
        HStack(spacing: 12) {
            // Status indicator
            Circle()
                .fill(entry.isValid ? Color.green : Color.red)
                .frame(width: 8, height: 8)
            
            // Type icon
            Image(systemName: typeIcon)
                .foregroundColor(typeColor)
                .frame(width: 24)
            
            // Info
            VStack(alignment: .leading, spacing: 2) {
                HStack {
                    Text(entry.typeName)
                        .font(.subheadline)
                        .fontWeight(.medium)
                    Text("#\(entry.sequence)")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
                
                Text(entry.timestamp, style: .time)
                    .font(.caption2)
                    .foregroundColor(.secondary)
            }
            
            Spacer()
            
            // Stats
            VStack(alignment: .trailing, spacing: 2) {
                Text("\(entry.size) bytes")
                    .font(.caption)
                HStack(spacing: 8) {
                    Text("\(entry.rssi) dBm")
                    Text(String(format: "%.1f dB", entry.snr))
                }
                .font(.caption2)
                .foregroundColor(.secondary)
            }
        }
        .padding(.vertical, 4)
    }
    
    private var typeIcon: String {
        switch entry.type {
        case .telemetry: return "antenna.radiowaves.left.and.right"
        case .imageMeta: return "doc.text"
        case .imageData: return "photo"
        case .textMessage: return "message"
        default: return "questionmark.circle"
        }
    }
    
    private var typeColor: Color {
        switch entry.type {
        case .telemetry: return .blue
        case .imageMeta: return .orange
        case .imageData: return .green
        case .textMessage: return .purple
        default: return .gray
        }
    }
}

// MARK: - Packet Detail View

struct PacketDetailView: View {
    let entry: PacketLogEntry
    @Environment(\.dismiss) var dismiss
    
    var body: some View {
        NavigationStack {
            List {
                Section("Packet Info") {
                    DetailRow(label: "Type", value: entry.typeName)
                    DetailRow(label: "Sequence", value: "#\(entry.sequence)")
                    DetailRow(label: "Size", value: "\(entry.size) bytes")
                    DetailRow(label: "Time", value: entry.timestamp.formatted())
                    DetailRow(label: "Valid", value: entry.isValid ? "Yes" : "No")
                    if let error = entry.errorMessage {
                        DetailRow(label: "Error", value: error)
                    }
                }
                
                Section("Signal") {
                    DetailRow(label: "RSSI", value: "\(entry.rssi) dBm")
                    DetailRow(label: "SNR", value: String(format: "%.1f dB", entry.snr))
                }
                
                Section("Raw Data (first 64 bytes)") {
                    Text(entry.rawHex)
                        .font(.system(.caption, design: .monospaced))
                        .foregroundColor(.secondary)
                }
            }
            .navigationTitle("Packet Details")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                }
            }
        }
    }
}

// MARK: - Detail Row

private struct DetailRow: View {
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

#Preview {
    NavigationStack {
        PacketLogView()
    }
}
