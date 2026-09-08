import io
import logging
import os
import sys
import time
from datetime import datetime

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
# Polling schedule
# ---------------------------------------------------------------------------

def within_polling_hours():
    """
    Poll Monday-Friday from 07:00 through 15:59.

    Uses the local timezone of the machine running this script.
    """

    now = datetime.now()

    # Monday = 0 ... Sunday = 6
    if now.weekday() >= 5:
        return False

    return 4 <= now.hour < 16


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
    Take one screenshot and sample all configured regions.
    """

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
    log.info(
        "Polling schedule: Monday-Friday, 07:00-16:00",
    )
    log.info(
        "Available color: %s +/- %.1f",
        AVAILABLE_COLOR,
        AVAILABLE_TOLERANCE,
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

            log.debug(
                "Sampled colors: %s",
                colors,
            )

            now = time.monotonic()

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

