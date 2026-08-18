//
//  PositionFusion.swift
//  RaptorHabGS
//
//  Combines every source that can say where the balloon is, into one answer
//  the map can draw.
//
//  Priority, highest first:
//      1. RAPTOR direct   — the GFSK modem. Your own receiver, your own link.
//      2. Meshtastic direct — a Meshtastic node plugged into this Mac.
//      3. Meshtastic MQTT — someone else's node, relayed via the internet.
//      4. Extrapolated    — dead reckoning from the last real fix.
//
//  Two rules make this trustworthy rather than merely clever:
//
//    - A lower-priority source never overwrites a *fresher* higher-priority
//      one. Losing the modem for thirty seconds should not make the map jump
//      to a third-party report and back.
//
//    - The map always shows which source it is drawing and how old it is.
//      A position from a stranger's node on the far side of the country is
//      useful information, but only if you know that is what you are seeing.
//

import Combine
import Foundation
import MapKit

enum PositionSource: Int, CaseIterable, Comparable {
    case raptorDirect = 0
    case meshtasticDirect = 1
    case meshtasticMQTT = 2
    case extrapolated = 3

    /// Lower raw value means higher priority.
    static func < (lhs: PositionSource, rhs: PositionSource) -> Bool {
        lhs.rawValue < rhs.rawValue
    }

    var label: String {
        switch self {
        case .raptorDirect:     return "RAPTOR"
        case .meshtasticDirect: return "Meshtastic"
        case .meshtasticMQTT:   return "Mesh (MQTT)"
        case .extrapolated:     return "Estimated"
        }
    }

    var detailedLabel: String {
        switch self {
        case .raptorDirect:     return "RAPTOR modem (direct)"
        case .meshtasticDirect: return "Meshtastic node (direct)"
        case .meshtasticMQTT:   return "Meshtastic MQTT (third party)"
        case .extrapolated:     return "Dead reckoning"
        }
    }

    var symbolName: String {
        switch self {
        case .raptorDirect:     return "antenna.radiowaves.left.and.right"
        case .meshtasticDirect: return "point.3.connected.trianglepath.dotted"
        case .meshtasticMQTT:   return "network"
        case .extrapolated:     return "questionmark.circle"
        }
    }

    /// How long a fix from this source stays authoritative.
    ///
    /// The RAPTOR link updates constantly, so a gap means real trouble and
    /// should hand over quickly. Meshtastic beacons are minutes apart by
    /// design, so a two-minute-old one is perfectly normal.
    var staleAfter: TimeInterval {
        switch self {
        case .raptorDirect:     return 45
        case .meshtasticDirect: return 600
        case .meshtasticMQTT:   return 900
        case .extrapolated:     return 300
        }
    }

    /// Whether this came from someone else's equipment. Surfaced in the UI,
    /// because trusting a stranger's report needs to be a conscious choice.
    var isThirdParty: Bool { self == .meshtasticMQTT }
}

struct PositionFix: Identifiable {
    let id = UUID()
    let source: PositionSource
    let coordinate: CLLocationCoordinate2D
    let altitude: Double
    let timestamp: Date
    var satellites: Int?
    var rssi: Int?
    var snr: Float?
    /// Free-text provenance: which node, which gateway.
    var detail: String?

    var age: TimeInterval { Date().timeIntervalSince(timestamp) }

    /// Reconcile a sender-supplied time with when we actually received it.
    ///
    /// Freshness drives which source the map draws, so a bad clock on a remote
    /// node silently changes what the operator sees. Two failure modes matter,
    /// and both are common on a real mesh:
    ///
    /// - A node with no time sync reports epoch 0. Taken literally that is a
    ///   fix over fifty years old, so it is discarded as stale the instant it
    ///   arrives -- losing a perfectly good position from exactly the kind of
    ///   bare node most likely to be the only one hearing the balloon.
    /// - A node with a clock running fast reports the future. That gives a
    ///   negative age, so the fix never becomes stale and sits on the map
    ///   claiming to be current long after it stopped being true.
    ///
    /// Reception time is the honest fallback: it is what we can actually
    /// vouch for. A supplied time is only trusted when it is plausible.
    static func reconcileTimestamp(_ supplied: Date?, received: Date = Date()) -> Date {
        guard let supplied else { return received }

        // Anything before this is an unset or nonsense clock, not a real fix.
        let plausibleEpoch = Date(timeIntervalSince1970: 1_577_836_800)  // 2020-01-01
        if supplied < plausibleEpoch { return received }

        // Allow a little skew; beyond that the sender's clock is wrong, and
        // trusting it would make the fix immortal.
        if supplied > received.addingTimeInterval(60) { return received }

        return supplied
    }
    var isStale: Bool { age > source.staleAfter }

    var ageDescription: String {
        let seconds = Int(age)
        if seconds < 60 { return "\(seconds)s ago" }
        if seconds < 3600 { return "\(seconds / 60)m ago" }
        return "\(seconds / 3600)h ago"
    }
}

@MainActor
final class PositionFusion: ObservableObject {
    static let shared = PositionFusion()

    /// The position the map should draw.
    @Published private(set) var best: PositionFix?

