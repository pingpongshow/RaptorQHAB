"""
Meshtastic channel encryption: AES-256-CTR.

Meshtastic encrypts the payload of every packet with AES in counter mode,
keyed by the channel's pre-shared key. The counter block is:

    bytes  0..7   packet_id, little-endian uint64
    bytes  8..11  sender node id, little-endian uint32
    bytes 12..15  block counter, little-endian uint32, starting at zero

Per the no-dependencies preference this ships a self-contained AES
implementation, validated against the FIPS-197 and SP 800-38A test vectors.
If the `cryptography` package happens to be installed it is used instead,
purely for speed -- the results are identical either way.

A note on what this protects. Meshtastic's default channel key is published in
the source of every client, so "encrypted" on the default channel means
obfuscated, not private: anyone can read it. Only a channel with a key you
generated yourself is actually confidential. The pure-Python path here is also
not written to resist timing side-channels; that is irrelevant for a balloon
broadcasting its own position, but it would matter if this key material were
ever reused for something that needs real secrecy.
"""

import hashlib
import logging
import os
import struct
from typing import List, Optional

logger = logging.getLogger(__name__)

# Meshtastic's default channel key ("AQ==" in the client UI) is the single
# byte 0x01 expanded against a fixed base key. Published in the firmware, so
# traffic on the default channel is readable by anyone.
DEFAULT_PSK = bytes(
    [
        0xD4, 0xF1, 0xBB, 0x3A, 0x20, 0x29, 0x07, 0x59,
        0xF0, 0xBC, 0xFF, 0xAB, 0xCF, 0x4E, 0x69, 0x01,
    ]
)

AES_BLOCK_SIZE = 16


# --------------------------------------------------------------------------
# AES core
# --------------------------------------------------------------------------

_SBOX = bytes.fromhex(
    "637c777bf26b6fc53001672bfed7ab76"
    "ca82c97dfa5947f0add4a2af9ca472c0"
    "b7fd9326363ff7cc34a5e5f171d83115"
    "04c723c31896059a071280e2eb27b275"
    "09832c1a1b6e5aa0523bd6b329e32f84"
    "53d100ed20fcb15b6acbbe394a4c58cf"
    "d0efaafb434d338545f9027f503c9fa8"
    "51a3408f929d38f5bcb6da2110fff3d2"
    "cd0c13ec5f974417c4a77e3d645d1973"
    "60814fdc222a908846eeb814de5e0bdb"
    "e0323a0a4906245cc2d3ac629195e479"
    "e7c8376d8dd54ea96c56f4ea657aae08"
    "ba78252e1ca6b4c6e8dd741f4bbd8b8a"
    "703eb5664803f60e613557b986c11d9e"
    "e1f8981169d98e949b1e87e9ce5528df"
    "8ca1890dbfe6426841992d0fb054bb16"
)

_RCON = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36,
         0x6C, 0xD8, 0xAB, 0x4D)


def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _mul(a: int, b: int) -> int:
    """Multiply in GF(2^8)."""
    result = 0
    while b:
        if b & 1:
            result ^= a
        a = _xtime(a)
        b >>= 1
    return result


