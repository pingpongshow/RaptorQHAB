"""
Core modules for RaptorHabGS.

Names are resolved on first use rather than imported eagerly. Importing
anything from this package used to pull in every core module, and with them
every dependency any of them needed -- so a headless Pi install that only
wanted the serial protocol still had to satisfy pynmea2, and a script that
only wanted the config had to satisfy the lot.

Deferring keeps `from raptorhabgs.core import X` working exactly as before
while letting an install carry only the dependencies it actually uses. The UI
package already does the same thing for its own reasons.
"""

from typing import Any

__all__ = [
    "TelemetryPoint", "GPSPosition", "BearingDistance", "Mission",
    "LandingPrediction",
    "get_config", "save_config", "AppConfig",
    "LandingPredictionManager",
    "MissionManager",
    "WebGroundStationManager", "WebGPSManager", "WebSerialManager",
    "PayloadLink", "discover_payload_ports",
]

# name -> module it lives in
_EXPORTS = {
    "TelemetryPoint": "telemetry",
    "GPSPosition": "telemetry",
    "BearingDistance": "telemetry",
    "Mission": "telemetry",
    "LandingPrediction": "telemetry",
    "get_config": "config",
    "save_config": "config",
    "AppConfig": "config",
    "LandingPredictionManager": "prediction",
    "MissionManager": "mission_manager",
    "WebGroundStationManager": "web_managers",
    "WebGPSManager": "web_managers",
    "WebSerialManager": "web_managers",
    "PayloadLink": "payload_link",
    "discover_payload_ports": "payload_link",
}


def __getattr__(name: str) -> Any:
    """PEP 562 lazy attribute access."""
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from importlib import import_module
    module = import_module(f".{module_name}", __name__)
    value = getattr(module, name)
    globals()[name] = value          # cache, so this runs once per name
    return value


def __dir__():
    return sorted(__all__)
