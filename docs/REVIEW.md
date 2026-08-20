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
`payload/airborne/main.py:311-314` increments `_error_count`, calls
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
`payload/airborne/config.py:92-95` runs `os.makedirs("/RaptorHAB/airborne/images")`
from `__post_init__`, and `DEFAULT_CONFIG = Config()` at line 220 executes it at
**module import**. Any non-root import of `airborne.config` — a unit test, a
config-dump CLI, the new USB config service — raises `PermissionError` before
`main()` is reached. Move directory creation into `_initialize_components()`.

### A6. `receive()` has almost certainly never been exercised on hardware **[CLOSED — since validated]**
The receive path is now wired (`radio_manager.receive_lora_window`, driven by
`Activity.LISTEN` in the transmit scheduler) and has been exercised against a
bench transmitter and a live mesh node at two hops, −59 dBm.
`payload/common/radio.py:833` implements RX, but nothing in `airborne/` calls it, and
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
`payload/airborne/packets.py:233-240`. With the airborne defaults
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
`payload/airborne/commands.py` (604 lines) defines a full `CommandHandler` with
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
  the new `.gitignore`, but `payload/common/__pycache__/radio_hardware_spi.cpython-313.pyc`
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
- **M2** ~~`SerialPortManager.swift:34` hardcodes `baudRate = 921600`.~~
  **[CLOSED — not a defect.]** That class serves only the RAPTOR modem, which
  is always 921600. The payload console and Meshtastic nodes have their own
  transports (`RawSerialPort`, `MeshtasticTransport`, `PiLinkManager`) with
  their own rates.
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

---

## Second full review of the balloon code (2026-08)

A fresh comprehensive pass over `payload/airborne/` and `payload/common/` — about 13,500
lines. Nine defects, all fixed, all with regression tests in
`payload/tests/test_code_review_fixes.py`. None of them were visible on the bench;
they need a clock step, a partial GPS fix, or a power cut to appear, which is
why they survived the first review.

### D1 — A 2D GPS fix was reported as a 3D fix **(flight-critical)**

`common/gps.py` set `fix_type = FIX_3D if fix >= 1`, reading field 6 of GGA.
That field is fix *quality* — 0 none, 1 GPS, 2 DGPS, 4 RTK — and says nothing
about whether the solution has a height component. A receiver tracking three
satellites reports quality 1 and an altitude either held over from the last 3D
solution or invented outright.

Everything downstream keys off `fix_type >= 2`:

| Consumer | What it does on a "3D" fix |
|---|---|
| `region_manager` | Changes **transmit frequency and power** for the region |
| `zone_manager` | Changes flight mode, arms the landing detector |
| `meshtastic_beacon` | Declares the position valid and broadcasts it |

So the payload was prepared to re-tune the PA and change flight behaviour on an
altitude it had no basis for. GSA field 2 is the only sentence that reports
dimensionality (1 none, 2 = 2D, 3 = 3D); the parser read GSA for its DOP values
and threw that field away.

Now GGA supplies validity and GSA supplies dimensionality. Two details matter:

- **Cycles are delimited by the position sentence, not by a timer.** A
  multi-constellation receiver sends one GSA per constellation and the best
  wins — GPS may report 3D in the same breath GLONASS reports none. The first
  attempt grouped them with a 0.5 s window, which is wrong: at 9600 baud a full
  NMEA burst takes about half a second, so any window narrow enough to separate
  cycles is also narrow enough to split one. GGA now consumes the accumulated
  best and clears it.
- **A receiver that emits no GSA keeps working.** If no GSA has been seen for
  `GSA_STALE_SEC`, the old assumption stands. The fix must not cost a fix.

### D2 — A clock step could reboot the payload, or hang it **(flight-critical)**

The Pi has no RTC. It boots with whatever `fake-hwclock` saved and
`systemd-timesyncd` later steps the clock — by months, on a card that has been
on the shelf. I watched this Pi's clock jump two months during setup.

`Watchdog` measured elapsed time with `time.time()`. That step reads as a
months-long gap since the last feed, and the watchdog reboots a payload that
was working perfectly. A step *backwards* is worse in the other loops: the pause
cycle computes `remaining = duration - (now - start)`, which after a backward
step is enormous, and the loop keeps petting the watchdog while it sits there —
so nothing rescues it. The payload would idle for the length of the jump.

Every elapsed-time measurement on the flight path now uses `time.monotonic()`:
the watchdog, the TX and pause cycles, the capture interval, the SX1262 BUSY and
TX-done waits, GPS position age and `wait_for_fix`, the duty-cycle rolling
window, the zone and region dwell timers, the beacon interval, and the repeater's
spacing and hourly rate limit. Genuine wall-clock timestamps — image and
telemetry row times, the beacon's packet time — are unchanged.

`test_flight_modules_use_monotonic_for_elapsed_time` guards the whole class
rather than the instances, so this cannot quietly come back.

