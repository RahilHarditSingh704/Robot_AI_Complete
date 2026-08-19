"""
robot_api.py

Everything under /robot: the remote control page and the endpoints behind
it. This is the half of the merged app that is reachable from the network,
and only while Remote Control is switched on at the kiosk - see access.py
for the policy and the single place it's enforced.

Two things moved out of here in the merge:

  - The Follow toggle. Follow me is a button on Ruby's own screen now, so
    POST /robot/follow is localhost-only (access.local_only). The remote
    page still *reads* GET /robot/follow_status, because it has to disable
    its D-pad while the follower is driving - /robot/command rejects manual
    commands during follow, and a page that didn't know that would just look
    broken.
  - The voice/LLM section, dropped entirely. Ruby handles conversation.
"""

from flask import Blueprint, Response, jsonify, render_template, request

import access
import face_follow
import hardware
import motion
from camera import camera
from hardware import follower, link

VIDEO_FEED_SOCKET_TIMEOUT_S = 10  # see video_feed() below

robot = Blueprint(access.ROBOT_BLUEPRINT, __name__, url_prefix="/robot")


@robot.route("/")
def index():
    # no-store: this page is actively evolving and a stale cached copy
    # silently missing new controls is worse than refetching.
    #
    # The D-pad's buttons and key bindings are built from the mapping
    # motion.py resolved at startup rather than hardcoded in the HTML, so
    # changing MOTION_PROFILE moves this page and the follower together
    # instead of leaving them disagreeing the way they used to.
    resp = Response(
        render_template("robot.html", commands=motion.WIRE_COMMANDS),
        mimetype="text/html",
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@robot.route("/command", methods=["POST"])
def command():
    if not link.available:
        return jsonify({"ok": False, "error": f"No ESP32 connected ({link.reason})"}), 503

    data = request.get_json(force=True, silent=True) or {}
    cmd = data.get("cmd", "")
    stop_cmd = motion.command_for(motion.STOP)

    if follower.is_running():
        # Stop always wins - it's the emergency-stop path (button, keyboard,
        # beforeunload beacon) and doubles as how someone at the remote page
        # takes back control from follow mode. Any other manual command while
        # follow is driving would just fight the follower over the serial
        # link, so it's rejected instead of silently racing it.
        if cmd.upper().strip() == stop_cmd:
            follower.stop()
        else:
            return jsonify({
                "ok": False,
                "error": "Follow me is running - press Stop before driving manually",
            }), 409

    try:
        link.send_command(cmd)
        return jsonify({"ok": True, "cmd": cmd, "label": motion.describe(cmd)})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@robot.route("/follow_status")
def follow_status():
    # Three separate things have to be present for Follow me to mean
    # anything: a camera to see with, the YuNet model to recognise a face,
    # and an ESP32 to actually move. The follower knows about the first two;
    # the link is checked here so Ruby hides the button rather than offering
    # one that would fail on press.
    return jsonify({
        "available": follower.is_available() and link.available,
        "enabled": follower.is_running(),
        "mode": follower.mode if follower.is_running() else None,
        "disabled_reason": follower.disabled_reason,
    })


@robot.route("/follow", methods=["POST"])
@access.local_only
def follow():
    """Ruby's Follow me button. Kiosk-only on purpose - see the module
    docstring."""
    data = request.get_json(force=True, silent=True) or {}
    enabled = bool(data.get("enabled"))
    # Body is the default so an older client that only sends {"enabled": true}
    # keeps its previous behaviour rather than erroring.
    mode = str(data.get("mode") or face_follow.MODE_BODY).strip().lower()

    if enabled:
        if mode not in face_follow.MODES:
            return jsonify({
                "ok": False,
                "error": f"Unknown follow mode {mode!r}, expected one of {list(face_follow.MODES)}",
            }), 400
        if not link.available:
            return jsonify({"ok": False, "error": f"No ESP32 connected ({link.reason})"}), 503
        if not follower.start(mode):
            return jsonify({"ok": False, "error": "Camera or face model not available"}), 400
    else:
        follower.stop()

    return jsonify({
        "ok": True,
        "enabled": follower.is_running(),
        "mode": follower.mode if follower.is_running() else None,
    })


@robot.route("/camera_status")
def camera_status():
    return jsonify({"available": camera.is_available()})


@robot.route("/motor_current")
def motor_current():
    """Measured current through each motor, in amps.

    Read by all three surfaces that show it - Ruby's screen, the Remote
    Control mini-app, and the driving page - rather than each deriving it
    some other way. The ESP32 measures it (it owns the sense pins, and
    already samples them for its own protection logic) and pushes it up the
    serial link; MotorLink just holds the latest one. So this endpoint is
    cheap enough to poll at 1Hz from several places at once: no hardware is
    touched, and no request is what causes a measurement to happen.

    `available: false` covers no ESP32, an ESP32 that has gone quiet, and one
    running firmware predating the CUR: line - all of which mean the same
    thing to a display, which is to show no number rather than a wrong one.

    `link` separates those cases for the one caller that needs them apart:
    the driving page, which warns when the ESP32 has stopped answering. See
    MotorLink.link_health(). It rides along here rather than getting its own
    endpoint because this is already the only thing polling fast enough to
    notice, and adding a second 1Hz poll to say the same thing would be waste.

    `tripped` is the ESP32's current-protection latch, which fires for every
    command source alike but previously had no Pi-side effect at all unless
    Follow me was the one driving. See MotorLink.trip_state().
    """
    return jsonify({
        **link.get_motor_currents(),
        "link": link.link_health(),
        "tripped": link.trip_state(),
    })


@robot.route("/wifi_status")
def wifi_status():
    return jsonify(hardware.get_wifi_signal())


@robot.route("/cpu_status")
def cpu_status():
    return jsonify(hardware.get_cpu_usage())


@robot.route("/heartbeat", methods=["POST"])
def heartbeat():
    """Sent continuously by the remote page, independent of button state -
    see HEARTBEAT_TIMEOUT_S in hardware.py for why this has to be separate
    from /command."""
    hardware.record_heartbeat()
    return jsonify({"ok": True})


@robot.route("/video_resolution", methods=["GET", "POST"])
def video_resolution():
    if request.method == "POST":
        data = request.get_json(force=True, silent=True) or {}
        if not camera.set_stream_resolution(data.get("resolution")):
            return jsonify({"ok": False, "error": "Invalid resolution"}), 400
        return jsonify({"ok": True, "resolution": camera.get_stream_resolution()})
    return jsonify({"resolution": camera.get_stream_resolution()})


@robot.route("/video_feed")
def video_feed():
    if not camera.is_available():
        return "", 404
    # gthread's socket send() has no default timeout, so a client that goes
    # dark mid-stream (WiFi drop) leaves the thread writing this response
    # blocked forever - it never returns to the pool. Repeat that a few
    # times (e.g. the browser retrying /video_feed after every drop) and
    # every one of the pool's few threads (gunicorn.conf.py: threads=8) ends
    # up permanently stuck on a dead socket, wedging the whole server even
    # for unrelated requests. gunicorn exposes the raw socket via
    # environ["gunicorn.socket"] for exactly this; bounding it here turns a
    # dead client into a normal dropped-connection exception instead of a
    # thread leak.
    sock = request.environ.get("gunicorn.socket")
    if sock is not None:
        sock.settimeout(VIDEO_FEED_SOCKET_TIMEOUT_S)

    allowed = access.stream_allowed()

    def gated_frames():
        """Switching Remote Control off has to actually cut the video, not
        just stop new viewers connecting: the before_request gate only sees
        requests, and this response's generator can outlive the switch for
        as long as a remote client holds the connection open."""
        for chunk in camera.mjpeg_generator():
            if not allowed():
                return
            yield chunk

    return Response(
        gated_frames(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )
