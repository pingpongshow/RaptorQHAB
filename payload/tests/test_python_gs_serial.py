"""The Qt serial manager's configuration handshake.

configure_modem() used to assume success the moment the command was written:
a modem that refused the settings -- or was not a RaptorHAB modem at all --
was reported as configured. It now waits for the modem's verdict.
"""
import os
import sys
import threading
import time

import pytest

GS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "groundstation/python",
)
sys.path.insert(0, GS)

pytest.importorskip("PyQt6", reason="Qt UI dependencies not installed")
pytest.importorskip("serial", reason="pyserial not installed")

from raptorhabgs.core.serial_manager import SerialManager
from raptorhabgs.core.config import ModemConfig


class FakeSerial:
    def __init__(self):
        self.written = b""
    def write(self, data):
        self.written += data
        return len(data)
    def flush(self):
        pass


def manager_with_fake_serial():
    m = SerialManager()
    m.serial = FakeSerial()
    m.is_connected = True
    return m


def test_configure_waits_for_ok():
    m = manager_with_fake_serial()
    def answer():
        time.sleep(0.1)
        m._process_text_line("CFG_OK:915.0,96.0,50.0,234.3,32")
    threading.Thread(target=answer, daemon=True).start()
    assert m.configure_modem(ModemConfig(), timeout=2.0) is True
    assert m.is_configured is True
    assert m.serial.written.startswith(b"CFG:")


def test_configure_reports_refusal():
    m = manager_with_fake_serial()
    errors = []
    m.error.connect(errors.append)
    def answer():
        time.sleep(0.1)
        m._process_text_line("CFG_ERR:Radio refused the settings")
    threading.Thread(target=answer, daemon=True).start()
    assert m.configure_modem(ModemConfig(), timeout=2.0) is False
    assert m.is_configured is False


def test_configure_times_out_when_nothing_answers():
    m = manager_with_fake_serial()
    start = time.monotonic()
    assert m.configure_modem(ModemConfig(), timeout=0.3) is False
    assert time.monotonic() - start < 2.0


def test_binary_debris_does_not_reach_the_line_handler():
    """A 0x0A inside a binary frame splits as a 'line' in the text path.
    Frame debris must never match a prefix or corrupt the config handshake."""
    m = manager_with_fake_serial()
    seen = []
    m._process_text_line = lambda line: seen.append(line)
    m._extract_text_lines(b"\x7e\x00\x30CFG_OK:junk\x81\x99\n[STATS] Total:5\n")
    assert seen == ["[STATS] Total:5"]