The repeater needed care: `should_repeat()` reads the spacing state that
`build_repeat()` writes. Converting one and not the other would have left
`_last_repeat` about 1.7 billion seconds in the future and silenced the repeater
for the rest of the flight — a worse bug than the one being fixed. There is a
test asserting both use the same clock.

### D3 — Encrypted flight logs were split across two files **(flight-critical)**

`TelemetryLogger._write_header()` assigned the writer's return value back to
`self.filepath`. With sealing on, that return value already ends in `.rhs`, so
every subsequent `log()` handed an `.rhs` path to a writer that appends `.rhs`:

```
telemetry_RAPTOR_20260818.csv           0 bytes    (stray)
telemetry_RAPTOR_20260818.csv.rhs       219 bytes  (header only)
telemetry_RAPTOR_20260818.csv.rhs.rhs   740 bytes  (the actual flight log)
```

The path the payload reported in its logs, and the only one an operator would
think to look for, was the one containing nothing but a header. The flight data
was in a file with a doubled extension.

The logger now keeps the base path it hands to the writer separate from the path
the writer actually wrote. Verified end to end: header and every row in one
file, decrypting correctly with the matching private key.

### D4 — The watchdog was petted on a packet counter, not a clock

`if packets_this_cycle % 100 == 0` — wrong in both directions. While the counter
sat at zero it petted on *every* loop iteration; once the counter came to rest on
a non-multiple of 100, which is exactly what happens when the radio starts
failing partway through a cycle, it stopped petting entirely. Now time-based,
matching the pause loop.

### D5 — `SealedWriter.append_line()` did not keep the promise the class makes

The module docstring states that a sealing failure never loses data. `write()`
honoured it; `append_line()` called `seal()` outside any try and raised into its
caller — which is a GPS reader-thread callback. The GPS layer catches per-callback
exceptions so the thread survived, but the row was lost with a traceback rather
than a diagnosis. `TelemetryLogger.log()` compounded it by catching only
`IOError`, which does not cover a `ValueError` from the crypto path.

Both fixed: `append_line()` falls back to plaintext exactly as `write()` does,
and the logger catches broadly.

### D6 — Nothing was ever flushed to the card

`append_line()`'s docstring reasons carefully about power loss — it is the stated
justification for sealing each row as its own record instead of one growing box —
and then never flushed anything. The records were correct and sitting in the page
cache, where a power cut discards them.

Whole-file writes (images) are now atomic: temp file, `fsync`, `rename`, `fsync`
the directory. This matters more for sealed files than plaintext ones, because a
truncated box is not "most of an image" — the authentication tag is at the end,
so a partial file does not open at all.

Appended logs `fsync` every 10 seconds. Every line would be honest but punishing:
a 1 Hz log means 3600 erase-block flushes an hour on an SD card. Ten seconds
bounds the loss to ten rows.

### D7 — The uplink replay window was open on the far side

`abs(seq - last_seq) < window` with everything outside treated as "a large gap,
assume wraparound, not a duplicate". With `last_seq = 100`, a replayed command
carrying `seq = 65000` fell through the far side and executed. Every sequence
number more than `window` away was accepted, which is most of the 16-bit space.

Now computed as a forward distance modulo 2^16, which handles genuine wraparound
(65530 → 3 is a distance of 9, accepted) without the hole (100 → 65000 is 64900,
rejected).

This path is still not wired to anything — see B4 — and the module now says so at
the top, including the fact that it authenticates nothing: any transmitter that
can produce a valid CRC reaches `_handle_reboot`. The Meshtastic uplink path,
which *is* live, requires the private channel key. Fixed anyway so the hazard is
not waiting for whoever wires it up.

### D8 — The GPS thread woke 100 times a second all flight

`_read_loop` polled `in_waiting` and slept 10 ms when it found nothing, so it
woke a hundred times a second for the whole flight to learn that a 1 Hz receiver
had not spoken yet. The port is already opened with `timeout=1.0`, so a blocking
one-byte read parks the thread in the kernel and costs nothing while it waits;
the rest of the burst is then drained in one call. Shutdown latency stays inside
the 2 s join in `stop()`.

### D9 — Two copies of the frame format

`_build_and_advance_raw()` re-implemented `build_packet()` inline — same sync
word, same `'>BHB'` header, same CRC — and silently dropped `build_packet()`'s
`MAX_PAYLOAD_SIZE` check. `build_packet()` already accepts raw bytes, so the
duplicate had no reason to exist. Deleted.

### D10 — The launch reference was taken from the worst fix of the flight

Found only once the GPS antenna went on, which is the point of testing with one.

`ZoneManager._capture_launch_point()` fired on the very first fix with
`fix_type >= 2`. That fix is the least accurate one a receiver will produce: it
has just met the minimum satellite count and its altitude solution is still
converging. Measured on the bench, stationary on a desk throughout:

```
first 3D fix:    202.0 m MSL,  6 satellites
two minutes on:  172.9 m MSL, 10 satellites
```

