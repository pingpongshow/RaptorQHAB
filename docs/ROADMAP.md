# RaptorHAB — Meshtastic + USB Configuration Roadmap

Baseline: `c02134b`. Each phase is a branch merged to `main` with a tag.
Nothing in a later phase starts until the prior phase's exit criteria pass.

---

## 0. Architecture answer: what radio does the balloon actually use?

**The balloon does not use LoRa.** The SX1262 is driven in **GFSK/FSK** mode:

| | Current RaptorHAB link | Meshtastic (US LongFast) |
|---|---|---|
| Modulation | GFSK | LoRa |
| Bitrate | 96 kbps (`config.py:25`) | ~1.07 kbps effective |
| Deviation / BW | 50 kHz fdev | BW 250 kHz, SF11, CR 4/5 |
| Frequency | 915.0 MHz | 906.875 MHz (US slot 20) |
| Sync word | `"RAPT"` = `52 41 50 54` | `0x2B` |
| Framing | 8-byte header + CRC32 | 16-byte Meshtastic header |
| Payload | RaptorQ fountain symbols | AES-256-CTR protobuf |

`_configure_fsk()` (`radio.py:522`) and `_set_fsk_modulation()` (`radio.py:570`)
confirm it. The 96 kbps GFSK link is what makes image downlink practical — the
same images over LoRa would be ~90× slower.

### Consequence for the design

The SX1262 supports both, but **only one at a time**. Switching means
`SetPacketType` + re-issuing modulation params, packet params, sync word, and
`SetRfFrequency`. That is a handful of SPI transactions — call it **~5-15 ms**,
to be measured on the bench in Phase 2 — so **time-division multiplexing between
"image mode" and "Meshtastic mode" is entirely feasible.** That mode switch is
the central mechanism of this whole project, and the scheduler is built around it.

Two things follow that are worth stating plainly:

1. **While in GFSK image mode the balloon is deaf to LoRa.** Repeating and
   uplink only work inside explicitly scheduled LoRa RX dwell windows. Message
   latency to the balloon equals the RX window cadence, not "instant."
2. **Retuning between 915.0 and 906.875 MHz is fine** — one `SetRfFrequency`,
   and the existing image calibration covers the whole 902-928 band.

---

## Decisions (confirmed 2026-08-17)

| Question | Decision |
|---|---|
| Q2 Bench-validate RX before Phase 5 | **Yes** — a second node is available |
| Q4 Broadcast hop limit | **`hop_limit = 0`** — heard directly, never rebroadcast by others |
| Q5 Private channel PSK | No key defined yet; **set through configuration** |
| LANDED mode | **Yes** — build it (folded into Phase 4) |
| Q1 GPS-loss fallback | **Hold last known mode** |
| Q3 External dependencies | **Prefer none** — hand-roll protobuf and AES on both sides |
| Q6 Image link frequency | Changeable, but **must match the ground modem** — never auto-tuned |
| Hardware | Waveshare SX1262 (HF) LoRa HAT for Pi Zero + L76K GPS |
| Hardware band | **850-930 MHz** (Core1262-HF). The 433 MHz regions and China are unreachable |
| Regional frequency | **Auto-select Meshtastic band from GPS position** (see below) |
| Phase status | 1, 2, 3, 4, 6, 7 complete. **5 blocked on the RX bench test** |

---

## Phase 1 — Foundation ✅ COMPLETE

*Branch `phase-1-foundation`. No new features. Everything downstream depends on
config that actually persists and a payload that actually recovers.*

1. ✅ Fixed `REVIEW.md` A1-A5, A7, A8, B1-B3, B5-B7, C1-C5, C8.
2. ✅ `Pi/common/configstore.py`: JSON-backed, schema-versioned, atomic write
   (temp file + fsync + `os.replace` + directory fsync), `0600` permissions,
   quarantines a corrupt file, leaves an *unreadable* one alone, and preserves
   unknown keys across a firmware downgrade.
3. ✅ `AirborneConfig` resolves defaults → JSON file → env → CLI. `from_env()`
   still works. One bad key no longer discards the whole file.
4. ✅ `Pi/airborne/params.py` — every parameter carries type, range, category,
   description, env var, and `live` vs `restart` apply semantics.
   `--print-schema` emits the JSON the Phase 2 UI will render from.
