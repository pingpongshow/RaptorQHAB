//
//  PiLinkManager.swift
//  RaptorHabGS
//
//  Talks to the payload over its USB gadget port: configuration on one
//  channel, a terminal on another.
//
//  USB only, by design. The payload service refuses to bind to anything but
//  its gadget TTY, so a shell is reachable only by someone holding the cable
//  and never over the radio.
//

import Combine
import Foundation

// MARK: - Schema

/// One configurable payload parameter, as described by the Pi.
///
/// The form is generated from these, so adding a parameter on the payload
/// makes it appear in the UI with no change here.
struct ParameterSpec: Codable, Identifiable, Hashable {
    let name: String
    let kind: String
    let category: String
    let description: String
    let apply: String
    let advanced: Bool
    let secret: Bool
    let minimum: Double?
    let maximum: Double?
    let unit: String?
    let env: String?
    let choices: [ParameterValue]?
    let choiceLabels: [String]?
    let `default`: ParameterValue?

    var id: String { name }
    var requiresRestart: Bool { apply == "restart" }

    enum CodingKeys: String, CodingKey {
        case name, kind, category, description, apply, advanced, secret
        case minimum, maximum, unit, env, choices
        case choiceLabels = "choice_labels"
        case `default`
    }
}

struct ParameterSchema: Codable {
    let categories: [String]
    let parameters: [ParameterSpec]
}

/// A configuration value, which may be a bool, number, string, or a
/// two-element list for a resolution.
enum ParameterValue: Codable, Hashable, CustomStringConvertible {
    case bool(Bool)
    case number(Double)
    case string(String)
    case list([Double])
    case null

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() { self = .null }
        else if let value = try? container.decode(Bool.self) { self = .bool(value) }
        else if let value = try? container.decode(Double.self) { self = .number(value) }
        else if let value = try? container.decode(String.self) { self = .string(value) }
        else if let value = try? container.decode([Double].self) { self = .list(value) }
        else {
            throw DecodingError.dataCorruptedError(
                in: container, debugDescription: "unsupported parameter value"
            )
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .bool(let value):   try container.encode(value)
        case .number(let value):
            // Encode whole numbers as integers, so an int-typed parameter on
            // the Pi does not receive 22.0 and reject it.
            if value == value.rounded() && abs(value) < 1e15 {
                try container.encode(Int(value))
            } else {
                try container.encode(value)
            }
        case .string(let value): try container.encode(value)
        case .list(let value):   try container.encode(value.map { Int($0) })
        case .null:              try container.encodeNil()
        }
    }

    var description: String {
        switch self {
        case .bool(let value):   return value ? "true" : "false"
        case .number(let value):
            return value == value.rounded() ? String(Int(value)) : String(format: "%g", value)
        case .string(let value): return value
        case .list(let value):   return value.map { String(Int($0)) }.joined(separator: " x ")
        case .null:              return "—"
        }
    }

    var doubleValue: Double? {
        switch self {
        case .number(let value): return value
        case .bool(let value):   return value ? 1 : 0
        case .string(let value): return Double(value)
        default: return nil
        }
    }

    var boolValue: Bool {
        switch self {
        case .bool(let value):   return value
        case .number(let value): return value != 0
        case .string(let value): return ["true", "1", "yes", "on"].contains(value.lowercased())
        default: return false
        }
    }

    var stringValue: String {
        if case .string(let value) = self { return value }
        return description
    }
}

// MARK: - Responses

struct PayloadIdentity: Codable {
    let protocolVersion: Int
    let callsign: String
    let payloadId: Int
    let hostname: String
    let stateRoot: String

    enum CodingKeys: String, CodingKey {
        case protocolVersion = "protocol_version"
        case callsign
        case payloadId = "payload_id"
        case hostname
        case stateRoot = "state_root"
    }
}

struct ConfigUpdateResult: Codable {
    let ok: Bool
    let applied: [String]
    let rejected: [String: String]
    let restartRequired: [String]
    let saved: Bool?

    enum CodingKeys: String, CodingKey {
        case ok, applied, rejected, saved
        case restartRequired = "restart_required"
    }
}

struct PayloadStatus: Codable {
    struct Service: Codable {
        let name: String?
        let active: String?
        let sub: String?
        let restarts: Int?
    }
    struct System: Codable {
        let uptimeSec: Double?
        let cpuTempC: Double?
        let memoryPercent: Double?
        let load: Double?

        enum CodingKeys: String, CodingKey {
            case uptimeSec = "uptime_sec"
            case cpuTempC = "cpu_temp_c"
            case memoryPercent = "memory_percent"
            case load
        }
    }
    struct Storage: Codable {
        let freeBytes: Int?
        let percentUsed: Double?
        let imageCount: Int?

        enum CodingKeys: String, CodingKey {
            case freeBytes = "free_bytes"
            case percentUsed = "percent_used"
            case imageCount = "image_count"
        }
    }

    let service: Service?
    let system: System?
    let storage: Storage?
}

enum PiLinkError: LocalizedError {
    case notConnected
    case timeout(String)
    case remote(String)
    case decoding(String)

