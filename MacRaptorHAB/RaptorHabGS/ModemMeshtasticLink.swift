//
//  ModemMeshtasticLink.swift
//  Meshtastic over the RaptorHAB modem.
//
//  The dual-E22 modem carries two radios: one listening for RAPTOR image
//  traffic, one sitting on the Meshtastic channel. It forwards whole LoRa
//  packets, still encrypted, on their own frame delimiter (0x7B), and it
//  accepts packets to transmit.
//
//  The modem holds no channel keys, deliberately -- a borrowed board never
//  carries them. So everything Meshtastic-shaped happens here: decrypt and
//  parse on the way in, build and encrypt on the way out.
//
//  This is the ground station's *own* radio, which is what makes it different
//  from the two Meshtastic sources the app already has. A node plugged into
//  the USB port hears what a node on the ground hears. MQTT hears what the
//  internet has been told. This hears the balloon directly, at whatever range
//  the ground station's own antenna reaches, and it is the only one of the
//  three that can transmit to the balloon on the private channel without a
//  second radio.
//

import Foundation

final class ModemMeshtasticLink: ObservableObject {

    struct Channel {
        let name: String
        let key: Data
    }

    /// What the modem answers an `MTX:` with.
    private enum Reply {
        static let ok = "MTX_OK"
        static let error = "MTX_ERR"
    }

    @Published private(set) var heard: Int = 0
    @Published private(set) var decrypted: Int = 0
    @Published private(set) var undecryptable: Int = 0
    @Published private(set) var sent: Int = 0
    @Published private(set) var sendFailed: Int = 0
    @Published private(set) var lastError: String?

    /// Called for every packet that decrypted on one of our channels.
    var onPacket: ((MeshtasticPacket) -> Void)?

    private(set) var channels: [Channel] = []
    private weak var serial: SerialPortManager?
    private var nodeID: UInt32 = 0

    private let replyLock = NSCondition()
    private var pendingReply: String?

    init(serial: SerialPortManager? = nil, callsign: String = "GROUND") {
        self.serial = serial
        self.nodeID = Self.nodeID(fromCallsign: callsign)
        // LongFast by default, so a bare connection still shows beacons from
        // any stock radio.
        addChannel(name: "LongFast", key: "AQ==")
    }

    func attach(serial: SerialPortManager) {
        self.serial = serial
    }

    /// A private channel is the only one the balloon accepts commands on.
    @discardableResult
    func addChannel(name: String, key: String) -> Bool {
        guard let psk = MeshtasticCrypto.parsePSK(key) else {
            // A bad key must not be silently equivalent to no key: the operator
            // asked for that channel and would otherwise never find out.
            lastError = "Channel \(name): key is not valid base64 or hex"
            return false
        }
        // parsePSK gives the PSK as configured; expand turns Meshtastic's
        // one-byte shorthand ("AQ==" for the default LongFast key) into the
        // actual AES key. Skipping this hands AES a one-byte key and a channel
        // the operator configured perfectly correctly stops working.
        channels.append(Channel(name: name, key: MeshtasticCrypto.expand(psk: psk)))
        return true
    }

    func setChannels(primary: (name: String, key: String),
                     privateChannel: (name: String, key: String)?) {
        channels.removeAll()
        addChannel(name: primary.name, key: primary.key)
        if let priv = privateChannel, !priv.key.isEmpty {
            addChannel(name: priv.name, key: priv.key)
        }
    }

    var privateChannel: Channel? { channels.count > 1 ? channels[1] : nil }

    // MARK: - Receive

    /// Decode frames the modem forwarded on the Meshtastic delimiter.
    func handleFrames(_ frames: [(rssi: Float, snr: Float, data: Data)]) {
        for frame in frames {
            heard += 1

            let keys = channels.map { $0.key }
            guard let packet = MeshtasticProtocol.parsePacket(
                frame.data,
                channelKeys: keys,
                rssi: Int(frame.rssi),
                snr: MeshtasticSNR.isMeasured(frame.snr) ? frame.snr : nil
            ) else {
                // Heard, but not for us -- another channel, or another mesh.
                // Counted rather than dropped silently: hearing traffic you
                // cannot read still says the radio is working.
                undecryptable += 1
                continue
            }

            decrypted += 1
            onPacket?(packet)
        }
    }

    // MARK: - Transmit

    /// Send a text message. Returns whether the modem confirmed it.
    @discardableResult
    func sendText(_ text: String,
                  destination: UInt32 = MeshtasticHeader.broadcast,
                  overPrivateChannel: Bool = false,
                  hopLimit: UInt8 = 3,
                  timeout: TimeInterval = 5.0) -> (sent: Bool, detail: String) {

        let channel: Channel
        if overPrivateChannel {
            guard let priv = privateChannel else {
                return (false, "no private channel is configured")
            }
            channel = priv
        } else {
            guard let first = channels.first else {
                return (false, "no channel is configured")
            }
            channel = first
        }

        let packet = MeshtasticProtocol.buildPacket(
            portNum: .textMessage,
            payload: MeshtasticProtocol.buildTextMessage(text),
            sender: nodeID,
            destination: destination,
            channelKey: channel.key,
            channelHash: MeshtasticCrypto.channelHash(name: channel.name, key: channel.key),
            hopLimit: hopLimit
        )
        return sendRaw(packet, timeout: timeout)
    }

