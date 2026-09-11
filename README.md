# Blink Charger Watcher

Polls a phone running the Blink Charging app over network ADB, samples the
screen for a charger's status color, and sends a notification via Apprise
when it transitions to available.

## Prerequisites

### 1. Docker + Docker Compose
Standard install on the host that will run the container.

### 2. A webhook or an Apprise-compatible notification target
Any URL Apprise supports (ntfy, Discord, Pushover, a homelab Apprise API
endpoint, etc). You'll need the full notify URL (ex: https://alerts.example.com/notify/apprise).
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
2. Settings → System → Developer options:
   - Turn on **USB debugging**.
   - On Android 11+, also turn on **Wireless debugging**.

### 5. Enable network ADB and authorize your computer's key
This is the step that generates the key files this project bind-mounts
into the container (`~/.android/adbkey` and `~/.android/adbkey.pub`), so
it has to happen on the Docker **host**, not inside the container.

Install the ADB client on the host first if you don't have it:
```bash
sudo apt install adb          # Debian/Ubuntu
# or: sudo dnf install android-tools
```

**Android 11+ (recommended — no USB cable needed):**
1. On the phone: Developer options → Wireless debugging → **Pair device
   with pairing code**. Note the IP:port and 6-digit code shown.
2. On the host:
   ```bash
   adb pair <phone-ip>:<pairing-port>
   # enter the 6-digit code when prompted
   adb connect <phone-ip>:<debug-port>
   ```
3. The phone will show an **"Allow wireless debugging on this network?"**
   prompt — tap **Allow**. This is what authorizes your host's key.

**Older Android (USB required for the first pairing):**
1. Connect the phone to the host via USB cable.
2. On the host:
   ```bash
   adb devices
   # accept the "Allow USB debugging?" prompt on the phone
   adb tcpip 5555
   ```
3. Unplug the cable, then:
   ```bash
   adb connect <phone-ip>:5555
   ```

**Either way**, once `adb connect` succeeds, confirm the key files exist:
```bash
ls ~/.android/adbkey ~/.android/adbkey.pub
```
These are the files `compose.yaml` mounts read-only into the container —
if they're missing, the connect/authorize step above didn't complete.

### 6. Note the port coordinates you'll need for calibration
Not required yet, just have a way to take a screenshot handy — covered
below.

## Setup

```bash
git clone <repo-url>
cd blink
cp .env.example .env
```

Edit `.env`:

| Variable | Description |
|---|---|
| `ADB_HOST` | Static IP of the phone |
| `ADB_PORT` | Network ADB port (`5555` for `adb tcpip`, or the wireless-debugging debug port) |
| `TZ` | Timezone for this deployment (e.g. `America/New_York`). Drives the Mon–Fri 07:00–16:00 polling window — set this to *your* local timezone |
| `POLL_INTERVAL` | Seconds between polls |
| `APPRISE_URL` | Your Apprise notify URL |
| `CROP_REGIONS` | See calibration below |
| `REFRESH_TAPS` | See calibration below |
| `DEBOUNCE_SECONDS` | Minimum seconds between repeat notifications for the same region |

### Calibrating `CROP_REGIONS` and `REFRESH_TAPS`

These are pixel coordinates tied to *your* phone's screen resolution and
*your* charger screen layout in the Blink app — they won't carry over
from someone else's setup.

1. Open the Blink app to the charger screen you want to watch, then grab
   a screenshot:
   ```bash
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

```bash
docker compose up -d
docker compose logs -f
```

On first run it'll log each region's initial sampled color — confirm
those look sane (and log `AVAILABLE`/`NOT AVAILABLE` correctly) before
walking away from it.

