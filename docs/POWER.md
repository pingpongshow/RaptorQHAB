# Power on a battery

A flight ends when the battery does. Everything here is about making that
happen later — none of it makes the payload do anything new.

Figures are for a Pi Zero 2 W. Treat them as the right order of magnitude, not
a specification; the only number that matters for your flight is the one you
measure with your pack, your camera and your transmit schedule.

## What was found, and what was done

| Draw | Measured on hardware | Status |
|---|---|---|
| WiFi radio, associated and idle | ~55 mA | **switched off**, opt-in |
| Bluetooth idle | ~10 mA | **switched off**, opt-in |
| Activity LED | ~3 mA | **switched off**, opt-in |
| HDMI | ~25 mA on a desktop image | already idle on Lite; see below |
| Camera held open between captures | ~150–250 mA | **not changed** — see below |
| CPU woken 10×/s in the idle loop | small but constant | **fixed** |
| CPU spun at 1 kHz waiting for packets | small but constant | **fixed** |

Verified on a real payload: **68 mA** of saving actually applied.

## Enabling it

Off by default. Disabling WiFi takes away SSH, and doing that to somebody's
bench Pi by surprise would be hostile.

```
flight_power_saving = true
```

Set it before launch, from either ground station or over the USB console. The
individual switches (`power_disable_wifi`, `power_disable_bluetooth`,
`power_disable_hdmi`, `power_disable_led`) default to on once you have opted in,
so you do not have to enable each one.

**The USB console is unaffected.** It is a separate gadget on a different bus,
so a payload with WiFi disabled is still fully reachable over the cable — which
is how you would configure it in the field anyway.

Everything is reversible with a reboot. `rfkill` is used rather than taking the
interface down, because an interface that is merely down still has a powered
radio.

## HDMI, honestly

This is the one saving that is usually not available, and the code says so
rather than claiming it.

- `tvservice -o` worked on the legacy firmware stack and was removed in
  Bookworm.
- `vcgencmd display_power 0` **returns success and changes nothing** on a KMS
  system. That false confirmation is why the result is read back rather than
  trusted; without the read-back the payload would have logged a 25 mA saving
  it never made.
- The DRM `enabled` files are read-only status, not switches.

On Pi OS Lite with no monitor attached and no display server, the HDMI PHY is
not driving anything to begin with. The 25 mA belongs to a desktop image with a
display attached, which a payload is not.

## The camera

`Picamera2.start()` was called once at boot and `stop()` only at shutdown, so
the sensor and ISP ran for the entire flight while being used about one second
in thirty.

### What it actually costs

I first asserted 150–250 mA for this, which was not measured and was too high.
Without a current meter, the honest proxy is temperature:

```
camera STREAMING   mean 49.9 C
camera STOPPED     mean 47.8 C
difference         +2.0 C with the camera merely open
```

Two degrees on a passively cooled Zero 2 W is roughly 20–30 mA of SoC-side
draw, plus whatever the sensor board itself takes, which this measurement
cannot see. Real, worth having, and smaller than the figure I first gave.

### What releasing costs

Measured with an IMX219 at 1280×960, burst of three:

| | Median per capture |
|---|---|
| Always streaming | 2510 ms |
| Released, one discarded frame | 2672 ms |
| Released, no warm-up | 2615 ms |
| Released, 0.4 s + 2 frames | 3007 ms |

**About 160 ms**, or half a percent of a thirty-second interval.

### The assumption that was wrong

The reason this was not done sooner was a belief that auto-exposure reconverges
from scratch after a restart, risking a badly exposed frame at altitude.

Measured in a lit scene, that is simply not true. libcamera keeps its exposure
state across `stop()`/`start()` while the configuration is unchanged:

```
settled reference brightness: 122.7
trial 1: 122.6  122.7  122.6  122.6 ...
trial 2: 122.7  122.6  122.7  122.7 ...
trial 3: 122.8  122.7  122.7  122.7 ...
frames until within 5% of reference: frame 0, all three trials
```

Frame zero is already correct. Restarting does not cost an exposure.

### The real risk, which is narrower

What the camera cannot do while stopped is *adapt*. If the scene changes during
the idle period — the balloon climbs out of cloud into direct sun — the first
frame after the restart is metered for the scene as it was. That is why the
default discards one frame: it gives the exposure loop a cycle to react before
anything is kept. It is not about convergence after a restart, which does not
happen; it is about a stale meter after a change.

This is the part still untested. The bench scene was static and indoors. A
flight is neither.

### Enabling it

```
camera_release_when_idle = true
```

Off by default only because it has not yet flown. The evidence supports turning
it on: the cost is 160 ms a capture, exposure survives the restart, and a
failed restart returns no image rather than a bad one.

## The idle loop

