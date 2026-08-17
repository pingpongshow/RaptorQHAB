# RaptorHAB — Code Review Findings

Baseline commit: `c02134b`. Review focused on the airborne payload and shared
`common/` layer (the code this project's next phase modifies), with a lighter
pass over the ground station and macOS app.

Severity: **A** = will bite you in flight · **B** = wrong behavior, recoverable ·
**C** = hygiene / latent

---

## A — Flight-critical

### A1. Error-state recovery is dead code — the payload never reboots
`Pi/airborne/main.py:311-314` increments `_error_count`, calls
`_set_state(State.ERROR_STATE)` and `break`s out of the main loop. Control then
falls to `finally: self._cleanup()` and the process exits. `_handle_error_state()`
(`main.py:520`), which contains the `reboot_on_fatal_error` logic, is **never
called from anywhere**. Same in `start()` (`main.py:152-156`).

Net effect: after 10 accumulated errors the payload silently stops transmitting
for the rest of the flight. `reboot_on_fatal_error: bool = True` is a lie.

Fix: call `_handle_error_state()` on the ERROR_STATE transition, and rely on
systemd `Restart=always` as the real backstop rather than `os.system("sudo reboot")`.

### A2. `_error_count` never resets
`main.py:309`. A single transient SPI glitch every few minutes across a 3-hour
flight will accumulate to `_max_errors = 10` and trip A1. Errors should decay
(reset on N consecutive successful cycles, or use a sliding window).

### A3. Watchdog timeout is hardcoded and `watchdog_enabled` is ignored
`main.py:168-172` passes `timeout_sec=60` literally instead of
`self.config.watchdog_timeout_sec`, and never checks `self.config.watchdog_enabled`.
The config fields are decorative.

### A4. Watchdog firing does nothing
`_watchdog_triggered()` (`main.py:532`) sets `ERROR_STATE`, but the main loop
never reads `self._state` — it only exits on `_shutdown`. A genuinely hung loop
stays hung. The watchdog needs to either set `_shutdown` (letting systemd
restart) or drive the hardware watchdog at `/dev/watchdog`.

### A5. Import-time `os.makedirs` on an absolute root path
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

---

## B — Wrong behavior

### B1. `IMAGE_META` interval slot never fires
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

### B2. `_images_transmitted` counts queued, not transmitted
`main.py:405` increments after `scheduler.add_image()`, which is a *queue*
operation that can silently return `False` when the scheduler's
`Queue(maxsize=5)` is full (`packets.py:159`). The return value is discarded, so
a dropped image is reported as transmitted and the `ImageInfo` is lost.

### B3. Bare `except:` swallows everything on image enqueue
`main.py:433-436`. Catches `KeyboardInterrupt`/`SystemExit` too. Should be
`except queue.Full:`.

### B4. Uplink command path is entirely dead
`Pi/airborne/commands.py` (604 lines) defines a full `CommandHandler` with
`CMD_PING` / `CMD_SETPARAM` / `CMD_CAPTURE` / `CMD_REBOOT`. Nothing in
`airborne/` imports it — verified by grep. The payload is TX-only in practice.
This is relevant to the new work: the wire protocol for remote configuration
already exists and is untested, and Phase 3 should either wire it up or
deliberately retire it in favor of the USB path.

### B5. `_telemetry_bytes` is an implicit instance attribute
`packets.py:223` assigns it inside `get_next_packet`, and `packets.py:254`
guards with `hasattr`. Declare it in `__init__` as `Optional[bytes] = None`.

---

## C — Hygiene / latent

- **C1** `os.system("sudo reboot")` (`main.py:530`) — use
  `subprocess.run(["systemctl", "reboot"], check=False)`; `os.system` inherits
  the shell and offers no error handling.
- **C2** Config has no persistence layer. Everything is dataclass defaults +
  `RAPTORHAB_*` env vars (`config.py:126-216`), so a setting changed at the
  launch site does not survive a reboot. Phase 1 must fix this before any of the
  new configurable parameters are meaningful.
- **C3** `from_env()` will raise an unhandled `ValueError` on a malformed
  environment value (`float(os.getenv(...))`, `config.py:155`) and take the
  payload down at startup. Needs per-field try/except with a logged fallback.
- **C4** Duplicated defaults: `common/constants.py` and `airborne/config.py`
  disagree (`bitrate` 200000 vs 96000, `fdev` 125000 vs 50000, `tx_period`
  10 vs 2, `max_stored_images` 100 vs 20000). `constants.py` values are unused
  dead weight and will mislead the next reader.
- **C5** `SX1262Config` defaults (`radio.py:156-157`) are a third copy of the
  same numbers.
- **C6** `PacketScheduler.__init__` accepts a `telemetry_callback` that
  `main.py:233` never passes, so the callback branch (`packets.py:258`) is dead.
- **C7** Committed `__pycache__/` and `.DS_Store` throughout — now handled by
  the new `.gitignore`, but `Pi/common/__pycache__/radio_hardware_spi.cpython-313.pyc`
  references a module that no longer exists in the tree, which suggests a
  refactor left something behind.
- **C8** No tests anywhere in the repo. Given a flight system with a one-shot
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
