#!/usr/bin/env python3
"""
RaptorHabGS Web Server - Headless Mode

Run the ground station as a web server for remote access.
Access the GUI via web browser at http://<host>:<port>

Usage:
    python web_server.py [--host HOST] [--port PORT] [--debug]
    
Examples:
    python web_server.py                    # Run on 0.0.0.0:5000
    python web_server.py --port 8080        # Run on port 8080
    python web_server.py --host 127.0.0.1   # Localhost only
"""

import sys
import argparse
from pathlib import Path

# Add the package directory to path
sys.path.insert(0, str(Path(__file__).parent))


def main():
    parser = argparse.ArgumentParser(
        description='RaptorHabGS Web Server - Headless ground station with web GUI'
    )
    parser.add_argument(
        '--host', 
        default='0.0.0.0',
        help='Host address to bind to (default: 0.0.0.0 for all interfaces)'
    )
    parser.add_argument(
        '--port', 
        type=int, 
        default=5000,
        help='Port to listen on (default: 5000)'
    )
    parser.add_argument(
        '--debug', 
        action='store_true',
        help='Enable debug mode'
    )
    
    args = parser.parse_args()
    
    # Create data directories
    from raptorhabgs.core.config import get_data_directory
    data_dir = get_data_directory()
    for subdir in ["images", "missions", "telemetry", "logs"]:
        (data_dir / subdir).mkdir(parents=True, exist_ok=True)
    
    # Start web server
    from raptorhabgs.web.server import WebServer
    
    server = WebServer(host=args.host, port=args.port)
    
    try:
        server.run(debug=args.debug)
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