29 metres of drift without the payload moving. Every AGL figure this class
produces is measured against the launch altitude, so that error propagates into
the landed-altitude threshold, the cruise altitude override, and the arming
height for landing detection. The payload was reporting **−37 m AGL** while
sitting on a bench.

The reference is now provisional at first — nothing waits on it, and the zone
logic works immediately — and refined over `zone_launch_settle_sec` (default
180 s) while the payload is still on the pad. Two details:

- **The median of the *later* half of the samples, not all of them.** A receiver
  acquiring satellites produces a converging series, not noise scattered about a
  true value, so the median of the whole window sits in the middle of the
  convergence. Better than the first fix, still wrong. The later half is closer
  to converged and still a median, so one wild sample cannot drag it.
- **Refinement stops the moment the payload moves** more than
  `zone_launch_settle_max_drift_m` (default 50 m). Movement means it launched,
  and refining then would drag the reference along with the balloon. It
  finalises on whatever was gathered while stationary, which still beats the
  first fix.

Measured against the real bench series:

| Settling window | Launch altitude | AGL error |
|---|---|---|
| 0 s (previous behaviour) | 202.0 m | 29.1 m |
| 90 s | 186.3 m | 13.4 m |
| **180 s (new default)** | **173.3 m** | **0.4 m** |

An operator who typed in launch coordinates is never second-guessed; refinement
only ever touches a point the payload captured itself.

### Noted, not changed

- **Negative altitudes clamp to zero.** `TelemetryPayload` serialises altitude as
  an unsigned millimetre count, so a launch below sea level — the Dead Sea,
  Death Valley, much of the Netherlands — reports 0 m. Fixing it means changing
  the wire format in Python, Swift and the modem firmware simultaneously, which
  is not worth it for the launch sites this will realistically see. Recorded so
  the next person does not have to rediscover it.
- **`RateLimiter` in `airborne/utils.py` is unused.** Converted to the monotonic
  clock with everything else rather than deleted, since it is harmless and
  someone may want it.

### Verification on hardware

All of it was re-run against the real payload — Pi Zero 2 W, L76K with antenna,
Waveshare HF HAT.

**The clock step is real, and it was measured on this boot.** No RTC
(`timedatectl` reports `RTC time: n/a`), and `systemd-timesyncd` stepped the
clock 44.6 s into the boot:

```
wall clock at first kernel line : 12:17:59
wall clock at NTP sync          : 12:21:52
apparent elapsed (wall)         : 233 s
actual elapsed (monotonic)      : 44.6 s
>>> clock stepped FORWARD by      188 s
```

188 s against a 60 s watchdog timeout. A `TX timeout or failed` warning appears
in the same second as the sync — the SX1262 TX-done wait tripped by that same
step, on the old code, live. Whether the old watchdog would actually have
rebooted is a race: it checks once a second, and the main loop pets far more
often than that, so most of the time the payload wins. The window where it does
not is the pause cycle, which pets every 15 s. Worth stating plainly rather than
claiming a guaranteed reboot — but the step is three times the timeout, and the
race is one the payload should not be running at all.

**The 2D/3D fix, caught live.** The payload's own telemetry across the antenna
being connected:

```
 #   fix_type      sats  altitude    latitude
 1   1 = FIX_2D      6     202.0   51.5011077
 2   2 = FIX_3D      6     201.4   51.5011052   <-- CHANGED
...
85   2 = FIX_3D     10     173.5   51.5011877
```

Row 1 is exactly the case: a 2D solution carrying an altitude. Under the old
code it was labelled `FIX_3D` and would have opened the region switch, the zone
change and the beacon. The same real sentences replayed through both trees:

```
OLD (installed)  receiver reports 2D -> fix_type=FIX_3D  gates opened: region switch + zone change + beacon
NEW (reviewed)   receiver reports 2D -> fix_type=FIX_2D  gates opened: none
```

The launch point was correctly captured from the first *3D* fix, not the 2D row.

**The real receiver sends two GSA sentences per position cycle** (GPS and
GLONASS), which is the multi-constellation case the fix has to get right, and
its GSA carries 19 fields — NMEA 4.10 appends a system ID after VDOP. Both
handled: `parts[2]` for the mode and `parts[15..17]` for the DOPs land correctly
on the real sentence.

**Sealed telemetry** writes one file on the Pi, and 11 records (header plus ten
rows) decrypt correctly with the matching private key.

**The launch-point refinement, running live on the real receiver:**

```
12:45:56  Launch point captured from first fix: 51.50117, -0.10865 at 164 m MSL; refining for 180 s
12:48:57  Launch point settled over 180 s (81 fixes, 41 used): 51.50118, -0.10868 at 165 m MSL (+1 m vs the first fix)
```