class AES:
    """
    AES block cipher, encryption direction only.

    Counter mode never needs the inverse cipher: both encryption and
    decryption XOR against an encrypted counter block.
    """

    def __init__(self, key: bytes):
        if len(key) not in (16, 24, 32):
            raise ValueError(
                f"AES key must be 16, 24, or 32 bytes; got {len(key)}"
            )
        self._rounds = {16: 10, 24: 12, 32: 14}[len(key)]
        self._round_keys = self._expand_key(key)

    def _expand_key(self, key: bytes) -> List[List[int]]:
        nk = len(key) // 4
        total_words = 4 * (self._rounds + 1)
        words = [list(key[4 * i : 4 * i + 4]) for i in range(nk)]

        for i in range(nk, total_words):
            temp = list(words[i - 1])
            if i % nk == 0:
                temp = temp[1:] + temp[:1]                      # RotWord
                temp = [_SBOX[b] for b in temp]                 # SubWord
                temp[0] ^= _RCON[i // nk - 1]
            elif nk > 6 and i % nk == 4:
                temp = [_SBOX[b] for b in temp]
            words.append([words[i - nk][j] ^ temp[j] for j in range(4)])

        return words

    def encrypt_block(self, block: bytes) -> bytes:
        if len(block) != AES_BLOCK_SIZE:
            raise ValueError(f"AES block must be 16 bytes; got {len(block)}")

        state = list(block)
        self._add_round_key(state, 0)

        for round_index in range(1, self._rounds):
            self._sub_bytes(state)
            self._shift_rows(state)
            self._mix_columns(state)
            self._add_round_key(state, round_index)

        self._sub_bytes(state)
        self._shift_rows(state)
        self._add_round_key(state, self._rounds)

        return bytes(state)

    def _add_round_key(self, state: List[int], round_index: int) -> None:
        base = round_index * 4
        for column in range(4):
            word = self._round_keys[base + column]
            for row in range(4):
                state[4 * column + row] ^= word[row]

    @staticmethod
    def _sub_bytes(state: List[int]) -> None:
        for i in range(16):
            state[i] = _SBOX[state[i]]

    @staticmethod
    def _shift_rows(state: List[int]) -> None:
        # State is column-major: state[4*col + row].
        for row in range(1, 4):
            row_bytes = [state[4 * col + row] for col in range(4)]
            row_bytes = row_bytes[row:] + row_bytes[:row]
            for col in range(4):
                state[4 * col + row] = row_bytes[col]

    @staticmethod
    def _mix_columns(state: List[int]) -> None:
        for col in range(4):
            i = 4 * col
            a0, a1, a2, a3 = state[i], state[i + 1], state[i + 2], state[i + 3]
            state[i]     = _mul(a0, 2) ^ _mul(a1, 3) ^ a2 ^ a3
            state[i + 1] = a0 ^ _mul(a1, 2) ^ _mul(a2, 3) ^ a3
            state[i + 2] = a0 ^ a1 ^ _mul(a2, 2) ^ _mul(a3, 3)
            state[i + 3] = _mul(a0, 3) ^ a1 ^ a2 ^ _mul(a3, 2)


# --------------------------------------------------------------------------
# Optional acceleration
# --------------------------------------------------------------------------

try:  # pragma: no cover - depends on the host
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    _CRYPTOGRAPHY_AVAILABLE = True
except ImportError:  # pragma: no cover
    _CRYPTOGRAPHY_AVAILABLE = False


def cryptography_available() -> bool:
    """Whether the optional accelerated backend is in use."""
    return _CRYPTOGRAPHY_AVAILABLE


# --------------------------------------------------------------------------
# Counter mode
# --------------------------------------------------------------------------


def build_nonce(packet_id: int, sender_node_id: int) -> bytes:
    """
    Build the 16-byte AES-CTR initial counter block for a Meshtastic packet.

    Layout: packet_id (uint64 LE) || sender (uint32 LE) || block counter (0).
    """
    if not 0 <= packet_id <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError(f"packet_id {packet_id} out of range")
    if not 0 <= sender_node_id <= 0xFFFFFFFF:
        raise ValueError(f"sender_node_id {sender_node_id} out of range")

    return struct.pack("<QII", packet_id, sender_node_id, 0)


def _increment_counter(counter: bytearray) -> None:
    """
    Increment the counter block as a big-endian 128-bit integer.

    This must match mbedtls_aes_crypt_ctr, which is what Meshtastic firmware
    uses: it carries from the last byte backwards. Incrementing the little-
    endian uint32 at offset 12 instead would produce the correct keystream for
    the first block and garbage for every block after it -- invisible for a
    payload under 16 bytes, and broken for every real beacon.
    """
    for i in range(len(counter) - 1, -1, -1):
        counter[i] = (counter[i] + 1) & 0xFF
        if counter[i]:
            return


def aes_ctr(key: bytes, nonce: bytes, data: bytes) -> bytes:
    """
    AES-CTR transform. Symmetric: the same call encrypts and decrypts.

    Args:
        key: 16, 24, or 32 byte key.
        nonce: 16-byte initial counter block from build_nonce().
        data: Plaintext or ciphertext of any length.
    """
    if len(nonce) != AES_BLOCK_SIZE:
        raise ValueError(f"nonce must be 16 bytes; got {len(nonce)}")
    if not data:
        return b""

    if _CRYPTOGRAPHY_AVAILABLE:  # pragma: no cover - depends on the host
        cipher = Cipher(algorithms.AES(key), modes.CTR(nonce))
        encryptor = cipher.encryptor()
        return encryptor.update(data) + encryptor.finalize()

    cipher = AES(key)
    counter = bytearray(nonce)
    out = bytearray(len(data))

    for offset in range(0, len(data), AES_BLOCK_SIZE):
        keystream = cipher.encrypt_block(bytes(counter))
        chunk = data[offset : offset + AES_BLOCK_SIZE]
        for i, byte in enumerate(chunk):
            out[offset + i] = byte ^ keystream[i]
        _increment_counter(counter)

    return bytes(out)


def encrypt_payload(
    key: bytes, packet_id: int, sender_node_id: int, plaintext: bytes
) -> bytes:
    """Encrypt a Meshtastic packet payload."""
    return aes_ctr(key, build_nonce(packet_id, sender_node_id), plaintext)


def decrypt_payload(
    key: bytes, packet_id: int, sender_node_id: int, ciphertext: bytes
) -> bytes:
    """Decrypt a Meshtastic packet payload. Identical operation to encrypt."""
    return aes_ctr(key, build_nonce(packet_id, sender_node_id), ciphertext)


# --------------------------------------------------------------------------
# Channel keys
# --------------------------------------------------------------------------


def expand_psk(psk: bytes) -> bytes:
    """
    Expand a Meshtastic PSK into a usable AES key.

    Meshtastic's shorthand: a single-byte PSK selects one of the well-known
    default keys by adding (byte - 1) to the last byte of DEFAULT_PSK. A zero
    length PSK means the channel is unencrypted. Anything else is used as-is
    and must be a valid AES key length.
    """
    if not psk:
        return b""

    if len(psk) == 1:
        index = psk[0]
        if index == 0:
            return b""  # explicitly no encryption
        key = bytearray(DEFAULT_PSK)
        key[-1] = (key[-1] + index - 1) & 0xFF
        return bytes(key)

    if len(psk) in (16, 24, 32):
        return bytes(psk)

    raise ValueError(
        f"PSK must be empty, 1 byte (default-key selector), or 16/24/32 bytes; "
        f"got {len(psk)}"
    )


def channel_hash(channel_name: str, key: bytes) -> int:
    """
    The single-byte channel hash carried in the Meshtastic packet header.

    Receivers use it to pick which channel key to try, so it must match the
    firmware's XOR-fold of the name and key bytes exactly.
    """
    value = 0
    for byte in channel_name.encode("utf-8"):
        value ^= byte
    for byte in key:
        value ^= byte
    return value & 0xFF


def parse_psk(text: str) -> bytes:
    """
    Parse a PSK from user configuration.

    Accepts base64 (as shown in the Meshtastic apps), hex with or without a
    "0x" prefix, or an empty string for no encryption.

    Raises:
        ValueError: if the text is not a recognisable key.
    """
    import base64
    import binascii

    text = (text or "").strip()
    if not text:
        return b""

    candidate = text[2:] if text.lower().startswith("0x") else text
    stripped = candidate.replace(" ", "").replace(":", "").replace("-", "")

    # Hex first: unambiguous when the length matches a key size.
    if len(stripped) in (2, 32, 48, 64):
        try:
            return bytes.fromhex(stripped)
        except ValueError:
            pass

    try:
        decoded = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"PSK is neither valid hex nor base64: {e}") from e

    if len(decoded) not in (0, 1, 16, 24, 32):
        raise ValueError(
            f"decoded PSK is {len(decoded)} bytes; expected 1, 16, 24, or 32"
        )
    return decoded


def format_psk_fingerprint(key: bytes) -> str:
    """
    A short, non-reversible identifier for a key.

    Shown in the configuration UI so an operator can confirm the balloon and
    their handheld hold the same key without the key itself ever being
    displayed or sent back over the wire.
    """
    if not key:
        return "none (unencrypted)"
    digest = hashlib.sha256(key).hexdigest()
    return f"{digest[:8]} ({len(key) * 8}-bit)"


def generate_psk(length: int = 32) -> bytes:
    """Generate a fresh random channel key."""
    if length not in (16, 24, 32):
        raise ValueError("PSK length must be 16, 24, or 32 bytes")
    return os.urandom(length)
