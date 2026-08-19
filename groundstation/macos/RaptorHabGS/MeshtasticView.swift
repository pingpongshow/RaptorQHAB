//
//  MeshtasticView.swift
//  RaptorHabGS
//
//  Connect a Meshtastic node, watch the mesh, and message the balloon.
//

import CoreBluetooth
import MapKit
import SwiftUI

struct MeshtasticView: View {
    @StateObject private var mesh = MeshtasticManager.shared
    @EnvironmentObject var groundStation: GroundStationManager

    @State private var selectedTab = 0

    var body: some View {
        VStack(spacing: 0) {
            connectionBar
            Divider()

            TabView(selection: $selectedTab) {
                MeshNodeListView()
                    .tabItem { Label("Nodes", systemImage: "list.bullet") }
                    .tag(0)

                MeshMessagesView()
                    .tabItem { Label("Messages", systemImage: "bubble.left.and.bubble.right") }
                    .tag(1)

                MeshChannelsView()
                    .tabItem { Label("Channels", systemImage: "key") }
                    .tag(2)
            }
        }
        .onAppear { mesh.refreshSerialDevices() }
    }

    private var connectionBar: some View {
        HStack(spacing: 12) {
            Circle()
                .fill(mesh.isConnected ? Color.green : Color.secondary.opacity(0.4))
                .frame(width: 9, height: 9)

            Text(mesh.isConnected
                 ? "Connected over \(mesh.connectionKind.rawValue)"
                 : "No Meshtastic node")
                .font(.callout)

            if mesh.isConnected {
                Text("\(mesh.packetsDecoded)/\(mesh.packetsReceived) packets decoded")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .help("Packets we could decrypt, out of those heard. A low "
                          + "ratio usually means a channel key is missing.")
            }

            Spacer()

            if let error = mesh.lastError {
                Text(error)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .lineLimit(1)
            }

            if mesh.isConnected {
                Button("Disconnect") { mesh.disconnect() }
            } else {
                MeshConnectMenu()
            }
        }
        .padding(10)
    }
}

// MARK: - Connect

private struct MeshConnectMenu: View {
    @StateObject private var mesh = MeshtasticManager.shared

    var body: some View {
        HStack(spacing: 8) {
            Menu("Connect over USB") {
                if mesh.availableSerialDevices.isEmpty {
                    Text("No serial device found")
                } else {
                    ForEach(mesh.availableSerialDevices) { device in
                        Button(device.displayName) { mesh.connectUSB(device) }
                    }
                }
                Divider()
                Button("Rescan") { mesh.refreshSerialDevices() }
            }
            .menuStyle(.borderlessButton)
            .fixedSize()

            Menu(mesh.isScanning ? "Scanning…" : "Connect over Bluetooth") {
                if mesh.availableBLEDevices.isEmpty {
                    Text(mesh.isScanning ? "Looking for nodes…" : "Start a scan to find nodes")
                } else {
                    ForEach(mesh.availableBLEDevices, id: \.identifier) { peripheral in
                        Button(peripheral.name ?? peripheral.identifier.uuidString) {
                            mesh.connectBluetooth(peripheral)
                        }
                    }
                }
                Divider()
                if mesh.isScanning {
                    Button("Stop scanning") { mesh.stopBLEScan() }
                } else {
                    Button("Scan") { mesh.startBLEScan() }
                }
            }
            .menuStyle(.borderlessButton)
            .fixedSize()
        }
    }
}

// MARK: - Nodes

private struct MeshNodeListView: View {
    @StateObject private var mesh = MeshtasticManager.shared

    private var sortedNodes: [MeshtasticNode] {
        mesh.nodes.values.sorted { $0.lastHeard > $1.lastHeard }
    }

