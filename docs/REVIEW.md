# RaptorHAB — Code Review Findings

Baseline commit: `c02134b`. Review focused on the airborne payload and shared
`common/` layer (the code this project's next phase modifies), with a lighter
pass over the ground station and macOS app.

Severity: **A** = will bite you in flight · **B** = wrong behavior, recoverable ·
**C** = hygiene / latent

Status: items marked **[FIXED in Phase 1]** are resolved on `phase-1-foundation`
with a regression test. A7, A8, B6, and B7 were found while writing those tests.

---

## A — Flight-critical

### A1. Error-state recovery is dead code — the payload never reboots **[FIXED in Phase 1]**
`Pi/airborne/main.py:311-314` increments `_error_count`, calls
`_set_state(State.ERROR_STATE)` and `break`s out of the main loop. Control then
falls to `finally: self._cleanup()` and the process exits. `_handle_error_state()`
(`main.py:520`), which contains the `reboot_on_fatal_error` logic, is **never
called from anywhere**. Same in `start()` (`main.py:152-156`).

Net effect: after 10 accumulated errors the payload silently stops transmitting
for the rest of the flight. `reboot_on_fatal_error: bool = True` is a lie.

Fix: call `_handle_error_state()` on the ERROR_STATE transition, and rely on
systemd `Restart=always` as the real backstop rather than `os.system("sudo reboot")`.

### A2. `_error_count` never resets **[FIXED in Phase 1]**
`main.py:309`. A single transient SPI glitch every few minutes across a 3-hour
flight will accumulate to `_max_errors = 10` and trip A1. Errors should decay
(reset on N consecutive successful cycles, or use a sliding window).

### A3. Watchdog timeout is hardcoded and `watchdog_enabled` is ignored **[FIXED in Phase 1]**
`main.py:168-172` passes `timeout_sec=60` literally instead of
`self.config.watchdog_timeout_sec`, and never checks `self.config.watchdog_enabled`.
The config fields are decorative.

### A4. Watchdog firing does nothing **[FIXED in Phase 1]**
`_watchdog_triggered()` (`main.py:532`) sets `ERROR_STATE`, but the main loop
never reads `self._state` — it only exits on `_shutdown`. A genuinely hung loop
stays hung. The watchdog needs to either set `_shutdown` (letting systemd
restart) or drive the hardware watchdog at `/dev/watchdog`.

### A5. Import-time `os.makedirs` on an absolute root path **[FIXED in Phase 1]**
`Pi/airborne/config.py:92-95` runs `os.makedirs("/RaptorHAB/airborne/images")`
from `__post_init__`, and `DEFAULT_CONFIG = Config()` at line 220 executes it at
**module import**. Any non-root import of `airborne.config` — a unit test, a
config-dump CLI, the new USB config service — raises `PermissionError` before
`main()` is reached. Move directory creation into `_initialize_components()`.

### A6. `receive()` has almost certainly never been exercised on hardware
`Pi/common/radio.py:833` implements RX, but nothing in `airborne/` calls it, and
the board uses **both** `SET_DIO2_AS_RF_SWITCH_CTRL` (`radio.py:558`) and a
separate `pin_txen` GPIO (`radio.py:781`). That combination needs bench
validation before the Meshtastic repeater / uplink features can be trusted —
see the open question in `ROADMAP.md`.

### A7. The image path silently degrades to an encoder nothing can decode **[FIXED in Phase 1]**

`FountainEncoder.__init__` fell back from RaptorQ to LT codes whenever the
`raptorq` wheel failed to import, logging one INFO line: `"raptorq not
available, using LT codes"`.

But the ground station has **no LT decoding path at all**. `FountainDecoder`
only ever constructs a `RaptorQDecoder` — verified by grep, `LTDecoder(` is
never called anywhere in the product code. And `LTDecoder` could not have
saved it: I ran the airborne `LTEncoder` against it directly and it reports
`is_complete() == True` while returning **incorrect bytes**, because it derives
neighbours from `random.Random(symbol_id)` while the encoder uses
`random.Random(seed + symbol_id)` with a random seed that is never transmitted
(`ImageMetaPayload` has no seed field).

So a Pi that booted with a broken or missing raptorq wheel would transmit
happily for an entire flight and not one image could ever be reconstructed.
The ground station's checksum check (`decoder.py:611`) means you'd get no
corrupt images — just silence.

Fix: the LT fallback now raises `IncompatibleEncoderError` unless explicitly
opted into via the new `allow_lt_fallback` bench-only setting, and the payload
runs a preflight check at startup so this fails on the bench rather than at
altitude.

### A8. The receive path used the wrong length for every image packet **[FIXED in Phase 1]**

`protocol._get_expected_payload_length` returned `2 + 4 + FOUNTAIN_SYMBOL_SIZE`
= 206 bytes for `IMAGE_DATA`. The real payload is **210** bytes: RaptorQ
prefixes each symbol with a 4-byte payload ID (RFC 6330 §3.2), so a 200-byte
symbol arrives as 204 bytes on the wire. Measured directly against the encoder.

Consequence: the parser sliced the buffer at the wrong offset, the CRC check
failed on *every* image data packet, and reception only worked at all via a
fallback branch that re-tried `verify_crc32_packet(data)` on the whole
received buffer. That fallback is correct only when the radio reports an exact
length — and the function's own docstring says the SX1262 "may return padded
data (255 bytes)". Any padding, and every image packet is discarded.

Fix: `_candidate_payload_lengths` now returns both valid lengths for
`IMAGE_DATA` (RaptorQ first, then LT) and the CRC selects the right one, with
the full-buffer length still tried last for variable-length types.

---

## B — Wrong behavior

### B1. `IMAGE_META` interval slot never fires **[FIXED in Phase 1]**
`Pi/airborne/packets.py:233-240`. With the airborne defaults
(`telemetry_interval_packets = 5`, `image_meta_interval_packets = 10`), every
counter value divisible by 10 is *also* divisible by 5, so the telemetry branch
always wins first. Verified: over 1000 packets the distribution is
**200 telemetry / 0 image-meta / 800 image-data**.

The only `IMAGE_META` ever transmitted is the one emitted when a new image
starts (`packets.py:309`). A receiver that joins mid-image, or drops that single
packet, cannot decode the image at all — it never learns `symbol_size` /
`num_source_symbols` / `checksum`. This is likely a real cause of failed image
reassembly.

Fix: make the two schedules independent (`elif`-chain is wrong; use separate
counters), and pick intervals that aren't multiples of one another.

### B2. `_images_transmitted` counts queued, not transmitted **[FIXED in Phase 1]**
`main.py:405` increments after `scheduler.add_image()`, which is a *queue*
operation that can silently return `False` when the scheduler's
`Queue(maxsize=5)` is full (`packets.py:159`). The return value is discarded, so
a dropped image is reported as transmitted and the `ImageInfo` is lost.

### B3. Bare `except:` swallows everything on image enqueue **[FIXED in Phase 1]**
`main.py:433-436`. Catches `KeyboardInterrupt`/`SystemExit` too. Should be
`except queue.Full:`.

### B4. Uplink command path is entirely dead
`Pi/airborne/commands.py` (604 lines) defines a full `CommandHandler` with
`CMD_PING` / `CMD_SETPARAM` / `CMD_CAPTURE` / `CMD_REBOOT`. Nothing in
`airborne/` imports it — verified by grep. The payload is TX-only in practice.
This is relevant to the new work: the wire protocol for remote configuration
already exists and is untested, and Phase 3 should either wire it up or
deliberately retire it in favor of the USB path.

### B5. `_telemetry_bytes` is an implicit instance attribute **[FIXED in Phase 1]**
`packets.py:223` assigns it inside `get_next_packet`, and `packets.py:254`
guards with `hasattr`. Declare it in `__init__` as `Optional[bytes] = None`.

---

### B6. `parse_packet_full` raised on every input **[FIXED in Phase 1]**
`protocol.py:526` did `header, payload_bytes = parse_packet(data)`, but
`parse_packet` returns a **4-tuple** `(type, sequence, flags, payload)` or
`None`. So the function raised `ValueError` on every well-formed packet and
`TypeError` on every malformed one. It has no callers today, which is the only
reason this never surfaced.

### B7. `queue_command_ack` passed keyword names the payload does not have **[FIXED in Phase 1]**
`packets.py:206` constructed `CommandAckPayload(command_type=…, command_seq=…)`
but the dataclass fields are `acked_type` / `acked_seq` (`protocol.py:209-210`),
so any call raised `TypeError`. Dead today because the uplink path is unwired
(B4), but it would have failed the moment Phase 5 turned it on.

---

## C — Hygiene / latent

- **C1** **[FIXED in Phase 1]** `os.system("sudo reboot")` (`main.py:530`) — use
  `subprocess.run(["systemctl", "reboot"], check=False)`; `os.system` inherits
  the shell and offers no error handling.
- **C2** **[FIXED in Phase 1]** Config had no persistence layer. Everything is dataclass defaults +
  `RAPTORHAB_*` env vars (`config.py:126-216`), so a setting changed at the
  launch site does not survive a reboot. Phase 1 must fix this before any of the
  new configurable parameters are meaningful.
- **C3** **[FIXED in Phase 1]** `from_env()` would raise an unhandled `ValueError` on a malformed
  environment value (`float(os.getenv(...))`, `config.py:155`) and take the
  payload down at startup. Needs per-field try/except with a logged fallback.
- **C4** **[FIXED in Phase 1]** Duplicated defaults: `common/constants.py` and `airborne/config.py`
  disagree (`bitrate` 200000 vs 96000, `fdev` 125000 vs 50000, `tx_period`
  10 vs 2, `max_stored_images` 100 vs 20000). `constants.py` values are unused
  dead weight and will mislead the next reader.
- **C5** **[FIXED in Phase 1]** `SX1262Config` defaults (`radio.py:156-157`) are a third copy of the
  same numbers.
- **C6** `PacketScheduler.__init__` accepts a `telemetry_callback` that
  `main.py:233` never passes, so the callback branch (`packets.py:258`) is dead.
- **C7** Committed `__pycache__/` and `.DS_Store` throughout — now handled by
  the new `.gitignore`, but `Pi/common/__pycache__/radio_hardware_spi.cpython-313.pyc`
  references a module that no longer exists in the tree, which suggests a
  refactor left something behind.
- **C8** **[FIXED in Phase 1 — 148 tests]** No tests anywhere in the repo. Given a flight system with a one-shot
  failure mode, the fountain encoder/decoder round-trip, the CRC, the packet
  framing, and the new mode-scheduler all warrant unit tests that run on a
  laptop without hardware.

---

## macOS app / ground station (lighter pass)

- **M1** `ContentView.swift` is 2118 lines and mixes six top-level tab views;
  the two new tabs (Config/Console, Meshtastic) should be new files rather than
  extending it.
- **M2** `SerialPortManager.swift:34` hardcodes `baudRate = 921600` as a static.
  The Pi USB-gadget console and a Meshtastic device (115200) need different
  rates — this needs to become per-connection.
- **M3** `SerialPortManager.swift:89` accepts any port matching
  `usbserial|usbmodem|cu.` — the `cu.` clause matches essentially every serial
  device including Bluetooth ports. With three device classes now on the bus
  (Heltec modem, Pi gadget, Meshtastic node) the app needs real
  VID/PID-based identification, not substring matching.

---

## Radio defects found on hardware (post-Phase-7 bench testing)

These three were only reachable with a real payload and a real modem. Together
they meant the ground station received nothing whatsoever, while the payload's
own logs reported tens of thousands of packets transmitted successfully. Every
one of them is silent by construction: nothing fails, nothing throws, and the
airtime accounting looks healthy.

- **R1 — A Meshtastic beacon moved the image downlink off the ground station's
  frequency.** `set_frequency()` assigned to `config.frequency_mhz`. That field
  is the GFSK home frequency, but `configure_lora()` calls `set_frequency()` to
  tune to a Meshtastic channel, so after one beacon "home" *was* the Meshtastic
  channel. `restore_gfsk()` faithfully re-tuned to 906.875 MHz and every
  subsequent image went out on the mesh frequency, unheard.

  Severity: total loss of imagery for the remainder of a flight, beginning at
  the first beacon — roughly one minute after launch. Measured 15,414 packets
  transmitted, 0 received.

  Fixed by tracking the current tuning separately from the configured home
  frequency. `current_frequency_mhz` is now a read-only view of where the radio
  actually is; `config.frequency_mhz` is never written by the driver.

- **R2 — The transmit preamble exactly equalled the receiver's preamble
  detector.** Both were 32 bits. A detector needs whole preamble bits *after*
  its AGC has settled, so it never completed a detection: measured 0 packets out
  of 20, with no sync errors and no CRC errors, because nothing was ever
  detected to fail. At 128 bits the same link detects 100%. The extra 96 bits
  cost about 1 ms per packet.

  This one predates the Meshtastic work — v1 shipped the same 32/32 pairing and
  happened to work, which is exactly what a zero-margin setting looks like until
  it doesn't.

- **R3 — RX bandwidth was sized for a modulation the payload no longer uses.**
  467 kHz is right for 200 kbps / 125 kHz deviation. At the current 96 kbps /
  50 kHz, Carson's rule gives 196 kHz, so the receiver was admitting 2.4x more
  noise than the signal occupies. Now 234.3 kHz, and the payload, the modem
  firmware and the macOS app all had to be changed together — they must agree
  or the link is deaf.

### Not a defect: the all-zeros test artifact

Bench packets built from `bytes(n)` failed in proportion to their length, which
looked convincingly like clock drift. It was not. With whitening disabled, a
payload of constant bits gives the receiver no transitions to recover its clock
from, and the failure rate compounds with packet length. Real payloads are
RaptorQ-encoded WebP, which is effectively random: 8/8 delivery at 210 bytes,
the actual image packet size. Recorded here because the false trail cost real
time, and because it argues for enabling whitening if a future payload type ever
carries long runs of constant bytes.

### Ground station modem

- **R4 — The modem was deaf after every power cycle.** It did not initialise its
  radio until a host sent `CFG:`, and it accepted `CFG:` only during a 120-second
  boot window. Unplugging the modem, or restarting the app, left it silently
  receiving nothing; reconfiguring a running modem did nothing at all. It now
  persists its RF settings in flash, comes up listening on them, and accepts
  `CFG:` at any time. Fallback defaults are deliberately never persisted, so a
  modem that has never been configured still waits for the app rather than
  committing to a guess.
