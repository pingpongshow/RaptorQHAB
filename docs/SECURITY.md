# What a finder can read

A balloon lands where it lands. Sometimes a stranger picks it up. This is what
they get, and what you can do about it.

---

## The card is not encrypted, and cannot usefully be

Full-disk encryption does not work on a balloon. The payload has to boot on
the pad with nobody there to type a passphrase, so any key it can use to start
itself travels with it on the same card. Storing the key beside the data it
protects is not encryption, it is filing.

File permissions do not help either. `0600` means nothing to someone who pulls
the card and mounts it on their own computer as root.

So the approach here is different, and it has two halves.

---

## Half one: encrypt what you write, to a key you keep

**This works, and it is enabled with two settings.**

Public-key encryption breaks the circular problem. The payload carries only a
*public* key. It can seal an image or a telemetry row, and it cannot open one
again. The private half never leaves your ground station. A finder gets
ciphertext and nothing that decrypts it.

Generate a keypair once, on your Mac:

```bash
python3 payload/tools/recording_key.py generate
```

Then set two parameters on the payload, from the macOS app or the config file:

```
recording_encryption_enabled = true
recording_public_key = <the public key it printed>
```

After a flight, decrypt what you recover:

```bash
python3 payload/tools/recording_key.py decrypt /Volumes/SDCARD/var/lib/raptorhab --out ./recovered
```

### How it works

X25519 key agreement, HKDF-SHA256 key derivation, AES-256-CTR encryption,
HMAC-SHA256 authentication. Every file gets a fresh ephemeral keypair whose
private half is discarded immediately after sealing, so each file is
independent and the payload cannot reconstruct any of them.

Encrypt-then-MAC, so a tampered file is rejected before anything is decrypted.
Overhead is 74 bytes per file, and a 50 KB image seals in 31 ms on a Pi Zero
2 W.

Telemetry logs are written as a **sequence of independently sealed records**
rather than one growing box. A balloon can lose power mid-write at any moment;
a single box truncated at the end would be undecryptable in its entirety,
whereas a record stream costs you only the final row.

### If sealing fails, the data survives

An encryption bug must not be the reason you lose a flight's imagery. If
sealing raises, the plaintext is written instead and the failure is logged
loudly. Similarly, if encryption is enabled but the configured key is
malformed, the payload says so at startup rather than quietly writing
plaintext you believed was protected.

### Lose the private key and the recordings are gone

There is no recovery path. That is the point. Back it up before you fly.

---

## Half two: do not fly the secrets you do not need

Encryption protects what the payload *writes*. It does nothing for credentials
that were already on the card when it took off. Audit them:

```bash
sudo /opt/raptorhab/tools/preflight_secrets.py
```

```bash
sudo /opt/raptorhab/tools/preflight_secrets.py --sanitize --keep-wifi
```

| What | Why it matters | Removable |
|---|---|---|
| Wi-Fi credentials | Your network password, in plaintext. Usually the worst thing aboard. | Yes |
| SSH host and user keys | Lets someone impersonate the Pi, or reach other machines | Yes |
| Passwordless sudo rules | Convenient while developing, not something to fly | Yes |
| Old flight logs | Contain previous positions, including your launch site | Yes |
| Meshtastic private channel key | The payload needs it to transmit | **No** |

### The channel key is the one you cannot protect

A private Meshtastic channel key has to be readable by the payload, because
the payload has to encrypt beacons with it unattended. Anyone holding the card
can read it, and could then read or forge traffic on that channel.

Treat it as burned after any flight you do not personally recover. Rotate it
on your handhelds before the next one, and consider a per-flight key so a
single loss does not compromise your whole history.

---

## Decrypting by hand

Every path below reads the same files. Use whichever suits the situation: the
apps when you have them, the CLI when you are on a spare machine, and the raw
recipe when you have neither and only the bytes matter.

### The apps

All three ground stations — the macOS app, the Python desktop app and the web
UI — have an **SD Card** tab. Point it at a mounted card, and
it reports what is there and — before you copy anything — whether the sealed
files can actually be opened. Sealed images are decrypted for preview without
being written anywhere; **Import & decrypt** writes plaintext out.

### The command line

