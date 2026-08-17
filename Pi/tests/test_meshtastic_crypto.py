"""
AES and Meshtastic channel encryption.

The AES implementation is hand-written to avoid a dependency, so it is checked
against the NIST vectors rather than trusted. A silent error here would mean
the balloon transmits packets no Meshtastic client can read.
"""

import pytest

from common.meshtastic.crypto import (
    AES,
    DEFAULT_PSK,
    aes_ctr,
    build_nonce,
    channel_hash,
    decrypt_payload,
    encrypt_payload,
    expand_psk,
    format_psk_fingerprint,
    generate_psk,
    parse_psk,
)


# --- FIPS-197 block cipher vectors ----------------------------------------


@pytest.mark.parametrize(
    "key,plaintext,ciphertext",
    [
        # FIPS-197 Appendix C.1, C.2, C.3
        (
            "000102030405060708090a0b0c0d0e0f",
            "00112233445566778899aabbccddeeff",
            "69c4e0d86a7b0430d8cdb78070b4c55a",
        ),
        (
            "000102030405060708090a0b0c0d0e0f1011121314151617",
            "00112233445566778899aabbccddeeff",
            "dda97ca4864cdfe06eaf70a0ec0d7191",
        ),
        (
            "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f",
            "00112233445566778899aabbccddeeff",
            "8ea2b7ca516745bfeafc49904b496089",
        ),
    ],
    ids=["AES-128", "AES-192", "AES-256"],
)
def test_aes_matches_fips197(key, plaintext, ciphertext):
    cipher = AES(bytes.fromhex(key))
    assert cipher.encrypt_block(bytes.fromhex(plaintext)).hex() == ciphertext


def test_aes_rejects_bad_key_length():
    for length in (0, 8, 15, 17, 31, 33, 64):
        with pytest.raises(ValueError, match="key must be"):
            AES(b"\x00" * length)


def test_aes_rejects_bad_block_length():
    cipher = AES(b"\x00" * 32)
    with pytest.raises(ValueError, match="block must be"):
        cipher.encrypt_block(b"\x00" * 15)


# --- NIST SP 800-38A counter mode -----------------------------------------


def test_ctr_mode_matches_sp800_38a():
    """
    F.5.5 CTR-AES256.Encrypt, all four blocks.

    The multi-block part is what matters: the counter must increment as a
    big-endian 128-bit integer, matching the mbedtls implementation Meshtastic
    firmware uses. Incrementing a little-endian word instead produces a correct
    first block and garbage after it -- invisible for a payload under 16 bytes,
    and broken for every real beacon.
    """
    key = bytes.fromhex(
        "603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4"
    )
    counter = bytes.fromhex("f0f1f2f3f4f5f6f7f8f9fafbfcfdfeff")
    plaintext = bytes.fromhex(
        "6bc1bee22e409f96e93d7e117393172a"
        "ae2d8a571e03ac9c9eb76fac45af8e51"
        "30c81c46a35ce411e5fbc1191a0a52ef"
        "f69f2445df4f9b17ad2b417be66c3710"
    )
    expected = (
        "601ec313775789a5b7a7f504bbf3d228"
        "f443e3ca4d62b59aca84e990cacaf5c5"
        "2b0930daa23de94ce87017ba2d84988d"
        "dfc9c58db67aada613c2dd08457941a6"
    )

    assert aes_ctr(key, counter, plaintext).hex() == expected


def test_ctr_counter_carries_across_byte_boundary():
    """The vector above starts at ...feff, so block 2 forces a carry."""
    key = b"\x00" * 32
    counter = bytes.fromhex("000000000000000000000000000000ff")

    out = aes_ctr(key, counter, b"\x00" * 32)
    manual_block1 = AES(key).encrypt_block(counter)
    manual_block2 = AES(key).encrypt_block(bytes.fromhex("00000000000000000000000000000100"))

    assert out[:16] == manual_block1
    assert out[16:] == manual_block2


def test_ctr_is_symmetric():
    key = generate_psk(32)
    nonce = build_nonce(12345, 0xDEADBEEF)
    plaintext = b"RaptorHAB position beacon payload, longer than one block"
    assert aes_ctr(key, nonce, aes_ctr(key, nonce, plaintext)) == plaintext


def test_ctr_handles_partial_final_block():
    key = generate_psk(32)
    nonce = build_nonce(1, 2)
    for length in (1, 15, 16, 17, 31, 33, 100):
        data = bytes(range(256))[:length]
        assert aes_ctr(key, nonce, aes_ctr(key, nonce, data)) == data


def test_ctr_on_empty_data():
    assert aes_ctr(generate_psk(32), build_nonce(1, 1), b"") == b""


def test_ctr_rejects_wrong_nonce_size():
    with pytest.raises(ValueError, match="nonce must be 16 bytes"):
        aes_ctr(generate_psk(32), b"\x00" * 12, b"data")


# --- nonce construction ---------------------------------------------------


