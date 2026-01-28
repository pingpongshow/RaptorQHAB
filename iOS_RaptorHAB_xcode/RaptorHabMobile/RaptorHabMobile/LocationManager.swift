//
//  LocationManager.swift
//  RaptorHabMobile
//
//  Manages internal GPS location for ground station position
//  Uses CoreLocation framework for iOS/iPadOS
//

import Foundation
import CoreLocation
import Combine

// MARK: - Location Manager

class LocationManager: NSObject, ObservableObject, CLLocationManagerDelegate {
    
    private let locationManager = CLLocationManager()
    
    // MARK: - Published Properties
    
    @Published var currentLocation: CLLocation?
    @Published var authorizationStatus: CLAuthorizationStatus = .notDetermined
    @Published var isUpdating = false
    @Published var errorMessage: String?
    @Published var heading: CLHeading?
    
    // Bearing to payload (calculated)
    @Published var bearingToPayload: BearingDistance?
    
    // MARK: - Computed Properties
    
    var hasValidFix: Bool {
        guard let location = currentLocation else { return false }
        return location.horizontalAccuracy >= 0 && location.horizontalAccuracy < 100
    }
    
    var coordinate: CLLocationCoordinate2D? {
        currentLocation?.coordinate
    }
    
    var altitude: Double? {
        guard let location = currentLocation, location.verticalAccuracy >= 0 else { return nil }
        return location.altitude
    }
    
    var accuracy: Double? {
        currentLocation?.horizontalAccuracy
    }
    
    // MARK: - Initialization
    
    override init() {
        super.init()
        locationManager.delegate = self
        locationManager.desiredAccuracy = kCLLocationAccuracyBest
        locationManager.distanceFilter = 5 // Update every 5 meters
        authorizationStatus = locationManager.authorizationStatus
    }
    
    // MARK: - Public Methods
    
    func requestPermission() {
        locationManager.requestWhenInUseAuthorization()
    }
    
    func startUpdating() {
        guard authorizationStatus == .authorizedWhenInUse || authorizationStatus == .authorizedAlways else {
            errorMessage = "Location permission not granted"
            return
        }
        
        locationManager.startUpdatingLocation()
        locationManager.startUpdatingHeading()
        isUpdating = true
    }
    
    func stopUpdating() {
        locationManager.stopUpdatingLocation()
        locationManager.stopUpdatingHeading()
        isUpdating = false
    }
    
    /// Update bearing/distance to payload position
    func updateBearing(toLatitude lat: Double, toLongitude lon: Double, toAltitude alt: Double) {
        guard let currentLocation = currentLocation else {
            bearingToPayload = nil
            return
        }
        
        let payloadCoordinate = CLLocationCoordinate2D(latitude: lat, longitude: lon)
        
        let bearing = calculateBearing(from: currentLocation.coordinate, to: payloadCoordinate)
        let distance = calculateDistance(from: currentLocation.coordinate, to: payloadCoordinate)
        let altDiff = alt - currentLocation.altitude
        let elevation = atan2(altDiff, distance) * 180 / .pi
        
        bearingToPayload = BearingDistance(
            bearing: bearing,
            distance: distance,
            elevation: elevation,
            altitudeDiff: altDiff
        )
    }
    
    // MARK: - CLLocationManagerDelegate
    
    func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let location = locations.last else { return }
        
        // Filter out inaccurate readings
        guard location.horizontalAccuracy >= 0 else { return }
        
        currentLocation = location
        errorMessage = nil
    }
    
    func locationManager(_ manager: CLLocationManager, didUpdateHeading newHeading: CLHeading) {
        if newHeading.headingAccuracy >= 0 {
            heading = newHeading
        }
    }
    
    func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        if let clError = error as? CLError {
            switch clError.code {
            case .denied:
                errorMessage = "Location access denied"
                stopUpdating()
            case .locationUnknown:
                errorMessage = "Unable to determine location"
            default:
                errorMessage = error.localizedDescription
            }
        } else {
            errorMessage = error.localizedDescription
        }
    }
    
    func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        authorizationStatus = manager.authorizationStatus
        
        switch authorizationStatus {
        case .authorizedWhenInUse, .authorizedAlways:
            if !isUpdating {
                startUpdating()
            }
        case .denied, .restricted:
            errorMessage = "Location access denied. Please enable in Settings."
            stopUpdating()
        case .notDetermined:
            break
        @unknown default:
            break
        }
    }
    
    // MARK: - Calculations
    
    private func calculateBearing(from: CLLocationCoordinate2D, to: CLLocationCoordinate2D) -> Double {
        let lat1 = from.latitude * .pi / 180
        let lat2 = to.latitude * .pi / 180
        let dLon = (to.longitude - from.longitude) * .pi / 180
        
        let y = sin(dLon) * cos(lat2)
        let x = cos(lat1) * sin(lat2) - sin(lat1) * cos(lat2) * cos(dLon)
        
        let bearing = atan2(y, x) * 180 / .pi
        return (bearing + 360).truncatingRemainder(dividingBy: 360)
    }
    
    private func calculateDistance(from: CLLocationCoordinate2D, to: CLLocationCoordinate2D) -> Double {
        let R = 6371000.0  // Earth radius in meters
        
        let lat1 = from.latitude * .pi / 180
        let lat2 = to.latitude * .pi / 180
        let dLat = (to.latitude - from.latitude) * .pi / 180
        let dLon = (to.longitude - from.longitude) * .pi / 180
        
        let a = sin(dLat/2) * sin(dLat/2) +
                cos(lat1) * cos(lat2) * sin(dLon/2) * sin(dLon/2)
        let c = 2 * atan2(sqrt(a), sqrt(1-a))
        
        return R * c
    }
}

// MARK: - Bearing and Distance Model

struct BearingDistance {
    var bearing: Double      // degrees true (0-360)
    var distance: Double     // meters
    var elevation: Double    // degrees (positive = look up)
    var altitudeDiff: Double // meters
    
    var bearingCardinal: String {
        let directions = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                          "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
        let index = Int((bearing + 11.25) / 22.5) % 16
        return directions[index]
    }
    
    var distanceFormatted: String {
        if distance < 1000 {
            return String(format: "%.0f m", distance)
        } else {
            return String(format: "%.2f km", distance / 1000)
        }
    }
    
    var distanceMiles: String {
        let miles = distance / 1609.344
        if miles < 1 {
            let feet = distance * 3.28084
            return String(format: "%.0f ft", feet)
        }
        return String(format: "%.2f mi", miles)
    }
}
