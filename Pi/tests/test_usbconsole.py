"""
The USB link: frame protocol, configuration API, and the shell channel.

The framing tests matter because a serial line has no message boundaries: the
reader has to survive connecting mid-transmission, byte-at-a-time delivery,
and console output that happens to contain the magic sequence.
"""

import json
import os
import pty
import threading
import time

import pytest

from airborne.config import Config
from airborne.usbconsole import (
    ConsoleSecurityError,
    ControlHandler,
    ShellSession,
    UsbConsoleService,
)
from common.linkproto import (
    Channel,
    FrameDecoder,
    FrameError,
    MAX_PAYLOAD,
    encode_frame,
)


# --- framing --------------------------------------------------------------


def test_frame_round_trip():
    frames = FrameDecoder().feed(encode_frame(Channel.CONTROL, b'{"a":1}'))
    assert frames == [(Channel.CONTROL, b'{"a":1}')]


def test_empty_payload_round_trips():
    assert FrameDecoder().feed(encode_frame(Channel.CONSOLE, b"")) == [
        (Channel.CONSOLE, b"")
    ]


def test_multiple_frames_in_one_read():
    data = (
        encode_frame(Channel.CONTROL, b"one")
        + encode_frame(Channel.CONSOLE, b"two")
        + encode_frame(Channel.EVENT, b"three")
    )
    assert FrameDecoder().feed(data) == [
        (Channel.CONTROL, b"one"),
        (Channel.CONSOLE, b"two"),
        (Channel.EVENT, b"three"),
    ]


def test_a_frame_split_across_reads_reassembles():
    """Serial delivers whatever the driver happens to have buffered."""
    frame = encode_frame(Channel.CONTROL, b"x" * 500)
    decoder = FrameDecoder()

    assert decoder.feed(frame[:10]) == []
    assert decoder.feed(frame[10:200]) == []
    assert decoder.feed(frame[200:]) == [(Channel.CONTROL, b"x" * 500)]


def test_byte_at_a_time_delivery_still_works():
    frame = encode_frame(Channel.CONSOLE, b"hello")
    decoder = FrameDecoder()

    collected = []
    for byte in frame:
        collected.extend(decoder.feed(bytes([byte])))

    assert collected == [(Channel.CONSOLE, b"hello")]


def test_leading_garbage_is_skipped():
    """Connecting mid-stream lands in the middle of someone else's frame."""
    data = b"\xde\xad\xbe\xef garbage " + encode_frame(Channel.CONTROL, b"ok")
    decoder = FrameDecoder()

    assert decoder.feed(data) == [(Channel.CONTROL, b"ok")]
    assert decoder.resyncs > 0


def test_a_truncated_frame_does_not_block_the_next_one():
    partial = encode_frame(Channel.CONTROL, b"lost")[:-6]
    data = partial + encode_frame(Channel.CONTROL, b"found")

    frames = FrameDecoder().feed(data)
    assert (Channel.CONTROL, b"found") in frames


def test_a_corrupt_crc_is_rejected():
    frame = bytearray(encode_frame(Channel.CONTROL, b"payload"))
    frame[-1] ^= 0xFF

    decoder = FrameDecoder()
    assert decoder.feed(bytes(frame)) == []
    assert decoder.crc_errors > 0


def test_console_data_containing_the_magic_does_not_break_framing():
    """
    Terminal output is arbitrary bytes and will eventually contain "RH". The
    CRC is what stops that being mistaken for a frame header.
    """
    payload = b"RH" * 100 + b"\x00\xff" + b"RH"
    frames = FrameDecoder().feed(encode_frame(Channel.CONSOLE, payload))
    assert frames == [(Channel.CONSOLE, payload)]


def test_an_absurd_length_field_is_rejected_not_allocated():
    """A corrupt length must not make the reader try to allocate a gigabyte."""
    import struct

    bogus = b"RH" + struct.pack(">BBI", 0, 0, 0xFFFFFFFF) + b"\x00" * 4
    decoder = FrameDecoder()
    assert decoder.feed(bogus) == []
    assert decoder.buffered_bytes < 100


