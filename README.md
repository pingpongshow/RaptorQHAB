# RaptorHAB

**A high-altitude balloon payload that sends pictures home from the
stratosphere — and keeps talking when the pictures stop.**

RaptorHAB is a complete high-altitude balloon system: a Raspberry Pi payload,
a purpose-built ground station modem, and a macOS companion app. It downlinks
photographs over a fast custom radio link, beacons its position onto the
Meshtastic mesh so anyone within a few hundred miles can help find it, and
switches strategy on its own as it climbs, drifts, and lands.

---

## Why it exists

Most balloon payloads make you choose. You can have a fast link that sends
imagery but only reaches your own receiver, or a slow beacon that anyone can
hear but shows you nothing. Lose line of sight and the fast link goes quiet
exactly when you need it most — during descent, and on the ground where
terrain blocks everything.

RaptorHAB runs both, on one radio, and decides for itself how to divide the
airtime:

- **Near the launch site**, almost all airtime goes to imagery. You are
  standing right there, the link is short and strong, and pictures are the
  point.
- **In cruise**, imagery drops to a trickle and the balloon spends most of its
  time silent, conserving battery, punctuated by Meshtastic beacons that any
  passing node can relay.
- **On the ground**, imagery stops entirely and the payload becomes a slow,
  patient recovery beacon — because when the payload is in a field behind a
  hill, a low-rate LoRa beacon is what actually finds it.

---

## Highlights

**Two radios in one.** The SX1262 runs 96 kbps GFSK for imagery and switches
to LoRa for Meshtastic beacons. Measured switching cost: **7.7 ms** round trip,
0.38% of airtime at the default cadence.

**Fountain-coded imagery.** Photographs are RaptorQ-encoded, so the ground
station reconstructs a complete image from *any* sufficient subset of packets.
No retransmission, no acknowledgements, no lost image because one packet was
missed.

**It follows the law across borders.** Meshtastic frequencies differ by
country. The payload picks the correct regional band from its own GPS
position, clamps transmit power to that region's ceiling, and — over territory
with no band plan it can reach — **stops transmitting rather than guessing**.

**Flight recordings a finder cannot read.** Images and telemetry logs are
sealed to an X25519 public key as they are written. The payload holds only the
public half and physically cannot decrypt its own recordings, so recovering
the balloon yields ciphertext. **31 ms** to seal a 50 KB image on a Pi Zero.

The keypair is created when you prepare the card, and the private half never
goes near the payload. That matters more than it sounds: sealing to a key you
do not hold does not make the recordings hard to read, it makes them
impossible, and nothing detects that until you try. Card provisioning refuses
to enable encryption without a keypair it has confirmed you have.

**Configure everything over one USB cable.** All **113** payload parameters,
plus a real terminal, over the Pi's USB port. The macOS app builds its
configuration form from a schema the payload sends, so the two never drift
apart. Configuration is USB-only by design — the payload will not accept
settings over the radio.

**Four position sources, one answer.** Your own modem, a Meshtastic node
plugged into your Mac, the public Meshtastic MQTT network, and dead reckoning
— fused by priority and freshness. The map always shows which source it is
drawing and how old it is.

---

## The system

### Airborne payload

| | |
|---|---|
| Computer | Raspberry Pi Zero 2 W (64-bit, Debian 13) |
| Radio | Waveshare Core1262-HF (SX1262), 850–930 MHz |
| Camera | IMX219 / OV5647 / IMX708 / IMX477 |
| GPS | L76K on the PL011 UART |
| Downlink | GFSK, 96 kbps, 50 kHz deviation, 234 kHz RX, 915 MHz, RaptorQ |
| Mesh | LoRa, Meshtastic-compatible, 17 regions reachable |
| Power | Runs unattended; restarts itself on any fault |

### Ground station modem

| | |
|---|---|
| Boards | Seven builds: Heltec WiFi LoRa 32 V3 & V4, Wireless Stick Lite V3, Vision Master T190, LilyGO T3-S3, Seeed XIAO + Wio-SX1262 |
| Display | 1.9" TFT — live RSSI, SNR, packet counts, radio settings |
| Link | USB serial to the Mac at 921600 baud |
| Config | RF parameters set from the macOS app, no reflashing |
| Persistence | Settings stored in flash; comes up listening after a power cycle |

### macOS companion

Telemetry · Map · Graphs · Landing predictions · Images · Missions · Packets ·
**Meshtastic** · **Config** · **Console**

---

## What makes it different

### It knows where it is, and behaves accordingly

Four flight zones with deliberately sticky transitions — hysteresis on the
launch radius, an altitude override, and a landing detector that stays
disarmed until the balloon has actually flown, so a payload sitting on the pad
during setup never mistakes itself for a landed one.

| Zone | Imagery | Meshtastic | Idle | Beacon |
|---|---|---|---|---|
| Launch | 98% | 1% | 1% | 10 min |
| Cruise | 5% | 5% | 90% | 5 min |
| Descent | 15% | 20% | 65% | 45 s |
| Landed | 0% | 5% | 95% | 1 min |

Descent is the half hour that decides whether the payload is found — the
landing prediction converges then, and the balloon drops below the horizon of
everything that was hearing it. It is the one zone where the mesh beacon is
worth more than the pictures.

Percentages are of **airtime, not packets** — a GFSK image packet lasts a
couple of milliseconds and a LongFast beacon several hundred, so only airtime
reflects what actually costs battery and occupies the channel. The beacon
interval is a hard floor: an overdue beacon beats any image backlog, because a
balloon that stops beaconing to send pictures is a balloon nobody can find.

