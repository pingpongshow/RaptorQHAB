//
//  BLESerialManager.swift
//  RaptorHabMobile
//
//  Manages Bluetooth LE communication with RaptorModem
//  Uses CoreBluetooth framework for iOS/iPadOS
//
//  Protocol:
//    Nordic UART Service (NUS) compatible UUIDs
//    TX Characteristic (notify): Receives packets from modem
//    RX Characteristic (write): Sends configuration to modem
//
//  Frame format (same as USB):
//    [0x7E][LEN_HI][LEN_LO][RSSI_INT][RSSI_FRAC][SNR_INT][SNR_FRAC][DATA...][CHECKSUM][0x7E]
//

import Foundation
import CoreBluetooth
import Combine

// MARK: - BLE Serial Manager

@MainActor
class BLESerialManager: NSObject, ObservableObject {
    
    // Nordic UART Service UUIDs
    static let serviceUUID = CBUUID(string: "6E400001-B5A3-F393-E0A9-E50E24DCCA9E")
    static let rxCharacteristicUUID = CBUUID(string: "6E400002-B5A3-F393-E0A9-E50E24DCCA9E")  // Write to modem
    static let txCharacteristicUUID = CBUUID(string: "6E400003-B5A3-F393-E0A9-E50E24DCCA9E")  // Notify from modem
    
    static let modemName = "RaptorModem"
    
    // MARK: - Published State
    
    @Published var isScanning = false
    @Published var isConnected = false
    @Published var isConfigured = false
    @Published var connectionStatus: String = "Not Connected"
    @Published var discoveredDevices: [DiscoveredDevice] = []
    @Published var lastRSSI: Float = -120
    @Published var lastSNR: Float = 0
    
    // Statistics
    @Published var bytesReceived: Int = 0
    @Published var framesExtracted: Int = 0
    @Published var packetsReceived: Int = 0
    @Published var checksumFailures: Int = 0
    @Published var noRaptFailures: Int = 0
    
    // MARK: - Callbacks
    
    var onPacketReceived: ((Data, Float, Float) -> Void)?
    var onError: ((String) -> Void)?
    
    // MARK: - Private State
    
    private var centralManager: CBCentralManager!
    private var peripheral: CBPeripheral?
    private var txCharacteristic: CBCharacteristic?
    private var rxCharacteristic: CBCharacteristic?
    
    // Frame extraction state (HDLC-style with byte stuffing)
    private var receiveBuffer = Data()
    private var inFrame = false
    private var escapeNext = false
    
    private var configCompletion: ((Bool, String?) -> Void)?
    private var configResponseBuffer = ""
    
    // MARK: - Discovered Device
    
    struct DiscoveredDevice: Identifiable, Equatable {
        let id: UUID
        let peripheral: CBPeripheral
        let name: String
        let rssi: Int
        
        static func == (lhs: DiscoveredDevice, rhs: DiscoveredDevice) -> Bool {
            return lhs.id == rhs.id
        }
    }
    
    // MARK: - Initialization
    
    override init() {
        super.init()
        centralManager = CBCentralManager(delegate: self, queue: nil)
    }
    
    // MARK: - Public Methods
    
    func startScanning() {
        guard centralManager.state == .poweredOn else {
            connectionStatus = "Bluetooth not available"
            return
        }
        
        discoveredDevices.removeAll()
        isScanning = true
        connectionStatus = "Scanning..."
        
        // Scan for devices with our service
        centralManager.scanForPeripherals(
            withServices: [Self.serviceUUID],
            options: [CBCentralManagerScanOptionAllowDuplicatesKey: false]
        )
        
        print("[BLE] Started scanning for RaptorModem")
    }
    
    func stopScanning() {
        centralManager.stopScan()
        isScanning = false
        if !isConnected {
            connectionStatus = "Scan stopped"
        }
    }
    
    func connect(to device: DiscoveredDevice) {
        stopScanning()
        connectionStatus = "Connecting to \(device.name)..."
        
        peripheral = device.peripheral
        peripheral?.delegate = self
        centralManager.connect(device.peripheral, options: nil)
    }
    
