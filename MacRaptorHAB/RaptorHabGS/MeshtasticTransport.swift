//
//  MeshtasticTransport.swift
//  RaptorHabGS
//
//  Connects a Meshtastic node over USB serial or Bluetooth.
//
//  Both transports carry the same ToRadio/FromRadio protobuf stream; only the
//  framing differs. Serial prefixes each message with 0x94 0xC3 and a 16-bit
//  big-endian length. BLE writes and reads whole messages through GATT
//  characteristics, so it needs no framing of its own.
//

import CoreBluetooth
import Foundation

protocol MeshtasticTransportDelegate: AnyObject {
    /// A complete FromRadio protobuf arrived.
    func transport(_ transport: MeshtasticTransport, didReceive data: Data)
    func transport(_ transport: MeshtasticTransport, didChangeConnected connected: Bool)
    func transport(_ transport: MeshtasticTransport, didFail error: String)
}

protocol MeshtasticTransport: AnyObject {
    var delegate: MeshtasticTransportDelegate? { get set }
    var isConnected: Bool { get }
    func send(_ toRadio: Data)
    func disconnect()
}

// MARK: - Serial

/// Meshtastic's serial stream API.
///
/// Each message is `0x94 0xC3 <length hi> <length lo> <protobuf>`. The magic
/// exists because a Meshtastic node also emits plain-text log lines on the
/// same port, so the reader has to pick framed messages out of a mixed
/// stream — and resynchronise when it lands mid-message.
final class MeshtasticSerialTransport: MeshtasticTransport {
    private static let magic: [UInt8] = [0x94, 0xC3]
    private static let maxMessage = 512  // the firmware's own limit

    weak var delegate: MeshtasticTransportDelegate?

    private let port = RawSerialPort()
    private var buffer = Data()
    private let bufferLock = NSLock()

    var isConnected: Bool { port.isOpen }

    func connect(to device: SerialDevice) throws {
        try port.open(path: device.path, baudRate: device.kind.defaultBaudRate)

        port.onData = { [weak self] data in self?.ingest(data) }
        port.onDisconnect = { [weak self] reason in
            guard let self else { return }
            self.delegate?.transport(self, didChangeConnected: false)
            if let reason { self.delegate?.transport(self, didFail: reason) }
        }

        delegate?.transport(self, didChangeConnected: true)

        // A node that was mid-message when we attached will otherwise never
        // resynchronise. Sending a wake sequence prompts a fresh start.
        port.write(Data(repeating: 0, count: 32))
    }

    func send(_ toRadio: Data) {
        guard isConnected, toRadio.count <= Self.maxMessage else { return }

        var frame = Data(Self.magic)
        frame.append(UInt8((toRadio.count >> 8) & 0xFF))
        frame.append(UInt8(toRadio.count & 0xFF))
        frame.append(toRadio)
        port.write(frame)
    }

    func disconnect() {
        port.close()
        bufferLock.lock()
        buffer.removeAll()
        bufferLock.unlock()
        delegate?.transport(self, didChangeConnected: false)
    }

    private func ingest(_ data: Data) {
        bufferLock.lock()
        buffer.append(data)

        // Bound the buffer: a node emitting only log text would otherwise
        // grow it without limit.
        if buffer.count > 64 * 1024 {
            buffer.removeFirst(buffer.count - 8 * 1024)
        }

        var messages: [Data] = []

        while true {
            guard buffer.count >= 4 else { break }

            let bytes = [UInt8](buffer)
            guard bytes[0] == Self.magic[0], bytes[1] == Self.magic[1] else {
                // Not a frame start. Skip to the next candidate; anything
                // passed over is a log line, not a message.
                if let index = findMagic(in: bytes, from: 1) {
                    buffer.removeFirst(index)
                    continue
                }
                buffer.removeFirst(max(0, buffer.count - 1))
                break
            }

            let length = Int(bytes[2]) << 8 | Int(bytes[3])
            guard length <= Self.maxMessage else {
                buffer.removeFirst(2)  // bogus length; this was not a real header
                continue
            }
            guard buffer.count >= 4 + length else { break }

            messages.append(Data(buffer[4..<(4 + length)]))
            buffer.removeFirst(4 + length)
        }

        bufferLock.unlock()

        for message in messages {
            delegate?.transport(self, didReceive: message)
        }
    }

    private func findMagic(in bytes: [UInt8], from start: Int) -> Int? {
        var index = start
        while index + 1 < bytes.count {
            if bytes[index] == Self.magic[0] && bytes[index + 1] == Self.magic[1] {
                return index
            }
            index += 1
        }
        return nil
    }
}

// MARK: - Bluetooth