    var body: some View {
        if sortedNodes.isEmpty {
            emptyState
        } else {
            Table(sortedNodes) {
                TableColumn("Node") { node in
                    HStack(spacing: 6) {
                        if node.id == mesh.balloonNodeID {
                            Image(systemName: "balloon.fill")
                                .foregroundStyle(.orange)
                                .help("This is the balloon")
                        }
                        VStack(alignment: .leading, spacing: 1) {
                            Text(node.displayName)
                                .fontWeight(node.id == mesh.balloonNodeID ? .semibold : .regular)
                            Text(node.idString)
                                .font(.caption2)
                                .foregroundStyle(.tertiary)
                        }
                    }
                    .opacity(node.isStale ? 0.5 : 1)
                }

                TableColumn("Position") { node in
                    if let position = node.position {
                        Text(String(format: "%.4f, %.4f", position.latitude, position.longitude))
                            .font(.system(.caption, design: .monospaced))
                    } else {
                        Text("—").foregroundStyle(.tertiary)
                    }
                }

                TableColumn("Alt") { node in
                    Text(node.altitude.map { "\($0) m" } ?? "—")
                        .font(.caption)
                }

                TableColumn("Battery") { node in
                    if let level = node.batteryLevel {
                        // 101 is Meshtastic's "externally powered" sentinel.
                        Text(level > 100 ? "ext" : "\(level)%")
                            .font(.caption)
                            .foregroundStyle(level < 20 && level <= 100 ? .red : .primary)
                    } else {
                        Text("—").foregroundStyle(.tertiary)
                    }
                }

                TableColumn("Signal") { node in
                    if let rssi = node.rssi {
                        Text("\(rssi) dBm")
                            .font(.caption)
                            .foregroundStyle(rssi > -100 ? Color.primary : Color.orange)
                    } else {
                        Text("—").foregroundStyle(.tertiary)
                    }
                }

                TableColumn("Hops") { node in
                    Text(node.hopsAway.map(String.init) ?? "—")
                        .font(.caption)
                }

                TableColumn("Heard") { node in
                    Text(node.lastHeard, style: .relative)
                        .font(.caption)
                        .foregroundStyle(node.isStale ? .tertiary : .secondary)
                }
            }
        }
    }

    private var emptyState: some View {
        VStack(spacing: 12) {
            Image(systemName: "point.3.connected.trianglepath.dotted")
                .font(.system(size: 40))
                .foregroundStyle(.tertiary)
            Text(mesh.isConnected ? "No nodes heard yet" : "Connect a Meshtastic node")
                .font(.title3)
            if mesh.isConnected {
                Text("Nodes appear as they transmit. On a quiet mesh this can take "
                     + "several minutes.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 340)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

// MARK: - Messages

private struct MeshMessagesView: View {
    @StateObject private var mesh = MeshtasticManager.shared

    @State private var draft = ""
    @State private var selectedChannelID: UUID?
    @State private var sendToBalloonOnly = false

    private var selectedChannel: MeshtasticChannelConfig {
        mesh.channels.first { $0.id == selectedChannelID } ?? mesh.channels[0]
    }

    var body: some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 8) {
                        ForEach(mesh.messages) { message in
                            MessageRow(message: message)
                                .id(message.id)
                        }
                    }
                    .padding()
                }
                .onChange(of: mesh.messages.count) { _, _ in
                    if let last = mesh.messages.last {
                        withAnimation { proxy.scrollTo(last.id, anchor: .bottom) }
                    }
                }
            }

            Divider()
            composer
        }
    }

    private var composer: some View {
        VStack(spacing: 6) {
            HStack {
                Picker("Channel", selection: $selectedChannelID) {
                    ForEach(mesh.channels) { channel in
                        Text(channel.name).tag(UUID?.some(channel.id))
                    }
                }
                .frame(maxWidth: 220)

                Toggle("To balloon only", isOn: $sendToBalloonOnly)
                    .disabled(mesh.balloonNodeID == nil)
                    .help(mesh.balloonNodeID == nil
                          ? "Set the balloon's callsign in the Channels tab first"
                          : "Address this message to the balloon instead of broadcasting")

                Spacer()
            }
            .font(.caption)

            HStack {
                TextField("Message", text: $draft)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(send)

                Button("Send", action: send)
                    .buttonStyle(.borderedProminent)
                    .disabled(draft.isEmpty || !mesh.isConnected)
            }
        }
        .padding(10)
        .onAppear {
            if selectedChannelID == nil { selectedChannelID = mesh.channels.first?.id }
        }
    }