Two things were burning CPU for no purpose:

The pause between transmit cycles woke ten times a second to pet a watchdog
with a **sixty-second** timeout — six hundred times more often than needed, and
each wake keeps the core out of its deeper idle states. It now sleeps in
half-second slices and pets at a quarter of the configured timeout.

The transmit loop called `time.sleep(0.001)` whenever the scheduler had no
packet ready, spinning a thousand times a second waiting for something that
arrives far more slowly. Now 20 ms.

Neither is dramatic on its own. Both run for the entire flight, and in cruise
the idle path is most of it.

## What was deliberately left alone

**CPU governor.** Tempting, and probably wrong here: the payload's work is
bursty — encode an image, transmit it, wait — and `powersave` would stretch
those bursts, keeping the core busy longer at a lower clock. `ondemand` already
races to idle, which is generally the better strategy for this shape of load.
Worth measuring before changing.

**Radio sleep between transmissions.** The SX1262 is already put in standby
during pauses. Its deeper sleep saves perhaps a milliamp more but loses the
configuration, so every wake costs a full reconfiguration — around 4 ms, plus
the risk of the mode-restore bugs this project has already been bitten by
twice. Not worth it for two-second pauses.

**Undervolting or clock limits.** Real savings, real risk of instability at
altitude where cooling behaves differently. Not something to do without a
thermal chamber.

## Checking it worked

The startup log says exactly what it did:

```
Power saving: wifi off (rfkill blocked)
Power saving: bluetooth off (rfkill blocked)
Power saving: could not disable hdmi: vcgencmd accepted the call but the state stayed on
Power saving: activity_led off (/sys/class/leds/ACT/brightness)
Power saving applied; roughly 68 mA less draw. The USB console is unaffected.
```

And when it is off, it says that too, so a payload that flew with WiFi burning
55 mA cannot do so silently:

```
Power saving is off; WiFi, Bluetooth and HDMI stay powered (roughly 100 mA).
Enable flight_power_saving before launch.
```


## WiFi in flight

WiFi is the largest controllable draw on the payload, and at altitude it is
worse than useless: there is no access point at 30 km, so NetworkManager scans,
fails, and scans again for the entire flight.

It cannot simply be disabled at boot, because the pre-launch checklist is run
over it — that is how the operator confirms the fix, the link and the key. So
the radio stays up until the balloon proves it has launched, then goes down for
the rest of the flight.

### What counts as proof

Three conditions, all required, and both of the first two are only trustworthy
because of fixes made in the same review:

- **A 3D fix.** A 2D fix reports an altitude it did not solve for, which is the
  worst possible thing to trigger an irreversible action on. The GPS layer used
  to call every fix 3D; it no longer does.
- **Height above the launch reference.** Only meaningful because that reference
  is now settled over 180 s rather than taken from the receiver's first and
  worst fix, which was measured 29 m out.
- **Sustained across three consecutive updates.** One glitched fix should not
  end the flight's connectivity.

Default 300 m AGL. High enough that nothing on the pad reaches it, low enough
to take the saving early in the climb.

### How an unprivileged payload turns off a radio

It does not. Every escalation route is closed, measured on the target:

```
rfkill block wifi     -> cannot open /dev/rfkill: Permission denied
nmcli radio wifi off  -> Not authorized to perform this operation
sudo -n <helper> off  -> the "no new privileges" flag is set
```

That last one is the interesting failure. The unit sets
`NoNewPrivileges=true`, so a sudoers rule cannot help — and the first
implementation of this feature was exactly that, a sudoers rule, which would
have logged "armed" at launch and then silently done nothing.

Weakening the payload's hardening to let it escalate would be a poor trade for
a power saving. Instead it writes a request file in its own state directory and
`raptorhab-wifi-off.path` — already root — acts on it. The payload gains no
ability to run anything as root, only to ask for one specific action. That is
strictly less privilege than the sudoers rule would have been.

It then reads `/sys/class/rfkill/*/soft`, which is world-readable, to confirm
the request was actually heard. A request nobody is listening to otherwise
looks exactly like success.

### Getting back in

Power-cycle the payload. `raptorhab-wifi-restore.service` unblocks the radio at
boot, before NetworkManager, and does not depend on the payload software
running — "the payload is broken" being precisely when reaching the Pi matters
most.

This is not belt and braces. systemd-rfkill saves the soft-block state and
restores it on the next boot by design, so without that unit a payload blocked
in flight would come back blocked, every boot, forever.

### Verified on hardware

Unprivileged payload user, hardening intact, no sudo grant:

```
=== before ===                          soft=0
applied= True detail= blocked via systemd path unit
blocked now: True
=== after ===                           soft=1
=== request consumed? ===               No such file  (watcher re-armed)
wifi unblocked                          soft=0
```
