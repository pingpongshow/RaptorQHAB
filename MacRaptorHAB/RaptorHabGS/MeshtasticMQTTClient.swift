//
//  MeshtasticMQTTClient.swift
//  RaptorHabGS
//
//  Subscribes to a Meshtastic MQTT broker so the balloon can still be tracked
//  when neither the RAPTOR modem nor a local Meshtastic node can hear it.
//
//  Positions from here come from strangers' nodes relayed over the internet.
//  That is genuinely useful for a balloon several hundred miles downrange, and
//  it is also the least trustworthy source in the app, so it sits at the
//  bottom of the fusion priority and is always labelled as third party.
//
//  Off by default. Connecting sends a subscription to a public broker, which
//  is a network action the operator should choose deliberately.
//
//  A minimal MQTT 3.1.1 client rather than a library, consistent with the
//  no-dependencies decision. Only CONNECT, SUBSCRIBE, PUBLISH-in, PINGREQ and
//  DISCONNECT are implemented -- the app never publishes.
//

import Combine
import CoreLocation
import Foundation
import Network

@MainActor
final class MeshtasticMQTTClient: ObservableObject {
    static let shared = MeshtasticMQTTClient()

    // The public Meshtastic broker. These credentials are published in the
    // Meshtastic documentation and are the same for everyone; they gate
    // nothing but keep anonymous writes out.
    static let defaultHost = "mqtt.meshtastic.org"
    static let defaultPort: UInt16 = 1883
    static let defaultUsername = "meshdev"
    static let defaultPassword = "large4cats"

    /// The JSON topic tree, which carries messages the broker has already
    /// decoded. Subscribing to the protobuf tree instead would mean holding
    /// the channel key for every mesh we listen to.
    static let defaultTopic = "msh/+/2/json/#"

    enum State: Equatable {
        case disconnected
        case connecting
        case connected
        case failed(String)

        var label: String {
            switch self {
            case .disconnected:       return "Disconnected"
            case .connecting:         return "Connecting…"
            case .connected:          return "Connected"
            case .failed(let reason): return "Failed: \(reason)"
            }
        }
    }

    @Published private(set) var state: State = .disconnected
    @Published private(set) var messagesReceived = 0
    @Published private(set) var positionsForwarded = 0
    @Published private(set) var lastMessageAt: Date?

    @Published var host = defaultHost
    @Published var port = defaultPort
    @Published var username = defaultUsername
    @Published var password = defaultPassword
    @Published var topic = defaultTopic

    /// Only forward positions for this node. Without it the app would ingest
    /// every position on the public mesh.
    @Published var balloonNodeID: UInt32?

    private var connection: NWConnection?
    private var buffer = Data()
    private var pingTimer: Timer?
    private var packetIdentifier: UInt16 = 1

    private init() {}

    // MARK: - Connection

    func connect() {
        guard state != .connected, state != .connecting else { return }
        state = .connecting
        buffer.removeAll()

        let endpoint = NWEndpoint.hostPort(
            host: NWEndpoint.Host(host),
            port: NWEndpoint.Port(rawValue: port) ?? 1883
        )

        let connection = NWConnection(to: endpoint, using: .tcp)
        self.connection = connection

        connection.stateUpdateHandler = { [weak self] newState in
            Task { @MainActor in self?.handle(networkState: newState) }
        }
        connection.start(queue: .global(qos: .utility))
    }

    func disconnect() {
        pingTimer?.invalidate()
        pingTimer = nil

        if state == .connected {
            send(Data([0xE0, 0x00]))  // DISCONNECT
        }

        connection?.cancel()
        connection = nil
        state = .disconnected
    }

    private func handle(networkState: NWConnection.State) {
        switch networkState {
        case .ready:
            sendConnect()
            receive()
        case .failed(let error):
            state = .failed(error.localizedDescription)
            connection = nil
        case .cancelled:
            if state != .disconnected { state = .disconnected }
        case .waiting(let error):
            state = .failed(error.localizedDescription)
        default:
            break
        }
    }

    // MARK: - MQTT

    private func send(_ data: Data) {
        connection?.send(content: data, completion: .contentProcessed { _ in })
    }

    private func sendConnect() {
        var payload = Data()
        payload.append(mqttString("MQTT"))
        payload.append(0x04)  // protocol level 4 = MQTT 3.1.1

        // Clean session, plus username and password flags.
        payload.append(0x02 | 0x80 | 0x40)

        payload.append(contentsOf: [0x00, 0x3C])  // 60-second keepalive

        // A stable-but-unique client id. Reusing another client's id would
        // make the broker disconnect them.
        payload.append(mqttString("raptorhab-\(UInt32.random(in: 0...UInt32.max))"))
        payload.append(mqttString(username))
        payload.append(mqttString(password))

        send(packet(type: 0x10, payload: payload))
    }

    private func sendSubscribe() {
        var payload = Data()
        payload.append(contentsOf: [UInt8(packetIdentifier >> 8), UInt8(packetIdentifier & 0xFF)])
        packetIdentifier &+= 1

        payload.append(mqttString(topic))
        payload.append(0x00)  // QoS 0

        send(packet(type: 0x82, payload: payload))
    }

