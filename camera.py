"""
camera.py

Background USB-webcam capture for the web control page's live video feed.
Runs its own thread so frame capture keeps flowing independently of Flask
request handling; app.py's /video_feed route just serves whatever frame is
most recently captured.

Device selection: set CAMERA_DEVICE (e.g. "/dev/video0") to pin the exact
device once you know which node the webcam landed on - otherwise this probes
/dev/video* in ascending order and uses the first one that actually yields a
frame. That probe-and-verify step matters on the Pi: it also enumerates
/dev/video nodes belonging to the Pi 5's own internal ISP/HEVC decoder,
which open successfully but never produce a frame through a plain V4L2
capture, so a frame-read check is required to skip past them.
"""

import glob
import os
import threading
import time

import cv2

FRAME_WIDTH = int(os.environ.get("CAMERA_WIDTH", 1280))
FRAME_HEIGHT = int(os.environ.get("CAMERA_HEIGHT", 720))
TARGET_FPS = int(os.environ.get("CAMERA_FPS", 30))
JPEG_QUALITY = 80

# Resolutions the /video_feed stream can be downscaled to before JPEG encoding
# (independent of FRAME_WIDTH/FRAME_HEIGHT, which stay fixed - that's the
# capture resolution the webcam's hardware MJPEG mode was tuned for). This
# only shrinks what gets sent over the wire for the live feed; get_frame()
# (used by face_follow's detector) still returns the full-resolution capture.
STREAM_RESOLUTIONS = {
    "720p": (1280, 720),
    "360p": (640, 360),
    "240p": (426, 240),
}
# "off" is a valid stream mode alongside the resolutions above: the capture
# loop keeps grabbing frames (so get_frame()/follow mode are unaffected) but
# skips the overlay/resize/JPEG-encode work entirely, so /video_feed serves
# no frames - for isolating whether the video stream itself is contributing
# to command lag.
STREAM_MODES = set(STREAM_RESOLUTIONS) | {"off"}
_DEFAULT_STREAM_RESOLUTION = os.environ.get("CAMERA_STREAM_RESOLUTION", "360p")
if _DEFAULT_STREAM_RESOLUTION not in STREAM_MODES:
    _DEFAULT_STREAM_RESOLUTION = "360p"


def _candidate_devices():
    forced = os.environ.get("CAMERA_DEVICE")
    if forced:
        return [forced]
    # Prefer udev's stable /dev/v4l/by-id/ symlinks (keyed by the camera's
    # own USB serial number) over the raw numeric scan below - confirmed on
    # this hardware that it matters a lot: the Pi's internal ISP/codec nodes
    # (pispbe-*, e.g. /dev/video23-26 and 31-34) open successfully but then
    # block for a real ~10s each on the verification read in _open() before
    # failing. Scanning past all of them during a mid-session reconnect
    # (_capture_loop) can take well over a minute if the real camera lands
    # on a higher /dev/videoN number than they do this time around - by-id
    # points straight at the actual webcam regardless of its number. Falls
    # back to the numeric scan for anything by-id doesn't cover (e.g. no
    # udev by-id entry for some reason), so this is additive, not a
    # narrowing of what start()/_capture_loop can find.
    by_id = sorted(glob.glob("/dev/v4l/by-id/*-video-index*"))
    numeric = sorted(glob.glob("/dev/video*"), key=lambda p: int(p.rsplit("video", 1)[1]))
    by_id_targets = {os.path.realpath(p) for p in by_id}
    return by_id + [p for p in numeric if os.path.realpath(p) not in by_id_targets]


CAPTURE_FAILURE_RECONNECT_THRESHOLD = 10  # ~1s of consecutive failed reads

# How long to wait between full probe walks that found nothing. A failed walk
# is expensive - on this Pi it costs ~80s, because several of the internal
# ISP/codec nodes open successfully and then block ~10s each on the
# verification read - so retrying it on a 1s heartbeat would leave a core
# essentially always probing. Backs off to PROBE_RETRY_MAX_S, and resets on
# success so an unplug/replug is still picked up promptly.
PROBE_RETRY_MIN_S = 2.0
PROBE_RETRY_MAX_S = 60.0


def _open(path):
    cap = cv2.VideoCapture(path, cv2.CAP_V4L2)
    if not cap.isOpened():
        cap.release()
        return None
    # Request the webcam's onboard MJPEG compression, not raw YUYV - most USB
    # webcams can only sustain 30fps at this resolution in MJPEG; raw capture
    # over USB2 is often limited to a fraction of that.
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, FRAME_WIDTH)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
    cap.set(cv2.CAP_PROP_FPS, TARGET_FPS)
    ok, frame = cap.read()
    if not ok or frame is None:
        cap.release()
        return None
    return cap


def _probe():
    """Try each candidate device path in turn, returning (cap, path) for the
    first one that actually yields a frame, or (None, None). Shared by
    start() and the capture loop's reconnect path below - a device that
    disappears can come back under a different /dev/video* number (a
    whole-board USB brownout re-enumerating every USB device at once has
    been observed on this hardware; see CLAUDE.md), so recovery needs the
    same full probe, not just a re-open of the old path."""
    for path in _candidate_devices():
        cap = _open(path)
        if cap is not None:
            return cap, path
    return None, None


