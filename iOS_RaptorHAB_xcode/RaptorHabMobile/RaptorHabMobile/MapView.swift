//
//  MapView.swift
//  RaptorHabMobile
//
//  Map display with flight path tracking, payload/ground station positions, and offline map support
//

import SwiftUI
import MapKit

struct MapView: View {
    @EnvironmentObject var groundStation: GroundStationManager
    @EnvironmentObject var locationManager: LocationManager
    @ObservedObject var offlineMapManager = OfflineMapManager.shared
    @ObservedObject var predictionManager = LandingPredictionManager.shared
    
    @State private var followPayload = true
    @State private var mapType: MKMapType = .standard
    @State private var useOfflineTiles = false
    @State private var showOfflineMaps = false
    
    var body: some View {
        ZStack {
            // Map using UIKit for offline tile overlay support
            MapViewRepresentable(
                groundStation: groundStation,
                locationManager: locationManager,
                predictionManager: predictionManager,
                offlineMapManager: offlineMapManager,
                mapType: $mapType,
                useOfflineTiles: $useOfflineTiles,
                followPayload: $followPayload
            )
            .edgesIgnoringSafeArea(.all)
            
            // Overlay controls
            VStack {
                Spacer()
                
                HStack {
                    Spacer()
                    
                    VStack(spacing: 8) {
                        // Follow payload button
                        Button {
                            followPayload.toggle()
                        } label: {
                            Image(systemName: followPayload ? "lock.fill" : "lock.open")
                                .padding(12)
                                .background(Color(.systemBackground).opacity(0.9))
                                .clipShape(Circle())
                                .shadow(radius: 2)
                        }
                        
                        // Offline tiles toggle
                        Button {
                            useOfflineTiles.toggle()
                        } label: {
                            Image(systemName: useOfflineTiles ? "wifi.slash" : "wifi")
                                .padding(12)
                                .background(useOfflineTiles ? Color.orange.opacity(0.9) : Color(.systemBackground).opacity(0.9))
                                .foregroundColor(useOfflineTiles ? .white : .primary)
                                .clipShape(Circle())
                                .shadow(radius: 2)
                        }
                        
                        // Offline map settings
                        Button {
                            showOfflineMaps = true
                        } label: {
                            Image(systemName: "square.and.arrow.down")
                                .padding(12)
                                .background(Color(.systemBackground).opacity(0.9))
                                .clipShape(Circle())
                                .shadow(radius: 2)
                        }
                        
                        // Map style
                        Menu {
                            Button("Standard") { mapType = .standard }
                            Button("Satellite") { mapType = .satellite }
                            Button("Hybrid") { mapType = .hybrid }
                        } label: {
                            Image(systemName: "map")
                                .padding(12)
                                .background(Color(.systemBackground).opacity(0.9))
                                .clipShape(Circle())
                                .shadow(radius: 2)
                        }
                    }
                    .padding()
                }
            }
            
            // Info overlay
            VStack {
                HStack {
                    telemetryOverlay
                    Spacer()
                }
                .padding()
                Spacer()
            }
        }
        .navigationTitle("Map")
        .sheet(isPresented: $showOfflineMaps) {
            OfflineMapSettingsView()
        }
    }
    
    // MARK: - Telemetry Overlay
    
    private var telemetryOverlay: some View {
        VStack(alignment: .leading, spacing: 4) {
            if let telemetry = groundStation.latestTelemetry {
                HStack {
                    Image(systemName: "arrow.up.to.line")
                    Text(String(format: "%.0f m", telemetry.altitude))
                }
                .font(.headline)
                
                HStack {
                    Image(systemName: "speedometer")
                    Text(String(format: "%.1f m/s", telemetry.speed))
                }
                .font(.subheadline)
                
                if let bearing = locationManager.bearingToPayload {
                    HStack {
                        Image(systemName: "safari")
                        Text(String(format: "%.0f° • %@", bearing.bearing, bearing.distanceFormatted))
                    }
                    .font(.subheadline)
                }
                
                // Show landing prediction
                if let prediction = predictionManager.currentPrediction {
                    Divider()
                    HStack {
                        Image(systemName: "scope")
                        VStack(alignment: .leading) {
                            Text("Landing: \(String(format: "%.1f km", prediction.distanceToLanding / 1000))")
                            Text("\(String(format: "%.0f min", prediction.timeToLanding / 60))")
                                .font(.caption)
                                .foregroundColor(.secondary)
                        }
                    }
                    .font(.subheadline)
                }
            }
            
            // Offline status
            if useOfflineTiles {
                Divider()
                HStack {
                    Image(systemName: "wifi.slash")
                    Text("Offline: \(offlineMapManager.tileCount) tiles")
                }
                .font(.caption)
                .foregroundColor(.orange)
            }
        }
        .padding(12)
        .background(Color(.systemBackground).opacity(0.9))
        .cornerRadius(8)
        .shadow(radius: 2)
    }
}

