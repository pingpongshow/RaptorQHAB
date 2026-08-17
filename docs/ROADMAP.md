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
| Hardware | Waveshare SX1262 LoRa HAT for Pi Zero + L76K GPS |
| Regional frequency | **Auto-select Meshtastic band from GPS position** (see below) |

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

## Phase 2 — USB gadget: config + terminal over the Pi's USB port

*Branch `phase-2-usb-console`. **DEFERRED** — skipped for now, revisit after Phase 3.*

**Approach:** Pi Zero 2 W's data port supports USB OTG. Configure it as a
**composite USB gadget** via `libcomposite`:

- **CDC-ACM serial** (primary) → appears on the Mac as `/dev/cu.usbmodemXXXX`,
  no drivers, no networking. This carries a multiplexed protocol:
  channel 0 = JSON config/telemetry RPC, channel 1 = PTY console bytes.
- **CDC-ECM ethernet** (secondary, optional) → gives you real `ssh` for
  development. Recommended to enable but not depended on by the app.

Steps:

1. `Pi/setup/usb-gadget.sh` + a systemd unit to bring up the composite gadget at
   boot. Document the `dtoverlay=dwc2` / `modules-load` prerequisites.
2. `Pi/airborne/usbconsole.py` — service on the gadget TTY:
   - Length-prefixed frames with a 1-byte channel id, so config traffic and
     console output never interleave-corrupt each other at 921600 baud.
   - **Config RPC** (JSON): `get_schema`, `get_config`, `set_config`,
     `get_status`, `get_telemetry`, `capture_now`, `radio_test_tx`,
     `list_images`, `fetch_image`, `restart_service`.
   - **Console**: `pty.fork()` a login shell, pipe both directions, forward
     window size. **Bound to the gadget TTY only** — never reachable over the
     radio, enforced in code, not by convention.
3. Bench-test mode: a `radio_test_tx` that keys the transmitter at a chosen
   power/mode for N seconds so you can verify RF on a spectrum analyzer
   without launching the full stack.
4. macOS app: new **Config** tab (`ConfigView.swift`, `PiLinkManager.swift`).
   Schema-driven form generated from `get_schema` — so adding a Pi-side setting
   later requires no Swift changes. Restart-required fields visibly marked.
5. macOS app: new **Console** tab (`ConsoleView.swift`) — a real terminal view
   over channel 1. Gated on an active USB connection; the tab is disabled and
   explains why when the Pi is not plugged in.
6. Fix M2/M3 — per-connection baud rate, and VID/PID-based device
   identification so the app can tell the Pi gadget, the Heltec modem, and a
   Meshtastic node apart.
7. Check `RaptorHabGS.entitlements` for the USB / serial-device entitlement; if
   the app is sandboxed this will need `com.apple.security.device.usb`.

**Exit:** plug in the Pi, the Mac app finds it, you can read and change every
setting, and you get a working shell — all over one USB cable, with no radio
involvement.

---

## Phase 3 — Meshtastic transmit on the balloon

*Branch `phase-3-meshtastic-tx`.*

1. `Pi/common/radio_lora.py` — LoRa mode for the existing SX1262 driver:
   `SetPacketType(0x01)`, LoRa modulation params (SF/BW/CR/LDRO), packet params
   (preamble 16, explicit header, CRC on, IQ standard), sync word `0x2B`.
   Preserve the GFSK path unchanged.
2. `RadioModeManager` — owns the SX1262 and serializes mode switches
   (GFSK-TX / LoRa-TX / LoRa-RX). Measure and log real switch latency. This is
   the only thing allowed to touch the radio.
3. `Pi/common/meshtastic/` — a **minimal, dependency-free** implementation:
   - `protobuf.py`: hand-rolled varint/length-delimited writer. We need maybe
     six message types; pulling in the full `meshtastic` package on a Pi Zero
     is not worth the weight or the startup cost.
   - `packet.py`: the 16-byte header (dest, sender, packet_id, flags/hop_limit,
     channel hash, next-hop, relay-node).
   - `crypto.py`: AES-256-CTR, nonce = packet_id ‖ sender ‖ zero-extend.
     Default channel PSK and a user-supplied private-channel PSK.
   - `messages.py`: `Position`, `Telemetry/DeviceMetrics`, `NodeInfo`,
     `TextMessage` — encoded as `Data{portnum, payload}`.
4. Beacon content: position (lat/lon/alt/sats), telemetry (battery, CPU temp,
   uptime), and a **configurable free-text message**.
5. Dual-destination: broadcast (`0xFFFFFFFF`) on the primary channel **and**
   addressed traffic on a configured private channel with its own PSK.
