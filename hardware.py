"""
hardware.py

The robot's physical singletons - the ESP32 serial link, the camera, and the
face-follow tracker - plus the Pi's own health readouts and the browser
watchdog that backstops a held command.

Importing this module opens the serial port and starts the camera. That is
deliberate and unchanged from the pre-merge app.py: gunicorn imports the WSGI
app fresh in each worker process, and `workers = 1` in gunicorn.conf.py is
what guarantees only one process ever does this. gunicorn.conf.py itself must
never import this module (it imports tls_cert instead) - doing so would open
the serial port in the arbiter process too, racing the real worker for the
same hardware.
"""

import os
import threading
import time

import face_follow
import motion
from camera import camera
from face_follow import FaceFollower
from motor_link import MotorLink, NullMotorLink

WIFI_INTERFACE = os.environ.get("WIFI_INTERFACE", "wlan0")

# Browser-connection watchdog (see _connection_watchdog_loop below): the
# ESP32's own 500ms watchdog only protects against the Pi process/serial
# link dying - it does NOT help if the *browser* loses its connection to the
# Pi (WiFi dead spot, tab backgrounded on a flaky link) while a manual
# command is held, because MotorLink's resend thread keeps re-affirming
# that command to the ESP32 regardless of whether any browser can still
# reach the Pi at all. Held commands are sent exactly once (on keydown/
# button-down) - the browser goes silent for as long as the button stays
# held even when perfectly connected - so "time since last /command" can't
# be the liveness signal without misfiring on every ordinary long hold.
# A separate heartbeat, sent continuously and independent of button state,
# is what actually distinguishes "still connected, button just held" from
# "gone quiet because the link died".
HEARTBEAT_TIMEOUT_S = 3.0        # must stay well above the page's heartbeat interval
HEARTBEAT_CHECK_INTERVAL_S = 0.5

_last_heartbeat_time = time.time()


def record_heartbeat():
    """Called by the remote page's POST /robot/heartbeat."""
    global _last_heartbeat_time
    _last_heartbeat_time = time.time()


def get_wifi_signal():
    """Signal strength of the Pi's own WiFi link (WIFI_INTERFACE, default
    wlan0) to its access point - added to help tell apart "the Pi's WiFi
    uplink is flaky" from other causes of lag/disconnects. Reads
    /proc/net/wireless directly - no subprocess or root needed, present on
    any Linux box with a wireless driver loaded. Returns dBm (the standard,
    driver-independent unit) rather than the adjacent "link quality" column,
    whose maximum value is driver-specific and not directly comparable
    across cards. {"available": False} if there's no such interface (e.g.
    Ethernet-only)."""
    try:
        with open("/proc/net/wireless") as f:
            lines = f.readlines()
    except OSError:
        return {"available": False}

    for line in lines[2:]:  # first two lines are a fixed header
        parts = line.split()
        if not parts or parts[0].rstrip(":") != WIFI_INTERFACE:
            continue
        try:
            dbm = float(parts[3].rstrip("."))
        except (IndexError, ValueError):
            return {"available": False}
        if dbm >= -50:
            quality = "Excellent"
        elif dbm >= -60:
            quality = "Good"
        elif dbm >= -70:
            quality = "Fair"
        elif dbm >= -80:
            quality = "Weak"
        else:
            quality = "Poor"
        return {"available": True, "dbm": dbm, "quality": quality}

    return {"available": False}


_last_cpu_times = None  # (idle_jiffies, total_jiffies) from the previous call


def get_cpu_usage():
    """Pi CPU utilization since the previous call, from /proc/stat jiffy
    deltas - same no-subprocess/no-root approach as get_wifi_signal(), and
    for the same reason: computing a percentage needs two samples, and
    taking both inside one request would mean blocking that request thread
    on a sleep. Instead each call diffs against the last one, which lines up
    naturally with the frontend's periodic polling. The first call after
    startup has no prior sample yet, so it returns {"available": False}."""
    global _last_cpu_times
    try:
        with open("/proc/stat") as f:
            line = f.readline()
    except OSError:
        return {"available": False}

    parts = line.split()
    if len(parts) < 8 or parts[0] != "cpu":
        return {"available": False}
    try:
        user, nice, system, idle, iowait, irq, softirq = (int(x) for x in parts[1:8])
    except ValueError:
        return {"available": False}

    idle_time = idle + iowait
    total_time = user + nice + system + idle + iowait + irq + softirq

    prev = _last_cpu_times
    _last_cpu_times = (idle_time, total_time)
    if prev is None:
        return {"available": False}

    delta_idle = idle_time - prev[0]
    delta_total = total_time - prev[1]
    if delta_total <= 0:
        return {"available": False}

    percent = round((1 - delta_idle / delta_total) * 100, 1)
    return {"available": True, "percent": percent}


def _connection_watchdog_loop():
    """Force-stops the motors if the remote page's heartbeat goes stale while
    a manual command is still held - see the HEARTBEAT_TIMEOUT_S comment
    above for why this can't just be "time since last /command". Skips follow
    mode entirely: it drives autonomously off the camera, not a browser, and
    already has its own lost-face stop logic, so gating it on a heartbeat
    would be both wrong (it doesn't need any browser - Follow me is a button
    on the kiosk's own screen) and ineffective (a bare send_command("S") here
    wouldn't stick against the follower's own next loop iteration, since
    that's not what turns follow off - only follower.stop() does)."""
    stop_cmd = motion.command_for(motion.STOP)
    while True:
        time.sleep(HEARTBEAT_CHECK_INTERVAL_S)
        if follower.is_running():
            continue
        if link.current_command == stop_cmd:
            continue
        if time.time() - _last_heartbeat_time > HEARTBEAT_TIMEOUT_S:
            print("[hardware] Browser heartbeat lost while driving - stopping motors")
            link.stop()


follower = None  # assigned below, after `link` exists; referenced by _handle_trip


def _handle_trip():
    """MotorLink's on_trip callback: if the follower is the one currently
    driving when the ESP32 reports a protection trip, switch it off. Fires
    from MotorLink's reader thread, well after this module finishes
    importing, so `follower` is always assigned by the time this runs."""
    if follower is not None:
        follower.stop(reason="Motor protection tripped")


# --------------------------------------------------------------------------
# Startup. Order matters: `link` must exist before the follower that drives
# through it, and the camera before the follower that reads from it.
# --------------------------------------------------------------------------
print(f"[motion] {motion.summary()}")

try:
    # Opens the serial connection to the ESP32.
    link = MotorLink(on_trip=_handle_trip)
except Exception as exc:
    # Not fatal any more. Before the merge this process was only the robot
    # server, so failing to find the ESP32 rightly took the whole thing down
    # - there was nothing left for it to do. That same failure would now
    # take the kiosk assistant down with it, which is not a sensible way for
    # Ruby to react to a loose USB cable. Everything that could actually
    # move the robot checks link.available and says why it can't.
    print(f"[hardware] No ESP32: {exc}")
    print("[hardware] Driving and Follow me are disabled; the kiosk runs normally.")
    link = NullMotorLink(str(exc))

camera.start()              # no-op-safe if no USB camera is plugged in; the UI hides the feed
face_follow.is_available()  # loads the YuNet model now, not on the first Follow me press

follower = FaceFollower(camera, link)
camera.set_overlay(follower.draw_overlay)  # green box around the tracked face on the video feed

threading.Thread(target=_connection_watchdog_loop, daemon=True).start()


def close():
    """Release the serial port and camera. Wired to atexit in app.py."""
    camera.close()
    link.close()