Reported AGL went from **−37 m** on the old code to **−0 m**. The correction
itself was only +1 m on this run, and that is worth being precise about: the
receiver was hot-started, already holding ten satellites from the previous run,
so there was almost nothing to converge. The 29 m figure came from the cold
start earlier in the session — which is the condition a launch day actually
presents. The mechanism is what was verified here; the size of the correction
depends on how cold the receiver is.

**The payload runs.** Camera, radio, GPS, fountain encoder, zone scheduling; no
watchdog trips and no restarts across the test.

739 tests pass, 36 of them new.


---

## Ground station scan (2026-08), and the "ERR" on the T190 display

### What ERR actually was

The TFT shows `ERR` as `packetsRejectedCrc + packetsRejectedNoRapt`, which
merges two unrelated causes. Reading the modem's serial `[STATS]` line
separates them, and on the bench unit:

```
Total:955128 Fwd:940129 NoRAPT:3 BadCRC:14996 Err:0
```

So it is **entirely CRC failures**. Not false sync detections, not radio
errors, not lost interrupts — the radio hears a well-formed RAPTOR packet and
the checksum over the body does not match. Rate: **1.20%** measured over 7412
packets.

### Where the corruption is, which narrows the cause a lot

The hardware sync word is `RAPT`, so a packet is only counted at all if its
sync matched cleanly. The software then checks the *next* four bytes. If bit
errors were random — thermal noise, a weak signal, front-end overload — they
would land uniformly, and those four bytes would be hit about 4/44 of the time:
roughly 1360 of the 14996 failures.

**Observed: 3.** Errors are about 450× rarer at the start of a packet than
uniform noise would produce. Whatever is corrupting these packets accumulates
*through* the packet rather than striking at random.

That fits two mechanisms, and the measurements so far cannot separate them:

- **Sampling-clock drift.** Whitening is disabled on both the payload
  (`whitening = 0x00` in `radio.py`) and the modem. 94% of the traffic is
  48-byte telemetry, and those packets carry a **median longest identical-byte
  run of 9 bytes** — about 72 bit periods with no transition for the
  receiver's clock recovery to work from. This is the failure mode already
  recorded above under "the all-zeros test artifact", where the conclusion was
  that it "argues for enabling whitening if a future payload type ever carries
  long runs of constant bytes". Telemetry is that payload type.
- **Buffer overwrite during read.** The radio is in continuous receive, so a
  packet arriving while `readData()` is in progress can overwrite the tail of
  the one being read.

Removing a needless SPI transaction from between `readData()` and
`startReceive()` — see the SNR fix below — moved the rate from **1.201% to
0.916%** over 14951 packets. Real, and much too small to be the whole story. A
first short sample suggested it had fixed the problem outright; a proper
three-minute measurement showed it had not, which is the only reason that is
not written here as a fix.

### Confirmed: it was whitening

The A/B test, run over the USB gadget once the payload was reachable again:

| | Packets | Bad CRC | Rate |
|---|---|---|---|
| Whitening off | 14951 | 137 | **0.916%** |
| Whitening on | 16690 | **0** | **0.000%** |

Same hardware, same modem firmware, back to back. Not "improved" — no CRC
failures at all, and no rejected packets of any kind.

Whitening is now on in the payload and in all eight firmware builds. **This is
a wire-format setting: every modem must match the payload or the link does not
work at all.** A mismatch does not degrade it — the sync word is not whitened,
so packets arrive cleanly and then fail every content check. That is exactly
what the failed first attempt looked like: `Fwd 0, NoRAPT 1102`.

The Meshtastic slot on the dual-E22 is deliberately left alone. Meshtastic
defines its own on-air format, and changing it would make the balloon
unreadable by every stock radio, which is the whole point of using it.

### Why the first attempt looked like a failure

The first test showed the link dead with whitening set on both ends, which
looked like the two implementations not interoperating. Reverting only the
modem brought the link straight back — which meant the payload had never
applied whitening at all.

`radio.py` had **two** `SET_PACKET_PARAMS` call sites. One in
`_set_fsk_packet_params()`, and one in `_update_payload_length()`, which the
transmit path calls before every single packet, carrying its own hardcoded
literals and a comment reading `# CRC-8 (must match _set_fsk_packet_params)`.
A comment is not a mechanism. They did not match, and the transmit path
overwrote the configured whitening bit microseconds after it was set — so the
change reached the radio's configuration and never reached the air.

There is one definition now, `_fsk_packet_params(payload_len)`, used by both.

A second difference mattered once that was fixed: RadioLib writes the whitening
seed explicitly (0x01FF, preserving the 7 MSBs of the register, per a datasheet
note saying receive stops working otherwise), and the payload never wrote it at
all. `_set_whitening_seed()` now does, the same way.

### SNR was never a measurement

`SX126x::getSNR()` returns `RADIOLIB_ERR_WRONG_MODEM` when the active modem is
not LoRa. That constant is **the integer −20**. The firmware stored it in a
float, printed it on the display as `-20.0 dB` in red — the red because the
colour test is `lastSnr > 5 ? good : lastSnr > 0 ? warn : bad` — and forwarded
it over USB in every frame.

