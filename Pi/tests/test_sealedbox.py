"""
Public-key encryption of flight recordings.

The property that matters: the payload can seal a file and cannot open it
again. That is what makes recovery by a stranger harmless, and it is why
symmetric encryption would be useless here -- a balloon must boot unattended,
so any key it could decrypt with would travel with it.
"""

import os

import pytest

from common.sealedbox import (
    SealedBoxError,
    format_key,
    generate_keypair,
    key_fingerprint,
    open_sealed,
    parse_public_key,
    public_key_from_private,
    seal,
    x25519,
)
from common.sealedwriter import SEALED_SUFFIX, SealedWriter


def test_x25519_matches_rfc7748_vectors():
    """Section 6.1. A wrong curve implementation fails silently, not loudly."""
    a_private = bytes.fromhex(
        "77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
    b_private = bytes.fromhex(
        "5dab087e624a8a4b79e17f8b83800ee66f3bb1292618b6fd1c2f8b27ff88e0eb")

    a_public = public_key_from_private(a_private)
    b_public = public_key_from_private(b_private)

    assert a_public.hex() == (
        "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a")
    assert b_public.hex() == (
        "de9edb7d7b7dc1b4d35b61c2ece435373f8343c85b78674dadfc7e146f882b4f")

    expected = "4a5d9d5ba4ce2de1728e3bf480350f25e07e21c947d19e3376f09b3c1e161742"
    assert x25519(a_private, b_public).hex() == expected
    assert x25519(b_private, a_public).hex() == expected


def test_keypairs_are_unique():
    assert len({generate_keypair()[0] for _ in range(20)}) == 20


def test_round_trip():
    private, public = generate_keypair()
    data = os.urandom(5000)
    assert open_sealed(seal(data, public), private) == data


@pytest.mark.parametrize("size", [0, 1, 15, 16, 17, 1000, 100_000])
def test_round_trip_at_every_size(size):
    private, public = generate_keypair()
    data = os.urandom(size)
    assert open_sealed(seal(data, public), private) == data


def test_the_public_key_alone_cannot_decrypt():
    """
    The whole point. A payload holding only the public key writes files it can
    never read back, so recovering the balloon reveals nothing.
    """
    private, public = generate_keypair()
    sealed = seal(b"flight imagery", public)

    for wrong in (public, bytes(32), os.urandom(32)):
        with pytest.raises(SealedBoxError):
            open_sealed(sealed, wrong)


def test_a_different_key_is_rejected():
    _, public = generate_keypair()
    other_private, _ = generate_keypair()
    with pytest.raises(SealedBoxError, match="authentication failed"):
        open_sealed(seal(b"data", public), other_private)


def test_tampering_is_detected():
    """Encrypt-then-MAC: a modified file is refused before it is decrypted."""
    private, public = generate_keypair()
    sealed = bytearray(seal(b"x" * 500, public))

    for index in (5, 40, 100, len(sealed) - 1):
        corrupted = bytearray(sealed)
        corrupted[index] ^= 0x01
        with pytest.raises(SealedBoxError):
            open_sealed(bytes(corrupted), private)


def test_truncation_is_detected():
    private, public = generate_keypair()
    with pytest.raises(SealedBoxError):
        open_sealed(seal(b"y" * 500, public)[:-10], private)


def test_each_file_uses_a_fresh_ephemeral_key():
    """Identical plaintext must not produce identical output."""
    _, public = generate_keypair()
    assert len({seal(b"identical", public) for _ in range(10)}) == 10


def test_overhead_is_small():
    _, public = generate_keypair()
    data = os.urandom(50_000)
    assert len(seal(data, public)) - len(data) < 100


def test_garbage_is_not_mistaken_for_a_sealed_box():
    private, _ = generate_keypair()
    for junk in (b"", b"short", os.urandom(200), b"\x00" * 200):
        with pytest.raises(SealedBoxError):
            open_sealed(junk, private)


def test_a_bad_public_key_length_is_refused():
    with pytest.raises(ValueError):
        seal(b"data", os.urandom(16))


def test_public_key_parses_from_base64_and_hex():
    _, public = generate_keypair()
    assert parse_public_key(format_key(public)) == public
    assert parse_public_key(public.hex()) == public
    assert parse_public_key("0x" + public.hex()) == public


def test_empty_key_means_encryption_off():
    assert parse_public_key("") is None
    assert parse_public_key("   ") is None


def test_a_malformed_key_raises_rather_than_disabling_silently():
    for bad in ("not a key", "abcd", format_key(os.urandom(16))):
        with pytest.raises(ValueError):
            parse_public_key(bad)


def test_fingerprints_identify_without_revealing():
    _, public = generate_keypair()
    fingerprint = key_fingerprint(public)
    assert format_key(public) not in fingerprint
    assert key_fingerprint(public) == fingerprint


def test_writer_is_transparent_when_no_key_is_configured(tmp_path):
    writer = SealedWriter(public_key_text="")
    path = writer.write(str(tmp_path / "image.webp"), b"raw bytes")

    assert not writer.active
    assert not path.endswith(SEALED_SUFFIX)
    assert open(path, "rb").read() == b"raw bytes"


def test_writer_seals_and_renames_when_a_key_is_configured(tmp_path):
    private, public = generate_keypair()
    writer = SealedWriter(public_key_text=format_key(public))
    data = b"\x52\x49\x46\x46 webp payload"

    path = writer.write(str(tmp_path / "image.webp"), data)

    assert writer.active
    assert path.endswith(SEALED_SUFFIX)
    assert data not in open(path, "rb").read(), "plaintext must not be on disk"
    assert open_sealed(open(path, "rb").read(), private) == data


def test_a_malformed_key_does_not_silently_disable_encryption(caplog):
    """
    The operator asked for encryption. Falling back quietly would leave them
    believing recordings were protected when they were not.
    """
    writer = SealedWriter(public_key_text="this is not a key")
    assert not writer.active
    assert any("invalid" in r.message.lower() for r in caplog.records)


def test_a_disabled_writer_ignores_its_key():
    _, public = generate_keypair()
    assert not SealedWriter(public_key_text=format_key(public), enabled=False).active


def _read_records(raw, private):
    out, offset = [], 0
    while offset + 4 <= len(raw):
        length = int.from_bytes(raw[offset:offset + 4], "big")
        offset += 4
        if offset + length > len(raw):
            break
        out.append(open_sealed(raw[offset:offset + length], private).decode())
        offset += length
    return out


def test_log_records_are_sealed_individually(tmp_path):
    """
    A balloon can lose power mid-write. One growing box would be undecryptable
    in its entirety; a record stream loses only the tail.
    """
    private, public = generate_keypair()
    writer = SealedWriter(public_key_text=format_key(public))
    path = str(tmp_path / "telemetry.csv")

    rows = [f"row {i},{i * 1.5}\n" for i in range(20)]
    for row in rows:
        target = writer.append_line(path, row)

    assert _read_records(open(target, "rb").read(), private) == rows


def test_a_truncated_log_still_yields_everything_before_the_break(tmp_path):
    private, public = generate_keypair()
    writer = SealedWriter(public_key_text=format_key(public))
    path = str(tmp_path / "telemetry.csv")

    for i in range(10):
        target = writer.append_line(path, f"row {i}\n")

    recovered = _read_records(open(target, "rb").read()[:-25], private)
    assert len(recovered) >= 8, "most rows should survive a truncated tail"
    assert recovered[0] == "row 0\n"


def test_a_sealing_failure_falls_back_to_plaintext(tmp_path, monkeypatch, caplog):
    """Losing a flight's imagery to a crypto bug is worse than storing it."""
    _, public = generate_keypair()
    writer = SealedWriter(public_key_text=format_key(public))

    monkeypatch.setattr("common.sealedwriter.seal",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))

    path = writer.write(str(tmp_path / "image.webp"), b"important")
    assert open(path, "rb").read() == b"important"
    assert any("unencrypted" in r.message for r in caplog.records)
