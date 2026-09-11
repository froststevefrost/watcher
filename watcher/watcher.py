import io
import logging
import os
import sys
import time
from datetime import datetime
from datetime import time as dtime

import apprise
from adb_shell.adb_device import AdbDeviceTcp
from adb_shell.auth.sign_pythonrsa import PythonRSASigner
from adb_shell.exceptions import (
    AdbConnectionError,
    TcpTimeoutException,
)
from PIL import Image, UnidentifiedImageError


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)

log = logging.getLogger("blink-watcher")


# ---------------------------------------------------------------------------
# Environment/configuration
# ---------------------------------------------------------------------------

def env(name, default=None, required=False):
    value = os.environ.get(name, default)

    if required and not value:
        log.error("Missing required env var: %s", name)
        sys.exit(1)

    return value


ADB_HOST = env("ADB_HOST", required=True)
ADB_PORT = int(env("ADB_PORT", "5555"))

POLL_INTERVAL = int(env("POLL_INTERVAL", "30"))

APPRISE_URL = env("APPRISE_URL", required=True)

DEBOUNCE_SECONDS = int(env("DEBOUNCE_SECONDS", "5"))


# ---------------------------------------------------------------------------
# Availability detection
# ---------------------------------------------------------------------------

# Approximate RGB color of the green "Available" button.
AVAILABLE_COLOR = (210, 230, 201)

# Allow for small differences in rendering/color sampling.
AVAILABLE_TOLERANCE = 20


def color_distance(a, b):
    """Return Euclidean distance between two RGB colors."""

    return (
        (a[0] - b[0]) ** 2
        + (a[1] - b[1]) ** 2
        + (a[2] - b[2]) ** 2
    ) ** 0.5


def is_available(color):
    """
    Return True if the sampled color is close enough to the
    known green Available color.
    """

    distance = color_distance(
        color,
        AVAILABLE_COLOR,
    )

    return distance <= AVAILABLE_TOLERANCE


# ---------------------------------------------------------------------------
# Screen-off/locked detection
# ---------------------------------------------------------------------------

# A locked or sleeping screen samples as pure black at any coordinate,
# including the same CROP_REGIONS boxes used for availability checks.
SCREEN_OFF_COLOR = (0, 0, 0)
SCREEN_OFF_TOLERANCE = int(env("SCREEN_OFF_TOLERANCE", "15"))

# Separate, longer-lived cooldown from DEBOUNCE_SECONDS since a locked
# screen tends to stay locked for a while — no need to re-notify every
# poll cycle.
SCREEN_OFF_DEBOUNCE_SECONDS = int(
    env("SCREEN_OFF_DEBOUNCE_SECONDS", "300")
)


def is_black(color):
    """
    Return True if a sampled color is close enough to pure black.
    """

    distance = color_distance(
        color,
        SCREEN_OFF_COLOR,
    )

    return distance <= SCREEN_OFF_TOLERANCE


def screen_appears_off(colors):
    """
    Return True if every sampled region reads as black. A single dark
    region could just be a charger's real status color, but all
    configured regions reading black at once means the screen itself
    is locked or asleep, not that every port happens to be that color.
    """

    return all(is_black(color) for color in colors.values())


# ---------------------------------------------------------------------------
# Polling schedule
# ---------------------------------------------------------------------------

def parse_time_of_day(raw, name):
    """
    Parse an HH:MM (24-hour) string into a datetime.time.
    """

    parts = raw.split(":")

    if len(parts) != 2:
        log.error(
            "Bad %s value: %r (expected HH:MM)",
            name,
            raw,
        )
        sys.exit(1)

    try:
        hour, minute = (int(p) for p in parts)
    except ValueError:
        log.error(
            "Bad %s value: %r (expected HH:MM)",
            name,
            raw,
        )
        sys.exit(1)

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        log.error(
            "Bad %s value: %r (hour/minute out of range)",
            name,
            raw,
        )
        sys.exit(1)

    return dtime(hour=hour, minute=minute)


WEEKDAY_NAMES = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}

WEEKDAY_DISPLAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def parse_watch_days(raw, name):
    """
    Parse a comma-separated list of Mon/Tue/Wed/Thu/Fri/Sat/Sun
    (case-insensitive) into a set of Python weekday ints
    (Monday=0 ... Sunday=6).
    """

    days = set()

    for part in raw.split(","):
        part = part.strip().lower()

        if not part:
            continue

        if part not in WEEKDAY_NAMES:
            log.error(
                "Bad %s value: %r (expected comma-separated "
                "Mon,Tue,Wed,Thu,Fri,Sat,Sun)",
                name,
                part,
            )
            sys.exit(1)

        days.add(WEEKDAY_NAMES[part])

    if not days:
        log.error(
            "%s parsed to zero days",
            name,
        )
        sys.exit(1)

    return days