class Camera:
    def __init__(self):
        self._cap = None
        self.device_path = None
        self._frame = None
        self._frame_jpeg = None
        self._lock = threading.Lock()
        self._running = False
        self._thread = None
        self._overlay = None
        self._stream_resolution = _DEFAULT_STREAM_RESOLUTION

    def set_stream_resolution(self, name):
        """Change the /video_feed stream's target resolution, or "off" to
        stop producing frames for it. Returns True/False; unknown names are
        rejected rather than raising, since the caller is an HTTP route
        reflecting whatever the browser sent."""
        if name not in STREAM_MODES:
            return False
        with self._lock:
            self._stream_resolution = name
            if name == "off":
                # Drop the last-encoded frame immediately rather than
                # waiting for the capture loop's next iteration to notice -
                # otherwise mjpeg_generator would keep re-sending one stale
                # frame indefinitely instead of actually going quiet.
                self._frame_jpeg = None
        return True

    def get_stream_resolution(self):
        with self._lock:
            return self._stream_resolution

    def set_overlay(self, overlay):
        """overlay(frame) -> frame, called on a *copy* of each captured
        frame just before JPEG encoding for /video_feed - e.g. face_follow
        draws a box around the face it's tracking. Never touches what
        get_frame() hands back, so an overlay can't contaminate a detector's
        next input. None (the default) draws nothing."""
        self._overlay = overlay

    def start(self):
        """Start capturing. Returns immediately - the device probe happens on
        the capture thread, not here.

        This used to probe synchronously, so startup blocked until it found a
        camera or ran out of devices to try. A full failed walk costs ~80s on
        this Pi (see the comment above PROBE_RETRY_MIN_S), and since the merge
        that was 80s of blank kiosk screen on boot, not merely a video feed
        that showed up late - the assistant is served by the same process.
        The capture loop already re-probes whenever it has no device, so
        letting it do the first probe too costs nothing and unblocks startup.

        is_available() stays False until a camera actually turns up, which is
        what both UIs already poll for. Callers with nothing to do until there
        is a picture - follow_dryrun.py - should use wait_until_available()."""
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

    def wait_until_available(self, timeout=PROBE_RETRY_MAX_S):
        """Block until the probe finds a working camera, or give up. For
        foreground diagnostics; the servers never call this."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.is_available():
                return True
            time.sleep(0.2)
        return False

    def _capture_loop(self):
        consecutive_failures = 0
        retry_delay = PROBE_RETRY_MIN_S
        while self._running:
            if self._cap is None:
                # Either the first probe of the process, or the device went
                # away - e.g. the whole-board USB brownout documented in
                # CLAUDE.md, which can bring it back under a new /dev/video*
                # path. Keep re-probing, backing off, until it's there.
                self._cap, self.device_path = _probe()
                if self._cap is None:
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, PROBE_RETRY_MAX_S)
                    continue
                retry_delay = PROBE_RETRY_MIN_S
                print(f"[camera] Capturing on {self.device_path}")

            ok, frame = self._cap.read()
            if not ok:
                consecutive_failures += 1
                if consecutive_failures >= CAPTURE_FAILURE_RECONNECT_THRESHOLD:
                    print(f"[camera] Lost {self.device_path}, re-probing...")
                    self._cap.release()
                    self._cap = None
                    consecutive_failures = 0
                else:
                    time.sleep(0.1)
                continue
            consecutive_failures = 0
            with self._lock:
                self._frame = frame

            mode = self.get_stream_resolution()
            if mode == "off":
                continue

            display_frame = self._overlay(frame.copy()) if self._overlay is not None else frame
            target_w, target_h = STREAM_RESOLUTIONS[mode]
            if display_frame.shape[1] != target_w or display_frame.shape[0] != target_h:
                display_frame = cv2.resize(display_frame, (target_w, target_h), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", display_frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if ok:
                with self._lock:
                    self._frame_jpeg = buf.tobytes()

    def is_available(self):
        return self._cap is not None

    def get_frame(self):
        """Most recent raw BGR frame (a copy, safe to mutate), for callers
        that need pixels rather than the pre-encoded JPEG stream - e.g.
        face_follow.py's detector. None until the first frame arrives."""
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    def _get_jpeg(self):
        with self._lock:
            return self._frame_jpeg

    def mjpeg_generator(self):
        """Yields a multipart/x-mixed-replace stream - the standard trick for
        pushing a live JPEG sequence to a plain <img> tag with no extra JS.
        Ends the response as soon as the capture loop reports the device
        gone, rather than re-serving a stale frame forever: a frozen-but-
        still-open stream doesn't fire the browser's <img> error/reconnect
        handling, so an already-connected client would otherwise never
        notice the feed died at all."""
        boundary = b"--frame"
        while self._running and self._cap is not None:
            frame = self._get_jpeg()
            if frame is None:
                time.sleep(0.05)
                continue
            yield (
                boundary + b"\r\n"
                b"Content-Type: image/jpeg\r\n"
                b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n"
                + frame + b"\r\n"
            )
            time.sleep(1.0 / TARGET_FPS)

    def close(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1)
        if self._cap:
            self._cap.release()


camera = Camera()
