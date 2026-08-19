# Pre-Launch Checklist

Work top to bottom. Each item says how to check it, not just what to check —
"GPS working" is not a check, "`fix=3D` and the launch point has settled" is.

The order matters in two places. **Keys before first boot**, because images
sealed to a key you do not hold are not hard to read, they are impossible.
**Link test before the balloon is sealed up**, because a bad antenna connection
is a five-minute fix on the bench and a lost flight in the air.

> **Antenna first, always.** The payload transmits on boot. Never power it
> without both antennas fitted — transmitting into an open port can destroy the
> PA, and then nothing else on this list matters.

### Getting into the payload

Most checks below are run on the Pi. One command, whether it is on WiFi or only
on the USB cable:

```bash
ssh <your-username>@raptorhab.local
```

The payload advertises itself over the USB link too, so this works with nothing
configured on your machine and no administrator password — which is what you
want at a launch site, when the field WiFi is not yours.

Two things that will fool you into thinking it is dead when it is not:

- **`ping raptorhab.local` fails while `ssh` works.** `ping` asks for an IPv4
  address; only the IPv6 one is usable over the cable. Use `ping6` if you want
  to check.
- **`ssh 10.55.0.1` does not work** without hand-configuring your machine.
  Nothing on the cable hands out addresses.

If mDNS is unavailable — Windows without Bonjour, two payloads on one bench —
`./payload/tools/find_payload.sh <your-username>` prints the exact command.

---

## A. Before the card goes in the Pi

### A1. Generate the recording keypair

Images and telemetry are sealed to a public key as they are written. The
payload holds only the public half and cannot read its own recordings back.

```bash
python3 payload/tools/recording_key.py generate
```

- [ ] Keypair generated
- [ ] **Private key saved somewhere that is not the payload and not the SD
      card** — a password manager, a second machine, printed and in a drawer
- [ ] Private key backed up in a *second* place

> Losing the private key means the flight's imagery is gone. Not
> difficult-to-recover — gone. There is no recovery path and nothing warns you
> until you try to open the files after the flight.

### A2. Verify the public key against the private half

Do not skip this. A key that is present but wrong fails exactly like a key that
is right — silently, until the flight is over.

```bash
python3 payload/tools/recording_key.py show
```

```bash
python3 payload/tools/recording_key.py verify <the public key the payload will use>
```

The second command answers the only question that matters:

```
valid X25519 public key, fingerprint 4f2a…
matches the private key at ~/.raptorhab/recording_key: yes
```

- [ ] `verify` prints **`matches the private key: yes`**
- [ ] Fingerprint noted — the payload logs the same one at startup (§B1)

> `matches: NO` means the payload will produce recordings you cannot open.
> The command exits non-zero and says so; believe it.

### A3. Provision the card

```bash
./payload/tools/provision_sd.sh --camera imx219 --generate-key
```

Provisioning refuses to enable encryption without a keypair it has confirmed
you hold, so this cannot quietly produce an unreadable flight.

- [ ] Public key written to the payload config
- [ ] `recording_encryption_enabled = true`
- [ ] Callsign set
- [ ] Region set for where you are actually flying

---

## B. First boot, on the bench

### B1. Service comes up clean

```bash
systemctl status raptorhab-airborne
```

- [ ] Active, `NRestarts=0`
- [ ] No `ERROR` or `CRITICAL` in the first two minutes of the journal
- [ ] Encryption line in the log names the fingerprint you expect from A2

### B2. Installer verification

```bash
sudo /opt/raptorhab/setup/install.sh --check
```

- [ ] All checks pass
- [ ] WiFi power save reported **off**
- [ ] WiFi helper and sudoers rule installed (needed for §E)

### B3. Storage

- [ ] At least 2 GB free on the card
- [ ] Images appearing in `/var/lib/raptorhab/images/` with a `.rhs` suffix
- [ ] Telemetry log in `/var/lib/raptorhab/logs/`, **one file**, `.rhs` suffix
- [ ] Old flights cleared off, or you have deliberately kept them

---

## C. GPS

### C1. Get a real 3D fix

Outdoors, or at a window with a clear view. A cold receiver takes minutes.