Confirmed against the bench modem: **2281 frames out of 2281 reported exactly
−20.0**. Every SNR figure the ground station has ever shown or logged for the
image downlink was this error code.

GFSK has no SNR to report. The modem now sends −128, a value no real link can
produce, and the display shows `n/a`. The Meshtastic slot on the dual-E22 runs
LoRa and does report a real SNR, so it is unchanged — that distinction is the
whole point.

### Other defects found and fixed

- **A failed reconfiguration left the modem deaf and said `CFG_OK`.**
  `initializeRadio()`'s return value was discarded after a `CFG:` command.
  Settings can pass the firmware's range checks and still be refused by the
  radio, and the modem would then answer `CFG_OK` and hear nothing. It now
  checks, rolls back to the previous settings, and reports `CFG_ERR` — the same
  class of silent deafness recorded as R4 above.
- **The USB command buffer had no length limit.** `usbBuffer += c` grew without
  bound until a newline arrived. Anything that streams bytes without one — a
  wrong app, a stuck sender — would exhaust the heap from the far end of a
  cable. Capped at 128 bytes, resynchronising on the next newline.
- **The macOS app could not decode a dual-radio modem.** Same de-stuffing gap
  as the Python ground station: `0x7D 0x5B` was an unknown escape, so 55% of
  image packets failed. Fixed in `SerialPortManager.swift`.

### Noted, not changed

- `PRE: 32 bits` on the display is the *transmit* preamble length. RadioLib
  takes the receive preamble-detector length as a separate argument, so on a
  receive-only modem this number describes nothing that affects reception.
- The payload transmits `RAPT` twice — once as the hardware sync word, once as
  the first four bytes of the packet body. Four bytes per packet, and it is
  what makes `NoRAPT` such a useful diagnostic, so it stays.
- The payload enables a 1-byte hardware CRC on transmit while the modem sets
  `setCRC(0)`. The radio therefore does no error detection of its own and every
  corrupted packet reaches software, where the CRC-32 catches it. Harmless as
  it stands, and it is what makes the failures countable.

---

## macOS app review (2026-08)

18,851 lines across 42 files. Six defects fixed, and several suspicions checked
and dismissed — recorded below, because "I looked and it was fine" is worth as
much here as a fix.

### M1. Meshtastic decoding ran on the serial read thread

`SerialPortManager.readLoop()` runs on a dedicated `Thread`, and the Meshtastic
link I added the previous day mutated `@Published` counters and invoked its
packet callback straight from it. SwiftUI requires main-thread mutation; off it,
the behaviour is undefined and the main-thread checker flags it.

Decoding stays on the read thread — it is real work and does not belong on the
main queue — and only the counters and the callback hop.

### M2. Transmitting froze the interface for up to five seconds

`sendRaw` blocked its caller waiting for the modem's `MTX_OK`/`MTX_ERR`. Called
from a SwiftUI button, that is the whole timeout with a frozen window. The wait
was the right design; doing it on the caller's thread was not. It now runs on
its own serial queue, with completion-handler and `async` forms, so two sends
also cannot interleave and collect each other's replies.

### M3. A timer per view appearance, never cancelled

`GPSSettingsView.onAppear` created a repeating `Timer.scheduledTimer` and
discarded it. The run loop keeps a strong reference, so it lived forever and
kept firing after the view was gone — and `onAppear` runs *every* time the view
appears, so navigating away and back left another one running each time. They
accumulated, all toggling the same `@State` at 1 Hz, and each toggle rebuilt the
whole view through `.id()`.

Replaced with a `Timer.publish(...).autoconnect()` bound to the view, which
SwiftUI cancels when the view goes away.

### M4. Surveying an SD card blocked the interface

`CardImportManager.read()` is on a `@MainActor` class and asks the filesystem
for each file's size and modification date — **one stat syscall per file**. The
recovered card in this project held 710 images, over USB. Every one of those
was blocking the UI.

The walk touches no instance state, so it moved out wholesale to a
`nonisolated static func` run from a detached task; the key comparison stays on
the main actor because it needs the loaded private key. `busy` was already
wired to the UI, so the progress indicator now actually means something.

### M5. A force cast on data from a device driver

`IORegistryEntryCreateCFProperty(...).takeRetainedValue() as! String` in port
enumeration — which runs at launch and on every refresh. A device that does not
describe its callout path as a string would crash the app rather than be
skipped. Now `as?`.

### M6. Payload deserializers assumed a zero-based `Data`

`TelemetryPayload.deserialize` and its three siblings index with `data[offset]`
and `subdata(in: offset..<...)`, both of which use **absolute** indices. Hand
any of them a `Data` slice and they read the wrong bytes or trap.