// MARK: - UIKit MapView Representable

struct MapViewRepresentable: UIViewRepresentable {
    let groundStation: GroundStationManager
    let locationManager: LocationManager
    let predictionManager: LandingPredictionManager
    let offlineMapManager: OfflineMapManager
    @Binding var mapType: MKMapType
    @Binding var useOfflineTiles: Bool
    @Binding var followPayload: Bool
    
    func makeCoordinator() -> Coordinator {
        Coordinator(self)
    }
    
    func makeUIView(context: Context) -> MKMapView {
        let mapView = MKMapView()
        mapView.delegate = context.coordinator
        mapView.showsUserLocation = true
        mapView.showsCompass = true
        mapView.showsScale = true
        return mapView
    }
    
    func updateUIView(_ mapView: MKMapView, context: Context) {
        mapView.mapType = mapType
        
        // Handle offline tile overlay
        let hasOverlay = mapView.overlays.contains { $0 is OfflineTileOverlay }
        
        if useOfflineTiles && !hasOverlay {
            mapView.addOverlay(offlineMapManager.tileOverlay, level: .aboveLabels)
        } else if !useOfflineTiles && hasOverlay {
            mapView.removeOverlays(mapView.overlays.filter { $0 is OfflineTileOverlay })
        }
        
        // Update annotations and overlays
        context.coordinator.updateMap(mapView)
        
        // Follow payload if enabled
        if followPayload, let telemetry = groundStation.latestTelemetry {
            let coordinate = CLLocationCoordinate2D(latitude: telemetry.latitude, longitude: telemetry.longitude)
            let region = MKCoordinateRegion(center: coordinate, latitudinalMeters: 10000, longitudinalMeters: 10000)
            mapView.setRegion(region, animated: true)
        }
    }
    
    class Coordinator: NSObject, MKMapViewDelegate {
        var parent: MapViewRepresentable
        private var flightPathOverlay: MKPolyline?
        private var lineToPayloadOverlay: MKPolyline?
        private var predictionOverlay: MKPolyline?
        private var payloadAnnotation: MKPointAnnotation?
        private var landingAnnotation: MKPointAnnotation?
        
        init(_ parent: MapViewRepresentable) {
            self.parent = parent
        }
        
