# Blink Charger Watcher

Polls a phone running the Blink Charging app over network ADB, samples the
screen for a charger's status color, and sends a notification via Apprise
when it transitions to available.

## Prerequisites

### 1. Docker + Docker Compose

Standard install on the host that will run the container.

### 2. An Apprise-compatible notification target

Any URL Apprise supports (ntfy, Discord, Pushover, a homelab Apprise API
endpoint, etc). You'll need the full notify URL. (ex: https://alerts.example.com/notify/apprise)
A webhook URL will work as well.

### 3. A dedicated Android phone running the Blink Charging app

- Log into the Blink app and favorite/pin the charger screen you want to
  monitor.
- Give the phone a **static DHCP reservation by MAC address** on your
  router. If its IP changes later, `ADB_HOST` in `.env` goes stale and the
  watcher silently stops reaching it.
- Keep the phone plugged in and awake (disable sleep/screen-lock, or set
  "Stay awake while charging" in Developer options) — ADB can't screenshot
  a locked screen.

### 4. Enable ADB debugging on the phone

1. Settings → About phone → tap **Build number** 7 times to unlock
   Developer options.
2. Settings → System → Developer options → turn on **USB debugging**.

Do not use **Wireless debugging** for this project — see the next section
for why.

### 5. Enable network ADB and authorize your computer's key

This is the step that generates the key files this project bind-mounts
into the container (`~/.android/adbkey` and `~/.android/adbkey.pub`), so
it has to happen on the Docker **host**, not inside the container.

Install the ADB client on the host first if you don't have it:

```
sudo apt install adb          # Debian/Ubuntu
# or: sudo dnf install android-tools
```

**This project only works over legacy plaintext ADB (`adb tcpip`) — it
cannot use Android's native "Wireless debugging" feature, on any Android
version.** The watcher connects with the `adb-shell` Python library,
which only implements the older plaintext ADB protocol. Wireless
debugging (Settings → Developer options → Wireless debugging → pair with
code) opens a TLS-encrypted connection instead (an `STLS` handshake) that
this library does not support at all. If the phone is paired that way,
every connection attempt fails with:

```
adb_shell.exceptions.InvalidCommandError: Unknown command: ... 'STLS'
```

Use USB every time you need to (re-)establish network ADB — there's no
wireless-only path that works here:

1. Connect the phone to the host via USB cable.
2. On the host:

   ```
   adb devices
   # accept the "Allow USB debugging?" prompt on the phone
   adb tcpip 5555
   ```

3. Unplug the cable, then:

   ```
   adb connect <phone-ip>:5555
   ```

Once `adb connect` succeeds, confirm the key files exist:

```
ls ~/.android/adbkey ~/.android/adbkey.pub
```

These are the files `compose.yaml` mounts read-only into the container —
if they're missing, the connect/authorize step above didn't complete.

**Legacy `adb tcpip` mode does not survive a phone reboot.** If the phone
restarts for any reason (OS update, dead battery, manual reboot), it
drops back to USB-only debugging and network ADB has to be re-enabled
with the steps above before the watcher can reconnect. Repeated
"can't reach the phone" alerts (see `CONNECTION_FAILURE_THRESHOLD` below)
are the usual sign this has happened — redo the USB steps, and make sure
`ADB_PORT` in `.env` is still pointing at the port you used (`5555`
above), not a stale one from a previous pairing.

### 6. Note the port coordinates you'll need for calibration

Not required yet, just have a way to take a screenshot handy — covered
below.

## Setup

```
git clone <repo-url>
cd blink
cp .env.example .env
```

Edit `.env`:

| Variable | Description |
| --- | --- |
| `ADB_HOST` | Static IP of the phone |
| `ADB_PORT` | Network ADB port from `adb tcpip <port>` (`5555` unless you chose differently) |
| `TZ` | Timezone for this deployment (e.g. `America/New_York`). Drives `WATCH_START_TIME`/`WATCH_END_TIME` below — set this to *your* local timezone |
| `POLL_INTERVAL` | Seconds between polls |
| `APPRISE_URL` | Your Apprise notify URL |
| `CROP_REGIONS` | See calibration below |
| `REFRESH_TAPS` | See calibration below |
| `DEBOUNCE_SECONDS` | Minimum seconds between repeat notifications for the same region becoming available |
| `WATCH_DAYS` | Comma-separated days to poll on (`Mon,Tue,Wed,Thu,Fri` by default) |
| `WATCH_START_TIME` / `WATCH_END_TIME` | `HH:MM` (24-hour) polling window, start inclusive / end exclusive (default `07:00`–`16:00`) |
| `SCREEN_OFF_TOLERANCE` | Color-distance tolerance for detecting a locked/asleep screen (default `15`) |
| `SCREEN_OFF_DEBOUNCE_SECONDS` | Minimum seconds between repeat screen-off alerts (default `300`) |
| `CONNECTION_FAILURE_THRESHOLD` | Consecutive failed ADB connection attempts before alerting that the phone is unreachable (default `3`) |

### Calibrating `CROP_REGIONS` and `REFRESH_TAPS`

These are pixel coordinates tied to *your* phone's screen resolution and
*your* charger screen layout in the Blink app — they won't carry over
from someone else's setup.

1. Open the Blink app to the charger screen you want to watch, then grab
   a screenshot:

   ```
   adb shell screencap -p > screen.png
   adb pull screen.png   # if not already on the host
   ```

2. Open `screen.png` and find the pixel box `(x1,y1,x2,y2)` around each
   port's status indicator/button. Set `CROP_REGIONS` as:

   ```
   NAME:x1,y1,x2,y2;NAME2:x1,y1,x2,y2
   ```

3. `REFRESH_TAPS` replays the manual "tap away, tap back" gesture the app
   needs to re-fetch live status. Find two tap points (e.g. a bottom nav
   tab, then the favorited charger) the same way, using the screenshot's
   pixel coordinates:

   ```
   x1,y1;x2,y2
   ```

### Run it

```
docker compose up -d
docker compose logs -f
```

On first run it'll log each region's initial sampled color — confirm
those look sane (and log `AVAILABLE`/`NOT AVAILABLE` correctly) before
walking away from it.

## Troubleshooting

### `adb_shell.exceptions.InvalidCommandError: Unknown command ... 'STLS'`

The phone is currently paired over Wireless debugging (TLS) instead of
legacy `adb tcpip` — this project's ADB library can't speak that
protocol at all. Redo the USB `adb tcpip` pairing in [Prerequisites §5](#5-enable-network-adb-and-authorize-your-computers-key),
and double-check `ADB_PORT` in `.env` matches the port you used, not a
leftover Wireless-debugging port.

### Repeated "can't reach the phone" alerts

Almost always means the phone rebooted and lost `adb tcpip` mode (it
doesn't survive reboots). Same fix as above.