5. ✅ Radio defaults collapsed to one authoritative copy.
6. ✅ `Pi/tests/` — 148 hardware-free tests.

**Exit criteria — met:** `cd Pi && python -m pytest` passes on a laptop with no
radio, camera, or GPS; the payload recovers from an induced error storm; a
setting changed and reloaded persists.

New CLI surface:

```bash
python3 -m airborne.main --print-schema
```

```bash
python3 -m airborne.main --print-config
```

```bash
python3 -m airborne.main --callsign RPHAB7 --save-config
```

---

## Phase 2 — USB gadget: config + terminal ✅ COMPLETE

1. ✅ CDC-ACM composite gadget via libcomposite, with a distinct product
   string so the app can tell the payload apart from a Heltec modem or a
   Meshtastic node on the same bus.
2. ✅ `common/linkproto.py` + `LinkProtocol.swift` — length-prefixed frames
   with a channel id and CRC-32, so config traffic and terminal output share
   one line without corrupting each other. Resynchronises mid-stream, which is
   what makes attaching to an already-running payload work.
3. ✅ `airborne/usbconsole.py` — the config RPC (`hello`, `get_schema`,
   `get_config`, `set_config`, `reset_config`, `get_status`, `list_images`,
   `fetch_image`, `get_logs`, `generate_psk`, `restart_service`) plus a real
   PTY shell. **Binds only to the gadget TTY** and refuses anything else, so a
   shell can never be served over the radio — enforced in code and tested.
4. ✅ macOS **Config** tab: the entire form is generated from the schema the
   payload sends, so a parameter added on the Pi appears with no Swift change.
   Restart-required fields are marked, unsaved edits flagged, batches applied
   all-or-nothing.
5. ✅ macOS **Console** tab: terminal with history, Ctrl-C/Ctrl-D, and a
   scrollback cap. Disabled with an explanation when USB is not connected.
6. ✅ M2/M3 fixed — `RawSerialPort` takes a per-connection baud rate, and
   `SerialDeviceDiscovery` identifies devices by USB vendor/product ID and
   product string instead of matching `cu.` against every port on the Mac.

Secrets are write-only across the link: a channel key can be set and its
fingerprint read back, but the value itself never crosses the cable.

Deliberately **no `g_ether`**: an ethernet gadget creates an interface
`systemd-networkd-wait-online` blocks on, adding tens of seconds to every boot
when nothing is plugged in.

---

## Phase 3 — Meshtastic transmit on the balloon ✅ COMPLETE

*Branch `phase-3-meshtastic-tx`. Phase 2 skipped for now.*

1. ✅ `common/radio_lora.py` — LoRa mode for the SX1262 as a mixin on the
   existing driver, so one object owns the SPI bus and GPIO. All eight
   Meshtastic modem presets, band-aware image calibration, and a real
   time-on-air calculation from the datasheet.
2. ✅ `common/radio_manager.py` — `RadioModeManager` serialises GFSK/LoRa
   switching under one lock, measures switch latency, and clamps transmit
   power to the region ceiling on every switch.
3. ✅ `common/meshtastic/` — dependency-free, per Q3:
   - `protobuf.py` hand-rolled wire-format writer and tolerant reader
   - `crypto.py` pure-Python AES-256-CTR, validated against FIPS-197 and
     NIST SP 800-38A
   - `packet.py` the 16-byte header, channel hash, deterministic node id
   - `messages.py` Position, Telemetry, EnvironmentMetrics, NodeInfo, Text
   - `regions.py` the band table, frequency derivation, and geographic lookup
4. ✅ Beacons carry position, telemetry, CPU temperature as an environment
   metric, periodic NodeInfo, and a configurable operator message.
5. ✅ Dual destination: broadcast on the primary channel plus position and
   text on an optional private channel with its own key.
6. ✅ NodeInfo every N cycles so the balloon appears by name.
7. ✅ `airborne/region_manager.py` — auto region selection with dwell,
   edge margin, hold-on-GPS-loss, and hard suspension over unknown territory.
8. ✅ Region changes drive frequency and power together, and appear in logs
   and status.

