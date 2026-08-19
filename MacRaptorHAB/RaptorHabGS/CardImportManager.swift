//
//  CardImportManager.swift — reading a recovered payload card.
//
//  The card holds every image at full quality and the complete telemetry log,
//  not just the fraction that fitted in the airtime budget. This finds it,
//  reports whether it can be read, and copies it off, unsealing as it goes.
//
//  The readability check happens first and is stated plainly, because unlike
//  most errors this one has no remedy: recordings sealed to a key nobody holds
//  are gone, and finding that out after copying twenty gigabytes helps nobody.
//

import Foundation
import SwiftUI

struct CardFile: Identifiable, Hashable {
    let id = UUID()
    let url: URL
    let name: String
    let size: Int
    let sealed: Bool
    let kind: Kind
    let modified: Date

    enum Kind: String { case image, telemetry, log }

    /// The name it will have once unsealed.
    var plainName: String {
        sealed && name.hasSuffix(".rhs") ? String(name.dropLast(4)) : name
    }
}

struct CardSurvey {
    var root: URL
    var stateRoot: URL?
    var images: [CardFile] = []
    var telemetry: [CardFile] = []
    var logs: [CardFile] = []
    var callsign: String?
    var payloadPublicKey: String?
    var havePrivateKey = false
    var keyMatches: Bool?
    var notes: [String] = []

    var sealedCount: Int {
        (images + telemetry + logs).filter(\.sealed).count
    }

    var totalBytes: Int {
        (images + telemetry + logs).reduce(0) { $0 + $1.size }
    }

    /// Whether the sealed material here can actually be opened.
    var readable: Bool { sealedCount == 0 || keyMatches == true }

    var verdict: String {
        if sealedCount == 0 { return "nothing is sealed; all readable" }
        if readable { return "\(sealedCount) sealed file(s), and the key matches" }
        return "\(sealedCount) sealed file(s) that CANNOT be opened"
    }
}

struct ImportOutcome {
    var copied = 0
    var decrypted = 0
    var failed = 0
    var skipped = 0
    var errors: [String] = []
    var outputDirectory: URL?
}

@MainActor
final class CardImportManager: ObservableObject {
    static let stateRoot = "var/lib/raptorhab"
    static let sealedSuffix = ".rhs"

    @Published private(set) var candidates: [URL] = []
    @Published private(set) var survey: CardSurvey?
    @Published private(set) var busy = false
    @Published private(set) var progress: Double = 0
    @Published private(set) var progressLabel = ""
    @Published var lastOutcome: ImportOutcome?
    @Published var keyPath: URL = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent(".raptorhab/recording_key")

    private var privateKey: Data?

    init() {
        loadKey()
        rescan()
    }

    // MARK: - Key

    func loadKey() {
        privateKey = Self.readPrivateKey(at: keyPath)
    }

    var haveKey: Bool { privateKey != nil }

    /// Accepts the raw 32 bytes recording_key.py writes, and hex or base64 for
    /// keys that have been through a text channel on the way here.
    static func readPrivateKey(at url: URL) -> Data? {
        guard let raw = try? Data(contentsOf: url) else { return nil }
        if raw.count == 32 { return raw }
        guard let text = String(data: raw, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines) else { return nil }
        if let hex = Data(hex: text), hex.count == 32 { return hex }
        if let b64 = Data(base64Encoded: text), b64.count == 32 { return b64 }
        return nil
    }

    // MARK: - Finding cards

    func rescan() {
        let manager = FileManager.default
        var found: [URL] = []
        let volumes = (try? manager.contentsOfDirectory(
            at: URL(fileURLWithPath: "/Volumes"),
            includingPropertiesForKeys: nil)) ?? []
        for volume in volumes {
            let state = volume.appendingPathComponent(Self.stateRoot)
            if manager.fileExists(atPath: state.path) { found.append(volume) }
        }
        candidates = found
    }

    // MARK: - Surveying

