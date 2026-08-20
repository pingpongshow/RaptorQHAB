# Test Plan

Two arms, split by what each can actually establish.

**Arm A — automated.** Anything measurable from a machine: the suites, the
builds, protocol parity, and behaviour that can be driven over a wire and read
back. Fast, repeatable, and it proves the parts agree with each other.

**Arm B — manual.** Anything that needs hands, eyes, or judgement: does the card
boot, does the app show what it should, does the image look right, does the
antenna stay on. Arm A cannot tell you a photograph is in focus or that a
connector is seated.

Neither arm subsumes the other. Arm A passing means the software agrees with
itself; only Arm B tells you the *system* works.

Every check says what a pass looks like. "Verify it works" is not a check.

---

## Arm A — automated

### A1. Test suites

```bash
cd payload && python -m pytest tests/ -q
```

**Pass:** all passed, 0 failed. The suite covers the payload, both ground
stations, and Swift/Python protocol parity.

*Last run 2026-08-19: 829 passed, 0 failed.*

### A2. Every firmware target builds

```bash
cd firmware/gs-modem && pio run          # seven boards
cd firmware/dual-e22 && pio run          # one
```

**Pass:** eight SUCCESS. A board that stops compiling is a board someone will
try to flash on launch morning.

*Last run 2026-08-19: 8/8 SUCCESS.*

### A3. The macOS app builds

```bash
cd groundstation/macos && xcodebuild -scheme RaptorHabGS -configuration Debug build
```

**Pass:** BUILD SUCCEEDED, no new warnings.

*Last run 2026-08-19: BUILD SUCCEEDED, 0 warnings.*

### A4. Cross-implementation parity

Included in A1, but worth naming because it is the check that catches the
worst class of bug — the two ends of a wire protocol drifting apart, which
never fails loudly:

- Meshtastic packets built in Python decode in Swift, and the reverse
- AES-CTR, channel hash and node-id derivation agree byte for byte
- Sealed recordings written by one open in the other
- Frame stuffing round-trips, including the `0x7D 0x5B` escape

**Pass:** all parity tests green. A failure here means the app and the payload
disagree about the wire, and neither will tell you so at runtime.

### A5. Live link quality, against the real modem

With the ground station modem plugged in:

```bash
python - <<'EOF'
# read /dev/cu.usbmodem*, count 30 s of [STATS] lines
EOF
```

**Pass:** `BadCRC 0`, `NoRAPT 0`, forwarded ≈ received. Anything else means
whitening, framing or RF has regressed — this is the number that went from
0.916% to 0.000%.

*Last run 2026-08-19: 4243 received, 4243 forwarded, BadCRC 0, NoRAPT 0 —
100.00% over 40 s. Note for whoever runs this next: drain the port for two
seconds before counting. The modem's 16 KB TX ring holds stats lines from
before you attached, and a first-versus-last delta across that backlog spans
time when no host was connected — it reads as a catastrophic forwarding rate
that is pure measurement artifact.*

### A6. Payload health over the radio

**Pass:** packet count climbing, images being queued and completed, zone
sensible, no `ERROR` in the journal.

*Last run 2026-08-19: active, NRestarts=0, 0 ERROR lines in 10 min, packets
234,699 → 246,754 across the sample, image renders alternating
standard/noir with measured gains matching each variant.*

### A7. Fresh-install simulation

Run the installer's own verification on the payload:

```bash
sudo /opt/raptorhab/setup/install.sh --check
```

**Pass:** every check green, WiFi power save reported off.

*Last run 2026-08-19: 13/13 ok, `Power save: off`.*

### A8. USB access paths

**Pass:** `ssh raptorhab.local` connects over `usb0`; with DHCP configured,
`ping 10.55.0.1` answers and `ssh 10.55.0.1` connects with nothing configured
on the host. `ping raptorhab.local` failing is **expected** — ping wants IPv4.

*Last run 2026-08-19: `ping 10.55.0.1` 0% loss at 0.57 ms, `ssh 10.55.0.1`
connects, `ssh raptorhab.local` connects.*

---

## Arm B — manual, run by you

The order is deliberate: it is the order in which a mistake becomes expensive.

### B1. From a blank card to a booting payload

1. Flash Raspberry Pi OS Lite (64-bit) with Imager; set username, password, WiFi.
2. Generate the recording keypair, and **save the private half somewhere that
   is not the card**:
   ```bash
   python3 payload/tools/recording_key.py generate
   python3 payload/tools/recording_key.py verify <public key>
   ```
   **Pass:** `matches the private key: yes`.
3. Provision:
   ```bash
   ./payload/tools/provision_sd.sh --camera imx219 --generate-key
   ```
4. Boot it. Wait for the first-run installer.

**Pass:** it comes up on its own, with no keyboard or monitor attached.
**This is the check Arm A cannot do at all** — nobody can automate "the card
was inserted and the thing booted".

### B2. Get into it, both ways

```bash
ssh <you>@raptorhab.local                 # over WiFi or the cable
```
Then pull the WiFi (or take it out of range) and do it again.

**Pass:** reachable both ways. If the second fails, the payload is unreachable
in exactly the situation the cable exists for.

### B3. GPS, with an antenna and a view of the sky

**Pass:** `fix_type = 2` (3D), 7+ satellites, HDOP < 2, and the position is
where you actually are — check it on a map. Then wait for
`Launch point settled` in the log, and confirm reported AGL reads ≈ 0 m.

Watch for `fix_type = 1`: that is a 2D fix, and it is *correct* for the payload
to refuse to act on it.

### B4. The image link, end to end

With the modem on the Mac and the app running:

**Pass:** a complete image arrives and **looks right** — in focus, correctly
exposed, not torn. Checksum failures zero. Arm A can tell you 242 symbols
arrived; only you can tell whether the picture is of the sky or of the inside
of the payload box.

### B5. The apps, driven by hand

- macOS app: connect (**Auto** should find the modem and skip the payload's own
  USB console), watch telemetry update, open each tab.
- Python app: same, plus the web UI at `http://127.0.0.1:5055`.

**Pass:** telemetry updates live, the map moves, images appear, no tab is
blank or throwing. **SNR should read `n/a`** — GFSK has no SNR, and a number
there means old firmware.

### B6. Meshtastic

**Pass:** the balloon's beacon is heard by an **independent** radio — a phone
or a stock node, not the payload's own log. Correct frequency, hop limit 0.

### B7. Recovery rehearsal

Pull the card, put it in the Mac, and open a flight:

**Pass:** images decrypt with your private key and display. Do this **before**
you need it — a keypair that does not work is only discoverable by trying.

### B8. Endurance

Leave it running on the bench for as long as a flight, on the battery you will
fly.

**Pass:** still transmitting at the end, no restarts (`NRestarts=0`), no
thermal shutdown, card not full.

### B9. The pre-launch checklist

Work [PRELAUNCH.md](PRELAUNCH.md) top to bottom on the real payload.

**Pass:** every box ticked, including the ones about antennas and airspace that
no amount of software testing substitutes for.

---

## What neither arm covers

Worth stating so it is not mistaken for coverage:

- **Flight.** Nothing here has been to altitude. Cold, low pressure, and a
  swinging antenna are not simulated.
- **The dual-E22 board**, which does not exist yet.
- **Meshtastic uplink commands in flight** — tested on a bench, never at range.
- **The WiFi fault**, which is instrumented and waiting to recur.
