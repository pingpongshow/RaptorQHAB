"""
Landing prediction calculator.
Estimates landing site based on current trajectory and wind.
"""

import math
from datetime import datetime, timedelta
from typing import Optional, List, Tuple
from dataclasses import dataclass, field

from .telemetry import TelemetryPoint, LandingPrediction


@dataclass
class WindEstimate:
    """Estimated wind at altitude."""
    speed: float = 0.0  # m/s
    direction: float = 0.0  # degrees (direction wind is coming FROM)
    altitude: float = 0.0  # meters


class LandingPredictionManager:
    """
    Calculates landing predictions based on balloon trajectory.
    
    Uses:
    - Current position and velocity
    - Estimated burst altitude
    - Descent rate model
    - Wind drift estimation from trajectory
    """
    
    def __init__(self):
        # Configuration
        self.burst_altitude: float = 30000.0  # meters
        self.ascent_rate: float = 5.0  # m/s
        self.descent_rate_sea_level: float = 5.0  # m/s at sea level
        
        # State
        self.telemetry_history: List[TelemetryPoint] = []
        self.current_prediction: Optional[LandingPrediction] = None
        self.flight_phase: str = "prelaunch"  # prelaunch, ascending, descending, floating, landed
        
        # Burst detection
        self.max_altitude_seen: float = 0.0
        self.burst_detected: bool = False
        self.burst_altitude_actual: Optional[float] = None
        self.burst_time: Optional[datetime] = None
        
        # Wind estimation
        self.wind_estimates: List[WindEstimate] = []
        
    def update(self, telemetry: TelemetryPoint) -> Optional[LandingPrediction]:
        """
        Update prediction with new telemetry.
        
        Returns updated prediction or None if cannot predict.
        """
        self.telemetry_history.append(telemetry)
        
        # Limit history
        if len(self.telemetry_history) > 1000:
            self.telemetry_history = self.telemetry_history[-500:]
        
        # Detect flight phase
        self._detect_phase(telemetry)
        
        # Estimate wind from drift
        self._estimate_wind()
        
        # Calculate prediction
        self.current_prediction = self._calculate_prediction(telemetry)
        
        return self.current_prediction
    
    def _detect_phase(self, telemetry: TelemetryPoint):
        """Detect current flight phase."""
        alt = telemetry.altitude
        vspeed = telemetry.vertical_speed
        
        # Track max altitude
        if alt > self.max_altitude_seen:
            self.max_altitude_seen = alt
        
        # Burst detection: altitude dropping significantly from max
        if not self.burst_detected and self.max_altitude_seen > 1000:
            if alt < self.max_altitude_seen - 500 and vspeed < -2:
                self.burst_detected = True
                self.burst_altitude_actual = self.max_altitude_seen
                self.burst_time = datetime.now()
                self.flight_phase = "descending"
                return
        
        # Phase detection
        if alt < 100 and abs(vspeed) < 1:
            if self.burst_detected:
                self.flight_phase = "landed"
            else:
                self.flight_phase = "prelaunch"
        elif vspeed > 1:
            self.flight_phase = "ascending"
        elif vspeed < -1:
            self.flight_phase = "descending"
        elif self.max_altitude_seen > 5000 and abs(vspeed) < 2:
            self.flight_phase = "floating"
    
    def _estimate_wind(self):
        """Estimate wind from trajectory drift."""
        if len(self.telemetry_history) < 10:
            return
        
        # Get recent points
        recent = self.telemetry_history[-20:]
        
        # Calculate average ground track vs heading
        # Wind causes drift between heading and actual track
        total_drift_x = 0.0
        total_drift_y = 0.0
        count = 0
        
        for i in range(1, len(recent)):
            prev = recent[i-1]
            curr = recent[i]
            
            # Skip if no movement
            if curr.speed < 0.5:
                continue
            
            # Actual track direction (from position change)
            dlat = curr.latitude - prev.latitude
            dlon = curr.longitude - prev.longitude
            
            if abs(dlat) < 1e-7 and abs(dlon) < 1e-7:
                continue
            
            track = math.degrees(math.atan2(dlon * math.cos(math.radians(curr.latitude)), dlat))
            track = (track + 360) % 360
            
            # Heading is where balloon is pointing
            # Drift is difference between track and expected path
            # For a balloon, drift IS the wind effect
            
            # Estimate wind from ground speed when ascending slowly
            if self.flight_phase == "ascending" and curr.speed > 0:
                # Wind direction is opposite of drift direction
                wind_dir = (track + 180) % 360
                wind_speed = curr.speed
                
                self.wind_estimates.append(WindEstimate(
                    speed=wind_speed,
                    direction=wind_dir,
                    altitude=curr.altitude
                ))
                
                # Limit estimates
                if len(self.wind_estimates) > 100:
                    self.wind_estimates = self.wind_estimates[-50:]
    
    def _calculate_prediction(self, telemetry: TelemetryPoint) -> Optional[LandingPrediction]:
        """Calculate landing prediction."""
        if telemetry.altitude < 100:
            return None
        
        lat = telemetry.latitude
        lon = telemetry.longitude
        alt = telemetry.altitude
        
        # Determine time to landing
        if self.flight_phase == "ascending":
            # Time to burst + time to descend
            alt_to_burst = self.burst_altitude - alt
            time_to_burst = alt_to_burst / max(self.ascent_rate, 1)
            time_to_descend = self._descent_time(self.burst_altitude)
            total_time = time_to_burst + time_to_descend
            
            # Predict position at burst
            burst_lat, burst_lon = self._project_position(
                lat, lon, telemetry.speed, telemetry.heading, time_to_burst
            )
            
            # Then descend from burst point
            pred_lat, pred_lon = self._apply_descent_drift(
                burst_lat, burst_lon, self.burst_altitude, time_to_descend
            )
            
            confidence = "low"
            
        elif self.flight_phase == "descending":
            # Time to descend from current altitude
            total_time = self._descent_time(alt)
            
            # Apply descent drift
            pred_lat, pred_lon = self._apply_descent_drift(
                lat, lon, alt, total_time
            )
            
            # Confidence based on altitude
            if alt < 5000:
                confidence = "high"
            elif alt < 15000:
                confidence = "medium"
            else:
                confidence = "low"
        
        elif self.flight_phase == "floating":
            # Uncertain - use simple projection
            total_time = self._descent_time(alt)
            pred_lat, pred_lon = self._project_position(
                lat, lon, telemetry.speed, telemetry.heading, total_time
            )
            confidence = "low"
        
        else:
            return None
        
        # Calculate distance to predicted landing
        distance = self._haversine(lat, lon, pred_lat, pred_lon)
        bearing = self._bearing(lat, lon, pred_lat, pred_lon)
        
        # Descent rate at current altitude
        descent_rate = self._descent_rate_at_altitude(alt)
        
        return LandingPrediction(
            latitude=pred_lat,
            longitude=pred_lon,
            time_to_landing=total_time,
            distance_to_landing=distance,
            bearing_to_landing=bearing,
            confidence=confidence,
            descent_rate=descent_rate,
            phase=self.flight_phase,
            timestamp=datetime.now()
        )
    
    def _descent_time(self, altitude: float) -> float:
        """Calculate time to descend from altitude to ground."""
        # Simplified: average descent rate
        # In reality, descent rate varies with altitude due to air density
        avg_descent_rate = self.descent_rate_sea_level * 1.5  # Higher at altitude
        return altitude / max(avg_descent_rate, 1)
    
    def _descent_rate_at_altitude(self, altitude: float) -> float:
        """Get descent rate at specific altitude."""
        # Descent rate increases with altitude due to thinner air
        # Simple model: rate = sea_level_rate * sqrt(1 + alt/10000)
        density_factor = math.sqrt(1 + altitude / 10000)
        return self.descent_rate_sea_level * density_factor
    
    def _project_position(self, lat: float, lon: float, 
                          speed: float, heading: float, time: float) -> Tuple[float, float]:
        """Project position forward in time assuming constant velocity."""
        if speed < 0.1 or time < 1:
            return lat, lon
        
        distance = speed * time  # meters
        
        # Convert to lat/lon offset
        heading_rad = math.radians(heading)
        
        # Approximate meters per degree
        meters_per_deg_lat = 111320
        meters_per_deg_lon = 111320 * math.cos(math.radians(lat))
        
        dlat = (distance * math.cos(heading_rad)) / meters_per_deg_lat
        dlon = (distance * math.sin(heading_rad)) / meters_per_deg_lon
        
        return lat + dlat, lon + dlon
    
    def _apply_descent_drift(self, lat: float, lon: float,
                              altitude: float, time: float) -> Tuple[float, float]:
        """Apply wind drift during descent."""
        # Get average wind estimate
        if not self.wind_estimates:
            return lat, lon
        
        # Average recent wind
        recent_wind = self.wind_estimates[-10:]
        avg_speed = sum(w.speed for w in recent_wind) / len(recent_wind)
        avg_dir = sum(w.direction for w in recent_wind) / len(recent_wind)
        
        # Wind pushes balloon in direction wind is going TO
        wind_to_dir = (avg_dir + 180) % 360
        
        # Drift distance
        drift_distance = avg_speed * time * 0.7  # Reduce for varying winds
        
        return self._project_position(lat, lon, drift_distance / time, wind_to_dir, time)
    
    @staticmethod
    def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate distance between two points in meters."""
        R = 6371000
        
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dphi = math.radians(lat2 - lat1)
        dlambda = math.radians(lon2 - lon1)
        
        a = math.sin(dphi/2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda/2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
        
        return R * c
    
    @staticmethod
    def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate bearing from point 1 to point 2."""
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        dlambda = math.radians(lon2 - lon1)
        
        x = math.sin(dlambda) * math.cos(phi2)
        y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
        
        bearing = math.degrees(math.atan2(x, y))
        return (bearing + 360) % 360
    
    def reset(self):
        """Reset prediction state for new flight."""
        self.telemetry_history.clear()
        self.current_prediction = None
        self.flight_phase = "prelaunch"
        self.max_altitude_seen = 0.0
        self.burst_detected = False
        self.burst_altitude_actual = None
        self.burst_time = None
        self.wind_estimates.clear()