    @discardableResult
    func autoConnect() -> Bool {
        // Start scanning, will auto-connect when RaptorModem is found
        startScanning()
        return true
    }
    
    func disconnect() {
        if let peripheral = peripheral {
            centralManager.cancelPeripheralConnection(peripheral)
        }
        cleanupConnection()
    }
    
    func configureModem(_ config: ModemConfig, completion: @escaping (Bool, String?) -> Void) {
        guard isConnected, let rxChar = rxCharacteristic, let peripheral = peripheral else {
            completion(false, "Not connected to modem")
            return
        }
        
        configCompletion = completion
        configResponseBuffer = ""
        
        // Build config command
        let command = config.configCommand
        guard let data = command.data(using: .utf8) else {
            completion(false, "Failed to encode command")
            return
        }
        
        print("[BLE] Sending config: \(command)")
        peripheral.writeValue(data, for: rxChar, type: .withResponse)
        
        // Timeout after 5 seconds
        DispatchQueue.main.asyncAfter(deadline: .now() + 5.0) { [weak self] in
            if self?.configCompletion != nil {
                self?.configCompletion?(false, "Configuration timeout")
                self?.configCompletion = nil
            }
        }
    }
    
    func refreshAvailableAccessories() {
        // Re-scan for devices
        if isScanning {
            stopScanning()
        }
        startScanning()
    }
    
    // MARK: - Private Methods
    
    private func cleanupConnection() {
        isConnected = false
        isConfigured = false
        connectionStatus = "Disconnected"
        txCharacteristic = nil
        rxCharacteristic = nil
        receiveBuffer.removeAll()
        inFrame = false
        escapeNext = false
    }
    
    private func processReceivedData(_ data: Data) {
        bytesReceived += data.count
        
        // Check if this is a config response (text starting with CFG_)
        if let text = String(data: data, encoding: .utf8), 
           text.hasPrefix("CFG_") || configResponseBuffer.count > 0 {
            configResponseBuffer += text
            
            // Check for complete response
            if configResponseBuffer.contains("\n") {
                let response = configResponseBuffer.trimmingCharacters(in: .whitespacesAndNewlines)
                configResponseBuffer = ""
                
                if response.hasPrefix("CFG_OK:") {
                    print("[BLE] Config OK: \(response)")
                    isConfigured = true
                    configCompletion?(true, nil)
                    configCompletion = nil
                } else if response.hasPrefix("CFG_ERR:") {
                    let error = String(response.dropFirst(8))
                    print("[BLE] Config error: \(error)")
                    configCompletion?(false, error)
                    configCompletion = nil
                }
            }
            return
        }
        
        // Process binary frame data with HDLC byte stuffing
        for byte in data {
            processReceivedByte(byte)
        }
    }
    
    private func processReceivedByte(_ byte: UInt8) {
        if escapeNext {
            escapeNext = false
            if byte == 0x5E {
                receiveBuffer.append(0x7E)
            } else if byte == 0x5D {
                receiveBuffer.append(0x7D)
            } else {
                // Invalid escape sequence
                receiveBuffer.append(byte)
            }
            return
        }
        
        switch byte {
        case 0x7E:  // Frame delimiter
            if inFrame && receiveBuffer.count > 0 {
                // End of frame
                extractFrame()
            }
            // Start new frame
            receiveBuffer.removeAll()
            inFrame = true
            
        case 0x7D:  // Escape character
            if inFrame {
                escapeNext = true
            }
            
        default:
            if inFrame {
                receiveBuffer.append(byte)
            }
        }
    }
    
