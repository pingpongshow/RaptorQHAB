//
//  MeshtasticManager.swift
//  RaptorHabGS
//
//  Owns the connection to a Meshtastic node and the mesh state it reports.
//
//  This is a second, independent path to the balloon. The RAPTOR modem hears
//  the GFSK downlink; a Meshtastic node hears the balloon's LoRa beacons.
//  Either can tell you where it is, and Phase 7 fuses them.
//

import Combine
import CoreBluetooth
import Foundation
import MapKit

// MARK: - Node

struct MeshtasticNode: Identifiable, Equatable {
    let id: UInt32
    var longName: String?
    var shortName: String?
    var position: CLLocationCoordinate2D?
    var altitude: Int32?
    var satellites: Int?
    var batteryLevel: Int?
    var voltage: Float?
    var temperature: Float?
    var lastHeard: Date
    var rssi: Int?
    var snr: Float?
    var hopsAway: Int?

    var displayName: String {
        longName ?? shortName ?? MeshtasticNode.formatID(id)
    }

    static func formatID(_ id: UInt32) -> String {
        String(format: "!%08x", id)
    }

    var idString: String { MeshtasticNode.formatID(id) }

    /// A node not heard from recently is probably out of range rather than
    /// gone; the UI dims it rather than dropping it.
    var isStale: Bool { Date().timeIntervalSince(lastHeard) > 900 }

    static func == (lhs: MeshtasticNode, rhs: MeshtasticNode) -> Bool {
        lhs.id == rhs.id && lhs.lastHeard == rhs.lastHeard
    }
}

struct MeshtasticMessage: Identifiable {
    let id = UUID()
    let timestamp: Date
    let sender: UInt32
    let senderName: String
    let destination: UInt32
    let text: String
    let channelHash: UInt8
    let rssi: Int?
    let snr: Float?
    let isOutgoing: Bool

    var isBroadcast: Bool { destination == MeshtasticHeader.broadcast }
}

// MARK: - Channel

struct MeshtasticChannelConfig: Identifiable, Equatable {
    var id = UUID()
    var name: String
    var pskText: String
    var enabled: Bool = true

    var key: Data { MeshtasticCrypto.expand(psk: MeshtasticCrypto.parsePSK(pskText) ?? Data()) }
    var hash: UInt8 { MeshtasticCrypto.channelHash(name: name, key: key) }
    var isEncrypted: Bool { !key.isEmpty }

    /// True for the key every Meshtastic client ships with, which means the
    /// channel is obfuscated rather than private.
    var usesDefaultKey: Bool { key == MeshtasticCrypto.defaultPSK }

    static let longFast = MeshtasticChannelConfig(name: "LongFast", pskText: "AQ==")
}

// MARK: - Manager

@MainActor
final class MeshtasticManager: NSObject, ObservableObject {
    static let shared = MeshtasticManager()

    enum ConnectionKind: String {
        case none = "Not connected"
        case usb = "USB"
        case bluetooth = "Bluetooth"
    }

    @Published private(set) var connectionKind: ConnectionKind = .none
    @Published private(set) var isConnected = false
    @Published private(set) var nodes: [UInt32: MeshtasticNode] = [:]
    @Published private(set) var messages: [MeshtasticMessage] = []
    @Published private(set) var lastError: String?
    @Published private(set) var packetsReceived = 0
    @Published private(set) var packetsDecoded = 0

    @Published private(set) var availableSerialDevices: [SerialDevice] = []
    @Published private(set) var availableBLEDevices: [CBPeripheral] = []
    @Published private(set) var isScanning = false

    /// Channels the app will attempt to decrypt. The private channel is added
    /// by the operator, and must match what the payload is configured with.
    @Published var channels: [MeshtasticChannelConfig] = [.longFast]

    /// The balloon's node id, derived from its callsign the same way the
    /// payload derives it, so beacons can be attributed without configuration.
    @Published var balloonNodeID: UInt32?

    private var serialTransport: MeshtasticSerialTransport?
    private var bleTransport: MeshtasticBLETransport?
    private var activeTransport: MeshtasticTransport? {
        serialTransport ?? bleTransport
    }