def test_nonce_layout():
    """packet_id (uint64 LE) || sender (uint32 LE) || zero (uint32 LE)."""
    nonce = build_nonce(packet_id=0x0102030405060708, sender_node_id=0xAABBCCDD)
    assert nonce == bytes.fromhex("0807060504030201ddccbbaa00000000")
    assert len(nonce) == 16


def test_nonce_rejects_out_of_range_values():
    with pytest.raises(ValueError):
        build_nonce(packet_id=-1, sender_node_id=0)
    with pytest.raises(ValueError):
        build_nonce(packet_id=0, sender_node_id=0x1_0000_0000)


def test_different_packet_ids_give_different_ciphertext():
    """Reusing a keystream across packets would leak plaintext via XOR."""
    key = generate_psk(32)
    plaintext = b"identical payload"
    a = encrypt_payload(key, 1, 0xABCD, plaintext)
    b = encrypt_payload(key, 2, 0xABCD, plaintext)
    assert a != b


def test_different_senders_give_different_ciphertext():
    key = generate_psk(32)
    plaintext = b"identical payload"
    a = encrypt_payload(key, 1, 0x1111, plaintext)
    b = encrypt_payload(key, 1, 0x2222, plaintext)
    assert a != b


def test_payload_round_trip():
    key = expand_psk(b"\x01")
    plaintext = b"\x08\x03\x12\x18position payload bytes"
    encrypted = encrypt_payload(key, 999, 0x12345678, plaintext)
    assert encrypted != plaintext
    assert decrypt_payload(key, 999, 0x12345678, encrypted) == plaintext


# --- PSK handling ---------------------------------------------------------


def test_expand_default_psk_selector():
    """A one-byte PSK selects a well-known default key."""
    key = expand_psk(b"\x01")
    assert key == DEFAULT_PSK
    assert len(key) == 16


def test_expand_psk_selector_offsets_last_byte():
    assert expand_psk(b"\x02")[-1] == (DEFAULT_PSK[-1] + 1) & 0xFF
    assert expand_psk(b"\x03")[-1] == (DEFAULT_PSK[-1] + 2) & 0xFF


def test_expand_psk_zero_means_unencrypted():
    assert expand_psk(b"\x00") == b""
    assert expand_psk(b"") == b""


def test_expand_psk_passes_through_real_keys():
    for length in (16, 24, 32):
        key = generate_psk(length)
        assert expand_psk(key) == key


def test_expand_psk_rejects_odd_lengths():
    with pytest.raises(ValueError, match="PSK must be"):
        expand_psk(b"\x00" * 20)


def test_parse_psk_accepts_base64():
    assert parse_psk("AQ==") == b"\x01"


def test_parse_psk_accepts_hex():
    key = generate_psk(32)
    assert parse_psk(key.hex()) == key
    assert parse_psk("0x" + key.hex()) == key


def test_parse_psk_accepts_delimited_hex():
    assert parse_psk("00:11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff") == bytes(
        range(0, 256, 17)
    )


def test_parse_psk_empty_means_unencrypted():
    assert parse_psk("") == b""
    assert parse_psk("   ") == b""


def test_parse_psk_rejects_garbage():
    with pytest.raises(ValueError):
        parse_psk("this is definitely not a key")


def test_parse_psk_rejects_wrong_decoded_length():
    import base64

    with pytest.raises(ValueError, match="expected 1, 16, 24, or 32"):
        parse_psk(base64.b64encode(b"\x00" * 20).decode())


def test_fingerprint_does_not_reveal_the_key():
    key = generate_psk(32)
    fingerprint = format_psk_fingerprint(key)
    assert key.hex() not in fingerprint
    assert "256-bit" in fingerprint


def test_fingerprint_is_stable_and_distinguishing():
    a, b = generate_psk(32), generate_psk(32)
    assert format_psk_fingerprint(a) == format_psk_fingerprint(a)
    assert format_psk_fingerprint(a) != format_psk_fingerprint(b)


def test_fingerprint_of_no_key():
    assert "unencrypted" in format_psk_fingerprint(b"")


def test_generate_psk_is_random_and_correctly_sized():
    keys = {generate_psk(32) for _ in range(20)}
    assert len(keys) == 20
    assert all(len(k) == 32 for k in keys)


def test_generate_psk_rejects_bad_length():
    with pytest.raises(ValueError):
        generate_psk(20)


# --- channel hash ---------------------------------------------------------


def test_longfast_default_channel_hash():
    """
    Meshtastic's published hash for LongFast with the default key is 0x08.

    Receivers use this byte to choose which key to try, so a mismatch means
    the balloon's packets are ignored even though everything else is right.
    """
    assert channel_hash("LongFast", expand_psk(b"\x01")) == 0x08


def test_channel_hash_is_a_single_byte():
    for name in ("LongFast", "RaptorHAB", "a-very-long-channel-name"):
        assert 0 <= channel_hash(name, generate_psk(32)) <= 0xFF


def test_channel_hash_depends_on_name_and_key():
    key = generate_psk(32)
    assert channel_hash("Alpha", key) != channel_hash("Beta", key)
    assert channel_hash("Alpha", key) != channel_hash("Alpha", generate_psk(32))
