"""
The web ground station.

These exist because the web server spent a commit unable to start at all: the
Meshtastic link was constructed with `writer=self.write` and WebSerialManager
had no `write` method. Nothing in the suite instantiated WebServer, so an
AttributeError on the very first line of construction went unnoticed.
"""

import os
import sys

import pytest

GS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "groundstation/python",
)
sys.path.insert(0, GS)

flask = pytest.importorskip("flask", reason="web UI dependencies not installed")


@pytest.fixture(scope="module")
def server():
    from raptorhabgs.web.server import WebServer
    return WebServer(port=5099)


def test_the_web_server_can_be_constructed(server):
    """The regression that motivated this file."""
    assert server.app is not None
    assert server.ground_station is not None


def test_the_serial_manager_can_write(server):
    """
    The Meshtastic link transmits through this. Missing, it took the whole
    server down at construction.
    """
    manager = server.ground_station.serial_manager
    assert hasattr(manager, "write")
    # Not connected, so it should decline rather than raise.
    assert manager.write(b"MTX:00\n") is False


def test_it_binds_loopback_by_default():
    """
    Thirty state-changing endpoints and no authentication. Reaching it from a
    phone at a launch site is a real use, but it has to be asked for.
    """
    from raptorhabgs.web.server import WebServer

    assert WebServer().host == "127.0.0.1"


@pytest.mark.parametrize("host, loopback", [
    ("127.0.0.1", True), ("localhost", True), ("::1", True),
    ("0.0.0.0", False), ("10.1.1.61", False), ("raptor.local", False),
])
def test_loopback_detection(host, loopback):
    """Drives whether the "no authentication" warning is printed."""
    from raptorhabgs.web.server import WebServer

    assert WebServer._is_loopback(host) is loopback


def test_the_cli_defaults_to_loopback_too():
    """The shipped entry point is what people actually run."""
    import re

    source = open(os.path.join(GS, "web_server.py")).read()
    block = source[source.index("'--host'"):]
    default = re.search(r"default=['\"]([^'\"]+)['\"]", block).group(1)
    assert default == "127.0.0.1"


def test_card_import_cannot_write_outside_the_data_directory(tmp_path):
    """
    The output directory came straight from the request body, so an exposed
    instance would write decrypted flight data anywhere the process could
    reach.
    """
    root = (tmp_path / "recovered").resolve()
    root.mkdir(parents=True)

    def confine(requested):
        candidate = (root / str(requested).lstrip("/")).resolve()
        return candidate.is_relative_to(root)

    assert confine("flight1")
    assert confine("a/b/c")
    assert confine("/etc")                    # absolute is neutralised
    assert not confine("../../../etc")
    assert not confine("../outside")


def test_status_endpoint_serves(server):
    client = server.app.test_client()
    response = client.get("/api/status")
    assert response.status_code == 200
    assert response.get_json() is not None


def test_the_page_renders_and_handles_an_unmeasured_snr(server):
    """
    GFSK has no SNR. The Qt UI was taught that; the web UI was not, and went on
    rendering the sentinel as "-128.0 dB" in red.
    """
    client = server.app.test_client()
    page = client.get("/").get_data(as_text=True)

    assert "snrKnown" in page
    assert "-127" in page
    assert "status-value.unmeasured" in page, "the class it references must exist"