    /// This app's own node id when sending. Random per session: the app is not
    /// a real mesh node and should not squat on an id another device may use.
    private lazy var localNodeID: UInt32 = UInt32.random(in: 0x1000...0x7FFFFFFF)

    private override init() {
        super.init()
        refreshSerialDevices()
    }

    // MARK: - Discovery

    func refreshSerialDevices() {
        let all: [SerialDevice] = SerialDeviceDiscovery.discover()
        availableSerialDevices = all.filter { device in
            device.kind == SerialDeviceKind.meshtasticNode
                || device.kind == SerialDeviceKind.genericUSB
        }
    }

    func startBLEScan() {
        let transport = bleTransport ?? MeshtasticBLETransport()
        bleTransport = transport
        transport.delegate = self
        isScanning = true
        transport.startScanning { [weak self] peripherals in
            Task { @MainActor in self?.availableBLEDevices = peripherals }
        }
    }

    func stopBLEScan() {
        bleTransport?.stopScanning()
        isScanning = false
    }

    // MARK: - Connection

    func connectUSB(_ device: SerialDevice) {
        disconnect()
        lastError = nil

        let transport = MeshtasticSerialTransport()
        transport.delegate = self
        do {
            try transport.connect(to: device)
            serialTransport = transport
            connectionKind = .usb
        } catch {
            lastError = error.localizedDescription
        }
    }

    func connectBluetooth(_ peripheral: CBPeripheral) {
        disconnect()
        lastError = nil
        stopBLEScan()

        let transport = bleTransport ?? MeshtasticBLETransport()
        bleTransport = transport
        transport.delegate = self
        transport.connect(to: peripheral)
        connectionKind = .bluetooth
    }

    func disconnect() {
        serialTransport?.disconnect()
        serialTransport = nil
        bleTransport?.disconnect()
        isConnected = false
        connectionKind = .none
    }

    // MARK: - Sending

    /// Send a text message to the mesh, or to one node.
    func sendText(
        _ text: String,
        to destination: UInt32 = MeshtasticHeader.broadcast,
        channel: MeshtasticChannelConfig
    ) {
        guard let transport = activeTransport, transport.isConnected else {
            lastError = "Not connected to a Meshtastic node"
            return
        }

        let packet = MeshtasticProtocol.buildPacket(
            portNum: .textMessage,
            payload: MeshtasticProtocol.buildTextMessage(text),
            sender: localNodeID,
            destination: destination,
            channelKey: channel.key,
            channelHash: channel.hash,
            hopLimit: 3
        )

        transport.send(wrapToRadio(meshPacket: packet))

        messages.append(MeshtasticMessage(
            timestamp: Date(),
            sender: localNodeID,
            senderName: "This Mac",
            destination: destination,
            text: text,
            channelHash: channel.hash,
            rssi: nil, snr: nil,
            isOutgoing: true
        ))
        trimMessages()
    }

    /// Wrap a raw mesh packet in the ToRadio envelope the node expects.
    ///
    /// ToRadio field 1 is `packet` (a MeshPacket). MeshPacket field 8 is
    /// `encrypted`, which is the pre-encrypted form — the node forwards it
    /// without re-encrypting, so the app controls the channel key rather than
    /// depending on how the attached node is configured.
    private func wrapToRadio(meshPacket: Data) -> Data {
        guard let header = MeshtasticHeader.parse(meshPacket) else { return Data() }
        let body = meshPacket.dropFirst(MeshtasticHeader.size)

        var packet = ProtobufWriter()
        packet.fixed32(1, header.sender)         // from
        packet.fixed32(2, header.destination)    // to
        packet.uint32(3, UInt32(header.channelHash))
        packet.bytes(8, Data(body), force: true) // encrypted
        packet.fixed32(6, header.packetID)       // id
        packet.uint32(10, UInt32(header.hopLimit))
        packet.bool(11, header.wantAck)

        var toRadio = ProtobufWriter()
        toRadio.message(1, packet, force: true)
        return toRadio.data
    }

    // MARK: - Receiving

