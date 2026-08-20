//
//  ChaseView.swift
//  RaptorHabGS
//
//  The view for the passenger seat.
//
//  The map is for a desk. In a moving car, at speed, what a navigator can
//  actually use is three numbers -- which way, how far, how long -- large
//  enough to read at a glance and without a single control that has to be
//  hit accurately. Everything here is sized for that, and the one button
//  that matters hands the destination to Apple Maps, because turn-by-turn
//  to a field is a solved problem and this app should not try to solve it
//  again.
//
//  It aims at the *predicted landing point* while the balloon is flying,
//  and switches to the payload's last known position once it is down --
//  which is the moment the prediction stops being the thing you want to
//  drive to.
//

import SwiftUI
import CoreLocation
import MapKit

struct ChaseView: View {
    @ObservedObject var predictor = LandingPredictionManager.shared
    @ObservedObject var gps = GPSManager.shared
    @ObservedObject var fusion = PositionFusion.shared
    @EnvironmentObject var groundStation: GroundStationManager

    @Environment(\.dismiss) private var dismiss

    /// Where we are driving to, and what that place is.
    private struct Target {
        let coordinate: CLLocationCoordinate2D
        let label: String
        let isLanded: Bool
    }

    private var target: Target? {
        // Once the payload is on the ground its own position beats any
        // prediction of where it was going to end up.
        if let fix = fusion.best, fix.altitude < 1000, fix.age < 600 {
            if let p = predictor.currentPrediction, p.phase != .landed {
                return Target(coordinate: p.predictedCoordinate,
                              label: "Predicted landing", isLanded: false)
            }
            return Target(coordinate: fix.coordinate,
                          label: "Payload, last heard", isLanded: true)
        }
        if let p = predictor.currentPrediction {
            return Target(coordinate: p.predictedCoordinate,
                          label: "Predicted landing", isLanded: false)
        }
        if let fix = fusion.best {
            return Target(coordinate: fix.coordinate,
                          label: "Payload, last heard", isLanded: false)
        }
        return nil
    }

    private var here: CLLocationCoordinate2D? {
        gps.currentPosition?.coordinate
    }

    /// Bearing and distance from the car to the target.
    private var vector: (bearing: Double, distance: Double)? {
        guard let here = here, let target = target else { return nil }
        let from = CLLocation(latitude: here.latitude, longitude: here.longitude)
        let to = CLLocation(latitude: target.coordinate.latitude,
                            longitude: target.coordinate.longitude)
        return (bearing(from: here, to: target.coordinate),
                from.distance(from: to))
    }