        func updateMap(_ mapView: MKMapView) {
            // Remove old overlays (except tile overlay)
            let overlaysToRemove = mapView.overlays.filter { !($0 is OfflineTileOverlay) }
            mapView.removeOverlays(overlaysToRemove)
            
            // Remove old annotations
            let annotationsToRemove = mapView.annotations.filter { !($0 is MKUserLocation) }
            mapView.removeAnnotations(annotationsToRemove)
            
            // Add flight path
            if parent.groundStation.telemetryHistory.count > 1 {
                let coordinates = parent.groundStation.telemetryHistory.map {
                    CLLocationCoordinate2D(latitude: $0.latitude, longitude: $0.longitude)
                }
                let polyline = MKPolyline(coordinates: coordinates, count: coordinates.count)
                polyline.title = "flightPath"
                mapView.addOverlay(polyline)
            }
            
            // Add payload annotation
            if let telemetry = parent.groundStation.latestTelemetry {
                let annotation = MKPointAnnotation()
                annotation.coordinate = CLLocationCoordinate2D(latitude: telemetry.latitude, longitude: telemetry.longitude)
                annotation.title = "Payload"
                annotation.subtitle = String(format: "%.0f m", telemetry.altitude)
                mapView.addAnnotation(annotation)
                
                // Line from ground station to payload
                if let gsLocation = parent.locationManager.currentLocation {
                    let lineCoords = [gsLocation.coordinate, annotation.coordinate]
                    let line = MKPolyline(coordinates: lineCoords, count: 2)
                    line.title = "lineToPayload"
                    mapView.addOverlay(line)
                }
            }
            
            // Add prediction path and landing zone
            if let prediction = parent.predictionManager.currentPrediction {
                // Draw prediction trajectory from current position to landing
                if let telemetry = parent.groundStation.latestTelemetry {
                    let currentCoord = CLLocationCoordinate2D(latitude: telemetry.latitude, longitude: telemetry.longitude)
                    let landingCoord = CLLocationCoordinate2D(latitude: prediction.predictedLat, longitude: prediction.predictedLon)
                    
                    // Create prediction path polyline
                    let predictionCoords = [currentCoord, landingCoord]
                    let predLine = MKPolyline(coordinates: predictionCoords, count: 2)
                    predLine.title = "prediction"
                    mapView.addOverlay(predLine)
                }
                
                // Landing marker with circle for uncertainty
                let landingAnnotation = MKPointAnnotation()
                landingAnnotation.coordinate = CLLocationCoordinate2D(latitude: prediction.predictedLat, longitude: prediction.predictedLon)
                landingAnnotation.title = "Predicted Landing"
                landingAnnotation.subtitle = String(format: "%.1f km, %.0f min", prediction.distanceToLanding / 1000, prediction.timeToLanding / 60)
                mapView.addAnnotation(landingAnnotation)
                
                // Add uncertainty circle based on confidence
                let uncertaintyRadius: CLLocationDistance = {
                    switch prediction.confidence {
                    case .high: return 500
                    case .medium: return 1500
                    case .low: return 3000
                    case .veryLow: return 5000
                    }
                }()
                let circle = MKCircle(center: landingAnnotation.coordinate, radius: uncertaintyRadius)
                circle.title = "uncertainty"
                mapView.addOverlay(circle)
            }
        }
        
        func mapView(_ mapView: MKMapView, rendererFor overlay: MKOverlay) -> MKOverlayRenderer {
            if let tileOverlay = overlay as? MKTileOverlay {
                return MKTileOverlayRenderer(tileOverlay: tileOverlay)
            }
            
            if let circle = overlay as? MKCircle {
                let renderer = MKCircleRenderer(circle: circle)
                renderer.fillColor = UIColor.orange.withAlphaComponent(0.2)
                renderer.strokeColor = UIColor.orange
                renderer.lineWidth = 2
                return renderer
            }
            
            if let polyline = overlay as? MKPolyline {
                let renderer = MKPolylineRenderer(polyline: polyline)
                
                switch polyline.title {
                case "flightPath":
                    renderer.strokeColor = .red
                    renderer.lineWidth = 3
                case "lineToPayload":
                    renderer.strokeColor = UIColor.blue.withAlphaComponent(0.5)
                    renderer.lineWidth = 2
                    renderer.lineDashPattern = [5, 5]
                case "prediction":
                    renderer.strokeColor = .orange
                    renderer.lineWidth = 2
                    renderer.lineDashPattern = [10, 5]
                default:
                    renderer.strokeColor = .gray
                    renderer.lineWidth = 2
                }
                
                return renderer
            }
            
            return MKOverlayRenderer(overlay: overlay)
        }
        
        func mapView(_ mapView: MKMapView, viewFor annotation: MKAnnotation) -> MKAnnotationView? {
            if annotation is MKUserLocation { return nil }
            
            let identifier = "CustomAnnotation"
            var annotationView = mapView.dequeueReusableAnnotationView(withIdentifier: identifier)
            
            if annotationView == nil {
                annotationView = MKMarkerAnnotationView(annotation: annotation, reuseIdentifier: identifier)
                annotationView?.canShowCallout = true
            } else {
                annotationView?.annotation = annotation
            }
            
            if let markerView = annotationView as? MKMarkerAnnotationView {
                switch annotation.title {
                case "Payload":
                    markerView.markerTintColor = .red
                    markerView.glyphImage = UIImage(systemName: "airplane")
                case "Predicted Landing":
                    markerView.markerTintColor = .orange
                    markerView.glyphImage = UIImage(systemName: "scope")
                default:
                    markerView.markerTintColor = .blue
                }
            }
            
            return annotationView
        }
    }
}

#Preview {
    MapView()
        .environmentObject(GroundStationManager())
        .environmentObject(LocationManager())
}
