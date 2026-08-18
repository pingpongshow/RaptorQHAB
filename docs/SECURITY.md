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
python3 Pi/tools/recording_key.py generate
```

Then set two parameters on the payload, from the macOS app or the config file:

```
recording_encryption_enabled = true
recording_public_key = <the public key it printed>
```

After a flight, decrypt what you recover:

```bash
python3 Pi/tools/recording_key.py decrypt /Volumes/SDCARD/var/lib/raptorhab --out ./recovered
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