### It is a good citizen on the mesh

From 30 km up, a 22 dBm LoRa beacon is heard across roughly a 400-mile radius.
If every node that hears it rebroadcasts, one balloon can congest a regional
mesh. RaptorHAB broadcasts with **hop limit 0** — heard directly, forwarded by
nobody — and acts as a repeater only for messages explicitly tagged for it,
never blanket-repeating traffic it happens to overhear.

### It fails in the right direction

Sitting behind every feature is a bias toward staying on the air:

- Errors that recur are counted, but a clean cycle clears the count, so
  transient glitches cannot slowly accumulate into a shutdown.
- A wedged main loop is detected and the process is restarted, not left hung.
- A camera that will not initialise is logged and ignored — the payload keeps
  transmitting telemetry.
- An encryption failure writes plaintext rather than losing the image.
- Configuration that cannot be parsed falls back to defaults, loudly, instead
  of preventing boot.

---

## No external dependencies

The payload runs on Python's standard library plus the RaptorQ wheel. The
macOS app has **zero** third-party packages. Protocol buffers, AES-256-CTR,
X25519, HKDF, MQTT and the Meshtastic packet format are all implemented
directly and validated against published test vectors — FIPS-197, NIST
SP 800-38A, RFC 7748, RFC 5869.

Both halves of every shared protocol are written independently in Python and
Swift, and a test suite compiles the real Swift sources and compares their
output against Python's byte for byte. A framing or key-derivation mismatch
cannot slip through unnoticed.

---

## Verified on hardware

Not simulated. Measured against a real Heltec T190 and a Pi Zero 2 W with the
Waveshare HF HAT.

| | |
|---|---|
| Image downlink | Complete 1280x960 image reassembled from 242 RaptorQ symbols, 0 checksum failures |
| GFSK downlink | 210-byte packets, 8/8 at every length tested |
| Meshtastic transmit | 19/19 packets decrypted by an independent radio |
| LoRa receive | Bench transmitter **and** a live mesh node at 2 hops, −59 dBm |
| Radio mode switch | 3.68 ms into LoRa, 4.02 ms back |
| Recording encryption | 50 KB sealed in 31 ms; payload cannot decrypt it |
| USB configuration | 113 parameters, ~500 ms round trip |
| Tagged repeating | Untagged and third-party traffic ignored; public-channel commands refused |
| Link error rate | 0 bad CRC in 16,690 packets with whitening on, against 137 in 14,951 without |
| GPS fix handling | A 2D fix is reported as 2D; the launch reference settles to 0.4 m against a converged one |
| Tests | **789**, no hardware required |

---

## Getting started

Flash **Raspberry Pi OS Lite (64-bit)**, setting your username, password and
WiFi in Raspberry Pi Imager.

**Either** prepare the card before it ever boots, which also makes the Pi
reachable over the USB cable if WiFi is unavailable or your access point
isolates clients:

```bash
./payload/tools/provision_sd.sh --camera imx219
```

**Or** copy the code over the network:

```bash
rsync -av --exclude '__pycache__' payload/ raptorhab.local:~/raptorhab/
```

```bash
ssh raptorhab.local 'cd ~/raptorhab && sudo ./setup/install.sh --usb-gadget'
```

Reboot, then verify:

```bash
sudo /opt/raptorhab/setup/install.sh --check
```

Either way, one command gets you in — over WiFi, or over the USB cable if WiFi
is unavailable, with nothing to configure on your machine:

```bash
ssh <your-username>@raptorhab.local
```

`ping raptorhab.local` will fail while that succeeds: ping asks for an IPv4
address and only the IPv6 one is usable over the cable. The payload is fine.

Full instructions, including flashing the modem and troubleshooting, are in
**[docs/INSTALL.md](docs/INSTALL.md)**.

---

## Layout

```
payload/          the airborne Raspberry Pi — flight software, installer, tools
groundstation/
  macos/          the macOS companion app
  python/         the cross-platform ground station (Qt app and web UI)
firmware/
  gs-modem/       receiver firmware, seven boards from one source tree
  dual-e22/       dual-radio board: images and Meshtastic at the same time
docs/             everything written down, including the pre-launch checklist
archive/          abandoned experiments, kept for reference
```

## Documentation

| | |
|---|---|
| **[INSTALL.md](docs/INSTALL.md)** | From a blank SD card to a payload that transmits |
| **[PRELAUNCH.md](docs/PRELAUNCH.md)** | The checklist to work through before you let go |
| **[TESTPLAN.md](docs/TESTPLAN.md)** | How the whole system is verified, and by whom |
| **[ROADMAP.md](docs/ROADMAP.md)** | Architecture, design decisions, and hardware results |
| **[REVIEW.md](docs/REVIEW.md)** | Every defect found and fixed, with the reasoning |
| **[OUTSTANDING.md](docs/OUTSTANDING.md)** | What is known to be wrong and not yet fixed |
| **[MESHTASTIC.md](docs/MESHTASTIC.md)** | What the balloon broadcasts, the uplink commands, and message relaying |
| **[POWER.md](docs/POWER.md)** | What draws current in flight, and what was done about it |
| **[SECURITY.md](docs/SECURITY.md)** | What a finder can read, and what to do about it |

---

## A note on responsibility

A high-altitude balloon is aviation. You are responsible for the regulations
that apply where you fly — airspace notification, transmit power, band
allocation, and recovery. RaptorHAB enforces the regional power ceilings it
knows about and refuses to transmit outside the band its hardware supports,
but it cannot know your licence, your airspace, or your local rules. That part
is yours.
