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

## The camera: the biggest remaining saving

`Picamera2.start()` is called once at boot and `stop()` only at shutdown. With
the default 30-second capture interval, **the sensor and ISP run continuously
while being used for about one second in thirty.** That is somewhere around
150–250 mA, which is larger than everything above put together.

It has not been changed, because stopping and restarting the camera is not free:

- Restart latency is on the order of a second, so a capture triggered by an
  uplink command becomes noticeably slower.
- Auto-exposure and auto-white-balance reconverge from scratch each time. At
  altitude, against a bright horizon, the first frame after a restart may be
  badly exposed — and unlike a dropped packet, a bad image is not recoverable
  by trying again five minutes later.

The right fix is to stop the camera only when the schedule says the next
capture is far away — in cruise, and always in LANDED where capture is disabled
outright — and to discard the first frame after a restart. That is a real piece
of work with a real risk to image quality, and it deserves bench testing
against actual exposures rather than being merged on the strength of the
arithmetic.

**If you want the saving before that work is done**, raising
`auto_capture_interval_sec` reduces the number of captures but not the idle
draw, because the camera stays open either way. Turning capture off entirely in
cruise does not currently release it either.

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
