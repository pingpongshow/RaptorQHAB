"""
Encrypt files the payload writes so a finder cannot read them.

The problem this solves: the SD card is not encrypted and on an unattended
balloon it cannot usefully be, because any key the payload can use to boot and
run travels with it. Symmetric encryption is therefore no help -- whoever
holds the card holds the key.

Public-key encryption is different. The payload carries only a *public* key.
It can seal an image or a log, and it cannot open it again. The private half
never leaves your Mac. A finder gets ciphertext and nothing that decrypts it.

Scheme (X25519 + HKDF-SHA256 + AES-256-CTR + HMAC-SHA256):

    1. Generate a fresh ephemeral X25519 keypair per file.
    2. Shared secret = X25519(ephemeral_private, recipient_public).
    3. HKDF-SHA256 over that, salted with both public keys, yields a 32-byte
       encryption key and a 32-byte MAC key.
    4. AES-256-CTR encrypts the payload; HMAC-SHA256 authenticates the header
       and ciphertext.
    5. The ephemeral private key is discarded. Only the ephemeral public key
       is written to the file.

Forward secrecy falls out of step 5: each file has its own ephemeral key, and
the payload cannot reconstruct any of them, so recovering the balloon reveals
nothing about files already written.

Encrypt-then-MAC, so a tampered file is rejected before anything decrypts it.

No external dependencies. X25519 is implemented here in about eighty lines
because pulling in `cryptography` for one curve operation is not a trade worth
making on a Pi Zero, and the primitive is small and completely specified
(RFC 7748).
"""

import hashlib
import hmac
import os
import struct
from typing import Optional, Tuple

from .meshtastic.crypto import aes_ctr

MAGIC = b"RHSB"          # RaptorHab Sealed Box
FORMAT_VERSION = 1
HEADER_STRUCT = ">4sBB32sI"   # magic, version, flags, ephemeral pubkey, length
HEADER_SIZE = struct.calcsize(HEADER_STRUCT)
MAC_SIZE = 32


# --------------------------------------------------------------------------
# X25519 (RFC 7748)
# --------------------------------------------------------------------------

_P = 2 ** 255 - 19
_A24 = 121665


def _decode_scalar(data: bytes) -> int:
    """Clamp a 32-byte scalar per RFC 7748 section 5."""
    k = bytearray(data)
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    return int.from_bytes(k, "little")


def _decode_u(data: bytes) -> int:
    u = bytearray(data)
    u[31] &= 127           # mask the unused high bit
    return int.from_bytes(u, "little") % _P


def _encode_u(u: int) -> bytes:
    return (u % _P).to_bytes(32, "little")


def x25519(scalar: bytes, u_coordinate: bytes) -> bytes:
    """
    The X25519 function: a Montgomery ladder in constant number of steps.

    Not constant-*time* -- Python integers cannot be -- which is acceptable
    here. The secret scalar is an ephemeral key used once, on a device that is
    physically in the attacker's hands only after the fact, and the ladder is
    never run on the long-term private key at all: that stays on your Mac.
    """
    k = _decode_scalar(scalar)
    x1 = _decode_u(u_coordinate)

    x2, z2 = 1, 0
    x3, z3 = x1, 1
    swap = 0

    for t in range(254, -1, -1):
        k_t = (k >> t) & 1
        swap ^= k_t
        if swap:
            x2, x3 = x3, x2
            z2, z3 = z3, z2
        swap = k_t

        a = (x2 + z2) % _P
        aa = (a * a) % _P
        b = (x2 - z2) % _P
        bb = (b * b) % _P
        e = (aa - bb) % _P
        c = (x3 + z3) % _P
        d = (x3 - z3) % _P
        da = (d * a) % _P
        cb = (c * b) % _P

        x3 = pow((da + cb) % _P, 2, _P)
        z3 = (x1 * pow((da - cb) % _P, 2, _P)) % _P
        x2 = (aa * bb) % _P
        z2 = (e * ((aa + _A24 * e) % _P)) % _P

    if swap:
        x2, x3 = x3, x2
        z2, z3 = z3, z2

    return _encode_u((x2 * pow(z2, _P - 2, _P)) % _P)


_BASE_POINT = b"\x09" + b"\x00" * 31


def generate_keypair() -> Tuple[bytes, bytes]:
    """Return (private_key, public_key), 32 bytes each."""
    private = os.urandom(32)
    return private, x25519(private, _BASE_POINT)


def public_key_from_private(private: bytes) -> bytes:
    if len(private) != 32:
        raise ValueError(f"private key must be 32 bytes, got {len(private)}")
    return x25519(private, _BASE_POINT)


# --------------------------------------------------------------------------
# Key derivation
# --------------------------------------------------------------------------


