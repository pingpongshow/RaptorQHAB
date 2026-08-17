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

## Phase 1 — Foundation: fix the flight-critical bugs, add real config persistence

*Branch `phase-1-foundation`. No new features. Everything downstream depends on
config that actually persists and a payload that actually recovers.*

1. Fix `REVIEW.md` items A1-A5 and B1-B5.
2. Introduce `Pi/common/configstore.py`: JSON-backed, schema-versioned,
   atomic write (temp file + `os.replace`), falls back to defaults on a corrupt
   or unparseable file and logs loudly. Config file at
   `/RaptorHAB/config/airborne.json`.
3. Refactor `AirborneConfig` to load from: defaults → JSON file → env vars →
   CLI args (in that precedence order). Keep `from_env()` working.
4. Tag every parameter as **live-applicable** or **restart-required**; expose
   that in the schema so the UI can grey out the right controls.
5. Collapse the three duplicated copies of radio defaults (C4/C5) into one.
6. Add `Pi/tests/` with hardware-free unit tests: CRC, packet framing round-trip,
   fountain encode/decode round-trip, scheduler slot distribution (the test that
   would have caught B1), config store corruption recovery.

**Exit:** tests pass on a laptop; payload survives an induced error storm and
restarts; a setting changed and rebooted persists.

---

## Phase 2 — USB gadget: config + terminal over the Pi's USB port

*Branch `phase-2-usb-console`.*

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

**Exit:** a stock Meshtastic handheld receives the balloon's beacons, decodes
position and telemetry, and shows it on the Meshtastic map.

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

1. `ZoneManager` — great-circle distance from launch point, with **hysteresis**
   so a balloon drifting along the boundary doesn't thrash modes, plus the
   altitude override.
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
   `packet_id`s, max repeats/hour, minimum spacing, and **`hop_limit` forced to
   a low value on rebroadcast** (see Q4 — this matters a lot from 100k ft).
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

## Open questions — I'd like answers before Phase 3

**Q1 — GPS-loss behavior.** If the balloon loses its fix in cruise, should it
hold cruise mode, or fall back to in-zone (images-heavy)? My default above is
"hold last known," but you know the recovery priorities better than I do.

**Q2 — Bench-validate RX first?** Nothing has ever called `radio.receive()` on
this hardware (finding A6), and the board drives *both* DIO2-as-RF-switch and a
separate TXEN GPIO. Before Phase 5, I'd like a bench test with a second radio
confirming the Pi can actually hear LoRa. Do you have a spare SX1262 or a
Meshtastic node to transmit at it? If RX doesn't work, the repeater and uplink
features are hardware-blocked and we should know that in week one, not week six.

**Q3 — Is adding a Swift Package Manager dependency acceptable?** The Xcode
project currently has zero external dependencies. Meshtastic's protobufs are
large and evolving; `swift-protobuf` is the sane path for the Mac side. If
you'd rather stay dependency-free I can hand-roll a decoder for the ~8 message
types we need, but it becomes maintenance we own.

**Q4 — Mesh footprint / hop limit.** At 100,000 ft a 22 dBm LoRa beacon has a
line-of-sight footprint on the order of 400 miles radius. It will be heard by a
very large number of nodes. If those nodes rebroadcast, one balloon can
meaningfully congest regional meshes — this has caused real friction on
previous HAB flights. Strong recommendation: **`hop_limit = 0` on broadcasts**
(heard directly, never rebroadcast by others), beacon interval ≥ 5 minutes in
cruise, and the tagged-repeat gate you already specified. Are you OK with
`hop_limit = 0` as the default?

**Q5 — Private channel key management.** Where does the private-channel PSK
live? Proposal: on the Pi in the config JSON with `0600` permissions, entered
via the USB config UI only, never transmitted over the radio and never echoed
back over the RPC (write-only field, displayed as a fingerprint). Confirm.

**Q6 — Frequency plan.** Balloon images at 915.0 MHz, Meshtastic at
906.875 MHz. Is the image link's frequency fixed by the ground modem's
configuration, or is it retunable? If retunable, worth checking the two don't
sit close enough to desensitize a co-located receiver.

**Q7 — Does the balloon need a Meshtastic node id / long name registered?**
Suggest deriving the node id from the callsign so it's stable across flights.

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
