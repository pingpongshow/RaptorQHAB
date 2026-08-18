//
//  PayloadConsoleView.swift
//  RaptorHabGS
//
//  A terminal on the payload, over USB.
//
//  Gated on an active USB link. The payload's console service refuses to bind
//  to anything but its gadget TTY, so this is reachable only by someone
//  holding the cable — never over the radio.
//

import SwiftUI

struct PayloadConsoleView: View {
    @StateObject private var link = PiLinkManager.shared

    @State private var input = ""
    @State private var history: [String] = []
    @State private var historyIndex: Int?
    @State private var autoScroll = true

    var body: some View {
        VStack(spacing: 0) {
            if link.isConnected {
                terminal
                Divider()
                inputBar
            } else {
                unavailable
            }
        }
    }

    // MARK: - Unavailable

    private var unavailable: some View {
        VStack(spacing: 14) {
            Image(systemName: "terminal")
                .font(.system(size: 44))
                .foregroundStyle(.tertiary)

            Text("Terminal requires a USB connection")
                .font(.title3)

            Text("The payload only serves a shell on its USB port. Connect the "
                 + "Pi's data port and connect from the Config tab.")
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 400)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: - Terminal

    private var terminal: some View {
        VStack(spacing: 0) {
            toolbar
            Divider()

            ScrollViewReader { proxy in
                ScrollView {
                    Text(outputText)
                        .font(.system(.body, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(8)
                        .id("terminal-output")

                    // Scroll anchor: pinning to the text itself jumps to its
                    // top, which is the wrong end of a growing log.
                    Color.clear.frame(height: 1).id("terminal-bottom")
                }
                .background(Color(nsColor: .textBackgroundColor))
                .onChange(of: link.consoleOutput.count) { _, _ in
                    guard autoScroll else { return }
                    withAnimation(.easeOut(duration: 0.1)) {
                        proxy.scrollTo("terminal-bottom", anchor: .bottom)
                    }
                }
            }
        }
    }

    private var toolbar: some View {
        HStack(spacing: 10) {
            if link.shellRunning {
                Label("Shell running", systemImage: "circle.fill")
                    .font(.caption)
                    .foregroundStyle(.green)

                Button("Stop") {
                    Task { await link.stopShell() }
                }
            } else {
                Button("Start shell") {
                    Task { await link.startShell() }
                }
                .buttonStyle(.borderedProminent)
            }

            Spacer()

            Toggle("Follow", isOn: $autoScroll)
                .toggleStyle(.switch)
                .help("Scroll to the newest output")

            Button {
                link.clearConsole()
            } label: {
                Image(systemName: "trash")
            }
            .help("Clear the scrollback")
            .disabled(link.consoleOutput.isEmpty)
        }
        .padding(8)
    }

    private var inputBar: some View {
        HStack(spacing: 8) {
            Image(systemName: "chevron.right")
                .foregroundStyle(.secondary)
                .font(.system(.body, design: .monospaced))

            TextField("", text: $input)
                .textFieldStyle(.plain)
                .font(.system(.body, design: .monospaced))
                .disabled(!link.shellRunning)
                .onSubmit(send)
                .onKeyPress(.upArrow) { recallHistory(offset: -1); return .handled }
                .onKeyPress(.downArrow) { recallHistory(offset: 1); return .handled }

            // Ctrl-C is what you reach for when a command runs away, and a
            // plain text field has no way to send it.
            Button("^C") {
                link.sendConsole("\u{03}")
            }
            .help("Interrupt the running command")
            .disabled(!link.shellRunning)

            Button("^D") {
                link.sendConsole("\u{04}")
            }
            .help("End of input")
            .disabled(!link.shellRunning)
        }
        .padding(8)
        .background(Color.primary.opacity(0.04))
    }

    // MARK: - Behaviour

    private var outputText: String {
        // Terminal output is bytes, not necessarily valid UTF-8 at any given
        // boundary, so decode leniently rather than showing nothing.
        String(decoding: link.consoleOutput, as: UTF8.self)
    }

    private func send() {
        guard link.shellRunning else { return }

        link.sendConsole(input + "\n")

        if !input.trimmingCharacters(in: .whitespaces).isEmpty {
            history.append(input)
            if history.count > 200 { history.removeFirst() }
        }
        historyIndex = nil
        input = ""
    }

    private func recallHistory(offset: Int) {
        guard !history.isEmpty else { return }

        let current = historyIndex ?? history.count
        let next = max(0, min(history.count, current + offset))

        historyIndex = next
        input = next < history.count ? history[next] : ""
    }
}