6. Periodic `NodeInfo` so the balloon shows up with a name rather than a hex id.
7. `Pi/common/meshtastic/regions.py` — the regional band table, the Meshtastic
   frequency derivation, and lat/lon region lookup. See "Regional frequency
   compliance" below for the rules this must obey.
8. Region changes drive both frequency and the transmit power ceiling, and are
   surfaced in logs and downlink telemetry.

**Exit:** a stock Meshtastic handheld receives the balloon's beacons, decodes
position and telemetry, and shows it on the Meshtastic map; a simulated GPS
track crossing a regional boundary retunes the radio and clamps power without
ever transmitting outside a permitted band.

---

## Phase 4 — Zone-aware scheduler

*Branch `phase-4-scheduler`.*

New config block:

```
launch_zone_lat / launch_zone_lon      # 0,0 = auto-capture first 3D fix
launch_zone_radius_m          = 8000
zone_hysteresis_m             = 800    # exit at R, re-enter at R - 800
zone_altitude_override_m      = 3000   # above this AGL, force CRUISE
inzone_image_percent          = 98
inzone_mesh_interval_sec      = 600
cruise_image_percent          = 5
cruise_mesh_interval_sec      = 300
cruise_lora_rx_percent        = 5
mesh_beacon_text              = "..."
```

Plus a **LANDED** mode (confirmed): triggered by low altitude with no vertical
movement for a sustained period, it goes Meshtastic-only at a slow beacon rate
and stops image capture entirely to conserve battery. When terrain blocks the
GFSK link from a payload on the ground, a low-rate LoRa beacon is very often
what actually finds it.

```
landed_altitude_m             = 1000   # AGL relative to launch elevation
landed_vertical_rate_mps      = 0.5    # below this counts as stationary
landed_dwell_sec              = 120    # sustained before declaring LANDED
landed_mesh_interval_sec      = 60
```

1. `ZoneManager` — great-circle distance from launch point, with **hysteresis**
   so a balloon drifting along the boundary doesn't thrash modes, plus the
   altitude override and the LANDED transition.
2. **No-GPS-fix behavior must be explicit** (see open question Q1). Proposed
   default: hold the last known zone; if no fix has *ever* been acquired,
   assume IN-ZONE (safe: keeps the bandwidth on images near the launch site
   where recovery matters, and stays off the mesh).
3. `TransmitScheduler` — a time-slice allocator over `RadioModeManager` that
   honors the percentages, guarantees the Meshtastic beacon interval as a hard
   floor regardless of image backlog, and never interrupts a GFSK packet
   mid-transmission.
4. Auto-capture the launch point from the first 3D fix if lat/lon are unset, and
   log it prominently.
5. Every zone transition logged and reflected in downlink telemetry, so you can
   see from the ground which mode the balloon believes it's in.

**Exit:** simulated GPS track (no hardware) drives the scheduler through
in-zone → cruise → back, and the emitted mode ratios match config within
tolerance.

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

## Phase 6 — macOS app: Meshtastic device integration

*Branch `phase-6-mac-meshtastic`.*

1. `MeshtasticManager.swift` — connect a Meshtastic node by **USB serial**
   (115200, `0x94 0xC3` framed stream API) or **BLE** (CoreBluetooth: `toRadio`
   / `fromRadio` / `fromNum` characteristics).
2. Protobuf codec — see Q3; recommend `swift-protobuf` via SPM plus the
   official `.proto` files, over hand-rolling on the Swift side.
3. New **Meshtastic** tab: node list, channel config, message log, send a
   message to the balloon (broadcast or private channel), live RSSI/SNR.
4. Decode the balloon's beacons and surface them as a first-class position
   source.

**Exit:** a Meshtastic node plugged into the Mac shows received balloon beacons
and can send a message that the balloon acts on.

---

## Phase 7 — Unified position fusion on the map

*Branch `phase-7-position-fusion`.*

A `PositionSource` priority chain, each entry with its own staleness timeout,
shown on the map with distinct styling and an explicit "source + age" label:

1. **RAPTOR direct** (GFSK modem) — highest trust.
2. **Meshtastic beacon** via locally-connected node.
3. **Meshtastic MQTT** — subscribe to the public server
   (`mqtt.meshtastic.org`, topic `msh/US/2/json/#` for pre-decoded JSON, or
   `msh/US/2/e/#` for encrypted). Fully configurable, **off by default**, and
   the app must show clearly when a position came from a third-party node
   rather than from your own receiver.
4. Last known + dead reckoning, clearly marked as extrapolated.

Never let a lower-priority source overwrite a fresher higher-priority fix.
The existing SondeHub integration is the model to follow here.

**Exit:** pull the modem's antenna and the map keeps tracking via Meshtastic,
with the source indicator changing accordingly.

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