    var errorDescription: String? {
        switch self {
        case .notConnected:          return "Not connected to a payload"
        case .timeout(let method):   return "The payload did not answer \(method)"
        case .remote(let message):   return message
        case .decoding(let message): return "Could not read the payload's reply: \(message)"
        }
    }
}

// MARK: - Manager

@MainActor
final class PiLinkManager: ObservableObject {
    static let shared = PiLinkManager()

    @Published private(set) var availableDevices: [SerialDevice] = []
    @Published private(set) var isConnected = false
    @Published private(set) var identity: PayloadIdentity?
    @Published private(set) var schema: ParameterSchema?
    @Published private(set) var values: [String: ParameterValue] = [:]
    @Published private(set) var secretFingerprints: [String: String] = [:]
    @Published private(set) var status: PayloadStatus?
    @Published private(set) var lastError: String?
    @Published private(set) var isBusy = false

    /// Set when a change has been applied that only takes effect on restart.
    @Published private(set) var pendingRestart: Set<String> = []

    @Published private(set) var consoleOutput = Data()
    @Published private(set) var shellRunning = false

    private let port = RawSerialPort()
    private let decoder = LinkFrameDecoder()

    private var nextRequestID = 1
    private var pending: [Int: CheckedContinuation<Data, Error>] = [:]
    private var statusTimer: Timer?

    private init() {
        port.onData = { [weak self] data in
            Task { @MainActor in self?.ingest(data) }
        }
        port.onDisconnect = { [weak self] reason in
            Task { @MainActor in self?.handleDisconnect(reason) }
        }
        refreshDevices()
    }

    // MARK: - Discovery

    func refreshDevices() {
        let all: [SerialDevice] = SerialDeviceDiscovery.discover()
        availableDevices = all.filter { device in
            device.kind == SerialDeviceKind.raptorHabPayload
                || device.kind == SerialDeviceKind.genericUSB
        }
    }

    /// The payload, if exactly one is plugged in.
    var autoDetectedDevice: SerialDevice? {
        let payloads = availableDevices.filter { $0.kind == .raptorHabPayload }
        return payloads.count == 1 ? payloads.first : nil
    }

    // MARK: - Connection

    func connect(to device: SerialDevice) async {
        disconnect()
        lastError = nil
        isBusy = true
        defer { isBusy = false }

        do {
            try port.open(path: device.path, baudRate: device.kind.defaultBaudRate)
        } catch {
            lastError = error.localizedDescription
            return
        }

        decoder.reset()
        isConnected = true

        do {
            identity = try await call("hello", as: PayloadIdentity.self)
            schema = try await call("get_schema", as: ParameterSchema.self)
            try await reloadConfig()
            await refreshStatus()
            startStatusPolling()
        } catch {
            lastError = error.localizedDescription
            disconnect()
        }
    }

    func disconnect() {
        statusTimer?.invalidate()
        statusTimer = nil

        // Fail anything in flight, or its caller waits forever.
        for continuation in pending.values {
            continuation.resume(throwing: PiLinkError.notConnected)
        }
        pending.removeAll()

        port.close()
        isConnected = false
        shellRunning = false
        identity = nil
        status = nil
    }

    private func handleDisconnect(_ reason: String?) {
        guard isConnected else { return }
        lastError = reason
        disconnect()
    }

    // MARK: - Configuration

    func reloadConfig() async throws {
        struct Response: Codable {
            let values: [String: ParameterValue]
            let secrets: [String: String]
        }
        let response = try await call("get_config", as: Response.self)
        values = response.values
        secretFingerprints = response.secrets
    }

    @discardableResult
    func setConfig(_ updates: [String: ParameterValue]) async -> ConfigUpdateResult? {
        isBusy = true
        defer { isBusy = false }

        do {
            let encoded = try JSONEncoder().encode(updates)
            let object = try JSONSerialization.jsonObject(with: encoded)
            let data = try await callRaw("set_config", rawParams: ["values": object])
            let result = try JSONDecoder().decode(ConfigUpdateResult.self, from: data)
            if result.ok {
                try await reloadConfig()
                pendingRestart.formUnion(result.restartRequired)
                lastError = nil
            } else {
                lastError = result.rejected
                    .map { "\($0.key): \($0.value)" }
                    .sorted()
                    .joined(separator: "\n")
            }
            return result
        } catch {
            lastError = error.localizedDescription
            return nil
        }
    }

    func resetToDefaults(_ names: [String]?) async {
        isBusy = true
        defer { isBusy = false }
        do {
            let params: [String: Any] = names.map { ["names": $0] } ?? [:]
            _ = try await callRaw("reset_config", rawParams: params)
            try await reloadConfig()
        } catch {
            lastError = error.localizedDescription
        }
    }

    func restartPayloadService() async {
        isBusy = true
        defer { isBusy = false }
        do {
            _ = try await callRaw("restart_service", rawParams: [:])
            pendingRestart.removeAll()
            lastError = nil
            // The service takes a moment to come back before status is useful.
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            await refreshStatus()
        } catch {
            lastError = error.localizedDescription
        }
    }

