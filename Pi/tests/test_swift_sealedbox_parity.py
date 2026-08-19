"""
The macOS app must open exactly what the payload seals.

A divergence here would not announce itself. It would surface months later as a
recovered flight that will not open, with the card already wiped and no way
back. So this seals in Python and opens with the real Swift source rather than
a copy of it.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SWIFT_DIR = REPO / "MacRaptorHAB" / "RaptorHabGS"
SOURCES = [SWIFT_DIR / name for name in
           ("SealedBox.swift", "MeshtasticProtocol.swift", "MeshtasticProtobuf.swift")]

pytestmark = pytest.mark.skipif(
    shutil.which("swiftc") is None or not all(p.exists() for p in SOURCES),
    reason="swiftc or the macOS app sources are not available here",
)

from common.sealedbox import generate_keypair, seal  # noqa: E402

MAIN = r'''
import Foundation

let raw = try! Data(contentsOf: URL(fileURLWithPath: "vectors.json"))
let json = try! JSONSerialization.jsonObject(with: raw) as! [String: Any]
let privateKey = Data(hex: json["private"] as! String)!
let cases = json["cases"] as! [String: [String: String]]

var failures = 0
for (name, vector) in cases.sorted(by: { $0.key < $1.key }) {
    let sealed = Data(hex: vector["sealed"]!)!
    let expected = Data(hex: vector["plain"]!)!
    do {
        let opened = try SealedBox.open(sealed, privateKey: privateKey)
        print(opened == expected ? "PASS \(name)" : "FAIL \(name)")
        if opened != expected { failures += 1 }
    } catch {
        print("FAIL \(name): \(error.localizedDescription)")
        failures += 1
    }
}

var wrongKey = privateKey; wrongKey[0] ^= 0xFF
let sample = Data(hex: cases.first!.value["sealed"]!)!
do { _ = try SealedBox.open(sample, privateKey: wrongKey)
     print("FAIL wrong-key-accepted"); failures += 1 }
catch { print("PASS wrong-key-rejected") }

var tampered = sample; tampered[tampered.count - 40] ^= 0x01
do { _ = try SealedBox.open(tampered, privateKey: privateKey)
     print("FAIL tampered-accepted"); failures += 1 }
catch { print("PASS tampered-rejected") }

exit(failures == 0 ? 0 : 1)
'''


@pytest.fixture(scope="module")
def swift_result(tmp_path_factory):
    work = tmp_path_factory.mktemp("sealparity")

    private, public = generate_keypair()
    cases = {
        "empty": b"",
        "short": b"hi",
        "one_block": bytes(range(16)),
        "unaligned": os.urandom(1000),
        "image_sized": os.urandom(48_400),
    }
    (work / "vectors.json").write_text(json.dumps({
        "private": private.hex(),
        "cases": {name: {"sealed": seal(data, public).hex(), "plain": data.hex()}
                  for name, data in cases.items()},
    }))
    (work / "main.swift").write_text(MAIN)

    binary = work / "sealtest"
    compiled = subprocess.run(
        ["swiftc", "-O", "-o", str(binary), *map(str, SOURCES), str(work / "main.swift")],
        capture_output=True, text=True, timeout=600, cwd=work)
    if compiled.returncode != 0:
        pytest.fail(f"Swift compile failed:\n{compiled.stderr[-3000:]}")

    return subprocess.run([str(binary)], capture_output=True, text=True,
                          timeout=120, cwd=work)


def test_swift_opens_everything_python_seals(swift_result):
    assert swift_result.returncode == 0, swift_result.stdout


def test_an_empty_payload_round_trips(swift_result):
    """Zero-length files are a real case: a capture that failed mid-write."""
    assert "PASS empty" in swift_result.stdout


def test_a_full_size_image_round_trips(swift_result):
    assert "PASS image_sized" in swift_result.stdout


def test_the_wrong_key_is_rejected(swift_result):
    """It must fail, not return plausible-looking rubbish."""
    assert "PASS wrong-key-rejected" in swift_result.stdout


def test_a_tampered_file_is_rejected(swift_result):
    """Encrypt-then-MAC: one flipped bit must be caught before decryption."""
    assert "PASS tampered-rejected" in swift_result.stdout