    /// The most recent fix from each source, whether or not it won.
    @Published private(set) var latestBySource: [PositionSource: PositionFix] = [:]

    /// Every fix, for the track line.
    @Published private(set) var history: [PositionFix] = []

    @Published var extrapolationEnabled = true

    private var refreshTimer: Timer?
    private let historyLimit = 5000

    private init() {
        // Staleness is a function of wall-clock time, not of new data, so the
        // choice has to be re-evaluated even when nothing arrives. Without
        // this the map would sit on a dead RAPTOR fix indefinitely.
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { _ in
            Task { @MainActor [weak self] in self?.recompute() }
        }
    }

    // MARK: - Input

    func submit(_ fix: PositionFix) {
        guard fix.coordinate.isValid else { return }

        latestBySource[fix.source] = fix

        if fix.source != .extrapolated {
            history.append(fix)
            if history.count > historyLimit {
                history.removeFirst(history.count - historyLimit)
            }
        }

        recompute()
    }

    /// Feed a RAPTOR telemetry point in.
    func submitRaptor(_ point: TelemetryPoint) {
        submit(PositionFix(
            source: .raptorDirect,
            coordinate: CLLocationCoordinate2D(
                latitude: point.latitude, longitude: point.longitude
            ),
            altitude: point.altitude,
            timestamp: point.timestamp,
            satellites: Int(point.satellites),
            rssi: point.rssi,
            snr: point.snr,
            detail: "seq \(point.sequence)"
        ))
    }

    func clear() {
        latestBySource.removeAll()
        history.removeAll()
        best = nil
    }

    // MARK: - Selection

    private func recompute() {
        // Highest-priority source with a fix that is still fresh wins.
        for source in PositionSource.allCases where source != .extrapolated {
            if let fix = latestBySource[source], !fix.isStale {
                best = fix
                return
            }
        }

        // Nothing fresh. Extrapolate from the newest real fix if we can, so
        // the map shows a best guess rather than nothing at all -- clearly
        // marked as an estimate.
        if extrapolationEnabled, let estimate = extrapolate() {
            latestBySource[.extrapolated] = estimate
            best = estimate
            return
        }

        // Otherwise keep showing the newest real fix, stale and labelled as
        // such. A stale position is more useful than an empty map.
        best = latestBySource
            .filter { $0.key != .extrapolated }
            .values
            .max { $0.timestamp < $1.timestamp }
    }

    /// Dead reckoning from the last two real fixes.
    ///
    /// Only extrapolates a short way. A balloon's track curves and the wind
    /// changes with altitude, so a long projection is a confident lie.
    private func extrapolate() -> PositionFix? {
        let real = history.suffix(10).filter { $0.source != .extrapolated }
        guard real.count >= 2 else { return nil }

        let last = real[real.index(before: real.endIndex)]
        let previous = real[real.index(real.endIndex, offsetBy: -2)]

        let interval = last.timestamp.timeIntervalSince(previous.timestamp)
        guard interval > 0.5, interval < 600 else { return nil }

        let elapsed = Date().timeIntervalSince(last.timestamp)
        guard elapsed > 0, elapsed < 300 else { return nil }

        let latRate = (last.coordinate.latitude - previous.coordinate.latitude) / interval
        let lonRate = (last.coordinate.longitude - previous.coordinate.longitude) / interval
        let altRate = (last.altitude - previous.altitude) / interval

        let coordinate = CLLocationCoordinate2D(
            latitude: last.coordinate.latitude + latRate * elapsed,
            longitude: last.coordinate.longitude + lonRate * elapsed
        )
        guard coordinate.isValid else { return nil }

        return PositionFix(
            source: .extrapolated,
            coordinate: coordinate,
            altitude: max(0, last.altitude + altRate * elapsed),
            timestamp: Date(),
            detail: String(
                format: "projected %.0fs from %@", elapsed, last.source.label
            )
        )
    }

    // MARK: - Presentation

    /// One line describing what the map is currently showing.
    var sourceSummary: String {
        guard let best else { return "No position" }

        var text = "\(best.source.label) · \(best.ageDescription)"
        if best.isStale { text += " · stale" }
        if best.source.isThirdParty { text += " · third party" }
        return text
    }

    var sourceStatuses: [(source: PositionSource, fix: PositionFix?, isActive: Bool)] {
        PositionSource.allCases.map { source in
            (source, latestBySource[source], best?.source == source)
        }
    }

    /// The track, thinned for drawing. A five-thousand-point polyline makes
    /// the map view crawl.
    func trackCoordinates(maxPoints: Int = 800) -> [CLLocationCoordinate2D] {
        let real = history.filter { $0.source != .extrapolated }
        guard real.count > maxPoints else { return real.map(\.coordinate) }

        let stride = Double(real.count) / Double(maxPoints)
        return (0..<maxPoints).map { real[Int(Double($0) * stride)].coordinate }
    }
}

extension CLLocationCoordinate2D {
    /// Rejects the (0, 0) an unset GPS reports, along with out-of-range values.
    var isValid: Bool {
        guard latitude.isFinite, longitude.isFinite else { return false }
        guard abs(latitude) <= 90, abs(longitude) <= 180 else { return false }
        return !(latitude == 0 && longitude == 0)
    }
}