```bash
# A whole directory, recursively
python3 payload/tools/recording_key.py decrypt /Volumes/rootfs/var/lib/raptorhab --out ./recovered

# One file
python3 payload/tools/recording_key.py decrypt img_00042_1787104085.webp.rhs --out ./recovered

# A key kept somewhere other than ~/.raptorhab/recording_key
python3 payload/tools/recording_key.py --key /Volumes/backup/flightkey decrypt ./images --out ./recovered
```

Sealed files carry a `.rhs` suffix; the tool strips it, so
`img_00042_1787104085.webp.rhs` becomes `img_00042_1787104085.webp`.

Check a key against a card before you start:

```bash
python3 payload/tools/recording_key.py verify <the public key from the payload config>
```

The payload records which key it sealed to, so you can read that off the card
itself:

```bash
grep recording_public_key /Volumes/rootfs/var/lib/raptorhab/config/airborne.json
```

If that does not match your key, stop: those files cannot be opened and no
further effort changes it.

### From Python, with nothing installed

Useful on a machine that has neither ground station, and for anyone auditing
the format:

```python
from pathlib import Path
import sys
sys.path.insert(0, "Pi")                      # or groundstation/python

from common.sealedbox import open_sealed       # raptorhabgs.core.sealedbox works too

private = Path.home().joinpath(".raptorhab/recording_key").read_bytes()

for sealed in Path("images").glob("*.rhs"):
    plaintext = open_sealed(sealed.read_bytes(), private)
    sealed.with_suffix("").write_bytes(plaintext)   # drops the .rhs
```

Telemetry logs are sealed the same way, but as a stream of length-prefixed
records rather than one box, so a flight cut short still yields everything
written up to that moment. `open_sealed` handles both, and returns the CSV
text.

### The format, if you have to reimplement it

Each sealed file is:

Header is `struct` format `>4sBB32sI`, 42 bytes:

| Bytes | Field |
|---|---|
| 0–3 | magic `RHSB` |
| 4 | version, currently 1 |
| 5 | flags, currently 0 |
| 6–37 | ephemeral X25519 public key |
| 38–41 | plaintext length, big-endian uint32 |
| 42… | ciphertext |
| last 32 | HMAC-SHA256 tag |

Opening one:

1. X25519 between the ephemeral public key and your private key gives a shared
   secret.
2. HKDF-SHA256 over that secret, salted with the two public keys, gives an
   AES-256 key and a separate HMAC key.
3. Verify the HMAC over everything before the tag. **Encrypt-then-MAC**: check
   the tag before decrypting anything, so a tampered file is rejected rather
   than silently producing wrong plaintext.
4. AES-256-CTR with the derived key and an **all-zero counter**.

The zero counter is deliberate and safe here, and only because of the property
above it: the key is derived from a fresh ephemeral keypair for every single
file, so no key is ever used twice and the usual catastrophe of a repeated
CTR nonce cannot arise. Reuse that construction anywhere the key is not
per-file and it becomes a serious bug.

74 bytes of overhead per file: 42 of header and 32 of tag.

### When it does not work

**`no private key at ~/.raptorhab/recording_key`** — the ground station has no
key. If it is elsewhere, pass `--key`. If it was never generated, the files are
unreadable; see [INSTALL.md](INSTALL.md#recording-encryption-keys).

**`authentication failed`** — the file was sealed to a different keypair, or it
is damaged. The MAC cannot distinguish the two, and deliberately so: both mean
do not trust these bytes.

**Files read as 0 bytes** — not encryption. That is an unclean shutdown, where
the size was recorded but the data never reached the card. Nothing recovers it.


## A realistic threat model

Most balloons are found by a farmer, a hiker, or nobody. The realistic risk is
not a determined attacker — it is your home Wi-Fi password sitting in
plaintext on a card in a stranger's kitchen drawer.

Ranked by what actually goes wrong:

1. **Wi-Fi credentials.** Remove them before flight, or fly a throwaway SSID.
2. **The Meshtastic private key.** Rotate after any unrecovered flight.
3. **Flight logs.** Encrypted once you enable recording encryption.
4. **Imagery.** Same.
5. **SSH material.** Strip it if you will not need remote access in the field.

A five-minute `preflight_secrets.py --sanitize` plus recording encryption
covers all five.
