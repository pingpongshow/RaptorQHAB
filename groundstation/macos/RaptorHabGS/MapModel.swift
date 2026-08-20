//
//  MapModel.swift
//  RaptorHabGS
//
//  A throttled feed for the map, so panning and zooming stay smooth while
//  packets pour in.
//
//  The map only draws two things that change in flight -- the payload marker
//  and the flight path -- but it used to observe the whole GroundStation
//  manager. With the classic ObservableObject model there is no per-property
//  granularity: a view invalidates on *any* published change of an object it
//  observes. The manager publishes packet counters, RSSI and SNR on every
//  packet, roughly a hundred times a second, so the entire Map rebuilt a
//  hundred times a second and MapKit's own gesture handling never got a turn.
//  Disconnecting the modem "fixed" it because the churn stopped.
//
//  This object publishes only what the map draws, at a few hertz. A balloon
//  does not move far in 250 ms, so the marker and path are still live to the
//  eye, and the map is left alone the rest of the time.
//

import Foundation
import CoreLocation

@MainActor
final class MapModel: ObservableObject {
    static let shared = MapModel()

    /// Throttled snapshots. The map reads these instead of the manager's
    /// per-packet properties.
    @Published private(set) var latestTelemetry: TelemetryPoint?
    @Published private(set) var flightHistory: [TelemetryPoint] = []

    /// At most this often. Four times a second is smooth to the eye and a
    /// four-hundredth of the packet rate on a busy link.
    private let interval: TimeInterval = 0.25

    private var pendingLatest: TelemetryPoint?
    private var pendingHistory: [TelemetryPoint] = []
    private var havePending = false
    private var flushScheduled = false

    /// Called from the manager as telemetry arrives. Coalesces a burst into
    /// one publish per `interval`, always carrying the most recent state --
    /// so a hundred packets between flushes cost one map rebuild, not a
    /// hundred.
    func update(latest: TelemetryPoint?, history: [TelemetryPoint]) {
        pendingLatest = latest
        pendingHistory = history
        havePending = true

        guard !flushScheduled else { return }
        flushScheduled = true
        DispatchQueue.main.asyncAfter(deadline: .now() + interval) { [weak self] in
            guard let self else { return }
            self.flushScheduled = false
            guard self.havePending else { return }
            self.havePending = false
            // Assigning identical values would still fire objectWillChange,
            // so only publish when something the map draws actually moved.
            if self.latestTelemetry?.latitude != self.pendingLatest?.latitude
                || self.latestTelemetry?.longitude != self.pendingLatest?.longitude
                || self.latestTelemetry?.altitude != self.pendingLatest?.altitude {
                self.latestTelemetry = self.pendingLatest
            }
            if self.flightHistory.count != self.pendingHistory.count {
                self.flightHistory = self.pendingHistory
            }
        }
    }

    /// Clear on disconnect, so a stale marker does not linger.
    func reset() {
        pendingLatest = nil
        pendingHistory = []
        havePending = false
        latestTelemetry = nil
        flightHistory = []
    }
}