    private func extractFrame() {
        framesExtracted += 1
        
        // Frame format: [LEN_HI][LEN_LO][RSSI_INT][RSSI_FRAC][SNR_INT][SNR_FRAC][DATA...][CHECKSUM]
        guard receiveBuffer.count >= 8 else {
            print("[BLE] Frame too short: \(receiveBuffer.count) bytes")
            return
        }
        
        let lenHi = receiveBuffer[0]
        let lenLo = receiveBuffer[1]
        let expectedLen = Int(lenHi) << 8 | Int(lenLo)
        
        let rssiInt = Int8(bitPattern: receiveBuffer[2])
        let rssiFrac = receiveBuffer[3]
        let snrInt = Int8(bitPattern: receiveBuffer[4])
        let snrFrac = receiveBuffer[5]
        
        let rssi = Float(rssiInt) + Float(rssiFrac) / 100.0
        let snr = Float(snrInt) + Float(snrFrac) / 100.0
        
        // Extract packet data (between header and checksum)
        let headerLen = 6
        guard receiveBuffer.count >= headerLen + expectedLen + 1 else {
            print("[BLE] Frame incomplete: expected \(expectedLen) bytes, got \(receiveBuffer.count - headerLen - 1)")
            return
        }
        
        let packetData = receiveBuffer.subdata(in: headerLen..<(headerLen + expectedLen))
        let receivedChecksum = receiveBuffer[headerLen + expectedLen]
        
        // Verify checksum
        var calculatedChecksum: UInt8 = lenHi ^ lenLo ^ UInt8(bitPattern: rssiInt) ^ rssiFrac ^ UInt8(bitPattern: snrInt) ^ snrFrac
        for byte in packetData {
            calculatedChecksum ^= byte
        }
        
        guard calculatedChecksum == receivedChecksum else {
            checksumFailures += 1
            print("[BLE] Checksum mismatch: expected \(String(format: "%02X", receivedChecksum)), got \(String(format: "%02X", calculatedChecksum))")
            return
        }
        
        // Verify RAPT sync word
        guard packetData.count >= 4,
              packetData[0] == 0x52,
              packetData[1] == 0x41,
              packetData[2] == 0x50,
              packetData[3] == 0x54 else {
            noRaptFailures += 1
            print("[BLE] Missing RAPT sync word")
            return
        }
        
        // Valid packet!
        packetsReceived += 1
        lastRSSI = rssi
        lastSNR = snr
        
        print("[BLE] Received packet: \(packetData.count) bytes, RSSI: \(rssi) dBm, SNR: \(snr) dB")
        
        // Deliver to callback
        onPacketReceived?(packetData, rssi, snr)
    }
}

// MARK: - CBCentralManagerDelegate

extension BLESerialManager: CBCentralManagerDelegate {
    
    nonisolated func centralManagerDidUpdateState(_ central: CBCentralManager) {
        Task { @MainActor in
            switch central.state {
            case .poweredOn:
                connectionStatus = "Ready"
                print("[BLE] Bluetooth powered on")
            case .poweredOff:
                connectionStatus = "Bluetooth off"
                cleanupConnection()
            case .unauthorized:
                connectionStatus = "Bluetooth unauthorized"
            case .unsupported:
                connectionStatus = "Bluetooth not supported"
            case .resetting:
                connectionStatus = "Bluetooth resetting"
            case .unknown:
                connectionStatus = "Bluetooth unknown"
            @unknown default:
                connectionStatus = "Unknown state"
            }
        }
    }
    
    nonisolated func centralManager(_ central: CBCentralManager, didDiscover peripheral: CBPeripheral,
                                    advertisementData: [String: Any], rssi RSSI: NSNumber) {
        Task { @MainActor in
            // Check if this is RaptorModem
            let name = peripheral.name ?? advertisementData[CBAdvertisementDataLocalNameKey] as? String ?? "Unknown"
            
            if name == Self.modemName {
                let device = DiscoveredDevice(
                    id: peripheral.identifier,
                    peripheral: peripheral,
                    name: name,
                    rssi: RSSI.intValue
                )
                
                // Add if not already discovered
                if !discoveredDevices.contains(where: { $0.id == device.id }) {
                    discoveredDevices.append(device)
                    print("[BLE] Discovered RaptorModem: \(peripheral.identifier)")
                    
                    // Auto-connect to first RaptorModem found
                    connect(to: device)
                }
            }
        }
    }
    