```bash
python3 /opt/raptorhab/tools/gps_doctor.py
```

```bash
journalctl -u raptorhab-airborne -f | grep -iE "fix|launch point"
```

- [ ] **`fix_type = 2` (3D)** — not 1. A 2D fix reports an altitude it did not
      solve for. The payload distinguishes them; a 2D fix will not capture the
      launch point, will not switch region, and will not arm the WiFi cutoff.
- [ ] **At least 7 satellites**, ideally 9+
- [ ] **HDOP below 2**
- [ ] Position is actually where you are — check it on a map

### C2. Let the launch point settle

This is the step people skip. The receiver's first 3D fix is its worst: on the
bench, 202 m with 6 satellites and 173 m with 10, two minutes later, without
moving. Every AGL figure for the whole flight is measured against this
reference.

Wait for the second line:

```
Launch point captured from first fix: 51.50117, -0.10865 at 164 m MSL; refining for 180 s
Launch point settled over 180 s (81 fixes, 41 used): 51.50118, -0.10868 at 165 m MSL
```

- [ ] **"Launch point settled" has appeared** — not just "captured"
- [ ] Settled altitude is within ~15 m of the site's known elevation
- [ ] Reported AGL reads about **0 m** while the payload sits on the ground

> If you move the payload more than 50 m before it settles, it freezes the
> reference where it is. Set it down where it will launch from, then wait.

### C3. Antenna

- [ ] GPS antenna connected and will stay connected through launch
- [ ] Antenna has sky view in the flight configuration, not just on the bench

---

## D. The radio link — both directions

### D1. Image downlink, end to end

With the ground station running and the modem connected:

- [ ] A complete image arrives and renders
- [ ] **Checksum failures: 0**
- [ ] RSSI sane at bench range
- [ ] **SNR reads `n/a`** — GFSK has no signal-to-noise measurement, and the
      modem says so rather than inventing one. A number here means the modem is
      running firmware from before that was fixed, and every SNR it reports is
      an error code
- [ ] Reassembly completes without needing a retry — RaptorQ symbols arriving

> Do this with the payload in its flight configuration, not on the desk with
> the lid off. A cable that works when everything is loose is the classic
> failure.

### D2. Telemetry

- [ ] Position on the ground station map matches C1
- [ ] Altitude, satellites, fix type all populated
- [ ] Battery voltage reading (or a deliberate "unknown" if no monitor fitted)

### D3. Meshtastic beacon

- [ ] Beacon heard by an **independent** radio, not just the payload's own log
- [ ] Correct frequency for your region
- [ ] Transmit power at the regional ceiling, not above
- [ ] Hop limit 0 — the balloon must not be relayed by the whole mesh
- [ ] Project link and callsign present in the beacon text

### D4. Uplink, if you are using it

- [ ] A command from the ground reaches the payload and is acknowledged
- [ ] Commands on the **public** channel are refused
- [ ] Private channel key matches on both ends

---

## E. Flight power configuration

### E1. WiFi cutoff

WiFi is the largest controllable draw in flight, and at altitude it is worse
than useless: there is no access point up there, so NetworkManager scans and
fails for the whole flight. The payload leaves it up until the balloon proves
it has launched, then turns it off for good.

```
WiFi cutoff armed: off after 300 m AGL confirmed on 3 consecutive 3D fixes
```

- [ ] **That line is in the log.** If it is not, the cutoff is off and you will
      fly with WiFi scanning the whole way.
- [ ] `wifi_off_altitude_agl_m` is above anything at your site — a hill, a
      launch from a truck bed — and below your first few minutes of climb
- [ ] The watcher that performs the cutoff is running, or nothing will happen
      when the payload asks:

```bash
systemctl is-active raptorhab-wifi-off.path raptorhab-wifi-restore.service
```

Both should report `active`. The payload cannot turn the radio off itself — it
runs with `NoNewPrivileges=true` and cannot use `sudo` or open `/dev/rfkill` —
so it writes a request file and systemd does the privileged part. It then reads
back `/sys/class/rfkill/*/soft` to confirm, and logs an error naming this unit
if the radio is still up.

- [ ] No stale request left from a previous flight:

```bash
ls /var/lib/raptorhab/wifi-off.request
```

Should report *No such file*. One left behind would cut WiFi the moment the
watcher starts.

> **The way back in is a power cycle.** Once WiFi is off it stays off for the
> flight, by design. Power-cycling the payload brings it back — guaranteed by
> `raptorhab-wifi-restore.service`, which runs at boot and does not depend on
> the payload software running at all. That matters: systemd-rfkill otherwise
> restores the in-flight block on every subsequent boot, and a recovered
> payload would come back unreachable with no indication why.

### E2. Everything else

- [ ] `flight_power_saving = true`
- [ ] Bluetooth, HDMI, activity LED disabled
- [ ] Camera release-when-idle set as you intend
- [ ] Battery charged, and the pack is the one you actually measured the
      endurance with

---

## F. Flight configuration

- [ ] Region correct — this sets frequency **and** the power ceiling
- [ ] Transmit power at the ceiling for that region
- [ ] Capture interval matches the flight duration and card space
- [ ] Zone thresholds sane for the profile you expect
- [ ] Duty cycle limit correct for the region (EU is 10%; US is not limited)
- [ ] Callsign correct and matches your licence

---

## G. Physical

- [ ] Both antennas fitted and strain-relieved
- [ ] Camera lens clean, unobstructed, pointing where you want
- [ ] SD card fully seated
- [ ] Battery connector secure, cannot vibrate loose
- [ ] Payload closed as it will fly
- [ ] Insulation adequate — the SoC runs warm, but the battery will be at
      −50 °C
- [ ] Nothing loose inside that can move and short something

---

## H. Regulatory and recovery

- [ ] Airspace notification made, as your jurisdiction requires
- [ ] Licence covers the band and power you are transmitting at
- [ ] Flight prediction run for today's winds
- [ ] Predicted landing area is recoverable — not open water, not a restricted
      zone
- [ ] Someone knows where you are going
- [ ] Ground station tracking and logging before release, not after

---

## I. Final, at the pad

- [ ] Payload has been powered and stationary long enough for C2 to settle
- [ ] `fix_type = 2`, satellites in double figures
- [ ] Ground station receiving images **now**
- [ ] WiFi cutoff armed (§E1)
- [ ] Launch time noted
- [ ] Settled launch coordinates written down — if the ground station is lost,
      this is the number that lets you predict where it went

---

## J. Restarts in flight

The payload restarts in the air — systemd does it, the watchdog does it, and
both beat a payload that has stopped. It now carries the launch point and the
landing-detection arming across a restart, in
`/var/lib/raptorhab/flight_state.json`.

The test is altitude: if the payload comes up higher than the launch site it
recorded, it is still flying. On the pad or after recovery it is not, and it
takes a fresh reading.

- [ ] No stale state file before you power up for the flight:

```bash
ls /var/lib/raptorhab/flight_state.json
```

A file left from a previous flight is ignored anyway — it is rejected after 24
hours, and a payload on the pad is not above its own launch altitude — but
clearing it removes the question.

> Nothing to do here on launch day. It matters if the payload resets at 20 km:
> without it, it would capture a "launch point" 20 km up, read 0 m AGL for the
> rest of the flight, never cut WiFi, and come down with landing detection
> disarmed — still taking pictures in a field instead of beaconing.

---

## What happens after you let go

Roughly, and in this order:

1. The balloon climbs. At **300 m AGL**, confirmed on three consecutive 3D
   fixes, **WiFi turns off** and stays off. The payload is now radio-only.
2. Past the launch radius, or above the altitude override, it enters **cruise**:
   imagery drops to a trickle, most airtime becomes idle, Meshtastic beacons
   continue on their interval.
3. Above **2000 m AGL**, landing detection **arms**. It cannot trigger before
   this, so a payload sitting on the pad never mistakes itself for a landed one.
4. Burst, then descent. Nothing changes mode on the way down except through the
   same zone rules.
5. On the ground: low, stationary, and armed — the payload declares **landed**,
   stops imagery entirely, and becomes a slow recovery beacon.

To get back into it after recovery: **power-cycle it.** WiFi comes back at boot.
