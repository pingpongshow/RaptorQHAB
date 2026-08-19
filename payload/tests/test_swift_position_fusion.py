"""
The macOS app must not trust a remote node's clock.

Freshness decides which source the map draws, so a bad clock on somebody
else's radio silently changes what the operator sees. Two failure modes are
routine on a real mesh:

  - A node with no time sync reports epoch 0. Taken at face value that is a fix
    over fifty years old, discarded as stale the moment it arrives -- losing a
    good position from exactly the kind of bare node most likely to be the only
    one hearing the balloon.
  - A node with a fast clock reports the future, giving a negative age. That
    fix never becomes stale and sits on the map claiming to be current
    indefinitely.

This compiles the real PositionFusion.swift rather than a copy, so the
behaviour cannot drift away from the test.
"""

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FUSION = REPO / "groundstation/macos" / "RaptorHabGS" / "PositionFusion.swift"

pytestmark = pytest.mark.skipif(
    shutil.which("swiftc") is None or not FUSION.exists(),
    reason="swiftc or the macOS app sources are not available here",
)

# PositionFusion references TelemetryPoint; this is the smallest stand-in that
# lets the file compile on its own.
STUB = """
import Foundation
struct TelemetryPoint {
    let timestamp: Date
    let latitude: Double
    let longitude: Double
    let altitude: Double
    let satellites: UInt8
    let rssi: Int
    let snr: Float
    let sequence: UInt16
}
"""

MAIN = """
import Foundation
import CoreLocation

let now = Date()
var failures = 0

func check(_ label: String, _ got: Date, _ want: Date) {
    if abs(got.timeIntervalSince(want)) > 1.0 {
        print("FAIL \\(label)")
        failures += 1
    } else {
        print("PASS \\(label)")
    }
}

check("unset", PositionFix.reconcileTimestamp(Date(timeIntervalSince1970: 0), received: now), now)
check("future", PositionFix.reconcileTimestamp(now.addingTimeInterval(86_400), received: now), now)
check("nil", PositionFix.reconcileTimestamp(nil, received: now), now)

let recent = now.addingTimeInterval(-30)
check("plausible", PositionFix.reconcileTimestamp(recent, received: now), recent)

let skewed = now.addingTimeInterval(30)
check("small skew tolerated", PositionFix.reconcileTimestamp(skewed, received: now), skewed)

let fix = PositionFix(
    source: .meshtasticDirect,
    coordinate: CLLocationCoordinate2D(latitude: 40, longitude: -105),
    altitude: 1000,
    timestamp: PositionFix.reconcileTimestamp(Date(timeIntervalSince1970: 0)))
if fix.isStale {
    print("FAIL unsynced node fix is stale on arrival")
    failures += 1
} else {
    print("PASS unsynced node fix is usable on arrival")
}

exit(failures == 0 ? 0 : 1)
"""


@pytest.fixture(scope="module")
def swift_output(tmp_path_factory):
    work = tmp_path_factory.mktemp("fusion")
    (work / "stub.swift").write_text(textwrap.dedent(STUB))
    (work / "main.swift").write_text(textwrap.dedent(MAIN))
    binary = work / "fusiontest"

    compiled = subprocess.run(
        ["swiftc", "-o", str(binary), str(work / "stub.swift"),
         str(FUSION), str(work / "main.swift")],
        capture_output=True, text=True, timeout=300,
    )
    if compiled.returncode != 0:
        pytest.fail(f"Swift compile failed:\n{compiled.stderr[-3000:]}")

    return subprocess.run([str(binary)], capture_output=True, text=True, timeout=60)


def test_remote_clocks_are_reconciled(swift_output):
    assert swift_output.returncode == 0, swift_output.stdout


def test_an_unsynced_node_position_is_not_discarded(swift_output):
    """The case that actually loses you a position in the field."""
    assert "PASS unsynced node fix is usable on arrival" in swift_output.stdout


def test_a_future_clock_cannot_make_a_fix_immortal(swift_output):
    assert "PASS future" in swift_output.stdout
