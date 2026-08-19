//
//  SealedBox.swift — opening flight recordings sealed by the payload.
//
//  The payload seals every image and telemetry log to an X25519 public key as
//  it writes them, holding only the public half. Recovering the balloon yields
//  ciphertext; this is the other half of that arrangement.
//
//  The format is defined by payload/common/sealedbox.py and must match it byte for
//  byte. It is pinned by a cross-implementation test that seals in Python and
//  opens here, because a divergence would not show up as a crash — it would
//  show up as a flight that cannot be read, months later, with no way back.
//
//  Layout, header struct ">4sBB32sI", 42 bytes:
//
//      0..3    magic "RHSB"
//      4       version, currently 1
//      5       flags, currently 0
//      6..37   ephemeral X25519 public key
//      38..41  plaintext length, big-endian UInt32
//      42...   ciphertext
//      last 32 HMAC-SHA256 tag
//
//  Encrypt-then-MAC: the tag is verified before anything is decrypted, so a
//  tampered file is rejected rather than quietly yielding wrong plaintext.
//

import CryptoKit
import Foundation

enum SealedBoxError: LocalizedError {
    case tooShort
    case badMagic
    case unsupportedVersion(UInt8)
    case authenticationFailed
    case lengthMismatch
    case badKey

    var errorDescription: String? {
        switch self {
        case .tooShort:
            return "not a sealed file: too short to contain a header and tag"
        case .badMagic:
            return "not a sealed file: wrong magic"
        case .unsupportedVersion(let version):
            return "sealed with format version \(version), which this build does not know"
        case .authenticationFailed:
            // Deliberately does not distinguish the two causes: the MAC cannot,
            // and both mean the same thing operationally.
            return "authentication failed: wrong key, or the file is damaged"
        case .lengthMismatch:
            return "sealed file is internally inconsistent"
        case .badKey:
            return "the private key is not 32 bytes"
        }
    }
}

enum SealedBox {
    static let magic = Data("RHSB".utf8)
    static let formatVersion: UInt8 = 1
    static let headerSize = 42
    static let tagSize = 32
    static let overhead = headerSize + tagSize      // 74

    /// HKDF-SHA256 (RFC 5869). CryptoKit has HKDF, but taking the salt and info
    /// as raw bytes here keeps this readable next to the Python it must match.
    private static func hkdf(shared: Data, salt: Data, info: Data, length: Int) -> Data {
        let prk = HMAC<SHA256>.authenticationCode(
            for: shared, using: SymmetricKey(data: salt))
        let prkKey = SymmetricKey(data: Data(prk))

        var output = Data()
        var block = Data()
        var counter: UInt8 = 1
        while output.count < length {
            var input = block
            input.append(info)
            input.append(counter)
            block = Data(HMAC<SHA256>.authenticationCode(for: input, using: prkKey))
            output.append(block)
            counter += 1
        }
        return output.prefix(length)
    }

    /// Encryption and MAC keys, bound to both public keys so a box cannot be
    /// replayed against a different recipient.
    private static func derive(shared: Data,
                               ephemeralPublic: Data,
                               recipientPublic: Data) -> (encryption: Data, mac: Data) {
        let material = hkdf(shared: shared,
                            salt: ephemeralPublic + recipientPublic,
                            info: Data("raptorhab-sealed-box-v1".utf8),
                            length: 64)
        return (material.prefix(32), material.suffix(32))
    }

    /// The public key matching a private key, for checking a card before
    /// spending time on it.
    static func publicKey(forPrivateKey privateKey: Data) throws -> Data {
        guard privateKey.count == 32 else { throw SealedBoxError.badKey }
        let key = try Curve25519.KeyAgreement.PrivateKey(rawRepresentation: privateKey)
        return key.publicKey.rawRepresentation
    }

    /// Open a sealed file.
    static func open(_ sealed: Data, privateKey: Data) throws -> Data {
        guard privateKey.count == 32 else { throw SealedBoxError.badKey }
        guard sealed.count >= overhead else { throw SealedBoxError.tooShort }

        let bytes = [UInt8](sealed)
        guard Data(bytes[0..<4]) == magic else { throw SealedBoxError.badMagic }

        let version = bytes[4]
        guard version == formatVersion else {
            throw SealedBoxError.unsupportedVersion(version)
        }

        let ephemeralPublic = Data(bytes[6..<38])
        let declaredLength = (UInt32(bytes[38]) << 24) | (UInt32(bytes[39]) << 16)
                           | (UInt32(bytes[40]) << 8)  |  UInt32(bytes[41])

        let ciphertext = Data(bytes[headerSize..<(sealed.count - tagSize)])
        let tag = Data(bytes[(sealed.count - tagSize)...])

        guard Int(declaredLength) == ciphertext.count else {
            throw SealedBoxError.lengthMismatch
        }

        let priv = try Curve25519.KeyAgreement.PrivateKey(rawRepresentation: privateKey)
        let eph = try Curve25519.KeyAgreement.PublicKey(rawRepresentation: ephemeralPublic)
        let shared = try priv.sharedSecretFromKeyAgreement(with: eph)
        let sharedBytes = shared.withUnsafeBytes { Data($0) }

        let recipientPublic = priv.publicKey.rawRepresentation
        let keys = derive(shared: sharedBytes,
                          ephemeralPublic: ephemeralPublic,
                          recipientPublic: recipientPublic)

        // Verify before decrypting. The MAC covers the header as well as the
        // ciphertext, so the ephemeral key and declared length are authenticated
        // too -- otherwise an attacker could alter them freely.
        let macInput = Data(bytes[0..<(sealed.count - tagSize)])
        let expected = HMAC<SHA256>.authenticationCode(
            for: macInput, using: SymmetricKey(data: keys.mac))
        guard constantTimeEquals(Data(expected), tag) else {
            throw SealedBoxError.authenticationFailed
        }

        // AES-256-CTR with an all-zero counter. Safe only because the key is
        // derived from a fresh ephemeral keypair for every file, so no key is
        // ever used twice. Do not copy this pattern to a fixed key.
        guard let plaintext = MeshtasticCrypto.ctr(
                key: keys.encryption, nonce: Data(repeating: 0, count: 16),
                data: ciphertext) else {
            throw SealedBoxError.authenticationFailed
        }
        return plaintext
    }

    /// Whether a file looks sealed, without attempting to open it.
    static func isSealed(_ data: Data) -> Bool {
        data.count >= headerSize && data.prefix(4) == magic
    }

    private static func constantTimeEquals(_ a: Data, _ b: Data) -> Bool {
        guard a.count == b.count else { return false }
        var difference: UInt8 = 0
        for (x, y) in zip(a, b) { difference |= x ^ y }
        return difference == 0
    }
}