WATCH_DAYS = parse_watch_days(
    env("WATCH_DAYS", "Mon,Tue,Wed,Thu,Fri"),
    "WATCH_DAYS",
)

WATCH_START_TIME = parse_time_of_day(
    env("WATCH_START_TIME", "07:00"),
    "WATCH_START_TIME",
)

WATCH_END_TIME = parse_time_of_day(
    env("WATCH_END_TIME", "16:00"),
    "WATCH_END_TIME",
)

if WATCH_START_TIME >= WATCH_END_TIME:
    log.error(
        "WATCH_START_TIME (%s) must be earlier than "
        "WATCH_END_TIME (%s); overnight windows aren't "
        "supported",
        WATCH_START_TIME.strftime("%H:%M"),
        WATCH_END_TIME.strftime("%H:%M"),
    )
    sys.exit(1)


def within_polling_hours():
    """
    Poll on the configured WATCH_DAYS, between WATCH_START_TIME
    (inclusive) and WATCH_END_TIME (exclusive).

    Uses the local timezone of the machine running this script (set
    via TZ).
    """

    now = datetime.now()

    if now.weekday() not in WATCH_DAYS:
        return False

    current_time = now.time()

    return WATCH_START_TIME <= current_time < WATCH_END_TIME


# ---------------------------------------------------------------------------
# Region configuration
# ---------------------------------------------------------------------------

def parse_regions(raw):
    """
    Parse:

        NAME:x1,y1,x2,y2;NAME2:x1,y1,x2,y2

    into:

        {
            "NAME": (x1, y1, x2, y2),
            "NAME2": (x1, y1, x2, y2),
        }
    """

    regions = {}

    for part in raw.split(";"):
        part = part.strip()

        if not part:
            continue

        if ":" not in part:
            log.error(
                "Bad CROP_REGIONS entry (missing name): %r",
                part,
            )
            sys.exit(1)

        name, coords = part.split(":", 1)

        try:
            box = tuple(
                int(x.strip())
                for x in coords.split(",")
            )
        except ValueError:
            log.error(
                "Bad CROP_REGIONS coordinates for %s: %r",
                name,
                coords,
            )
            sys.exit(1)

        if len(box) != 4:
            log.error(
                "Bad CROP_REGIONS entry for %s: "
                "need x1,y1,x2,y2",
                name,
            )
            sys.exit(1)

        x1, y1, x2, y2 = box

        if x2 <= x1 or y2 <= y1:
            log.error(
                "Invalid region %s: %s",
                name,
                box,
            )
            sys.exit(1)

        regions[name.strip()] = box

    if not regions:
        log.error("CROP_REGIONS parsed to zero regions")
        sys.exit(1)

    return regions


REGIONS = parse_regions(
    env("CROP_REGIONS", required=True)
)


# ---------------------------------------------------------------------------
# Force-refresh before sampling
# ---------------------------------------------------------------------------

def parse_taps(raw):
    """
    Parse:

        x1,y1;x2,y2

    into:

        [(x1, y1), (x2, y2)]
    """

    taps = []

    for part in raw.split(";"):
        part = part.strip()

        if not part:
            continue

        x, y = (int(v.strip()) for v in part.split(","))
        taps.append((x, y))

    return taps


# Replays the manual "tap My Location, tap Parking Garage" sequence —
# the app doesn't auto-refresh port status on its own, so without this
# the watcher just re-confirms stale data every cycle. Defaults below
# are calibrated for the One Loudoun Parking Garage screen; recalibrate
# via CROP_REGIONS-style coordinate-finding if pointed elsewhere.
REFRESH_TAPS = parse_taps(
    env("REFRESH_TAPS", "535,3028;400,550")
)

REFRESH_TAP_DELAY = float(env("REFRESH_TAP_DELAY", "1"))
REFRESH_SETTLE_DELAY = float(env("REFRESH_SETTLE_DELAY", "2"))


def refresh_screen(device):
    """
    Force the app to re-fetch port status by replaying the same
    navigation a manual refresh uses (tap the "My Location" tab,
    then tap back into the favorited charger).
    """

    for x, y in REFRESH_TAPS:
        device.shell(f"input tap {x} {y}")
        time.sleep(REFRESH_TAP_DELAY)

    time.sleep(REFRESH_SETTLE_DELAY)


# ---------------------------------------------------------------------------
# Apprise
# ---------------------------------------------------------------------------

