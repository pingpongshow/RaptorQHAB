//
//  RaptorHabGSApp.swift
//  RaptorHabGS
//
//  RaptorHab Ground Station for macOS with RTL-SDR
//  Receives and decodes telemetry from RaptorHab high-altitude balloon payload
//

import SwiftUI

// MARK: - App Delegate for Termination Handling

class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        let missionManager = MissionManager.shared

        // Auto-save any active recording before quitting
        if missionManager.isRecording {
            missionManager.stopRecording()
        }

        return .terminateNow
    }
}

@main
struct RaptorHabGSApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @StateObject private var groundStation = GroundStationManager()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(groundStation)
                .frame(minWidth: 900, idealWidth: 1200, maxWidth: .infinity,
                       minHeight: 600, idealHeight: 800, maxHeight: .infinity)
        }
        .defaultSize(width: 1200, height: 800)
        .windowStyle(.hiddenTitleBar)
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandMenu("Radio") {
                Button("Start Receiving") {
                    groundStation.startReceiving()
                }
                .keyboardShortcut("r", modifiers: .command)
                .disabled(groundStation.isReceiving)
                
                Button("Stop Receiving") {
                    groundStation.stopReceiving()
                }
                .keyboardShortcut(".", modifiers: .command)
                .disabled(!groundStation.isReceiving)
                
                Divider()
                
                Button("Configure Radio...") {
                    groundStation.showRadioConfig = true
                }
                .keyboardShortcut(",", modifiers: .command)
            }
        }
        
        Settings {
            SettingsView()
                .environmentObject(groundStation)
        }
    }
}