    /// Handle one FromRadio protobuf.
    ///
    /// FromRadio field 2 is `packet` (a MeshPacket). Everything else — config,
    /// node database dumps, log records — is ignored: the app only needs
    /// what arrives over the air.
    private func handleFromRadio(_ data: Data) {
        guard let fields = try? ProtobufReader(data).fields() else { return }
        guard let packetData = fields[2]?.last?.dataValue else { return }

        packetsReceived += 1
        handleMeshPacket(packetData)
    }

    private func handleMeshPacket(_ data: Data) {
        guard let fields = try? ProtobufReader(data).fields() else { return }

        let sender = UInt32(truncatingIfNeeded: fields[1]?.last?.uintValue ?? 0)
        let destination = fields[2]?.last?.uintValue
            .map { UInt32(truncatingIfNeeded: $0) } ?? MeshtasticHeader.broadcast
        let channelHash = UInt8(truncatingIfNeeded: fields[3]?.last?.uintValue ?? 0)
        let rssi = fields[7]?.last?.intValue.map { Int($0) }
        let snr = fields[9]?.last?.floatValue
        let hopLimit = fields[10]?.last?.uintValue
        let hopStart = fields[15]?.last?.uintValue

        guard sender != 0 else { return }

        // A node may hand us either a decoded Data message (field 4) or the
        // still-encrypted payload (field 8), depending on whether it holds
        // the channel key. Handle both.
        var decoded: (port: MeshtasticPortNum, payload: Data)?

        if let dataFieldBytes = fields[4]?.last?.dataValue,
           let dataFields = try? ProtobufReader(dataFieldBytes).fields() {
            let port = MeshtasticPortNum(rawValue: Int(dataFields[1]?.last?.uintValue ?? 0))
            decoded = (port ?? .unknown, dataFields[2]?.last?.dataValue ?? Data())
        } else if let encrypted = fields[8]?.last?.dataValue {
            let packetID = UInt32(fields[6]?.last?.uintValue ?? 0)
            decoded = decrypt(encrypted, packetID: packetID, sender: sender)
        }

        guard let decoded else { return }
        packetsDecoded += 1

        var hops: Int?
        if let hopStart, let hopLimit, hopStart >= hopLimit {
            hops = Int(hopStart - hopLimit)
        }

        updateNode(sender, rssi: rssi, snr: snr, hopsAway: hops)
        apply(port: decoded.port, payload: decoded.payload, sender: sender,
              destination: destination, channelHash: channelHash, rssi: rssi, snr: snr)
    }

    private func decrypt(
        _ encrypted: Data, packetID: UInt32, sender: UInt32
    ) -> (MeshtasticPortNum, Data)? {
        let nonce = MeshtasticCrypto.nonce(packetID: packetID, sender: sender)

        for channel in channels where channel.enabled {
            guard let plaintext = MeshtasticCrypto.ctr(
                key: channel.key, nonce: nonce, data: encrypted
            ) else { continue }

            guard let fields = try? ProtobufReader(plaintext).fields() else { continue }
            let rawPort = Int(fields[1]?.last?.uintValue ?? 0)
            guard let port = MeshtasticPortNum(rawValue: rawPort) else { continue }

            let payload = fields[2]?.last?.dataValue ?? Data()
            // Random bytes decode to port 0 with nothing in them.
            if port == .unknown && payload.isEmpty { continue }

            return (port, payload)
        }
        return nil
    }