def test_oversized_payloads_are_refused_at_encode_time():
    with pytest.raises(FrameError, match="exceeds"):
        encode_frame(Channel.CONTROL, b"\x00" * (MAX_PAYLOAD + 1))


def test_pure_noise_does_not_grow_the_buffer_without_bound():
    decoder = FrameDecoder()
    for _ in range(200):
        decoder.feed(os.urandom(4096))
    assert decoder.buffered_bytes < 4 * MAX_PAYLOAD


def test_a_large_payload_survives():
    """A config schema is tens of kilobytes."""
    payload = os.urandom(200_000)
    assert FrameDecoder().feed(encode_frame(Channel.CONTROL, payload)) == [
        (Channel.CONTROL, payload)
    ]


# --- the control API ------------------------------------------------------


@pytest.fixture
def handler(tmp_path):
    config = Config()
    config.config_path = str(tmp_path / "airborne.json")
    config.image_storage_path = str(tmp_path / "images")
    config.log_path = str(tmp_path / "logs")
    config.ensure_directories()
    return ControlHandler(config)


def call(handler, method, **params):
    return handler.handle({"id": 1, "method": method, "params": params})


def test_hello_identifies_the_payload(handler):
    response = call(handler, "hello")
    assert response["ok"]
    assert response["result"]["callsign"] == "RPHAB1"
    assert response["result"]["protocol_version"] == 1


def test_the_request_id_is_echoed(handler):
    assert handler.handle({"id": "abc", "method": "hello"})["id"] == "abc"


def test_an_unknown_method_is_an_error_not_a_crash(handler):
    response = call(handler, "definitely_not_a_method")
    assert not response["ok"]
    assert "unknown method" in response["error"]


def test_get_schema_returns_every_parameter(handler):
    from airborne.params import PARAM_SPECS

    result = call(handler, "get_schema")["result"]
    assert len(result["parameters"]) == len(PARAM_SPECS)
    assert "categories" in result


def test_get_config_returns_current_values(handler):
    result = call(handler, "get_config")["result"]
    assert result["values"]["callsign"] == "RPHAB1"


def test_set_config_applies_and_persists(handler):
    response = call(handler, "set_config", values={"callsign": "KX0ABC"})
    assert response["ok"]
    assert response["result"]["applied"] == ["callsign"]
    assert response["result"]["saved"]

    reloaded = Config.load(path=handler.config.config_path)
    assert reloaded.callsign == "KX0ABC"


def test_set_config_rejects_an_invalid_value(handler):
    response = call(handler, "set_config", values={"radio_power_dbm": 500})
    assert response["ok"], "the call succeeds; the update inside it does not"
    assert not response["result"]["ok"]
    assert "radio_power_dbm" in response["result"]["rejected"]
    assert handler.config.radio_power_dbm == 22


def test_set_config_is_all_or_nothing(handler):
    response = call(
        handler, "set_config",
        values={"callsign": "GOOD", "radio_power_dbm": 500},
    )
    assert not response["result"]["ok"]
    assert handler.config.callsign == "RPHAB1", "nothing may be applied"


def test_set_config_reports_restart_required(handler):
    result = call(handler, "set_config", values={"gps_baudrate": 115200})["result"]
    assert result["restart_required"] == ["gps_baudrate"]


def test_set_config_enforces_cross_field_rules(handler):
    """A 433 MHz region on an HF board must be refused here too."""
    result = call(handler, "set_config", values={"meshtastic_region": "EU_433"})["result"]
    assert not result["ok"]
    assert "_cross_field" in result["rejected"]


def test_reset_config_restores_defaults(handler):
    call(handler, "set_config", values={"callsign": "KX0ABC", "webp_quality": 40})
    result = call(handler, "reset_config", names=["callsign"])["result"]

    assert result["ok"]
    assert handler.config.callsign == "RPHAB1"
    assert handler.config.webp_quality == 40, "only the named parameter resets"


