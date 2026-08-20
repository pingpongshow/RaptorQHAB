//
//  MissionReplayView.swift
//  RaptorHabGS
//
//  Play a recorded flight back.
//
//  A mission already holds everything that happened -- every telemetry point
//  and every image, with timestamps. Until now they could only be read as a
//  table and a grid, which is a poor way to understand a flight: you cannot
//  see the balloon accelerate into the jet stream, or notice that the
//  imagery stopped four minutes before the telemetry did.
//
//  Scrubbing a single time cursor across all of it turns the recording into
//  something you can actually review -- and makes bench testing legible too,
//  since a flightsim run records exactly like a real flight.
//

import SwiftUI
import MapKit

struct MissionReplayView: View {
    let mission: Mission
    let telemetry: [TelemetryPoint]
    let images: [MissionManager.RecordedImage]

    @Environment(\.dismiss) private var dismiss
    @State private var cursor: Double = 0          // seconds from first sample
    @State private var playing = false
    @State private var speed: Double = 60          // playback × real time
    @State private var camera: MapCameraPosition = .automatic

    /// One tick per 1/10 s of wall clock; the cursor advances by `speed`/10.
    private let tick = Timer.publish(every: 0.1, on: .main, in: .common)
        .autoconnect()

    private var start: Date? { telemetry.first?.timestamp }
    private var duration: Double {
        guard let first = telemetry.first?.timestamp,
              let last = telemetry.last?.timestamp else { return 0 }
        return max(last.timeIntervalSince(first), 1)
    }

    private var cursorDate: Date? {
        start.map { $0.addingTimeInterval(cursor) }
    }

    /// The last telemetry at or before the cursor -- not the nearest. A
    /// replay that jumped forward to the next sample would show the balloon
    /// somewhere it had not reached yet.
    private var current: TelemetryPoint? {
        guard let now = cursorDate else { return nil }
        return telemetry.last { $0.timestamp <= now } ?? telemetry.first
    }

    private var flownPath: [CLLocationCoordinate2D] {
        guard let now = cursorDate else { return [] }
        return telemetry
            .filter { $0.timestamp <= now && $0.latitude != 0 }
            .map { CLLocationCoordinate2D(latitude: $0.latitude, longitude: $0.longitude) }
    }

    /// The most recent image at or before the cursor.
    private var currentImage: MissionManager.RecordedImage? {
        guard let now = cursorDate else { return nil }
        return images.last { $0.timestamp <= now }
    }

    private var imageURL: URL? {
        currentImage.map {
            MissionManager.shared.getMissionFolder(mission)
                .appendingPathComponent("images")
                .appendingPathComponent($0.filename)
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            if telemetry.isEmpty {
                ContentUnavailableView("Nothing recorded",
                                       systemImage: "play.slash",
                                       description: Text("This mission has no telemetry to replay."))
            } else {
                HSplitView {
                    mapPane
                    sidePane.frame(minWidth: 300, idealWidth: 340)
                }
                transport
            }
        }
        .frame(minWidth: 900, minHeight: 620)
        .navigationTitle("Replay — \(mission.name)")
        .toolbar {
            ToolbarItem(placement: .cancellationAction) {
                Button("Done") { dismiss() }
            }
        }
        .onReceive(tick) { _ in
            guard playing else { return }
            cursor += speed / 10
            if cursor >= duration {
                cursor = duration
                playing = false          // stop at the end rather than loop
            }
        }
    }

    private var mapPane: some View {
        Map(position: $camera) {
            if flownPath.count > 1 {
                MapPolyline(coordinates: flownPath)
                    .stroke(.blue, lineWidth: 3)
            }
            if let p = current, p.latitude != 0 {
                Annotation("", coordinate: CLLocationCoordinate2D(
                    latitude: p.latitude, longitude: p.longitude)) {
                    Image(systemName: "balloon.fill")
                        .font(.title2)
                        .foregroundStyle(.red)
                        .shadow(radius: 3)
                }
            }
        }
        .onChange(of: current?.id) {
            guard let p = current, p.latitude != 0 else { return }
            camera = .region(MKCoordinateRegion(
                center: CLLocationCoordinate2D(latitude: p.latitude,
                                               longitude: p.longitude),
                span: MKCoordinateSpan(latitudeDelta: 0.35, longitudeDelta: 0.35)))
        }
    }

    private var sidePane: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                if let url = imageURL, let img = NSImage(contentsOf: url) {
                    Image(nsImage: img)
                        .resizable().scaledToFit()
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                    if let shot = currentImage {
                        Text("Image \(shot.imageId) · \(shot.timestamp.formatted(date: .omitted, time: .standard))")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                } else {
                    RoundedRectangle(cornerRadius: 8)
                        .fill(.quaternary)
                        .frame(height: 180)
                        .overlay(Text("No image yet at this point")
                            .font(.caption).foregroundStyle(.secondary))
                }

                if let p = current {
                    Grid(alignment: .leading, horizontalSpacing: 14, verticalSpacing: 7) {
                        row("Time", p.timestamp.formatted(date: .omitted, time: .standard))
                        row("Altitude", String(format: "%.0f m", p.altitude))
                        row("Speed", String(format: "%.1f m/s", p.speed))
                        row("Position", String(format: "%.5f, %.5f", p.latitude, p.longitude))
                        row("Satellites", "\(p.satellites) (\(p.fixType))")
                        row("RSSI", "\(p.rssi) dBm")
                        row("Battery", String(format: "%.2f V", Double(p.batteryMV) / 1000))
                    }
                    .font(.system(.body, design: .rounded))
                }
            }
            .padding(16)
        }
    }

    private func row(_ label: String, _ value: String) -> some View {
        GridRow {
            Text(label).foregroundStyle(.secondary)
            Text(value).monospacedDigit()
        }
    }

    private var transport: some View {
        VStack(spacing: 8) {
            Slider(value: $cursor, in: 0...max(duration, 1))
                .controlSize(.small)

            HStack(spacing: 14) {
                Button { cursor = 0; playing = false } label: {
                    Image(systemName: "backward.end.fill")
                }
                Button { playing.toggle() } label: {
                    Image(systemName: playing ? "pause.fill" : "play.fill")
                        .frame(width: 22)
                }
                .keyboardShortcut(.space, modifiers: [])

                Text(elapsed)
                    .font(.system(.body, design: .monospaced))
                    .foregroundStyle(.secondary)

                Spacer()

                Picker("Speed", selection: $speed) {
                    Text("1×").tag(1.0)
                    Text("10×").tag(10.0)
                    Text("60×").tag(60.0)
                    Text("300×").tag(300.0)
                }
                .pickerStyle(.segmented)
                .frame(width: 210)

                Text("\(images.count) images · \(telemetry.count) points")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(14)
        .background(.bar)
    }

    private var elapsed: String {
        let c = Int(cursor), d = Int(duration)
        func hms(_ s: Int) -> String {
            String(format: "%02d:%02d:%02d", s / 3600, (s % 3600) / 60, s % 60)
        }
        return "\(hms(c)) / \(hms(d))"
    }
}
