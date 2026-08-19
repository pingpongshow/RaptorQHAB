//
//  main.swift
//
//  Verifies the Swift half of the wire protocols against golden vectors
//  produced by the Python half.
//
//  These two implementations are written independently and must agree byte
//  for byte. A mismatch does not fail loudly at runtime -- it looks like a
//  payload that never answers, or a balloon whose beacons the app silently
//  ignores -- so it has to be caught here instead.
//
//  Run via payload/tests/test_swift_parity.py, which generates the vectors,
//  compiles this against the real app sources, and compares the output.
//

import Foundation

// The app sources are compiled alongside this file, so LinkFrame,
// LinkFrameDecoder, MeshtasticCrypto and SHA256Digest are the real
// implementations, not copies.

func hex(_ data: Data) -> String {
    data.map { String(format: "%02x", $0) }.joined()
}

func data(hex string: String) -> Data {
    var out = Data()
    var index = string.startIndex
    while index < string.endIndex {
        let next = string.index(index, offsetBy: 2)
        out.append(UInt8(string[index..<next], radix: 16)!)
        index = next
    }
    return out
}

struct Results: Encodable {
    var linkFrames: [String] = []
    var linkDecoded: [[String: String]] = []
    var nodeIDs: [String: UInt32] = [:]
    var aesCTR: [String] = []
    var channelHashes: [String: Int] = [:]
    var meshPackets: [String] = []
}

var results = Results()

// --- Link framing ---------------------------------------------------------
//
// Encode the same payloads Python encodes, so the two frame streams can be
// compared byte for byte.

let linkCases: [(UInt8, String)] = [
    (0, "7b226d6574686f64223a2268656c6c6f227d"),   // {"method":"hello"}
    (1, "6c73202d6c610a"),                          // ls -la\n
    (2, "7b226576656e74223a2274657374227d"),        // {"event":"test"}
    (0, ""),                                        // empty payload
    (1, String(repeating: "ab", count: 1000)),      // multi-read reassembly
    (1, String(repeating: "5248", count: 50)),      // payload full of "RH"
]

for (channel, payloadHex) in linkCases {
    let payload = data(hex: payloadHex)
    let frame = try! LinkFrame.encode(
        channel: LinkChannel(rawValue: channel)!, payload: payload
    )
    results.linkFrames.append(hex(frame))
}

// --- Link decoding --------------------------------------------------------
//
// Decode frames Python produced. Fed one byte at a time, because that is the
// realistic serial case and the path most likely to be wrong.

let pythonFrames = ProcessInfo.processInfo.environment["PYTHON_FRAMES"] ?? ""
if !pythonFrames.isEmpty {
    let decoder = LinkFrameDecoder()
    for byte in data(hex: pythonFrames) {
        for (channel, payload) in decoder.feed(Data([byte])) {
            results.linkDecoded.append([
                "channel": String(channel),
                "payload": hex(payload),
            ])
        }
    }
}

// --- Node id derivation ---------------------------------------------------
//
// If this disagrees with the payload, the app never recognises the balloon's
// beacons and the whole Meshtastic map path is silently dead.

for (callsign, payloadID) in [("RPHAB1", 1), ("RPHAB1", 0), ("KX0ABC", 3),
                              ("  rphab1  ", 1), ("A", 255)] {
    let id = MeshtasticManager.nodeID(forCallsign: callsign, payloadID: payloadID)
    results.nodeIDs["\(callsign)#\(payloadID)"] = id
}

// --- AES-256-CTR ----------------------------------------------------------
//
// The counter must increment as a big-endian 128-bit integer, matching the
// mbedtls implementation Meshtastic firmware uses. A little-endian word
// counter gives a correct first block and garbage after it -- invisible for a
// payload under 16 bytes, broken for every real beacon.

let aesKey = data(hex:
    "603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4")
let aesCases: [(UInt32, UInt32, String)] = [
    (1, 0xDEADBEEF, "48656c6c6f"),
    (0xCAFEBABE, 0x12345678, String(repeating: "00", count: 64)),
    (42, 0x1000, String(repeating: "ff", count: 100)),
]

for (packetID, sender, plaintextHex) in aesCases {
    let nonce = MeshtasticCrypto.nonce(packetID: packetID, sender: sender)
    let ciphertext = MeshtasticCrypto.ctr(
        key: aesKey, nonce: nonce, data: data(hex: plaintextHex)
    )
    results.aesCTR.append(hex(ciphertext ?? Data()))
}

// --- Channel hash ---------------------------------------------------------
//
// Receivers use this byte to choose which key to try. Wrong hash, ignored
// packet, even if everything else is right.

for name in ["LongFast", "RaptorHAB", "Private"] {
    let key = MeshtasticCrypto.expand(psk: Data([0x01]))
    results.channelHashes[name] = Int(
        MeshtasticCrypto.channelHash(name: name, key: key)
    )
}

// --- Whole Meshtastic packets ---------------------------------------------

let channelKey = MeshtasticCrypto.expand(psk: Data([0x01]))
let packet = MeshtasticProtocol.buildPacket(
    portNum: .textMessage,
    payload: Data("hello from the stratosphere".utf8),
    sender: 0xEFCAB5AC,
    destination: 0xFFFFFFFF,
    channelKey: channelKey,
    channelHash: MeshtasticCrypto.channelHash(name: "LongFast", key: channelKey),
    hopLimit: 0,
    packetID: 0x11223344
)
results.meshPackets.append(hex(packet))

// --- Output ---------------------------------------------------------------

let encoder = JSONEncoder()
encoder.outputFormatting = [.sortedKeys]
print(String(data: try! encoder.encode(results), encoding: .utf8)!)
