//
//  LandingPredictionView.swift
//  RaptorHabGS
//
//  UI for landing prediction display and configuration
//

import SwiftUI
import MapKit

// MARK: - Prediction Sidebar Section

struct LandingPredictionSidebarView: View {
    @ObservedObject var predictor = LandingPredictionManager.shared
    
    var body: some View {
        Section("Landing Prediction") {
            if let prediction = predictor.currentPrediction {
                // Phase indicator
                HStack {
                    Image(systemName: phaseIcon(prediction.phase))
                        .foregroundColor(phaseColor(prediction.phase))
                    Text(prediction.phase.rawValue)
                        .font(.caption)
                    Spacer()
                    Text(prediction.confidence.rawValue)
                        .font(.caption2)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(confidenceColor(prediction.confidence).opacity(0.2))
                        .foregroundColor(confidenceColor(prediction.confidence))
                        .cornerRadius(4)
                }
                
                // Wind source indicator
                HStack {
                    Image(systemName: prediction.usedWindProfile ? "wind" : "arrow.right")
                        .foregroundColor(prediction.usedWindProfile ? .blue : .secondary)
                        .font(.caption2)
                    Text(prediction.usedWindProfile ? "Multi-altitude wind" : "Single wind layer")
                        .font(.caption2)
                        .foregroundColor(.secondary)
                    
                    if predictor.isLoadingWindProfile {
                        ProgressView()
                            .scaleEffect(0.5)
                    }
                }
                
                // Predicted landing
                VStack(alignment: .leading, spacing: 4) {
                    HStack {
                        Image(systemName: "mappin.circle.fill")
                            .foregroundColor(.red)
                        Text("Landing")
                            .font(.caption)
                            .foregroundColor(.secondary)
                    }
                    
                    Text(String(format: "%.5f, %.5f", prediction.predictedLat, prediction.predictedLon))
                        .font(.caption.monospacedDigit())
                }
                
                // Distance and time
                HStack {
                    VStack(alignment: .leading) {
                        Text(formatDistance(prediction.distanceToLanding))
                            .font(.caption.monospacedDigit().bold())
                        Text("Distance")
                            .font(.caption2)
                            .foregroundColor(.secondary)
                    }
                    
                    Spacer()
                    
                    VStack(alignment: .center) {
                        Text(String(format: "%.0f°", prediction.bearingToLanding))
                            .font(.caption.monospacedDigit().bold())
                        Text("Bearing")
                            .font(.caption2)
                            .foregroundColor(.secondary)
                    }
                    
                    Spacer()
                    
                    VStack(alignment: .trailing) {
                        Text(formatTime(prediction.timeToLanding))
                            .font(.caption.monospacedDigit().bold())
                        Text("ETA")
                            .font(.caption2)
                            .foregroundColor(.secondary)
                    }
                }
                
                // Descent rate
                if prediction.descentRate != 0 {
                    HStack {
                        Image(systemName: prediction.descentRate > 0 ? "arrow.up" : "arrow.down")
                            .foregroundColor(prediction.descentRate > 0 ? .green : .orange)
                        Text(String(format: "%.1f m/s", abs(prediction.descentRate)))
                            .font(.caption.monospacedDigit())
                        
                        Spacer()
                        
                        Button {
                            predictor.showSettings = true
                        } label: {
                            Image(systemName: "gear")
                        }
                        .buttonStyle(.borderless)
                    }
                }
            } else {
                HStack {
                    Text("Waiting for telemetry...")
                        .font(.caption)
                        .foregroundColor(.secondary)
                    
                    Spacer()
                    
                    Button {
                        predictor.showSettings = true
                    } label: {
                        Image(systemName: "gear")
                    }
                    .buttonStyle(.borderless)
                }
            }
        }
    }
    
    private func phaseIcon(_ phase: FlightPhase) -> String {
        switch phase {
        case .prelaunch: return "circle"
        case .ascending: return "arrow.up.circle.fill"
        case .floating: return "arrow.left.arrow.right.circle.fill"
        case .descending: return "arrow.down.circle.fill"
        case .landed: return "checkmark.circle.fill"
        }
    }
    
    private func phaseColor(_ phase: FlightPhase) -> Color {
        switch phase {
        case .prelaunch: return .gray
        case .ascending: return .green
        case .floating: return .blue
        case .descending: return .orange
        case .landed: return .purple
        }
    }
    
    private func confidenceColor(_ confidence: PredictionConfidence) -> Color {
        switch confidence {
        case .high: return .green
        case .medium: return .yellow
        case .low: return .orange
        case .veryLow: return .red
        }
    }
    
    private func formatDistance(_ meters: Double) -> String {
        if meters < 1000 {
            return String(format: "%.0f m", meters)
        } else {
            return String(format: "%.1f km", meters / 1000)
        }
    }
    
    private func formatTime(_ seconds: TimeInterval) -> String {
        let minutes = Int(seconds) / 60
        let secs = Int(seconds) % 60
        if minutes >= 60 {
            let hours = minutes / 60
            let mins = minutes % 60
            return String(format: "%dh %dm", hours, mins)
        } else {
            return String(format: "%d:%02d", minutes, secs)
        }
    }
}

// MARK: - Prediction Settings View