    func generatePSK(length: Int = 32) async -> (base64: String, fingerprint: String)? {
        struct Response: Codable {
            let pskBase64: String
            let fingerprint: String
            enum CodingKeys: String, CodingKey {
                case pskBase64 = "psk_base64"
                case fingerprint
            }
        }
        do {
            let data = try await callRaw("generate_psk", rawParams: ["length": length])
            let response = try JSONDecoder().decode(Response.self, from: data)
            return (response.pskBase64, response.fingerprint)
        } catch {
            lastError = error.localizedDescription
            return nil
        }
    }

    func refreshStatus() async {
        guard isConnected else { return }
        status = try? await call("get_status", as: PayloadStatus.self)
    }

    private func startStatusPolling() {
        statusTimer?.invalidate()
        statusTimer = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { _ in
            Task { @MainActor [weak self] in await self?.refreshStatus() }
        }
    }

    // MARK: - Console

    func startShell(rows: Int = 30, cols: Int = 100) async {
        do {
            _ = try await callRaw("shell_start", rawParams: ["rows": rows, "cols": cols])
            consoleOutput.removeAll(keepingCapacity: true)
            shellRunning = true
        } catch {
            lastError = error.localizedDescription
        }
    }

    func stopShell() async {
        _ = try? await callRaw("shell_stop", rawParams: [:])
        shellRunning = false
    }

    func sendConsole(_ text: String) {
        guard shellRunning, let data = text.data(using: .utf8) else { return }
        sendFrame(channel: .console, payload: data)
    }

    func resizeShell(rows: Int, cols: Int) {
        guard shellRunning else { return }
        Task { _ = try? await callRaw("shell_resize", rawParams: ["rows": rows, "cols": cols]) }
    }

    func clearConsole() {
        consoleOutput.removeAll(keepingCapacity: true)
    }

    // MARK: - Transport

    private func sendFrame(channel: LinkChannel, payload: Data) {
        guard let frame = try? LinkFrame.encode(channel: channel, payload: payload) else {
            return
        }
        port.write(frame)
    }

    private func call<T: Decodable>(
        _ method: String,
        as type: T.Type,
        timeout: TimeInterval = 15
    ) async throws -> T {
        let data = try await callRaw(method, timeout: timeout)
        do {
            return try JSONDecoder().decode(T.self, from: data)
        } catch {
            throw PiLinkError.decoding("\(method): \(error)")
        }
    }

    @discardableResult
    private func callRaw(
        _ method: String,
        rawParams: [String: Any] = [:],
        timeout: TimeInterval = 15
    ) async throws -> Data {
        guard isConnected else { throw PiLinkError.notConnected }

        let requestID = nextRequestID
        nextRequestID += 1

        var request: [String: Any] = ["id": requestID, "method": method]
        if !rawParams.isEmpty {
            request["params"] = rawParams
        }

        let body = try JSONSerialization.data(withJSONObject: request)

        return try await withThrowingTaskGroup(of: Data.self) { group in
            group.addTask { @MainActor in
                try await withCheckedThrowingContinuation { continuation in
                    self.pending[requestID] = continuation
                    self.sendFrame(channel: .control, payload: body)
                }
            }
            group.addTask {
                try await Task.sleep(nanoseconds: UInt64(timeout * 1_000_000_000))
                throw PiLinkError.timeout(method)
            }

            guard let result = try await group.next() else {
                throw PiLinkError.timeout(method)
            }
            group.cancelAll()
            await MainActor.run { self.pending[requestID] = nil }
            return result
        }
    }

    private func ingest(_ data: Data) {
        for (channel, payload) in decoder.feed(data) {
            switch LinkChannel(rawValue: channel) {
            case .control: handleControl(payload)
            case .console: handleConsole(payload)
            case .event:   handleEvent(payload)
            case .none:    break
            }
        }
    }

    private func handleControl(_ payload: Data) {
        guard let object = try? JSONSerialization.jsonObject(with: payload),
              let message = object as? [String: Any] else { return }

        guard let requestID = message["id"] as? Int,
              let continuation = pending.removeValue(forKey: requestID) else {
            return  // a reply to a request that already timed out
        }

        if let ok = message["ok"] as? Bool, ok {
            let result = message["result"] ?? [:]
            if let data = try? JSONSerialization.data(withJSONObject: result) {
                continuation.resume(returning: data)
            } else {
                continuation.resume(throwing: PiLinkError.decoding("unserialisable result"))
            }
        } else {
            let message = message["error"] as? String ?? "the payload reported an error"
            continuation.resume(throwing: PiLinkError.remote(message))
        }
    }

    private func handleConsole(_ payload: Data) {
        consoleOutput.append(payload)

        // Cap the scrollback. An unbounded buffer on a chatty shell will
        // eventually make the text view unusable.
        let limit = 256 * 1024
        if consoleOutput.count > limit {
            consoleOutput.removeFirst(consoleOutput.count - limit)
        }
    }

    private func handleEvent(_ payload: Data) {
        guard let object = try? JSONSerialization.jsonObject(with: payload),
              let message = object as? [String: Any],
              let event = message["event"] as? String else { return }

        if event == "shell_exited" {
            shellRunning = false
        }
    }
}