def _hkdf(shared: bytes, salt: bytes, info: bytes, length: int) -> bytes:
    """HKDF-SHA256 (RFC 5869)."""
    prk = hmac.new(salt, shared, hashlib.sha256).digest()
    out = b""
    block = b""
    counter = 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def _derive(shared: bytes, ephemeral_public: bytes, recipient_public: bytes):
    """Encryption and MAC keys, bound to both public keys."""
    material = _hkdf(
        shared,
        salt=ephemeral_public + recipient_public,
        info=b"raptorhab-sealed-box-v1",
        length=64,
    )
    return material[:32], material[32:]


class SealedBoxError(ValueError):
    """Raised when a sealed file is malformed or fails authentication."""


# --------------------------------------------------------------------------
# Seal / open
# --------------------------------------------------------------------------


def seal(plaintext: bytes, recipient_public: bytes) -> bytes:
    """
    Encrypt for the holder of the matching private key.

    The caller cannot reverse this: the ephemeral private key is dropped
    before returning, which is exactly the property that makes it safe to run
    on a payload that may be recovered by someone else.
    """
    if len(recipient_public) != 32:
        raise ValueError(f"public key must be 32 bytes, got {len(recipient_public)}")

    ephemeral_private, ephemeral_public = generate_keypair()
    shared = x25519(ephemeral_private, recipient_public)

    # A shared secret of zero means a degenerate (small-order) public key.
    if shared == bytes(32):
        raise ValueError("recipient public key is not usable")

    encryption_key, mac_key = _derive(shared, ephemeral_public, recipient_public)

    # Drop the ephemeral private key. Nothing else in this process holds it.
    del ephemeral_private

    nonce = bytes(16)  # safe: the key is unique per file
    ciphertext = aes_ctr(encryption_key, nonce, plaintext)

    header = struct.pack(
        HEADER_STRUCT, MAGIC, FORMAT_VERSION, 0, ephemeral_public, len(plaintext)
    )
    tag = hmac.new(mac_key, header + ciphertext, hashlib.sha256).digest()

    return header + ciphertext + tag


def open_sealed(sealed: bytes, recipient_private: bytes) -> bytes:
    """
    Decrypt with the private key. Raises SealedBoxError if it does not
    authenticate, which is checked before any plaintext is produced.
    """
    if len(sealed) < HEADER_SIZE + MAC_SIZE:
        raise SealedBoxError("file is too short to be a sealed box")

    magic, version, _flags, ephemeral_public, length = struct.unpack(
        HEADER_STRUCT, sealed[:HEADER_SIZE]
    )
    if magic != MAGIC:
        raise SealedBoxError("not a sealed box")
    if version != FORMAT_VERSION:
        raise SealedBoxError(f"unsupported sealed box version {version}")

    header = sealed[:HEADER_SIZE]
    ciphertext = sealed[HEADER_SIZE:-MAC_SIZE]
    tag = sealed[-MAC_SIZE:]

    if len(ciphertext) != length:
        raise SealedBoxError("declared length does not match the ciphertext")

    recipient_public = public_key_from_private(recipient_private)
    shared = x25519(recipient_private, ephemeral_public)
    if shared == bytes(32):
        raise SealedBoxError("degenerate shared secret")

    encryption_key, mac_key = _derive(shared, ephemeral_public, recipient_public)

    expected = hmac.new(mac_key, header + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, tag):
        raise SealedBoxError(
            "authentication failed: wrong key, or the file has been altered"
        )

    return aes_ctr(encryption_key, bytes(16), ciphertext)


# --------------------------------------------------------------------------
# Key files
# --------------------------------------------------------------------------


def parse_public_key(text: str) -> Optional[bytes]:
    """
    Parse a public key from configuration: base64 or hex, or empty for off.

    Returns None when no key is configured, which callers must treat as
    "encryption disabled" rather than an error -- it is the default.
    """
    import base64
    import binascii

    text = (text or "").strip()
    if not text:
        return None

    stripped = text.removeprefix("0x").replace(":", "").replace(" ", "")
    if len(stripped) == 64:
        try:
            return bytes.fromhex(stripped)
        except ValueError:
            pass

    try:
        key = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"public key is neither 32-byte hex nor base64: {e}") from e

    if len(key) != 32:
        raise ValueError(f"public key must decode to 32 bytes, got {len(key)}")
    return key


def format_key(key: bytes) -> str:
    import base64
    return base64.b64encode(key).decode("ascii")


def key_fingerprint(key: bytes) -> str:
    """Short identifier, so a config UI can confirm the key without showing it."""
    if not key:
        return "none"
    return hashlib.sha256(key).hexdigest()[:16]