    private func bearing(from: CLLocationCoordinate2D,
                         to: CLLocationCoordinate2D) -> Double {
        let lat1 = from.latitude * .pi / 180, lat2 = to.latitude * .pi / 180
        let dLon = (to.longitude - from.longitude) * .pi / 180
        let y = sin(dLon) * cos(lat2)
        let x = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dLon)
        return (atan2(y, x) * 180 / .pi + 360).truncatingRemainder(dividingBy: 360)
    }

    private func cardinal(_ deg: Double) -> String {
        let names = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                     "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
        return names[Int((deg + 11.25) / 22.5) % 16]
    }

    private func distanceText(_ metres: Double) -> String {
        // Miles under ten get a decimal: "3.4 mi" is a different decision
        // from "3 mi" when you are looking for a turning.
        let miles = metres / 1609.34
        if miles < 0.2 { return String(format: "%.0f ft", metres * 3.28084) }
        if miles < 10 { return String(format: "%.1f mi", miles) }
        return String(format: "%.0f mi", miles)
    }

    private var etaText: String? {
        guard let p = predictor.currentPrediction, !(target?.isLanded ?? false),
              p.timeToLanding > 0 else { return nil }
        let m = Int(p.timeToLanding / 60)
        return m >= 60 ? "\(m / 60)h \(m % 60)m" : "\(m) min"
    }

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()

            VStack(spacing: 28) {
                header

                if let v = vector, let target = target {
                    arrow(bearing: v.bearing)

                    VStack(spacing: 4) {
                        Text(distanceText(v.distance))
                            .font(.system(size: 86, weight: .bold, design: .rounded))
                            .foregroundStyle(.white)
                            .monospacedDigit()
                        Text("\(cardinal(v.bearing)) · \(Int(v.bearing))°")
                            .font(.system(size: 30, weight: .medium, design: .rounded))
                            .foregroundStyle(.secondary)
                    }

                    if let eta = etaText {
                        Label("lands in \(eta)", systemImage: "clock")
                            .font(.system(size: 22, weight: .medium, design: .rounded))
                            .foregroundStyle(.orange)
                    }

                    openInMaps(target: target)
                } else {
                    waiting
                }

                Spacer()
                footer
            }
            .padding(36)
        }
        .toolbar {
            ToolbarItem(placement: .cancellationAction) {
                Button("Done") { dismiss() }
            }
        }
    }

    private var header: some View {
        HStack {
            Text(target?.label ?? "No target yet")
                .font(.system(size: 20, weight: .semibold, design: .rounded))
                .foregroundStyle(target?.isLanded == true ? .green : .secondary)
            Spacer()
            if let fix = fusion.best {
                // How stale the position is matters more than anything else
                // on this screen: a confident arrow built on a ten-minute-old
                // fix will drive you to the wrong field.
                Text("\(Int(fix.age))s old")
                    .font(.system(size: 18, weight: .medium, design: .rounded))
                    .foregroundStyle(fix.age > 120 ? .red : .secondary)
                    .monospacedDigit()
            }
        }
    }

    private func arrow(bearing: Double) -> some View {
        // Rotated to compass bearing, not to the car's heading: a Mac has no
        // idea which way the vehicle is pointing, and an arrow that silently
        // assumed north-up while the car drove south would be worse than no
        // arrow at all. The cardinal underneath says which way it means.
        Image(systemName: "location.north.fill")
            .resizable()
            .scaledToFit()
            .frame(width: 150, height: 150)
            .foregroundStyle(.blue)
            .rotationEffect(.degrees(bearing))
            .animation(.easeInOut(duration: 0.35), value: bearing)
    }

    private func openInMaps(target: Target) -> some View {
        Button {
            let item = MKMapItem(placemark: MKPlacemark(coordinate: target.coordinate))
            item.name = target.isLanded ? "Payload" : "Predicted landing"
            item.openInMaps(launchOptions: [
                MKLaunchOptionsDirectionsModeKey: MKLaunchOptionsDirectionsModeDriving
            ])
        } label: {
            Label("Directions in Maps", systemImage: "car.fill")
                .font(.system(size: 22, weight: .semibold, design: .rounded))
                .frame(maxWidth: .infinity)
                .padding(.vertical, 18)
        }
        .buttonStyle(.borderedProminent)
        .controlSize(.large)
    }

    private var waiting: some View {
        VStack(spacing: 14) {
            Image(systemName: gps.currentPosition == nil
                  ? "location.slash" : "antenna.radiowaves.left.and.right")
                .font(.system(size: 64))
                .foregroundStyle(.secondary)
            Text(gps.currentPosition == nil
                 ? "Connect the ground station GPS to get a bearing from here"
                 : "Waiting for a payload position")
                .font(.system(size: 22, weight: .medium, design: .rounded))
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .padding(.top, 60)
    }

    private var footer: some View {
        HStack(spacing: 26) {
            if let fix = fusion.best {
                stat("ALT", String(format: "%.0f m", fix.altitude))
                stat("SRC", fix.source.label)
            }
            if let p = predictor.currentPrediction {
                stat("DESCENT", String(format: "%.1f m/s", abs(p.descentRate)))
            }
            stat("RSSI", String(format: "%.0f dBm", groundStation.serialRSSI))
        }
        .font(.system(size: 17, weight: .medium, design: .rounded))
        .foregroundStyle(.secondary)
    }

    private func stat(_ label: String, _ value: String) -> some View {
        VStack(spacing: 2) {
            Text(label).font(.system(size: 12, weight: .semibold)).opacity(0.6)
            Text(value).monospacedDigit()
        }
    }
}