Every caller today passes a `Data` built by `subdata()` or `Data(ArraySlice)`,
both of which re-base, so this is latent rather than live. Fixed anyway: one
`let data = Data(data)` per function. The failure mode is a crash on a received
radio packet, and nothing in the signature warns a future caller.

### Checked and found fine

- **22 unaligned `load(as:)` calls.** `withUnsafeBytes { $0.load(as: UInt32.self) }`
  on a `Data` is a known Swift trap — `load` requires alignment the pointer may
  not have. Tested it: 66,000 parses at every offset, including slice-derived
  buffers, produced **zero** mismatches. `subdata(in:)` copies into a fresh
  allocation and arm64 tolerates unaligned access anyway. Technically undefined,
  demonstrably harmless here, and not worth churning 22 call sites over.
- **257 `@Published` writes with no visible main-queue hop.** Almost all are in
  classes marked `@MainActor`, where the compiler guarantees isolation. The raw
  `Thread` code — `SerialPortManager` — hops correctly on every one. The only
  real offender was M1, which I had written myself.
- **`FlightGraphsView` dividing by `data.count`.** Guarded by a
  `guard data.count >= 2` early return two blocks above.
- **`PositionFusion.trackCoordinates` indexing by a computed stride.** The
  guard that gets there forces stride > 1, which keeps the maximum index inside
  the array.
- **sqlite in `OfflineMapManager`.** Three prepares, three finalises, one open,
  one close.
- **No `try!`, no `fatalError`,** and the published history arrays are bounded.


### M7. "Auto" connected to the first serial port and called it a modem

Opening a serial port succeeds for any device on it, so `autoConnect()` --
"connect to the first port whose name contains usbmodem" -- would settle on the
wrong one, report success, and receive nothing.

Not hypothetical on this desk. The payload's USB gadget presents a CDC-ACM
console, so the machine has **two** `cu.usbmodem*` ports:

```
/dev/cu.usbmodem1101 : 28025 bytes, 188 frames in 3 s  -> the modem
/dev/cu.usbmodem21401:     0 bytes,   0 frames          -> the payload console
```

`availablePorts` is sorted, so it happened to pick the right one. Alphabetical
luck, not intent — a payload that enumerated lower would have taken the
connection and left the operator with a connected-looking app and no data.

Auto-connect now opens each candidate and listens: a framed packet or a line
only the firmware prints settles it, silence moves on. It is asynchronous
because that listening is real waiting and it is called from a button, and it
reports *why* it failed — "no serial ports found" and "opened three, none of
them a modem" send you to different places.

---

## Python ground station review (2026-08)

14,807 lines. Five defects fixed, several suspicions checked and dismissed.

### P1. The web server could not start at all

`WebSerialManager` was constructed with
`ModemMeshtasticLink(writer=self.write)` — and had no `write` method. Every
attempt to start the web UI raised `AttributeError` before construction
finished. Introduced by my own Meshtastic work the previous day, and invisible
because nothing in the suite ever instantiated `WebServer`.

`tests/test_python_gs_web.py` now constructs it, exercises `write()`, and
serves a page, so this cannot repeat quietly.

### P2. The web UI listened on every interface, unauthenticated

`--host` defaulted to `0.0.0.0` and `WebServer.__init__` matched it. There is
no authentication anywhere, in front of thirty state-changing endpoints:
stopping the receiver, rewriting the radio configuration, deleting recorded
flights.

Now defaults to `127.0.0.1`. Binding wider still works — a phone on the same
network is a genuinely good second screen at a launch site — but it has to be
asked for, and prints what it is exposing when it happens.

### P3. Card import would write anywhere

`/api/card/import` took its output directory from the request body, with an
`overwrite` flag, and wrote decrypted flight data there. Combined with P2 that
is an arbitrary file write for anyone on the network. Confined to the
`recovered` folder; absolute paths are neutralised into it and traversal is
refused.

### P4. Local elapsed time measured on the wall clock

The same class of defect that hit the payload, and it matters here for the same
reason — the ground station is meant to run on a Pi, which has no RTC.

| | Effect of a clock step |
|---|---|
| `payload_link` request timeout | Backward step parks the caller on a condition variable far past the timeout |
| `meshtastic_mqtt` keepalive | Backward step suppresses pings; the broker drops the connection |
| `audio_alerts` silence detection | Forward step fires a spurious "signal lost"; backward step suppresses a real one |

All three are now monotonic.

`position_fusion` deliberately keeps the wall clock and was left alone: its
timestamps arrive from remote Meshtastic nodes and MQTT gateways as epoch
times, so they have to be comparable to one. `reconcile_timestamp` already
guards the skew that matters there.

### P5. Surveying a card blocked the Qt event loop

`card_tab.read_card()` called `survey_card()` inline, which walks the images
and logs directories and stats every file — 710 of them on the card in this
project, over USB. The import beside it already ran in a `QThread`; the survey
did not. Now it does.

Exactly the same defect as the macOS app's `CardImportManager.read()`, found
independently in both.

### Checked and found fine