apobj = apprise.Apprise()
apobj.add(APPRISE_URL)


def notify(region_name):
    """
    Send the availability notification.
    """

    #title = f"🔌 {region_name} is open!"

    body = (
        f"🔌 {region_name} is open!\n\n"
        "Get it quick!"
    )

    log.info(
        "Sending notification for %s",
        region_name,
    )

    ok = apobj.notify(
        #title=title,
        body=body,
    )

    if not ok:
        log.error(
            "Apprise notification failed for %s",
            region_name,
        )


def notify_screen_off():
    """
    Send the screen-locked/asleep notification.
    """

    body = (
        "📱 The phone's screen appears locked or asleep — "
        "availability monitoring is paused until it wakes up."
    )

    log.warning(
        "Sending screen-off notification",
    )

    ok = apobj.notify(
        body=body,
    )

    if not ok:
        log.error(
            "Apprise notification failed for screen-off alert",
        )


# ---------------------------------------------------------------------------
# ADB connection
# ---------------------------------------------------------------------------

def connect():
    """
    Connect to the Android device using the same ADB key that
    the normal adb client uses.
    """

    key_dir = os.path.expanduser("~/.android")

    private_key = os.path.join(
        key_dir,
        "adbkey",
    )

    public_key = os.path.join(
        key_dir,
        "adbkey.pub",
    )

    if not os.path.exists(private_key):
        raise RuntimeError(
            f"ADB private key not found: {private_key}"
        )

    if not os.path.exists(public_key):
        raise RuntimeError(
            f"ADB public key not found: {public_key}"
        )

    with open(private_key, "r") as f:
        priv = f.read()

    with open(public_key, "r") as f:
        pub = f.read()

    signer = PythonRSASigner(
        pub,
        priv,
    )

    device = AdbDeviceTcp(
        ADB_HOST,
        ADB_PORT,
        default_transport_timeout_s=15,
    )

    log.info(
        "Connecting to ADB device %s:%s...",
        ADB_HOST,
        ADB_PORT,
    )

    device.connect(
        rsa_keys=[signer],
        auth_timeout_s=15,
    )

    log.info(
        "Connected to %s:%s",
        ADB_HOST,
        ADB_PORT,
    )

    return device


# ---------------------------------------------------------------------------
# Screenshot
# ---------------------------------------------------------------------------

def get_screenshot(device):
    """
    Capture the Android screen and return it as a PIL Image.
    """

    png_bytes = device.shell(
        "screencap -p",
        decode=False,
    )

    if not png_bytes:
        raise RuntimeError(
            "ADB returned an empty screenshot"
        )

    try:
        return Image.open(
            io.BytesIO(png_bytes)
        ).convert("RGB")

    except UnidentifiedImageError:
        log.warning(
            "ADB returned invalid screenshot data "
            "(%d bytes)",
            len(png_bytes),
        )
        raise


# ---------------------------------------------------------------------------
# Color sampling
# ---------------------------------------------------------------------------

def sample_color(image, box):
    """
    Calculate the average RGB color of a region.
    """

    crop = image.crop(box)

    # Reduce the crop before calculating the average.
    crop = crop.resize((10, 10))

    pixels = list(crop.getdata())

    count = len(pixels)

    return tuple(
        sum(pixel[channel] for pixel in pixels) // count
        for channel in range(3)
    )