**Exit criteria — met in software; the on-air half needs the bench.**
372 tests pass with no hardware. Frequency derivation reproduces the published
Meshtastic values, AES matches the NIST vectors, and the LongFast channel hash
matches Meshtastic's 0x08. A simulated track crossing a border retunes the
radio and clamps power, and one crossing into unmapped territory stops
Meshtastic transmission without touching the image downlink.

**Remaining, needs hardware:** run `tools/bench_lora.py` against your second
node. That closes Q2 and is the gate on Phase 5.

```bash
sudo python3 Pi/tools/bench_lora.py rx --duration 120
```

```bash
sudo python3 Pi/tools/bench_lora.py tx --count 5 --power 17
```

```bash
sudo python3 Pi/tools/bench_lora.py switch --count 50
```

The `switch` measurement matters beyond a pass/fail: it sets how finely the
Phase 4 scheduler can interleave images with beacons.

---

## Phase 4 — Zone-aware scheduler ✅ COMPLETE

*Branch `phase-4-scheduler`.*

1. ✅ `airborne/zone_manager.py` — four zones with sticky transitions:
   **LAUNCH** (inside the radius), **CRUISE** (outside it, or above the
   altitude override), **LANDED** (low and stationary), **UNKNOWN** (no fix
   yet, treated as LAUNCH since the balloon is almost certainly still on the
   pad). Hysteresis on the radius, an altitude override, and a least-squares
   vertical-rate fit that survives GPS altitude noise.
2. ✅ Launch point auto-captured from the first 3D fix when unset, including
   the launch elevation, so altitudes are reported above ground level.
3. ✅ `airborne/transmit_scheduler.py` — a debt-model airtime allocator.
   Entitlement accrues in proportion to each activity's share of wall-clock
   time and the most-indebted activity goes next, so the long-run ratios hold
   even though slices routinely overrun (a slice always finishes the packet it
   started). The Meshtastic beacon interval is a **hard floor**: an overdue
   beacon beats any image backlog.
4. ✅ **LANDED mode** (your call): images stop entirely, capture is disabled,
   and everything goes to a slow recovery beacon.
5. ✅ Zone changes are logged with distance, altitude AGL, and vertical rate,
   and surfaced in the periodic status line.

Default budgets, all configurable:

| Zone | Images | Meshtastic | Idle | Beacon |
|---|---|---|---|---|
| LAUNCH | 98% | 1% | 1% | 600 s |
| CRUISE | 5% | 5% | 90% | 300 s |
| LANDED | 0% | 5% | 95% | 60 s |

Percentages are of **airtime, not packets** — a GFSK image packet is a couple
of milliseconds and a LongFast beacon is several hundred, so "5% of packets"
and "5% of airtime" are wildly different things, and airtime is what costs
battery and occupies the channel.

**Exit criteria — met.** `tests/test_flight_simulation.py` drives a synthetic
track through the real controller: pad → ascent → drift → descent → landing.
Zones appear in order, airtime lands within tolerance of the budget in each
zone, images stop after landing, no stretch of the flight goes without a
beacon, and a US-to-Europe drift retunes to 869.525 MHz clamped to 14 dBm.
473 tests, no hardware.

**Bug caught by these tests:** a payload sitting on the launch pad is low and
stationary for far longer than any dwell period, so it declared itself LANDED
*before launch* — stopping image capture and dropping to slow beacons while
still on the ground. Landing detection is now disarmed until the balloon has
actually been above `zone_landed_arm_altitude_m` (default 2000 m AGL).

---

## Phase 5 — Balloon as a tagged repeater + Meshtastic uplink

*Branch `phase-5-repeater`. Depends on A6 being resolved — RX must be proven
on hardware first.*

1. LoRa RX dwell windows, scheduled only in CRUISE mode.
2. **Tagged-repeat gate:** only rebroadcast a packet if it carries an explicit
   marker. Proposed: addressed to the balloon's node id **or** a text payload
   with a configurable prefix (default `!RPT `). Never blanket-repeat.
3. Repeat guard rails, all configurable: dedupe cache of recently-seen
   `packet_id`s, max repeats/hour, minimum spacing, and **`hop_limit = 0` on
   the balloon's own broadcasts** (confirmed), so nothing the balloon
   originates is rebroadcast onward by the thousands of nodes within its
   ~400-mile footprint.
4. Uplink command handling — messages addressed to the balloon on the private
   channel can trigger a small, explicit allowlist of commands. Decide whether
   to reuse `airborne/commands.py` (B4) or retire it.