/// Meshtastic's BLE service.
///
/// `fromNum` notifies that something is waiting; the client then drains
/// `fromRadio` by reading it repeatedly until it returns empty. Reading once
/// per notification loses messages whenever more than one queues up.
final class MeshtasticBLETransport: NSObject, MeshtasticTransport {
    static let serviceUUID = CBUUID(string: "6BA1B218-15A8-461F-9FA8-5DCAE273EAFD")
    private static let toRadioUUID = CBUUID(string: "F75C76D2-129E-4DAD-A1DD-7866124401E7")
    private static let fromRadioUUID = CBUUID(string: "2C55E69E-4993-11ED-B878-0242AC120002")
    private static let fromNumUUID = CBUUID(string: "ED9DA18C-A800-4F66-A670-AA7547E34453")

    weak var delegate: MeshtasticTransportDelegate?

    private var central: CBCentralManager!
    private var peripheral: CBPeripheral?
    private var toRadio: CBCharacteristic?
    private var fromRadio: CBCharacteristic?

    private(set) var discovered: [CBPeripheral] = []
    private var onDiscovery: (([CBPeripheral]) -> Void)?
    private var pendingConnect: CBPeripheral?

    var isConnected: Bool { peripheral?.state == .connected && toRadio != nil }

    override init() {
        super.init()
        central = CBCentralManager(delegate: self, queue: .main)
    }

    func startScanning(onDiscovery: @escaping ([CBPeripheral]) -> Void) {
        self.onDiscovery = onDiscovery
        discovered.removeAll()
        guard central.state == .poweredOn else { return }
        central.scanForPeripherals(withServices: [Self.serviceUUID])
    }

    func stopScanning() {
        central.stopScan()
    }

    func connect(to peripheral: CBPeripheral) {
        stopScanning()
        pendingConnect = peripheral
        peripheral.delegate = self
        central.connect(peripheral)
    }

    func send(_ toRadioData: Data) {
        guard let peripheral, let characteristic = toRadio else { return }
        peripheral.writeValue(toRadioData, for: characteristic, type: .withResponse)
    }

    func disconnect() {
        if let peripheral {
            central.cancelPeripheralConnection(peripheral)
        }
        peripheral = nil
        toRadio = nil
        fromRadio = nil
        delegate?.transport(self, didChangeConnected: false)
    }

    /// Read fromRadio until it comes back empty.
    private func drain() {
        guard let peripheral, let characteristic = fromRadio else { return }
        peripheral.readValue(for: characteristic)
    }
}

extension MeshtasticBLETransport: CBCentralManagerDelegate {
    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        if central.state == .poweredOn, onDiscovery != nil {
            central.scanForPeripherals(withServices: [Self.serviceUUID])
        }
    }

    func centralManager(
        _ central: CBCentralManager,
        didDiscover peripheral: CBPeripheral,
        advertisementData: [String: Any],
        rssi RSSI: NSNumber
    ) {
        guard !discovered.contains(where: { $0.identifier == peripheral.identifier }) else {
            return
        }
        discovered.append(peripheral)
        onDiscovery?(discovered)
    }

    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        self.peripheral = peripheral
        peripheral.discoverServices([Self.serviceUUID])
    }

    func centralManager(
        _ central: CBCentralManager,
        didFailToConnect peripheral: CBPeripheral,
        error: Error?
    ) {
        delegate?.transport(self, didFail: error?.localizedDescription ?? "connection failed")
    }

    func centralManager(
        _ central: CBCentralManager,
        didDisconnectPeripheral peripheral: CBPeripheral,
        error: Error?
    ) {
        self.peripheral = nil
        toRadio = nil
        fromRadio = nil
        delegate?.transport(self, didChangeConnected: false)
    }
}

extension MeshtasticBLETransport: CBPeripheralDelegate {
    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        guard let service = peripheral.services?.first(where: {
            $0.uuid == Self.serviceUUID
        }) else {
            delegate?.transport(self, didFail: "this device does not expose the Meshtastic service")
            return
        }
        peripheral.discoverCharacteristics(
            [Self.toRadioUUID, Self.fromRadioUUID, Self.fromNumUUID], for: service
        )
    }

    func peripheral(
        _ peripheral: CBPeripheral,
        didDiscoverCharacteristicsFor service: CBService,
        error: Error?
    ) {
        for characteristic in service.characteristics ?? [] {
            switch characteristic.uuid {
            case Self.toRadioUUID:
                toRadio = characteristic
            case Self.fromRadioUUID:
                fromRadio = characteristic
            case Self.fromNumUUID:
                peripheral.setNotifyValue(true, for: characteristic)
            default:
                break
            }
        }

        if toRadio != nil && fromRadio != nil {
            delegate?.transport(self, didChangeConnected: true)
            drain()
        }
    }

    func peripheral(
        _ peripheral: CBPeripheral,
        didUpdateValueFor characteristic: CBCharacteristic,
        error: Error?
    ) {
        if characteristic.uuid == Self.fromNumUUID {
            drain()
            return
        }

        guard characteristic.uuid == Self.fromRadioUUID else { return }

        guard let value = characteristic.value, !value.isEmpty else {
            return  // queue drained
        }

        delegate?.transport(self, didReceive: value)

        // Keep reading: more than one message may be queued, and stopping
        // after the first would silently lose the rest.
        drain()
    }
}