def sample_regions(device):
    """
    Force a refresh, then take one screenshot and sample all
    configured regions.
    """

    refresh_screen(device)

    image = get_screenshot(device)

    return {
        name: sample_color(image, box)
        for name, box in REGIONS.items()
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    device = None

    # Last sampled color for each region.
    last_colors = {
        name: None
        for name in REGIONS
    }

    # Last time a notification was sent.
    last_notified_at = {
        name: 0.0
        for name in REGIONS
    }

    # Last time a screen-off notification was sent (shared across
    # regions, since it describes the phone as a whole).
    last_screen_off_notified_at = 0.0

    # Track whether we are currently inside polling hours.
    was_polling = False

    log.info("Starting blink watcher")
    log.info(
        "ADB target: %s:%s",
        ADB_HOST,
        ADB_PORT,
    )
    log.info(
        "Poll interval: %s seconds",
        POLL_INTERVAL,
    )
    watched_days = ",".join(
        WEEKDAY_DISPLAY[d] for d in sorted(WATCH_DAYS)
    )

    log.info(
        "Polling schedule: %s, %s-%s",
        watched_days,
        WATCH_START_TIME.strftime("%H:%M"),
        WATCH_END_TIME.strftime("%H:%M"),
    )
    log.info(
        "Available color: %s +/- %.1f",
        AVAILABLE_COLOR,
        AVAILABLE_TOLERANCE,
    )
    log.info(
        "Screen-off detection: color=%s +/- %s, "
        "notify cooldown=%ss",
        SCREEN_OFF_COLOR,
        SCREEN_OFF_TOLERANCE,
        SCREEN_OFF_DEBOUNCE_SECONDS,
    )

    log.info("Regions:")

    for name, box in REGIONS.items():
        log.info(
            "  %s: %s",
            name,
            box,
        )

    while True:
        try:
            polling = within_polling_hours()

            # ---------------------------------------------------------------
            # Outside polling hours
            # ---------------------------------------------------------------

            if not polling:
                if was_polling:
                    log.info(
                        "Polling hours ended; "
                        "disconnecting ADB"
                    )

                    if device is not None:
                        try:
                            device.close()
                        except Exception:
                            pass

                        device = None

                was_polling = False

                # Check once per minute while inactive.
                time.sleep(60)
                continue

            # ---------------------------------------------------------------
            # Entering polling hours
            # ---------------------------------------------------------------

            if not was_polling:
                log.info(
                    "Polling hours started"
                )

                was_polling = True

            # ---------------------------------------------------------------
            # Connect to ADB
            # ---------------------------------------------------------------

            if device is None:
                device = connect()

            # ---------------------------------------------------------------
            # Capture screenshot and sample regions
            # ---------------------------------------------------------------

            colors = sample_regions(device)

            now = time.monotonic()

            log.debug(
                "Sampled colors: %s",
                colors,
            )

            # ---------------------------------------------------------------
            # Screen locked/asleep — every region reads black at once.
            # Skip availability comparison this cycle so a blank screen
            # doesn't get recorded as a real "went unavailable" change.
            # ---------------------------------------------------------------

            if screen_appears_off(colors):
                log.warning(
                    "All regions read black; phone screen appears "
                    "locked or asleep. Skipping this cycle.",
                )

                elapsed = now - last_screen_off_notified_at

                if elapsed >= SCREEN_OFF_DEBOUNCE_SECONDS:
                    notify_screen_off()
                    last_screen_off_notified_at = now
                else:
                    log.info(
                        "Screen-off notification debounced",
                    )

                time.sleep(POLL_INTERVAL)
                continue

            # ---------------------------------------------------------------
            # Check each region
            # ---------------------------------------------------------------

            for name, color in colors.items():
                previous = last_colors[name]

                # First sample establishes the baseline.
                if previous is None:
                    last_colors[name] = color

                    log.info(
                        "%s initial color: %s [%s]",
                        name,
                        color,
                        "AVAILABLE"
                        if is_available(color)
                        else "NOT AVAILABLE",
                    )

                    continue

                # No RGB change at all.
                if color == previous:
                    continue

                log.info(
                    "%s changed: %s -> %s",
                    name,
                    previous,
                    color,
                )

                was_available = is_available(previous)
                now_available = is_available(color)

                # -----------------------------------------------------------
                # Only notify when transitioning INTO available.
                # -----------------------------------------------------------

                if not was_available and now_available:
                    elapsed = (
                        now - last_notified_at[name]
                    )

                    if elapsed >= DEBOUNCE_SECONDS:
                        log.info(
                            "%s is now AVAILABLE!",
                            name,
                        )

                        notify(name)

                        last_notified_at[name] = now

                    else:
                        log.info(
                            "%s became available, but "
                            "notification is debounced",
                            name,
                        )

                elif was_available and not now_available:
                    log.info(
                        "%s is no longer available; "
                        "no notification",
                        name,
                    )

                # Remember the latest sampled color.
                last_colors[name] = color

        # -------------------------------------------------------------------
        # ADB connection failures
        # -------------------------------------------------------------------

        except (
            TcpTimeoutException,
            AdbConnectionError,
            ConnectionResetError,
            ConnectionRefusedError,
            BrokenPipeError,
            OSError,
        ) as e:

            log.warning(
                "ADB connection problem: %s; "
                "will reconnect next cycle",
                e,
            )

            device = None

        # -------------------------------------------------------------------
        # Bad screenshot
        # -------------------------------------------------------------------

        except UnidentifiedImageError:
            log.warning(
                "Received a bad/empty screenshot; "
                "will retry next cycle",
            )

        # -------------------------------------------------------------------
        # Anything unexpected
        # -------------------------------------------------------------------

        except Exception:
            log.exception(
                "Unexpected error; "
                "will retry next cycle",
            )

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