    /// Send an uplink command to the balloon.
    ///
    /// Commands go on the private channel and nowhere else. The payload refuses
    /// them on the public channel because anyone can transmit there, so sending
    /// one publicly is a message the balloon reads and ignores. Hop limit zero:
    /// a ground station does not need the whole mesh relaying its commands.
    @discardableResult
    func sendCommand(_ command: String, timeout: TimeInterval = 5.0)
        -> (sent: Bool, detail: String) {
        guard privateChannel != nil else {
            return (false, "commands need a private channel; the balloon "
                         + "refuses them on the public one because anyone can "
                         + "transmit there")
        }
        let text = command.hasPrefix("!") ? command : "!" + command
        return sendText(text, overPrivateChannel: true, hopLimit: 0, timeout: timeout)
    }

    /// Hand an already-built packet to the modem and wait for its verdict.
    ///
    /// Synchronous by design. An uplink to a balloon is not fire and forget: if
    /// it did not leave the ground station the operator needs to see that now,
    /// rather than wonder why nothing happened.
    @discardableResult
    func sendRaw(_ packet: Data, timeout: TimeInterval = 5.0)
        -> (sent: Bool, detail: String) {

        guard let serial = serial, serial.isConnected else {
            sendFailed += 1
            return (false, "no modem connected")
        }
        guard packet.count <= 255 else {
            sendFailed += 1
            return (false, "packet is \(packet.count) bytes; the radio takes 255")
        }

        let hex = packet.map { String(format: "%02x", $0) }.joined()

        replyLock.lock()
        pendingReply = nil
        replyLock.unlock()

        guard serial.write("MTX:\(hex)\n") else {
            sendFailed += 1
            return (false, "write to the modem failed")
        }

        replyLock.lock()
        let deadline = Date().addingTimeInterval(timeout)
        while pendingReply == nil, Date() < deadline {
            replyLock.wait(until: deadline)
        }
        let reply = pendingReply
        replyLock.unlock()

        guard let answer = reply else {
            sendFailed += 1
            return (false, "the modem did not answer within \(Int(timeout))s")
        }
        if answer.hasPrefix(Reply.ok) {
            sent += 1
            return (true, "sent")
        }
        sendFailed += 1
        lastError = answer
        return (false, answer)
    }

    /// Feed the modem's text output in. Returns true if the line was ours.
    ///
    /// The transmit verdict arrives on the same text channel as everything else
    /// the modem prints, so the serial layer passes each line through here.
    @discardableResult
    func handleModemLine(_ line: String) -> Bool {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.hasPrefix(Reply.ok) || trimmed.hasPrefix(Reply.error) else {
            return false
        }
        replyLock.lock()
        pendingReply = trimmed
        replyLock.signal()
        replyLock.unlock()
        return true
    }

    // MARK: - Slot configuration

    /// Point the modem's Meshtastic radio at a region's channel.
    ///
    /// Without this the modem uses its built-in default, which is correct for
    /// exactly one region.
    @discardableResult
    func configureSlot(frequencyMHz: Double,
                       bandwidthKHz: Double = 250.0,
                       spreadingFactor: Int = 11,
                       codingRate: Int = 5,
                       powerDBm: Int = 30) -> Bool {
        guard let serial = serial, serial.isConnected else { return false }
        let line = String(format: "MCFG:%.4f,%.1f,%d,%d,%d\n",
                          frequencyMHz, bandwidthKHz, spreadingFactor,
                          codingRate, powerDBm)
        return serial.write(line)
    }

    // MARK: - Helpers

    /// Same derivation as the payload and the Python ground station, so the
    /// three agree on what this station is called on the mesh.
    static func nodeID(fromCallsign callsign: String) -> UInt32 {
        var hash: UInt32 = 5381
        for byte in callsign.uppercased().utf8 {
            hash = (hash &* 33) &+ UInt32(byte)
        }
        return hash
    }
}

/// GFSK has no signal-to-noise measurement -- the SX1262 reports SNR only for
/// LoRa. Modems send a sentinel rather than a number that looks like a reading.
/// Older firmware sent -20, which was RadioLib's RADIOLIB_ERR_WRONG_MODEM error
/// code being forwarded as decibels; both mean "not measured".
enum MeshtasticSNR {
    static let notAvailable: Float = -128.0
    static let legacyErrorCode: Float = -20.0

    static func isMeasured(_ snr: Float) -> Bool {
        snr > notAvailable + 1.0 && snr != legacyErrorCode
    }
}