struct PredictionSettingsView: View {
    @ObservedObject var predictor = LandingPredictionManager.shared
    @Environment(\.dismiss) var dismiss
    
    var body: some View {
        VStack(spacing: 0) {
            // Header
            HStack {
                Image(systemName: "location.circle")
                    .foregroundColor(.orange)
                Text("Landing Prediction Settings")
                    .font(.title2.bold())
                Spacer()
                Button("Done") { dismiss() }
            }
            .padding()
            
            Divider()
            
            Form {
                // Wind Settings
                Section("Wind Data") {
                    Toggle("Use multi-altitude wind profile", isOn: $predictor.config.useWindProfile)
                    
                    if predictor.config.useWindProfile {
                        HStack {
                            if predictor.isLoadingWindProfile {
                                ProgressView()
                                    .scaleEffect(0.7)
                                Text("Loading wind data...")
                                    .foregroundColor(.secondary)
                            } else if let profile = predictor.windProfile, profile.isValid {
                                Image(systemName: "checkmark.circle")
                                    .foregroundColor(.green)
                                Text("\(profile.layers.count) altitude layers")
                                    .foregroundColor(.secondary)
                            } else if let error = predictor.windProfileError {
                                Image(systemName: "exclamationmark.triangle")
                                    .foregroundColor(.orange)
                                Text(error)
                                    .font(.caption)
                                    .foregroundColor(.orange)
                            } else {
                                Text("Wind profile will load when telemetry received")
                                    .font(.caption)
                                    .foregroundColor(.secondary)
                            }
                        }
                        
                        Text("Fetches wind data at multiple altitudes from Open-Meteo API for accurate predictions")
                            .font(.caption2)
                            .foregroundColor(.secondary)
                    }
                    
                    Divider()
                    
                    Toggle("Auto-calculate from drift", isOn: $predictor.config.useAutoWind)
                        .disabled(predictor.config.useWindProfile)
                    
                    if !predictor.config.useWindProfile {
                        if predictor.config.useAutoWind {
                            HStack {
                                Text("Calculated:")
                                Spacer()
                                Text(String(format: "%.1f m/s from %.0f°",
                                           predictor.calculatedWindSpeed,
                                           predictor.calculatedWindDirection))
                                    .foregroundColor(.secondary)
                            }
                        } else {
                            HStack {
                                Text("Wind Speed")
                                Spacer()
                                TextField("", value: $predictor.config.windSpeed, format: .number)
                                    .textFieldStyle(.roundedBorder)
                                    .frame(width: 80)
                                Text("m/s")
                                    .foregroundColor(.secondary)
                            }
                            
                            HStack {
                                Text("Wind Direction")
                                Spacer()
                                TextField("", value: $predictor.config.windDirection, format: .number)
                                    .textFieldStyle(.roundedBorder)
                                    .frame(width: 80)
                                Text("° from")
                                    .foregroundColor(.secondary)
                            }
                        }
                    }
                }
                
                // Flight Parameters
                Section("Flight Parameters") {
                    HStack {
                        Text("Burst Altitude")
                        Spacer()
                        TextField("", value: $predictor.config.burstAltitude, format: .number)
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 100)
                        Text("m")
                            .foregroundColor(.secondary)
                    }
                    
                    HStack {
                        Text("Target Landing Alt")
                        Spacer()
                        TextField("", value: $predictor.config.seaLevelTarget, format: .number)
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 100)
                        Text("m")
                            .foregroundColor(.secondary)
                    }
                }
                
                // Descent Model
                Section("Descent Model") {
                    HStack {
                        Text("Ascent Rate")
                        Spacer()
                        TextField("", value: $predictor.config.ascentRate, format: .number)
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 80)
                        Text("m/s")
                            .foregroundColor(.secondary)
                    }
                    
                    HStack {
                        Text("Descent at Burst")
                        Spacer()
                        TextField("", value: $predictor.config.descentRateAtBurst, format: .number)
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 80)
                        Text("m/s")
                            .foregroundColor(.secondary)
                    }
                    
                    HStack {
                        Text("Descent at Landing")
                        Spacer()
                        TextField("", value: $predictor.config.descentRateAtLanding, format: .number)
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 80)
                        Text("m/s")
                            .foregroundColor(.secondary)
                    }
                    
                    HStack {
                        Text("Manual Override")
                        Spacer()
                        TextField("Optional", value: $predictor.config.descentRateOverride, format: .number)
                            .textFieldStyle(.roundedBorder)
                            .frame(width: 80)
                        Text("m/s")
                            .foregroundColor(.secondary)
                    }
                }
                
                // Info
                Section("About") {
                    Text("Landing prediction uses current position, descent rate, and wind to estimate where the payload will land.")
                        .font(.caption)
                        .foregroundColor(.secondary)
                    
                    Text("Accuracy improves as the payload descends. At low altitudes, predictions are typically within a few hundred meters.")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
            }
            .formStyle(.grouped)
        }
        .frame(width: 450, height: 550)
    }
}

// MARK: - Map Annotation for Prediction

struct PredictionAnnotation: Identifiable {
    let id = UUID()
    let coordinate: CLLocationCoordinate2D
    let confidence: PredictionConfidence
}

#Preview {
    LandingPredictionSidebarView()
        .frame(width: 250)
}