    private func apply(
        port: MeshtasticPortNum, payload: Data, sender: UInt32,
        destination: UInt32, channelHash: UInt8, rssi: Int?, snr: Float?
    ) {
        switch port {
        case .position:
            guard let position = MeshtasticProtocol.parsePosition(payload) else { return }
            updateNode(sender, rssi: rssi, snr: snr) { node in
                node.position = CLLocationCoordinate2D(
                    latitude: position.latitude, longitude: position.longitude
                )
                node.altitude = position.altitude
                node.satellites = position.satellites
            }
            publishPositionIfBalloon(sender: sender, position: position, rssi: rssi, snr: snr)

        case .nodeInfo:
            guard let user = MeshtasticProtocol.parseUser(payload) else { return }
            updateNode(sender, rssi: rssi, snr: snr) { node in
                node.longName = user.longName.isEmpty ? node.longName : user.longName
                node.shortName = user.shortName.isEmpty ? node.shortName : user.shortName
            }

        case .telemetry:
            guard let telemetry = MeshtasticProtocol.parseTelemetry(payload) else { return }
            updateNode(sender, rssi: rssi, snr: snr) { node in
                if let device = telemetry.device {
                    node.batteryLevel = device.batteryLevel ?? node.batteryLevel
                    node.voltage = device.voltage ?? node.voltage
                }
                node.temperature = telemetry.temperature ?? node.temperature
            }

        case .textMessage:
            let text = String(decoding: payload, as: UTF8.self)
            guard !text.isEmpty else { return }
            messages.append(MeshtasticMessage(
                timestamp: Date(),
                sender: sender,
                senderName: nodes[sender]?.displayName ?? MeshtasticNode.formatID(sender),
                destination: destination,
                text: text,
                channelHash: channelHash,
                rssi: rssi, snr: snr,
                isOutgoing: false
            ))
            trimMessages()

        default:
            break
        }
    }

    private func publishPositionIfBalloon(
        sender: UInt32, position: MeshtasticProtocol.Position, rssi: Int?, snr: Float?
    ) {
        guard sender == balloonNodeID else { return }

        PositionFusion.shared.submit(PositionFix(
            source: .meshtasticDirect,
            coordinate: CLLocationCoordinate2D(
                latitude: position.latitude, longitude: position.longitude
            ),
            altitude: Double(position.altitude),
            timestamp: PositionFix.reconcileTimestamp(position.timestamp),
            satellites: position.satellites,
            rssi: rssi,
            snr: snr,
            detail: nodes[sender]?.displayName
        ))
    }

    private func updateNode(
        _ id: UInt32, rssi: Int?, snr: Float?, hopsAway: Int? = nil,
        mutate: ((inout MeshtasticNode) -> Void)? = nil
    ) {
        var node = nodes[id] ?? MeshtasticNode(id: id, lastHeard: Date())
        node.lastHeard = Date()
        if let rssi { node.rssi = rssi }
        if let snr { node.snr = snr }
        if let hopsAway { node.hopsAway = hopsAway }
        mutate?(&node)
        nodes[id] = node
    }

    private func trimMessages() {
        if messages.count > 500 {
            messages.removeFirst(messages.count - 500)
        }
    }

    // MARK: - Balloon identity

    /// Derive the balloon's node id from its callsign, matching
    /// `node_id_from_callsign` on the payload. Deterministic, so beacons are
    /// attributed to the balloon without the operator configuring an id.
    static func nodeID(forCallsign callsign: String, payloadID: Int) -> UInt32 {
        let seed = "\(callsign.trimmingCharacters(in: .whitespaces).uppercased())#\(payloadID)"
        let digest = SHA256Digest.hash(Data(seed.utf8))

        let value = UInt32(digest[0])
            | UInt32(digest[1]) << 8
            | UInt32(digest[2]) << 16
            | UInt32(digest[3]) << 24

        let span: UInt32 = 0xFFFFFFFE - 0x00001000
        return 0x00001000 + (value % span)
    }

    func setBalloonIdentity(callsign: String, payloadID: Int) {
        balloonNodeID = MeshtasticManager.nodeID(forCallsign: callsign, payloadID: payloadID)
    }
}

// MARK: - Transport delegate

extension MeshtasticManager: MeshtasticTransportDelegate {
    nonisolated func transport(_ transport: MeshtasticTransport, didReceive data: Data) {
        Task { @MainActor in self.handleFromRadio(data) }
    }

    nonisolated func transport(
        _ transport: MeshtasticTransport, didChangeConnected connected: Bool
    ) {
        Task { @MainActor in
            self.isConnected = connected
            if !connected { self.connectionKind = .none }
        }
    }

    nonisolated func transport(_ transport: MeshtasticTransport, didFail error: String) {
        Task { @MainActor in self.lastError = error }
    }
}