5. Repeater statistics in telemetry and in the USB config UI.

**Exit:** a tagged message from a handheld is repeated; an untagged one on the
same channel is provably ignored.

---

## Phase 6 — macOS Meshtastic integration ✅ COMPLETE

1. ✅ `MeshtasticTransport.swift` — USB serial (the `0x94 0xC3` framed stream
   API, picking messages out of a stream that also carries plain log text) and
   BLE (CoreBluetooth, draining `fromRadio` until empty on each `fromNum`
   notification rather than reading once and losing queued messages).
2. ✅ Hand-rolled protobuf and AES-256-CTR, per Q3. CommonCrypto provides CTR;
   CryptoKit does not expose it.
3. ✅ **Meshtastic** tab: node list with position, battery, signal and hop
   count; message log with send-to-mesh or send-to-balloon; channel management
   with key entry and a warning when a channel uses the published default key.
4. ✅ The balloon's node id is derived from its callsign exactly as the payload
   derives it, so its beacons are attributed automatically and feed the map.

---

## Phase 7 — Unified position fusion ✅ COMPLETE

`PositionFusion.swift` maintains one authoritative position from four sources,
in priority order:

| Priority | Source | Stale after |
|---|---|---|
| 1 | RAPTOR modem (your own receiver) | 45 s |
| 2 | Meshtastic node attached to this Mac | 10 min |
| 3 | Meshtastic MQTT (third-party relay) | 15 min |
| 4 | Dead reckoning from the last two fixes | 5 min |

The staleness windows differ because the sources do: RAPTOR updates
constantly, so a gap means trouble and should hand over quickly, while
Meshtastic beacons are minutes apart by design.

- A lower-priority source never overwrites a *fresher* higher-priority one, so
  losing the modem briefly does not make the map jump to a stranger's report
  and back.
- The map always shows **which source it is drawing and how old it is**, with
  third-party fixes explicitly flagged. A position relayed from someone else's
  node is useful, but only if you know that is what you are looking at.
- Dead reckoning is capped at five minutes and clearly labelled. A balloon's
  track curves and the wind changes with altitude, so a long projection is a
  confident lie.
- `MeshtasticMQTTClient.swift` is a minimal MQTT 3.1.1 client (no dependency),
  **off by default** — connecting reaches a public broker, which should be a
  deliberate choice — and filters to the balloon's node id so it does not
  ingest the whole public mesh.

---

## Questions — all answered

**Q1 — GPS-loss behavior.** ✅ **Hold last known mode.** If the balloon loses
its fix, the zone state it was already in persists rather than reverting.

**Q2 — Bench-validate RX first.** ✅ Yes, before Phase 5. Hardware confirmed as
the **Waveshare SX1262 LoRa HAT for Pi Zero with an L76K GPS**.

**Q3 — Swift Package Manager dependency.** ✅ **Prefer no dependencies**, add
one only if genuinely needed. Applied to both sides: the Pi gets a hand-rolled
protobuf writer and a pure-Python AES-256-CTR, and Phase 6 will hand-roll the
Swift decoder for the handful of message types we actually parse.

**Q4 — Mesh footprint / hop limit.** ✅ `hop_limit = 0` on broadcasts.

**Q5 — Private channel key management.** ✅ Configured through the USB terminal
and the macOS app. Stored in the config JSON at `0600`, never transmitted over
the radio, and returned as `null` (never its value) by the config RPC.

**Q6 — Frequency plan.** ✅ The image link frequency is changeable but **must
match the ground modem**. It therefore stays a manually-set parameter and is
never touched by the region logic below — only the Meshtastic frequency moves
automatically.

**Q7 — Meshtastic node identity.** *(clarifying, not blocking)* Every
Meshtastic node has a 32-bit node id plus a long and short name — this is what
makes the balloon appear as `RaptorHAB-1` on someone's handheld instead of an
anonymous `!a1b2c3d4`. Nothing is "registered" anywhere; there's no central
authority, the node simply announces itself. The plan derives the node id
deterministically from your callsign so it stays stable across flights and
across reflashes, which means receivers keep a continuous history of the
balloon rather than seeing a new stranger each launch. No action needed from
you.

---

## Regional frequency compliance (added at your request)

