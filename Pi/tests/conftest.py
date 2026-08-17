"""
Shared pytest configuration.

Every test in this suite must run on a laptop with no radio, no camera, and
no GPS attached. Nothing here may import RPi.GPIO or spidev at module scope.
"""

import sys
from pathlib import Path

# Make `common` and `airborne` importable the same way the payload does it.
PI_ROOT = Path(__file__).resolve().parent.parent
if str(PI_ROOT) not in sys.path:
    sys.path.insert(0, str(PI_ROOT))