- **Qt threading.** Core never touches widgets directly and communicates by
  signal, 63 emissions. The discipline is right.
- **`mission_id` from the URL into `load_mission`/`delete_mission`.** Looked
  like a path-traversal delete. It is not: both iterate the missions directory
  and match an `id` field inside each `mission.json`, so the URL value never
  builds a path.
- **`send_from_directory` for images.** Werkzeug's `safe_join` handles
  traversal.
- **Resource lifecycle.** Threads joined, ports closed, and `offline_maps`
  explicitly closes sqlite connections in a `finally` — with a comment
  explaining that sqlite's own context manager does not.
- **Memory growth.** Telemetry bounded to 5000 points, messages to 100.
- **The 14 bare `except:`** are around `close()` in teardown paths, where
  swallowing is the right behaviour.
- **`MeshtasticManager.send_text`**, which the UI does call, does not block —
  it writes and returns.


### Correction: tools/ was never missing from an install

I claimed `find_payload.sh` "only existed in a checkout" and added an explicit
copy loop to the installer for it. That was wrong: `install.sh` rsyncs the
whole `payload/` tree to `/opt/raptorhab`, excluding only `.venv`, `__pycache__` and
`tests`, so everything under `tools/` has always shipped. The file was absent
from the bench Pi because it was written after that Pi was last installed, not
because the installer skips it. The loop has been removed — a second mechanism
doing the same job is a thing to keep in sync for no gain.

---

## Ground station link failure — the modem sends, the app hears nothing

Found on hardware: the T190 reported **27,289 packets forwarded** (22,856
telemetry, 4,433 image, 3 CRC errors, 100.0% success) while the macOS app
showed **64 packets received, 0 images**, and then "signal lost" with the
modem still connected and still forwarding.

Three faults, all in the framing layer, compounding.

### G1. Status text was split off by scanning the binary stream for newlines **[FIXED]**
`SerialPortManager.processReceivedData` called `extractTextLines()` on the raw
buffer before any frame parsing. It deleted everything up to and including
each `0x0A`.

`0x0A` is an ordinary data byte inside a frame and the modem does not escape
it. A 240-byte image frame contains one about **61%** of the time; a 57-byte
telemetry frame about **20%**. Worse, the scan removed everything *before* the
newline too, so one poisoned frame took its neighbours with it.

This is why **no images arrived at all** while telemetry trickled through:
image frames are four times the size, so they are far likelier to contain the
byte that destroyed them.

Measured on a 4,000-frame stream of real packets: **4,000 recovered with the
newline pass removed, 0 with it in place.**

Fix: the frame scan is what separates the two streams. Bytes inside a frame
are frame bytes; whatever falls outside one is text.

### G2. A one-byte slip silently killed the link for good **[FIXED]**
Frames are delimited at both ends:

```
0x7E <frame> 0x7E   0x7E <frame> 0x7E
```

A scanner that loses one byte pairs a *closing* delimiter with the next
frame's *opening* one. It then extracts the two-byte gap as an empty frame,
lands on frame data with no opening delimiter, discards to the next delimiter
— and repeats, forever. There is no self-correction: every subsequent frame is
dropped in silence.

Measured: starting the parser one byte early took **4,000 valid frames to 0**.

That is the "signal lost": G1 corrupts the stream, the parser flips parity,
and the app never receives another packet even though the modem keeps sending.

Fix: validate a candidate against its own length field *and* checksum **before
consuming any of it**. The modem XORs every byte it sends into the checksum,
so a whole valid frame XORs to zero. A candidate that fails costs exactly one
byte, so a false start can no longer swallow the real frames inside it.

### G3. `0x7B` was honoured as a frame start on a modem that never escapes it **[FIXED]**
The dual-radio board carries Meshtastic on a second delimiter, `0x7B`, and
escapes that byte inside every frame. The single-radio firmware did not — it
has one radio and never opens a `0x7B` frame. But both parsers watch for both
delimiters, so a raw `0x7B` in image data (about **58%** of image packets) was
a frame start waiting to be mistaken for one, reachable whenever the parser
was already off alignment.

Fix: the single-radio firmware now escapes `0x7B` as well, so one wire format
covers every board and the parser needs no knowledge of which it is talking
to. G2's validation makes the case safe regardless, so an app built from this
commit recovers the link **without reflashing the modem**.

### What changed
- `groundstation/macos/RaptorHabGS/FrameScanner.swift` — new. The framing now
  lives in a dependency-free type that can be tested directly; the bug it
  replaced discarded every frame in silence, which is precisely the failure a
  unit test catches and a hardware session does not.
- `groundstation/macos/RaptorHabGS/SerialPortManager.swift` — delegates to it.
- `groundstation/python/raptorhabgs/core/protocol.py` — same validate-before-
  consume fix. The Python GS never had G1.
- `firmware/gs-modem/src/main.cpp` — escapes `0x7B`.