def test_reset_config_rejects_unknown_names(handler):
    assert not call(handler, "reset_config", names=["nope"])["ok"]


# --- secrets --------------------------------------------------------------


def test_secret_values_never_cross_the_link(handler):
    """
    A channel key can be set and its fingerprint read, but the value itself is
    never transmitted back.
    """
    from common.meshtastic.crypto import generate_psk

    key = generate_psk(32)
    call(handler, "set_config", values={"meshtastic_private_psk": key.hex()})

    result = call(handler, "get_config")["result"]
    assert result["values"]["meshtastic_private_psk"] is None
    assert key.hex() not in json.dumps(result)


def test_secret_fingerprints_are_reported(handler):
    result = call(handler, "get_config")["result"]
    assert "meshtastic_channel_psk" in result["secrets"]
    assert "128-bit" in result["secrets"]["meshtastic_channel_psk"]


def test_a_fingerprint_changes_when_the_key_does(handler):
    from common.meshtastic.crypto import generate_psk

    call(handler, "set_config",
         values={"meshtastic_private_psk": generate_psk(32).hex()})
    first = call(handler, "get_config")["result"]["secrets"]["meshtastic_private_psk"]

    call(handler, "set_config",
         values={"meshtastic_private_psk": generate_psk(32).hex()})
    second = call(handler, "get_config")["result"]["secrets"]["meshtastic_private_psk"]

    assert first != second


def test_generate_psk_returns_a_usable_key(handler):
    import base64

    result = call(handler, "generate_psk", length=32)["result"]
    assert len(base64.b64decode(result["psk_base64"])) == 32
    assert "256-bit" in result["fingerprint"]


# --- images ---------------------------------------------------------------


def test_list_images_is_empty_on_a_fresh_payload(handler):
    assert call(handler, "list_images")["result"]["images"] == []


def test_list_images_reports_captures(handler):
    from pathlib import Path

    directory = Path(handler.config.image_storage_path)
    (directory / "img_001.webp").write_bytes(b"\x00" * 100)
    (directory / "img_002.webp").write_bytes(b"\x00" * 200)

    images = call(handler, "list_images")["result"]["images"]
    assert len(images) == 2
    assert {i["name"] for i in images} == {"img_001.webp", "img_002.webp"}


def test_fetch_image_returns_its_bytes(handler):
    import base64
    from pathlib import Path

    payload = b"\x52\x49\x46\x46fake webp"
    (Path(handler.config.image_storage_path) / "img.webp").write_bytes(payload)

    result = call(handler, "fetch_image", name="img.webp")["result"]
    assert base64.b64decode(result["data_base64"]) == payload


def test_fetch_image_refuses_to_escape_the_image_directory(handler):
    """A crafted name must not read arbitrary files off the payload."""
    for name in ("../../../etc/passwd", "/etc/passwd", "..%2f..%2fetc%2fpasswd"):
        response = call(handler, "fetch_image", name=name)
        assert not response["ok"], f"{name!r} was not refused"


def test_fetch_image_refuses_a_symlink_out_of_the_directory(handler, tmp_path):
    from pathlib import Path

    secret = tmp_path / "secret.txt"
    secret.write_text("private")
    link = Path(handler.config.image_storage_path) / "escape.webp"
    link.symlink_to(secret)

    assert not call(handler, "fetch_image", name="escape.webp")["ok"]


def test_fetch_image_reports_a_missing_file(handler):
    assert not call(handler, "fetch_image", name="nope.webp")["ok"]


# --- the shell must stay on the cable -------------------------------------


def test_the_service_refuses_a_non_gadget_device(tmp_path):
    """
    Regression guard: this service offers a login shell. Binding it to a
    radio-facing serial port would put a shell on the air.
    """
    with pytest.raises(ConsoleSecurityError, match="refusing to serve"):
        UsbConsoleService(device="/dev/ttyAMA0", config=Config())

    with pytest.raises(ConsoleSecurityError):
        UsbConsoleService(device="/dev/serial0", config=Config())


