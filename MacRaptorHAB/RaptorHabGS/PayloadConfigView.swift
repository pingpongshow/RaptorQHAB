//
//  PayloadConfigView.swift
//  RaptorHabGS
//
//  Configuration UI for the payload over USB.
//
//  The whole form is generated from the schema the Pi sends, so a parameter
//  added on the payload appears here with no Swift change. Nothing about
//  individual settings is hardcoded.
//

import SwiftUI

struct PayloadConfigView: View {
    @StateObject private var link = PiLinkManager.shared

    @State private var selectedDevice: SerialDevice?
    @State private var selectedCategory: String?
    @State private var edits: [String: ParameterValue] = [:]
    @State private var showAdvanced = false
    @State private var searchText = ""
    @State private var generatedPSK: String?

    var body: some View {
        HSplitView {
            sidebar
                .frame(minWidth: 220, idealWidth: 240, maxWidth: 300)

            Group {
                if link.isConnected {
                    parameterPane
                } else {
                    disconnectedPane
                }
            }
            .frame(minWidth: 460)
        }
        .onAppear { link.refreshDevices() }
    }

    // MARK: - Sidebar

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 0) {
            connectionSection
            Divider()

            if link.isConnected, let schema = link.schema {
                List(selection: $selectedCategory) {
                    ForEach(schema.categories, id: \.self) { category in
                        Label(category, systemImage: symbol(for: category))
                            .tag(category)
                    }
                }
                .listStyle(.sidebar)
            } else {
                Spacer()
            }
        }
    }

    private var connectionSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Payload")
                    .font(.headline)
                Spacer()
                Button {
                    link.refreshDevices()
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(.borderless)
                .help("Rescan USB devices")
            }

            if link.isConnected {
                connectedSummary
            } else {
                devicePicker
            }

            if let error = link.lastError {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(12)
    }

    private var connectedSummary: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let identity = link.identity {
                Label(identity.callsign, systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.green)
                Text(identity.hostname)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            if let system = link.status?.system {
                HStack(spacing: 10) {
                    if let temp = system.cpuTempC {
                        Label(String(format: "%.0f°C", temp), systemImage: "thermometer")
                    }
                    if let memory = system.memoryPercent {
                        Label(String(format: "%.0f%%", memory), systemImage: "memorychip")
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }

            if let service = link.status?.service {
                let running = service.active == "active"
                Label(
                    running ? "Flight software running" : "Flight software \(service.active ?? "?")",
                    systemImage: running ? "play.circle.fill" : "stop.circle"
                )
                .font(.caption)
                .foregroundStyle(running ? .green : .orange)
            }

            Button("Disconnect") { link.disconnect() }
                .buttonStyle(.borderless)
                .padding(.top, 4)
        }
    }

    private var devicePicker: some View {
        VStack(alignment: .leading, spacing: 6) {
            if link.availableDevices.isEmpty {
                Text("No payload detected")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text("Connect the Pi's data port — the one marked USB, not PWR IN.")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Picker("Device", selection: $selectedDevice) {
                    Text("Select…").tag(SerialDevice?.none)
                    ForEach(link.availableDevices) { device in
                        Label(device.displayName, systemImage: device.kind.symbolName)
                            .tag(SerialDevice?.some(device))
                    }
                }
                .labelsHidden()

                Button("Connect") {
                    if let device = selectedDevice {
                        Task { await link.connect(to: device) }
                    }
                }
                .disabled(selectedDevice == nil || link.isBusy)
            }
        }
        .onAppear {
            if selectedDevice == nil { selectedDevice = link.autoDetectedDevice }
        }
    }

    // MARK: - Disconnected

    private var disconnectedPane: some View {
        VStack(spacing: 14) {
            Image(systemName: "cable.connector.horizontal")
                .font(.system(size: 44))
                .foregroundStyle(.tertiary)
            Text("Not connected to a payload")
                .font(.title3)
            Text("Configuration is available over USB only. The payload will not "
                 + "accept settings over the radio.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 380)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: - Parameters

    private var parameterPane: some View {
        VStack(spacing: 0) {
            toolbar
            Divider()

            ScrollView {
                LazyVStack(alignment: .leading, spacing: 14) {
                    ForEach(visibleParameters) { spec in
                        ParameterRow(
                            spec: spec,
                            value: binding(for: spec),
                            currentValue: link.values[spec.name],
                            fingerprint: link.secretFingerprints[spec.name],
                            isEdited: edits[spec.name] != nil
                        )
                    }

                    if visibleParameters.isEmpty {
                        Text(searchText.isEmpty
                             ? "Nothing in this category."
                             : "No parameter matches “\(searchText)”.")
                            .foregroundStyle(.secondary)
                            .padding()
                    }
                }
                .padding()
            }

            Divider()
            footer
        }
    }

    private var toolbar: some View {
        HStack(spacing: 10) {
            Text(selectedCategory ?? "All settings")
                .font(.headline)

            Spacer()

            TextField("Search", text: $searchText)
                .textFieldStyle(.roundedBorder)
                .frame(width: 170)

            Toggle("Advanced", isOn: $showAdvanced)
                .toggleStyle(.switch)
                .help("Show pin assignments and other rarely-changed settings")
        }
        .padding(10)
    }

    private var footer: some View {
        VStack(spacing: 8) {
            if !link.pendingRestart.isEmpty {
                HStack {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                    Text("Restart required for: "
                         + link.pendingRestart.sorted().joined(separator: ", "))
                        .font(.caption)
                    Spacer()
                    Button("Restart flight software") {
                        Task { await link.restartPayloadService() }
                    }
                    .disabled(link.isBusy)
                }
            }

            HStack {
                if !edits.isEmpty {
                    Text("\(edits.count) unsaved change\(edits.count == 1 ? "" : "s")")
                        .font(.caption)
                        .foregroundStyle(.orange)
                }

                Spacer()

                Button("Revert") { edits.removeAll() }
                    .disabled(edits.isEmpty)

                Button("Apply") {
                    Task {
                        let result = await link.setConfig(edits)
                        if result?.ok == true { edits.removeAll() }
                    }
                }
                .keyboardShortcut(.return, modifiers: .command)
                .buttonStyle(.borderedProminent)
                .disabled(edits.isEmpty || link.isBusy)
            }
        }
        .padding(10)
    }

    // MARK: - Data

    private var visibleParameters: [ParameterSpec] {
        guard let schema = link.schema else { return [] }

        return schema.parameters.filter { spec in
            if !showAdvanced && spec.advanced { return false }

            if !searchText.isEmpty {
                let needle = searchText.lowercased()
                return spec.name.lowercased().contains(needle)
                    || spec.description.lowercased().contains(needle)
            }

            guard let selectedCategory else { return true }
            return spec.category == selectedCategory
        }
    }

    private func binding(for spec: ParameterSpec) -> Binding<ParameterValue> {
        Binding(
            get: { edits[spec.name] ?? link.values[spec.name] ?? .null },
            set: { newValue in
                if newValue == link.values[spec.name] {
                    edits.removeValue(forKey: spec.name)
                } else {
                    edits[spec.name] = newValue
                }
            }
        )
    }

    private func symbol(for category: String) -> String {
        switch category {
        case "Identification":     return "tag"
        case "Radio":              return "antenna.radiowaves.left.and.right"
        case "Timing":             return "clock"
        case "Camera":             return "camera"
        case "Image Quality":      return "wand.and.stars"
        case "GPS":                return "location"
        case "Fountain Coding":    return "square.stack.3d.down.right"
        case "Flight Zones":       return "map"
        case "Zone Launch":        return "arrow.up.circle"
        case "Zone Cruise":        return "airplane"
        case "Zone Landed":        return "arrow.down.to.line"
        case "Meshtastic":         return "point.3.connected.trianglepath.dotted"
        case "Meshtastic Region":  return "globe"
        case "Meshtastic Private": return "lock"
        case "Storage":            return "internaldrive"
        case "Reliability":        return "shield"
        case "Debug":              return "ladybug"
        case "Hardware Pins":      return "cpu"
        default:                   return "gearshape"
        }
    }
}

// MARK: - One parameter

private struct ParameterRow: View {
    let spec: ParameterSpec
    @Binding var value: ParameterValue
    let currentValue: ParameterValue?
    let fingerprint: String?
    let isEdited: Bool

    @State private var secretEntry = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 6) {
                Text(label)
                    .font(.system(.body, design: .rounded))
                    .fontWeight(.medium)

                if spec.requiresRestart {
                    Text("restart")
                        .font(.caption2)
                        .padding(.horizontal, 5).padding(.vertical, 1)
                        .background(Color.orange.opacity(0.2), in: Capsule())
                        .foregroundStyle(.orange)
                        .help("Takes effect when the flight software restarts")
                }

                if isEdited {
                    Circle().fill(.orange).frame(width: 6, height: 6)
                        .help("Changed but not applied")
                }

                Spacer()
                control
            }

            Text(spec.description)
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            if spec.secret, let fingerprint {
                Text("Current key: \(fingerprint)")
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .help("The key itself is never sent back over the link")
            }
        }
        .padding(10)
        .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 8))
    }

    private var label: String {
        spec.name
            .replacingOccurrences(of: "_", with: " ")
            .capitalized
    }

    @ViewBuilder
    private var control: some View {
        switch spec.kind {
        case "bool":
            Toggle("", isOn: Binding(
                get: { value.boolValue },
                set: { value = .bool($0) }
            ))
            .labelsHidden()

        case "enum":
            Picker("", selection: Binding(
                get: { value },
                set: { value = $0 }
            )) {
                ForEach(Array((spec.choices ?? []).enumerated()), id: \.offset) { index, choice in
                    Text(spec.choiceLabels?[safe: index] ?? choice.description)
                        .tag(choice)
                }
            }
            .labelsHidden()
            .frame(maxWidth: 230)

        case "int", "float":
            HStack(spacing: 4) {
                TextField("", text: Binding(
                    get: { value.description },
                    set: { text in
                        if let number = Double(text) { value = .number(number) }
                    }
                ))
                .textFieldStyle(.roundedBorder)
                .frame(width: 90)
                .multilineTextAlignment(.trailing)

                if let unit = spec.unit, !unit.isEmpty {
                    Text(unit)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .frame(width: 34, alignment: .leading)
                }
            }

        case "resolution":
            TextField("", text: Binding(
                get: {
                    if case .list(let parts) = value, parts.count == 2 {
                        return "\(Int(parts[0]))x\(Int(parts[1]))"
                    }
                    return value.stringValue
                },
                set: { text in
                    let parts = text.lowercased().split(separator: "x")
                        .compactMap { Double($0.trimmingCharacters(in: .whitespaces)) }
                    if parts.count == 2 { value = .list(parts) }
                }
            ))
            .textFieldStyle(.roundedBorder)
            .frame(width: 120)

        default:
            if spec.secret {
                // Write-only: the field is always blank, because the payload
                // never sends the value back. Typing replaces the key.
                SecureField("unchanged", text: $secretEntry)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 190)
                    .onChange(of: secretEntry) { _, newValue in
                        if !newValue.isEmpty { value = .string(newValue) }
                    }
            } else {
                TextField("", text: Binding(
                    get: { value.stringValue },
                    set: { value = .string($0) }
                ))
                .textFieldStyle(.roundedBorder)
                .frame(width: 190)
            }
        }
    }
}

private extension Array {
    subscript(safe index: Int) -> Element? {
        indices.contains(index) ? self[index] : nil
    }
}