**Requirement:** in Meshtastic mode the balloon must transmit on the frequency
used by the region it is currently over, or nobody there can hear it.

This is more than a frequency change. Each Meshtastic region defines a band, a
transmit power ceiling, and in some places a duty-cycle limit — EU 868 is
14 dBm ERP with a 10% duty cycle, EU 433 is 12 dBm. Moving the frequency
without also clamping power would put the balloon outside what that region
permits, so the region table carries all three and the transmit path enforces
them.

Frequency is derived the same way Meshtastic firmware derives it, so the
balloon lands on exactly the channel local nodes are listening to:

```
num_channels = floor((freq_end - freq_start) / bandwidth)
channel_index = djb2(channel_name) % num_channels
frequency = freq_start + bandwidth/2 + channel_index * bandwidth
```

Verified against published values: US → 906.875, EU_868 → 869.525,
EU_433 → 433.875, ANZ → 919.875 MHz.

**Safety rules, because transmitting on the wrong band is a licensing problem
rather than a bug:**

- A configured **home region is always the default**. Auto-switching is opt-in.
- Auto-switch requires a **3D fix**, and applies hysteresis at the boundary so
  a balloon tracking along a border does not oscillate between bands.
- If the position falls in **no known region**, the balloon **stops
  transmitting Meshtastic entirely** rather than guessing. Images and the
  RAPTOR downlink continue unaffected.
- On GPS loss the last determined region is held (consistent with Q1).
- Transmit power is clamped to the active region's ceiling, always.
- Every region change is logged and reported in downlink telemetry.

**Hardware band limit.** The SX1262 die spans 150-960 MHz, but a board is not
a die: the matching network, filters and PA on the fitted HAT are tuned for one
range. The **Waveshare Core1262-HF** covers **850-930 MHz**; the LF variant
covers 410-510 MHz. Driving an HF board at 433 MHz radiates almost nothing into
a badly matched load and risks damaging the amplifier.

So the hardware band is a first-class constraint, not a footnote:

- `radio_hardware_band` declares the fitted front end (HF / LF / CUSTOM, with
  `radio_band_min_mhz` and `radio_band_max_mhz` for a non-stock board).
- Regions whose derived channel frequency falls outside it are **unavailable**,
  and flying over one suspends Meshtastic exactly like unmapped territory —
  with no dwell period, because out-of-band transmission gets no grace.
- Configuration is validated up front: a home region or an image-link frequency
  outside the board's band is rejected with the list of reachable regions.
- The bench tool refuses out-of-band frequencies as a hard stop.

On the HF board **17 of the 22 regions are reachable**: US, EU_868, JP, ANZ,
KR, TW, RU, IN, NZ_865, TH, UA_868, MY_919, SG_923, PH_868, PH_915, BR_902,
NP_865. The four 433 MHz regions and China (478 MHz) are not, and flying over
one silences Meshtastic while leaving the image downlink untouched.

Region determination uses a coarse bounding-box table rather than a country
polygon database — appropriate both for the payload's compute budget and for
the accuracy the task actually needs. Ocean and unassigned airspace resolve to
"no region", which is the safe outcome.

---

## Suggestions beyond what you asked for

- **A dry-run / HITL mode.** A `--simulate` path already exists in the driver.
  Extend it so the whole scheduler can be run on a laptop against a recorded or
  synthetic GPS track. For a system you get one shot at, being able to replay a
  full flight in 30 seconds is worth more than any single feature here.
- **Log the mode schedule to the downlink.** When a flight goes wrong you want
  to know what the balloon *thought* it was doing, not just what you received.
- **Meshtastic as a recovery beacon.** After descent, when the balloon is on the
  ground and the GFSK link is blocked by terrain, a low-rate LoRa beacon at
  ground level is often the thing that finds the payload. Consider a third
  zone mode — `LANDED`, triggered by low altitude + no vertical movement — that
  goes Meshtastic-only and slows everything down to conserve battery. This may
  be the single highest-value item on this list.
- **Power budget.** Phase 4 changes the transmit duty cycle substantially.
  Worth measuring current draw per mode on the bench and putting a projected
  flight-duration number in the config UI.
- **CI.** Once Phase 1 has tests, a GitHub Actions run on push costs nothing
  and prevents the Phase 4 scheduler from silently regressing Phase 1's fixes.
