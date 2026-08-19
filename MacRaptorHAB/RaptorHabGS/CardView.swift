//
//  CardView.swift — the recovered-card tab.
//
//  Readability is the first thing on screen. Everything else is secondary to
//  knowing whether this card can be opened at all.
//

import SwiftUI
import UniformTypeIdentifiers

struct CardView: View {
    @StateObject private var manager = CardImportManager()
    @State private var selectedCard: URL?
    @State private var selectedFile: CardFile?
    @State private var wantImages = true
    @State private var wantTelemetry = true
    @State private var wantLogs = true
    @State private var showingErrors = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            source
            Divider()

            if let survey = manager.survey {
                summary(survey)
                Divider()
                HSplitView {
                    fileList(survey)
                        .frame(minWidth: 260, idealWidth: 320)
                    preview
                        .frame(minWidth: 380)
                }
                importControls(survey)
            } else {
                Spacer()
                Text("Insert a payload card and choose Read card.")
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .center)
                Spacer()
            }
        }
        .padding()
        .alert("Some files could not be recovered", isPresented: $showingErrors) {
            Button("OK", role: .cancel) {}
        } message: {
            Text((manager.lastOutcome?.errors.prefix(15) ?? []).joined(separator: "\n"))
        }
    }

    // MARK: - Source

    private var source: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Picker("Card", selection: $selectedCard) {
                    Text("Select a card").tag(URL?.none)
                    ForEach(manager.candidates, id: \.self) { url in
                        Text(url.lastPathComponent).tag(URL?.some(url))
                    }
                }
                .frame(maxWidth: 320)

                Button("Rescan") { manager.rescan() }
                Button("Browse…") { browse() }
                Button("Read card") {
                    if let selectedCard { manager.read(selectedCard) }
                }
                .buttonStyle(.borderedProminent)
                .disabled(selectedCard == nil)
                Spacer()
            }

            Label(manager.haveKey
                  ? "Recording key loaded from \(manager.keyPath.path)"
                  : "No recording key at \(manager.keyPath.path) — sealed files cannot be opened",
                  systemImage: manager.haveKey ? "key.fill" : "key.slash")
                .font(.caption)
                .foregroundStyle(manager.haveKey ? AnyShapeStyle(.secondary) : AnyShapeStyle(Color.orange))
        }
    }

    private func browse() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.message = "Choose the card, or the directory holding its images and logs"
        if panel.runModal() == .OK, let url = panel.url {
            selectedCard = url
            manager.read(url)
        }
    }

    // MARK: - Summary

    private func summary(_ survey: CardSurvey) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 12) {
                Text(survey.callsign ?? "unknown payload").font(.headline)
                Text("\(survey.images.count) images · \(survey.telemetry.count) telemetry · "
                     + "\(survey.logs.count) logs · "
                     + String(format: "%.1f MB", Double(survey.totalBytes) / 1e6))
                    .foregroundStyle(.secondary)
            }

            Label(survey.verdict,
                  systemImage: survey.readable ? "lock.open.fill" : "lock.trianglebadge.exclamationmark")
                .foregroundStyle(survey.readable ? Color.green : Color.red)

            if let key = survey.payloadPublicKey {
                Text("sealed to \(key)").font(.caption2).foregroundStyle(.secondary)
            }
            ForEach(survey.notes, id: \.self) { note in
                Text(note).font(.caption).foregroundStyle(.orange).fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    // MARK: - Files

    private func fileList(_ survey: CardSurvey) -> some View {
        List(survey.images, id: \.id, selection: Binding(
            get: { selectedFile?.id },
            set: { id in selectedFile = survey.images.first { $0.id == id } }
        )) { file in
            HStack {
                if file.sealed { Image(systemName: "lock.fill").foregroundStyle(.secondary) }
                Text(file.plainName).font(.system(.body, design: .monospaced)).lineLimit(1)
                Spacer()
                Text("\(file.size / 1000) kB").font(.caption).foregroundStyle(.secondary)
            }
            .tag(file.id)
        }
    }

    private var preview: some View {
        VStack {
            if let file = selectedFile {
                if let data = manager.contents(of: file), let image = NSImage(data: data) {
                    Image(nsImage: image)
                        .resizable().scaledToFit()
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                    Text("\(file.plainName) — \(Int(image.size.width))×\(Int(image.size.height))"
                         + (file.sealed ? ", decrypted for display" : ""))
                        .font(.caption).foregroundStyle(.secondary)
                } else {
                    Spacer()
                    Label(file.sealed
                          ? "Sealed — no key here can open this"
                          : "Could not decode this file",
                          systemImage: "lock.fill")
                        .foregroundStyle(.secondary)
                    Spacer()
                }
            } else {
                Spacer()
                Text("Select an image").foregroundStyle(.secondary)
                Spacer()
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.black.opacity(0.15))
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }

    // MARK: - Import

    private func importControls(_ survey: CardSurvey) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Toggle("Images", isOn: $wantImages)
                Toggle("Telemetry", isOn: $wantTelemetry)
                Toggle("Logs", isOn: $wantLogs)
                Button("Import & decrypt…") { runImport(survey) }
                    .buttonStyle(.borderedProminent)
                    .disabled(manager.busy)
                Spacer()
            }

            if manager.busy {
                ProgressView(value: manager.progress) {
                    Text(manager.progressLabel).font(.caption)
                }
            }

            if let outcome = manager.lastOutcome {
                HStack(spacing: 4) {
                    Text("\(outcome.decrypted) decrypted, \(outcome.copied) copied, "
                         + "\(outcome.skipped) already present, \(outcome.failed) failed")
                        .font(.caption)
                    if !outcome.errors.isEmpty {
                        Button("Details") { showingErrors = true }.font(.caption)
                    }
                    if let directory = outcome.outputDirectory {
                        Button("Reveal") {
                            NSWorkspace.shared.open(directory)
                        }.font(.caption)
                    }
                }
                .foregroundStyle(.secondary)
            }
        }
    }

    private func runImport(_ survey: CardSurvey) {
        var files: [CardFile] = []
        if wantImages { files += survey.images }
        if wantTelemetry { files += survey.telemetry }
        if wantLogs { files += survey.logs }
        guard !files.isEmpty else { return }

        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.message = "Where should the recovered files go?"
        guard panel.runModal() == .OK, let base = panel.url else { return }

        manager.importFiles(files,
                            to: base.appendingPathComponent(survey.callsign ?? "payload"))
    }
}