    nonisolated func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        Task { @MainActor in
            print("[BLE] Connected to \(peripheral.name ?? "Unknown")")
            self.peripheral = peripheral
            peripheral.delegate = self
            isConnected = true
            connectionStatus = "Connected - Discovering services..."
            
            // Discover our service
            peripheral.discoverServices([Self.serviceUUID])
        }
    }
    
    nonisolated func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        Task { @MainActor in
            print("[BLE] Failed to connect: \(error?.localizedDescription ?? "Unknown error")")
            connectionStatus = "Connection failed"
            cleanupConnection()
            onError?("Failed to connect: \(error?.localizedDescription ?? "Unknown")")
        }
    }
    
    nonisolated func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral, error: Error?) {
        Task { @MainActor in
            print("[BLE] Disconnected from \(peripheral.name ?? "Unknown")")
            cleanupConnection()
            
            if let error = error {
                print("[BLE] Disconnect error: \(error.localizedDescription)")
                // Try to reconnect
                connectionStatus = "Reconnecting..."
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
                    self?.autoConnect()
                }
            }
        }
    }
}

// MARK: - CBPeripheralDelegate

extension BLESerialManager: CBPeripheralDelegate {
    
    nonisolated func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        Task { @MainActor in
            if let error = error {
                print("[BLE] Service discovery error: \(error.localizedDescription)")
                return
            }
            
            guard let services = peripheral.services else { return }
            
            for service in services {
                if service.uuid == Self.serviceUUID {
                    print("[BLE] Found RaptorModem service")
                    peripheral.discoverCharacteristics(
                        [Self.rxCharacteristicUUID, Self.txCharacteristicUUID],
                        for: service
                    )
                }
            }
        }
    }
    
    nonisolated func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        Task { @MainActor in
            if let error = error {
                print("[BLE] Characteristic discovery error: \(error.localizedDescription)")
                return
            }
            
            guard let characteristics = service.characteristics else { return }
            
            for characteristic in characteristics {
                if characteristic.uuid == Self.txCharacteristicUUID {
                    print("[BLE] Found TX characteristic (notify)")
                    txCharacteristic = characteristic
                    // Subscribe to notifications
                    peripheral.setNotifyValue(true, for: characteristic)
                } else if characteristic.uuid == Self.rxCharacteristicUUID {
                    print("[BLE] Found RX characteristic (write)")
                    rxCharacteristic = characteristic
                }
            }
            
            if txCharacteristic != nil && rxCharacteristic != nil {
                connectionStatus = "Connected to RaptorModem"
                print("[BLE] Ready for communication")
            }
        }
    }
    
    nonisolated func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        Task { @MainActor in
            if let error = error {
                print("[BLE] Update value error: \(error.localizedDescription)")
                return
            }
            
            guard let data = characteristic.value else { return }
            
            if characteristic.uuid == Self.txCharacteristicUUID {
                processReceivedData(data)
            }
        }
    }
    
    nonisolated func peripheral(_ peripheral: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic, error: Error?) {
        Task { @MainActor in
            if let error = error {
                print("[BLE] Write error: \(error.localizedDescription)")
                configCompletion?(false, error.localizedDescription)
                configCompletion = nil
            } else {
                print("[BLE] Write successful")
            }
        }
    }
    
    nonisolated func peripheral(_ peripheral: CBPeripheral, didUpdateNotificationStateFor characteristic: CBCharacteristic, error: Error?) {
        Task { @MainActor in
            if let error = error {
                print("[BLE] Notification state error: \(error.localizedDescription)")
                return
            }
            
            if characteristic.uuid == Self.txCharacteristicUUID {
                if characteristic.isNotifying {
                    print("[BLE] Notifications enabled for TX")
                } else {
                    print("[BLE] Notifications disabled for TX")
                }
            }
        }
    }
}