    private func send() {
        guard !draft.isEmpty else { return }
        let destination = sendToBalloonOnly
            ? (mesh.balloonNodeID ?? MeshtasticHeader.broadcast)
            : MeshtasticHeader.broadcast

        mesh.sendText(draft, to: destination, channel: selectedChannel)
        draft = ""
    }
}

private struct MessageRow: View {
    let message: MeshtasticMessage

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            if message.isOutgoing { Spacer(minLength: 60) }

            VStack(alignment: message.isOutgoing ? .trailing : .leading, spacing: 2) {
                HStack(spacing: 5) {
                    Text(message.senderName)
                        .font(.caption)
                        .fontWeight(.medium)

                    if !message.isBroadcast {
                        Image(systemName: "lock.fill")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .help("Addressed to one node, not broadcast")
                    }

                    Text(message.timestamp, style: .time)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }

                Text(message.text)
                    .textSelection(.enabled)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    .background(
                        message.isOutgoing ? Color.accentColor.opacity(0.2)
                                           : Color.primary.opacity(0.07),
                        in: RoundedRectangle(cornerRadius: 10)
                    )

                if let rssi = message.rssi {
                    Text("\(rssi) dBm"
                         + (message.snr.map { String(format: " · %.1f dB SNR", $0) } ?? ""))
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }

            if !message.isOutgoing { Spacer(minLength: 60) }
        }
    }
}

// MARK: - Channels

private struct MeshChannelsView: View {
    @StateObject private var mesh = MeshtasticManager.shared

    @State private var callsign = "RPHAB1"
    @State private var payloadID = 1
    @State private var newName = ""
    @State private var newPSK = ""

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                balloonSection
                Divider()
                channelSection
            }
            .padding()
        }
    }

    private var balloonSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Balloon identity")
                .font(.headline)

            Text("The payload derives its node id from its callsign, so entering the "
                 + "same callsign here lets the app recognise its beacons and feed "
                 + "them to the map.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            HStack {
                TextField("Callsign", text: $callsign)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 130)

                Stepper("Payload \(payloadID)", value: $payloadID, in: 0...255)
                    .frame(width: 150)

                Button("Set") {
                    mesh.setBalloonIdentity(callsign: callsign, payloadID: payloadID)
                }

                if let id = mesh.balloonNodeID {
                    Text(MeshtasticNode.formatID(id))
                        .font(.system(.body, design: .monospaced))
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    private var channelSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Channels")
                .font(.headline)

            Text("The app tries each enabled channel's key when decrypting. Add the "
                 + "balloon's private channel here with the same key you set on the "
                 + "payload.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            ForEach($mesh.channels) { $channel in
                HStack {
                    Toggle("", isOn: $channel.enabled)
                        .labelsHidden()

                    VStack(alignment: .leading, spacing: 1) {
                        Text(channel.name)
                            .fontWeight(.medium)
                        HStack(spacing: 6) {
                            Text(String(format: "hash 0x%02X", channel.hash))
                            if channel.usesDefaultKey {
                                Text("default key — not private")
                                    .foregroundStyle(.orange)
                            } else if channel.isEncrypted {
                                Text("encrypted")
                                    .foregroundStyle(.green)
                            } else {
                                Text("plaintext")
                                    .foregroundStyle(.orange)
                            }
                        }
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    }

                    Spacer()

                    if mesh.channels.count > 1 {
                        Button {
                            mesh.channels.removeAll { $0.id == channel.id }
                        } label: {
                            Image(systemName: "trash")
                        }
                        .buttonStyle(.borderless)
                    }
                }
                .padding(8)
                .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 6))
            }

            HStack {
                TextField("Channel name", text: $newName)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 150)

                SecureField("Key (base64 or hex)", text: $newPSK)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 240)

                Button("Add") {
                    mesh.channels.append(
                        MeshtasticChannelConfig(name: newName, pskText: newPSK)
                    )
                    newName = ""
                    newPSK = ""
                }
                .disabled(newName.isEmpty)
            }
        }
    }
}