    func read(_ root: URL) {
        loadKey()
        let manager = FileManager.default
        var result = CardSurvey(root: root)

        var state = root.appendingPathComponent(Self.stateRoot)
        if !manager.fileExists(atPath: state.path) {
            let direct = root.appendingPathComponent("images")
            if manager.fileExists(atPath: direct.path) {
                state = root
            } else {
                result.notes.append(
                    "No payload data under \(root.path). On a Raspberry Pi card the "
                    + "images and logs live on the ext4 root partition, not the FAT32 "
                    + "boot partition.")
                survey = result
                return
            }
        }
        result.stateRoot = state

        for (folder, kind) in [("images", CardFile.Kind.image), ("logs", .log)] {
            let directory = state.appendingPathComponent(folder)
            let entries = (try? manager.contentsOfDirectory(
                at: directory,
                includingPropertiesForKeys: [.fileSizeKey, .contentModificationDateKey]))
                ?? []
            for url in entries.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
                let values = try? url.resourceValues(
                    forKeys: [.fileSizeKey, .contentModificationDateKey])
                let name = url.lastPathComponent
                let sealed = name.hasSuffix(Self.sealedSuffix)
                let base = sealed ? String(name.dropLast(4)) : name
                let lowered = base.lowercased()

                let resolved: CardFile.Kind
                if lowered.hasSuffix(".webp") || lowered.hasSuffix(".jpg")
                    || lowered.hasSuffix(".jpeg") || lowered.hasSuffix(".png") {
                    resolved = .image
                } else if lowered.hasSuffix(".csv") {
                    resolved = .telemetry
                } else if lowered.hasSuffix(".log") || lowered.hasSuffix(".txt") {
                    resolved = .log
                } else {
                    continue
                }
                _ = kind

                let file = CardFile(url: url, name: name,
                                    size: values?.fileSize ?? 0, sealed: sealed,
                                    kind: resolved,
                                    modified: values?.contentModificationDate ?? .distantPast)
                switch resolved {
                case .image:     result.images.append(file)
                case .telemetry: result.telemetry.append(file)
                case .log:       result.logs.append(file)
                }
            }
        }

        // The payload records which key it sealed to; read it rather than guess.
        let configURL = state.appendingPathComponent("config/airborne.json")
        if let data = try? Data(contentsOf: configURL),
           let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            result.callsign = object["callsign"] as? String
            result.payloadPublicKey = object["recording_public_key"] as? String
        }

        checkKey(&result)
        survey = result
    }

    private func checkKey(_ result: inout CardSurvey) {
        result.havePrivateKey = privateKey != nil
        guard result.sealedCount > 0 else { return }

        guard let privateKey else {
            result.notes.append(
                "There is no recording key at \(keyPath.path), so the "
                + "\(result.sealedCount) sealed file(s) here cannot be opened. If the "
                + "key is elsewhere, point at it; if it was never kept, this data is "
                + "not recoverable.")
            return
        }

        guard let recorded = result.payloadPublicKey,
              let expected = Data(base64Encoded: recorded) ?? Data(hex: recorded) else {
            result.notes.append(
                "A key is loaded but the card does not record which public key it "
                + "was sealed to; opening a file will show whether they match.")
            return
        }

        if let ours = try? SealedBox.publicKey(forPrivateKey: privateKey) {
            result.keyMatches = (ours == expected)
            if ours != expected {
                result.notes.append(
                    "The key loaded here does not match the one this payload sealed "
                    + "to. These recordings were encrypted for a different keypair "
                    + "and cannot be opened with it.")
            }
        }
    }

    // MARK: - Reading one file

    /// Bytes for display, unsealed if necessary, without writing anything.
    func contents(of file: CardFile) -> Data? {
        guard let raw = try? Data(contentsOf: file.url) else { return nil }
        guard file.sealed else { return raw }
        guard let privateKey else { return nil }
        return try? SealedBox.open(raw, privateKey: privateKey)
    }

    // MARK: - Importing

    func importFiles(_ files: [CardFile], to directory: URL) {
        guard !busy else { return }
        busy = true
        progress = 0
        var outcome = ImportOutcome(outputDirectory: directory)
        let key = privateKey

        Task.detached(priority: .userInitiated) {
            try? FileManager.default.createDirectory(
                at: directory, withIntermediateDirectories: true)

            for (index, file) in files.enumerated() {
                await MainActor.run {
                    self.progress = Double(index) / Double(max(files.count, 1))
                    self.progressLabel = file.name
                }

                let target = directory.appendingPathComponent(file.plainName)
                if FileManager.default.fileExists(atPath: target.path) {
                    outcome.skipped += 1
                    continue
                }

                do {
                    let raw = try Data(contentsOf: file.url)
                    if file.sealed {
                        guard let key else {
                            outcome.failed += 1
                            outcome.errors.append("\(file.name): sealed, and no key is loaded")
                            continue
                        }
                        // A file that cannot be opened is reported and skipped,
                        // never written out as ciphertext under a plaintext
                        // name. Half a flight labelled as if it were whole is
                        // worse than a short list and an explanation.
                        let plain = try SealedBox.open(raw, privateKey: key)
                        try plain.write(to: target)
                        outcome.decrypted += 1
                    } else {
                        try raw.write(to: target)
                        outcome.copied += 1
                    }
                } catch {
                    outcome.failed += 1
                    outcome.errors.append("\(file.name): \(error.localizedDescription)")
                }
            }

            await MainActor.run {
                self.progress = 1
                self.progressLabel = ""
                self.lastOutcome = outcome
                self.busy = false
            }
        }
    }
}
