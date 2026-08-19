# The Meshtastic link

The balloon speaks standard Meshtastic. Any stock radio hears it — the packets
use LongFast parameters on the standard sync word, with the standard header and
AES-256-CTR channel encryption. No patched firmware, no special build.

This document covers what it sends, what you can send it, and how to have it
relay a message.

---

## What the balloon broadcasts

On the **public channel** (LongFast by default), one beacon cycle is:

| Packet | Contents | How often |
|---|---|---|
| `POSITION_APP` | latitude, longitude, altitude, satellites, ground speed and track | every cycle, once there is a 3D fix |
| `TELEMETRY_APP` (device) | battery level, voltage, uptime | every cycle |
| `TELEMETRY_APP` (environment) | temperature | every cycle, when a reading exists |
| `NODEINFO_APP` | node id, long name (`RaptorHAB <callsign>`), short name | every 6th cycle |
| `TEXT_MESSAGE_APP` | the project link | every 6th cycle, with node info |
| `TEXT_MESSAGE_APP` | the operator's beacon text | every cycle, when set |

Position goes first deliberately: if a cycle is cut short by a mode switch or a
schedule boundary, that is the packet that should already be on the air.

With **no GPS fix there is no position packet at all**, rather than a position
of zero. Somewhere in the Atlantic is worse than nowhere.

If a **private channel** is configured, position and beacon text are repeated
on it.

### The project link

Broadcast at the node-info cadence rather than every cycle: it is useful to a
stranger exactly once, and paying for it every beacon would spend airtime the
recovery beacons need.

It is deliberately **separate from the beacon text**, so changing the operator
message over the uplink cannot quietly remove it. Somebody who hears this
balloon and wants to know what it is should always be able to find out.

Configure with `meshtastic_project_url`; empty sends none.

### Beacon cadence

| Zone | Beacon every |
|---|---|
| Launch | 10 minutes |
| Cruise | 5 minutes |
| Landed | 1 minute |

The interval is a hard floor: an overdue beacon beats any image backlog,
because a balloon that stops beaconing to send pictures is a balloon nobody can
find.

---

## Sending commands to the balloon

### The four conditions

A command runs only if **all** of these hold:

1. `uplink_commands_enabled` is on
2. A private channel is configured with a real key
3. The message arrived **on that private channel**
4. The message is addressed to the balloon's node id specifically

A broadcast on the public channel can never command the balloon however it is
worded, because anyone at all can transmit there.

### The command set

Every command starts with `!`.

| Command | Reply |
|---|---|
| `!help` / `!commands` | the list of commands this balloon accepts |
| `!ping` | `<callsign> pong` |
| `!status` | zone, region, altitude, packets sent |
| `!pos` / `!position` | latitude, longitude, altitude, satellite count |
| `!beacon` | shows the current beacon text |
| `!beacon <text>` | sets it, up to 120 characters |
| `!beacon clear` | empties it |
| `!capture` | triggers an image capture |

Replies are addressed back to whoever asked, on the private channel.

`!beacon` with no arguments **reports** rather than clearing. Clearing has to be
asked for explicitly, because a link with no undo is a poor place to wipe
something by accident.

### What is deliberately missing

There is no command to stop transmitting, change frequency, change power, or
reboot.

A radio link is exactly the wrong place to expose controls whose failure mode
is silence. Every command above either reports something or changes something
recoverable; none of them can take the balloon off the air. Configuration that
could is USB-only, on the ground, where a mistake can be undone.

### Sending one

**macOS app** — Meshtastic tab: pick the private channel, tick *send to balloon
only*, type the command.

**Python ground station** — web or desktop Meshtastic tab, tick *as command*.
It refuses anything not starting with `!` rather than sending something the
balloon will silently ignore.

**Any Meshtastic client** — send a direct message to the balloon's node id on
the private channel. There is nothing special about the packets.

### When it will not hear you

The SX1262 cannot receive LoRa while transmitting images, so the balloon only
hears during its **listen windows**:

| Zone | Airtime spent listening |
|---|---|
| Launch | none — imagery has 98% and you are standing next to it |
| Cruise | 5%, taken from idle |
| Landed | 5%, taken from idle |

There is no acknowledgement and no retry. If a command lands during an image
burst it is simply missed, so send it again. Windows are `repeater_rx_window_sec`
long (4 s by default) and are charged to the listen budget precisely so the cost
is visible rather than hidden.

Listening is funded from **idle**, not from imagery: it costs battery, not
pictures.

---

## Having the balloon repeat a message

From 30 km up a 1 W beacon is heard across roughly a 400-mile radius. That makes
the balloon a useful relay, and a dangerous one — if it repeated everything it
heard, one balloon could swamp a regional mesh. So it repeats only what is
explicitly meant for it.

### Two ways to tag a message

**Prefix it with the tag**, `!RPT ` by default (`meshtastic_repeat_tag`):

```
!RPT Chase car heading to grid 12S, ETA 40 minutes
```

**Or address it directly to the balloon's node id**, with no leading `!`.

### What happens then

The tag is stripped, and the text goes out **on the channel it arrived on** —
public in, public out; private in, private out. It is sent as the balloon's own
packet rather than a forward of yours, because the balloon is the thing with the
enormous footprint and the receiving mesh should see who actually put it on the
air.

### Limits

- **20 repeats per hour** by default (`repeater_max_repeats_per_hour`)
- A **seen cache** means the same packet arriving by several mesh paths is
  relayed once
- **Hop limit 0**: heard directly, forwarded by nobody. From altitude that is
  already a very large audience
- Untagged traffic from other nodes is ignored entirely

### A command is never repeated

A message addressed to the balloon that starts with `!` is treated as a command
attempt, not a relay request — even when it is refused.

Without that rule, a command sent on the public channel would be rejected as a
command and then rebroadcast as a message, putting the operator's command text
in front of the whole mesh, arguments included.

---

## Configuration

| Setting | Meaning |
|---|---|
| `meshtastic_enabled` | Master switch |
| `meshtastic_channel_name` / `_psk` | Public channel |
| `meshtastic_private_channel_name` / `_psk` | Private channel: commands require this |
| `meshtastic_beacon_text` | Operator message, also settable with `!beacon` |
| `meshtastic_project_url` | Project link, sent at the node-info cadence |
| `uplink_commands_enabled` | Whether commands are accepted at all |
| `repeater_enabled` | Whether messages are relayed |
| `meshtastic_repeat_tag` | The prefix that marks a message for relay |
| `repeater_max_repeats_per_hour` | Relay ceiling |
| `repeater_rx_percent` | Share of airtime spent listening, in cruise and landed |
| `repeater_rx_window_sec` | Length of one listen window |

All of it is configurable over USB, from either ground station.