def test_the_gadget_tty_is_accepted():
    service = UsbConsoleService(device="/dev/ttyGS0", config=Config())
    assert service.device == "/dev/ttyGS0"


def test_the_check_can_be_bypassed_only_explicitly():
    service = UsbConsoleService(
        device="/dev/ttyAMA0", config=Config(), allow_any_device=True
    )
    assert service.device == "/dev/ttyAMA0"


# --- shell session --------------------------------------------------------


def test_a_shell_session_runs_a_command():
    received = bytearray()
    session = ShellSession(on_output=received.extend, shell="/bin/sh")
    session.start()
    try:
        session.write(b"echo raptorhab_marker\n")
        deadline = time.time() + 5.0
        while time.time() < deadline and b"raptorhab_marker" not in received:
            time.sleep(0.05)
        assert b"raptorhab_marker" in received
    finally:
        session.stop()

    assert not session.running


def test_stopping_a_shell_reaps_the_process():
    session = ShellSession(on_output=lambda _: None, shell="/bin/sh")
    session.start()
    pid = session._pid
    session.stop()

    assert not session.running
    with pytest.raises(OSError):
        os.kill(pid, 0)


# --- end to end over a pty ------------------------------------------------


@pytest.fixture
def linked_service(tmp_path):
    """A service on one end of a pty, with the test holding the other."""
    primary, secondary = pty.openpty()

    config = Config()
    config.config_path = str(tmp_path / "airborne.json")
    config.image_storage_path = str(tmp_path / "images")
    config.log_path = str(tmp_path / "logs")
    config.ensure_directories()

    service = UsbConsoleService(
        device=os.ttyname(secondary), config=config, allow_any_device=True
    )
    thread = threading.Thread(target=service.run, daemon=True)
    thread.start()
    time.sleep(0.3)

    yield service, primary

    service.stop()
    thread.join(timeout=3.0)
    for fd in (primary, secondary):
        try:
            os.close(fd)
        except OSError:
            pass


def exchange(fd, request, timeout=5.0):
    """Send a request and read frames until a matching reply arrives."""
    os.write(fd, encode_frame(Channel.CONTROL, json.dumps(request).encode()))

    decoder = FrameDecoder()
    deadline = time.time() + timeout
    while time.time() < deadline:
        import select as _select

        readable, _, _ = _select.select([fd], [], [], 0.2)
        if not readable:
            continue
        for channel, payload in decoder.feed(os.read(fd, 65536)):
            if channel == Channel.CONTROL:
                message = json.loads(payload)
                if message.get("id") == request.get("id"):
                    return message
    raise AssertionError(f"no reply to {request.get('method')!r} within {timeout}s")


def test_end_to_end_hello(linked_service):
    _service, fd = linked_service
    response = exchange(fd, {"id": 1, "method": "hello"})
    assert response["ok"]
    assert response["result"]["callsign"] == "RPHAB1"


def test_end_to_end_config_round_trip(linked_service):
    _service, fd = linked_service

    assert exchange(fd, {
        "id": 2, "method": "set_config", "params": {"values": {"callsign": "KX0ABC"}}
    })["result"]["applied"] == ["callsign"]

    values = exchange(fd, {"id": 3, "method": "get_config"})["result"]["values"]
    assert values["callsign"] == "KX0ABC"


def test_end_to_end_schema_is_a_large_frame(linked_service):
    """Exercises the multi-read reassembly path with a real payload."""
    _service, fd = linked_service
    result = exchange(fd, {"id": 4, "method": "get_schema"}, timeout=10.0)["result"]
    assert len(result["parameters"]) > 50


def test_end_to_end_malformed_json_gets_an_error_not_a_hang(linked_service):
    _service, fd = linked_service
    os.write(fd, encode_frame(Channel.CONTROL, b"{not json"))

    # The service must still answer the next well-formed request.
    assert exchange(fd, {"id": 5, "method": "hello"})["ok"]