    private func packet(type: UInt8, payload: Data) -> Data {
        var data = Data([type])
        data.append(remainingLength(payload.count))
        data.append(payload)
        return data
    }

    /// MQTT's variable-length integer: 7 bits per byte, high bit continues.
    private func remainingLength(_ length: Int) -> Data {
        var remaining = length
        var out = Data()
        repeat {
            var byte = UInt8(remaining % 128)
            remaining /= 128
            if remaining > 0 { byte |= 0x80 }
            out.append(byte)
        } while remaining > 0
        return out
    }

    private func mqttString(_ text: String) -> Data {
        let bytes = Data(text.utf8)
        var out = Data([UInt8(bytes.count >> 8), UInt8(bytes.count & 0xFF)])
        out.append(bytes)
        return out
    }

    private func receive() {
        connection?.receive(minimumIncompleteLength: 1, maximumLength: 65536) {
            [weak self] data, _, isComplete, error in
            guard let self else { return }

            if let data, !data.isEmpty {
                Task { @MainActor in self.ingest(data) }
            }

            if isComplete || error != nil {
                Task { @MainActor in
                    if let error { self.state = .failed(error.localizedDescription) }
                    else { self.state = .disconnected }
                }
                return
            }

            // receive() is main-actor isolated; hop back rather than calling
            // it from this network-queue callback.
            Task { @MainActor in self.receive() }
        }
    }

    private func ingest(_ data: Data) {
        buffer.append(data)

        while let (type, payload, consumed) = nextPacket() {
            buffer.removeFirst(consumed)

            switch type & 0xF0 {
            case 0x20:  // CONNACK
                let accepted = payload.count >= 2 && payload[payload.startIndex + 1] == 0
                if accepted {
                    state = .connected
                    sendSubscribe()
                    startPinging()
                } else {
                    let code = payload.count >= 2 ? payload[payload.startIndex + 1] : 255
                    state = .failed("broker refused the connection (code \(code))")
                    disconnect()
                }

            case 0x30:  // PUBLISH
                handlePublish(payload)

            default:
                break  // SUBACK, PINGRESP: nothing to do
            }
        }
    }

    /// Decode one packet from the buffer, or nil if it is incomplete.
    private func nextPacket() -> (UInt8, Data, Int)? {
        guard buffer.count >= 2 else { return nil }

        let bytes = [UInt8](buffer)
        let type = bytes[0]

        var length = 0
        var multiplier = 1
        var index = 1

        while true {
            guard index < bytes.count else { return nil }
            guard index <= 4 else { return nil }  // malformed length

            let byte = bytes[index]
            length += Int(byte & 0x7F) * multiplier
            multiplier *= 128
            index += 1
            if byte & 0x80 == 0 { break }
        }

        let total = index + length
        guard bytes.count >= total else { return nil }

        return (type, Data(bytes[index..<total]), total)
    }

    private func startPinging() {
        pingTimer?.invalidate()
        pingTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: true) { _ in
            Task { @MainActor [weak self] in
                guard self?.state == .connected else { return }
                self?.send(Data([0xC0, 0x00]))  // PINGREQ
            }
        }
    }

    // MARK: - Meshtastic payloads

    private func handlePublish(_ payload: Data) {
        // PUBLISH at QoS 0: topic length, topic, then the message.
        guard payload.count >= 2 else { return }
        let bytes = [UInt8](payload)
        let topicLength = Int(bytes[0]) << 8 | Int(bytes[1])
        guard payload.count >= 2 + topicLength else { return }

        let message = Data(bytes[(2 + topicLength)...])
        messagesReceived += 1
        lastMessageAt = Date()

        guard let object = try? JSONSerialization.jsonObject(with: message),
              let json = object as? [String: Any] else { return }

        // The JSON topic wraps everything as
        // {"from": <id>, "type": "position", "payload": {...}}
        guard let from = (json["from"] as? NSNumber)?.uint32Value else { return }
        guard from == balloonNodeID else { return }
        guard json["type"] as? String == "position" else { return }
        guard let body = json["payload"] as? [String: Any] else { return }

        guard let latitudeI = (body["latitude_i"] as? NSNumber)?.doubleValue,
              let longitudeI = (body["longitude_i"] as? NSNumber)?.doubleValue else { return }

        let coordinate = CLLocationCoordinate2D(
            latitude: latitudeI / 1e7, longitude: longitudeI / 1e7
        )
        guard coordinate.isValid else { return }

        let altitude = (body["altitude"] as? NSNumber)?.doubleValue ?? 0
        let timestamp = (json["timestamp"] as? NSNumber)
            .map { Date(timeIntervalSince1970: $0.doubleValue) } ?? Date()

        positionsForwarded += 1

        PositionFusion.shared.submit(PositionFix(
            source: .meshtasticMQTT,
            coordinate: coordinate,
            altitude: altitude,
            timestamp: timestamp,
            satellites: (body["sats_in_view"] as? NSNumber)?.intValue,
            rssi: (json["rssi"] as? NSNumber)?.intValue,
            snr: (json["snr"] as? NSNumber)?.floatValue,
            detail: "via \(json["sender"] as? String ?? "MQTT gateway")"
        ))
    }
}
