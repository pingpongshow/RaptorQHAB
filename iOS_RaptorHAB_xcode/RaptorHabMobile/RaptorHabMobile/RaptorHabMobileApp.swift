//
//  RaptorHabMobileApp.swift
//  RaptorHabMobile
//
//  iOS/iPadOS ground station app for RaptorHab balloon tracking
//  Uses internal GPS and Bluetooth LE connected Heltec modem
//

import SwiftUI

@main
struct RaptorHabMobileApp: App {
    @StateObject private var groundStation = GroundStationManager()
    @StateObject private var locationManager = LocationManager()
    @StateObject private var bleManager = BLESerialManager()
    
    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(groundStation)
                .environmentObject(locationManager)
                .environmentObject(bleManager)
                .onAppear {
                    // Connect managers
                    groundStation.locationManager = locationManager
                    groundStation.bleManager = bleManager
                    
                    // Request location permission on launch
                    locationManager.requestPermission()
                    
                    // Auto-connect to modem via Bluetooth
                    bleManager.autoConnect()
                }
        }
    }
}