### Tests
- `payload/tests/test_frame_resync.py` — 13 tests over real telemetry and
  image packets: newlines inside frames, image frames specifically, recovery
  from a one-byte slip and from five different starting offsets, a dropped
  mid-stream fragment, interleaved status text, and 200 KB of noise yielding
  nothing. Seven fail against the previous code.
- `payload/tests/test_swift_parity.py` — four more that compile and run the
  real `FrameScanner.swift` against streams Python builds.

### G4. Runtime `CFG:` froze the modem — the radio was rebuilt on a live bus **[FIXED]**
Reconfiguration called `initializeRadio()`, which constructs the SPI bus and
radio objects. Fine at boot; at runtime it built a **second** `SPIClass` on
the same live bus and orphaned the object the DIO1 interrupt was attached to,
leaving the radio in standby forever. The app sends `CFG:` on every connect,
so the modem froze the moment the app attached — unless that connect happened
to land in the boot-time configuration window, which is why the first
connection of a session sometimes survived and reconnects never did. RX
counters flat, nothing forwarded, only a power cycle recovered it.

Fix: construction (`initializeRadio`, once) is now separate from
configuration (`configureRadio`, re-runnable, always leaves the radio
listening). Verified against the T190: `CFG:` sent mid-stream twice, answered
`CFG_OK` both times, ~500 frames per 4 s before, during, and after.

### G5. A stalled USB host wedged the whole modem **[FIXED]**
`forwardPacket` wrote each frame one byte at a time — ~240 `Serial.write()`
calls plus two blocking `flush()`es — into the ESP32-S3's hardware USB CDC,
whose writes wait on the TX ring buffer. A host that stopped draining stalled
the loop that also services the radio: the modem went deaf as well as silent.
Frames are now built in memory and handed over in one write with the TX
timeout at zero. A slow host costs dropped frames — counted, and shown on the
display as `DROP:` — never a wedged modem. RaptorQ makes dropping the right
failure: the ground station reconstructs from any sufficient subset.
`FWD` now counts only frames actually accepted by USB, so `RATE` is truthful.

### G6. App UI: nested TabView and a stray SD Card pane **[FIXED]**
Opening the Meshtastic tab made every other tab vanish: `MeshtasticView`
wrapped its Nodes/Messages/Channels sections in a nested `TabView`, and on
macOS the inner one takes over the window's tab strip. Replaced with a
segmented control. Separately, `CardView` sat *outside* the `TabView` block —
a brace in the wrong place — so the SD-card pane rendered below every tab
instead of being the tab it was meant to be.

### G7. Read loop capped at half the payload's peak rate **[FIXED]**
The app read 1 KB then slept 20 ms unconditionally — a 51 kB/s ceiling, and
it slept even with bytes queued. The payload peaks near 100 packets/s. Now
16 KB reads, back-to-back while data is waiting, `poll()` when idle. The app
also displayed the GFSK SNR sentinel (−128, "not measured") as a red reading;
it now shows `n/a` like the modem and the Python GS.

### G8. Display: USB statistics read as radio problems **[FIXED]**
`RATE` and `DROP` on the modem screen described the USB side — frames the
attached host accepted — but sat beside RSSI and packet counts, where they
read as Pi-to-ground-station link quality. Removed from the display; both
remain in the serial `[STATS]` line, which is where diagnosis happens.

### G9. NoIR camera: purple images, and a tuning that silently did not load **[FIXED]**
The payload's camera is a NoIR (no infrared-cut filter), which reports the
same sensor ID as the filtered IMX219 and cannot be detected in software.
Under the standard tuning, infrared leaks into all three colour channels and
drags auto white balance to magenta — measured gains (1.11, 2.51), the
purple cast on every image.

Three findings stacked underneath:

1. **libcamera's camera manager is a process singleton** that reads the
   tuning environment exactly once. A second `Picamera2` constructed with a
   different tuning silently keeps the first one's — so per-photo tuning
   swaps are impossible, and even a *first* construction is poisoned if
   `global_camera_info()` ran earlier, because that call creates the
   manager. The payload shipped purple "natural" frames with `variant=noir`
   in the log. The sensor is now identified in a subprocess, keeping this
   process's manager unborn until the tuning is chosen.
2. **`AwbEnable: True` does not undo manual `ColourGains`** — the documented
   release is gains of zero. Verified on the bench: without it, AWB never
   resumes.
3. **AWB takes seconds to reconverge after release**, so the release happens
   immediately after the infrared capture, not before the natural one. The
   half-minute between captures does the converging invisibly.

`camera_tuning` (standard | noir | alternate) selects the render. Alternate
mode takes one infrared frame and one colour-balanced frame in turns — same
schedule, a control change per photo instead of a camera rebuild. The
infrared render uses fixed gains (1.1, 2.5), the values the purple cast was
made of, so infrared frames are rendered identically and comparably.
Verified on the payload: per-capture metadata shows gains alternating
between the manual pair and greyworld's converged point.
