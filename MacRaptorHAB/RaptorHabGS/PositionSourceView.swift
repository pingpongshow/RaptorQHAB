//
//  PositionSourceView.swift
//  RaptorHabGS
//
//  Shows which source the map's balloon position came from, and how old it is.
//
//  This exists because a fused position is only trustworthy if you can see
//  what produced it. A fix relayed from a stranger's node on the far side of
//  the country is useful, but only if you know that is what you are looking
//  at rather than assuming it came from your own receiver.
//

import SwiftUI

/// Compact badge for overlaying on the map.
struct PositionSourceBadge: View {
    @StateObject private var fusion = PositionFusion.shared

    var body: some View {
        if let best = fusion.best {
            HStack(spacing: 6) {
                Image(systemName: best.source.symbolName)
                    .foregroundStyle(tint(for: best))

                Text(best.source.label)
                    .fontWeight(.medium)

                Text("·")
                    .foregroundStyle(.tertiary)

                Text(best.ageDescription)
                    .foregroundStyle(best.isStale ? .orange : .secondary)

                if best.source.isThirdParty {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(.orange)
                        .help("Relayed by someone else's node over the internet")
                }
            }
            .font(.caption)
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(.regularMaterial, in: Capsule())
            .help(detail(for: best))
        }
    }

    private func tint(for fix: PositionFix) -> Color {
        if fix.isStale { return .orange }
        switch fix.source {
        case .raptorDirect:     return .green
        case .meshtasticDirect: return .blue
        case .meshtasticMQTT:   return .orange
        case .extrapolated:     return .gray
        }
    }

    private func detail(for fix: PositionFix) -> String {
        var lines = [fix.source.detailedLabel]
        if let detail = fix.detail { lines.append(detail) }
        lines.append(String(format: "%.5f, %.5f", fix.coordinate.latitude,
                            fix.coordinate.longitude))
        lines.append(String(format: "%.0f m", fix.altitude))
        return lines.joined(separator: "\n")
    }
}

/// The full panel: every source, which one is live, and MQTT controls.
struct PositionSourcePanel: View {
    @StateObject private var fusion = PositionFusion.shared
    @StateObject private var mqtt = MeshtasticMQTTClient.shared
    @StateObject private var mesh = MeshtasticManager.shared

    var body: some View {
        DisclosureGroup {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(fusion.sourceStatuses, id: \.source) { entry in
                    sourceRow(entry.source, fix: entry.fix, isActive: entry.isActive)
                }

                Divider()

                Toggle("Dead reckoning when nothing is heard",
                       isOn: $fusion.extrapolationEnabled)
                    .font(.caption)
                    .help("Project the balloon forward from its last two fixes. "
                          + "Clearly marked as an estimate, and only for a few minutes.")

                Divider()
                mqttControls
            }
            .padding(.top, 6)
        } label: {
            HStack {
                Text("Position Source")
                    .font(.headline)
                Spacer()
                if let best = fusion.best {
                    Text(best.source.label)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    private func sourceRow(
        _ source: PositionSource, fix: PositionFix?, isActive: Bool
    ) -> some View {
        HStack(spacing: 8) {
            Circle()
                .fill(isActive ? Color.green : Color.secondary.opacity(0.3))
                .frame(width: 7, height: 7)

            Image(systemName: source.symbolName)
                .font(.caption)
                .foregroundStyle(.secondary)
                .frame(width: 16)

            VStack(alignment: .leading, spacing: 1) {
                Text(source.label)
                    .font(.caption)
                    .fontWeight(isActive ? .semibold : .regular)

                if let fix {
                    Text(fix.ageDescription + (fix.isStale ? " · stale" : ""))
                        .font(.caption2)
                        .foregroundStyle(fix.isStale ? AnyShapeStyle(Color.orange)
                                                     : AnyShapeStyle(.tertiary))
                } else {
                    Text("no data")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                }
            }

            Spacer()
        }
    }

    private var mqttControls: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Meshtastic MQTT")
                    .font(.caption)
                    .fontWeight(.medium)
                Spacer()
                Text(mqtt.state.label)
                    .font(.caption2)
                    .foregroundStyle(mqtt.state == .connected ? .green : .secondary)
            }

            Text("Falls back to positions relayed by other people's nodes over the "
                 + "internet. Off by default: connecting reaches out to a public "
                 + "broker.")
                .font(.caption2)
                .foregroundStyle(.tertiary)
                .fixedSize(horizontal: false, vertical: true)

            HStack {
                if mqtt.state == .connected {
                    Button("Disconnect") { mqtt.disconnect() }
                        .controlSize(.small)
                    Text("\(mqtt.positionsForwarded) of \(mqtt.messagesReceived)")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .help("Balloon positions forwarded, out of messages seen")
                } else {
                    Button("Connect") {
                        // Only forward positions for the balloon; without an
                        // id this would ingest the whole public mesh.
                        mqtt.balloonNodeID = mesh.balloonNodeID
                        mqtt.connect()
                    }
                    .controlSize(.small)
                    .disabled(mesh.balloonNodeID == nil)
                    .help(mesh.balloonNodeID == nil
                          ? "Set the balloon's callsign in the Meshtastic tab first"
                          : "Connect to \(MeshtasticMQTTClient.defaultHost)")
                }
            }
        }
    }
}
