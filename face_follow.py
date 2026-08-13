"""
face_follow.py

Camera-driven "follow me" mode: detects the largest face in the live feed
and drives the robot to keep it centered and at a comfortable distance,
by calling MotorLink.send_command() - the same chokepoint the web UI's
D-pad and voice commands go through (see app.py's MotorLink docstring).

Detection uses OpenCV's YuNet (cv2.FaceDetectorYN), a small ONNX DNN model -
not the classic Haar cascade. Two reasons: the pinned opencv-python-headless
build (checked at 5.0.0) no longer ships the haarcascade_*.xml files under
cv2.data at all, so CascadeClassifier has nothing to load; and YuNet is also
just a better detector - it holds up far better on off-angle faces and
uneven lighting, which matters a lot for a camera bouncing around on a
moving robot.

Setup:
    mkdir -p models && cd models
    curl -LO https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx

Set FACE_MODEL_PATH if you put it somewhere else. If the model file isn't
found, is_available() returns false and the frontend hides the Follow me
button - same pattern as the camera when it isn't plugged in.

Which characters this sends now comes from motion.py rather than being
spelled out here. That is a behaviour fix, not just tidying: this module
used to use the firmware's command names literally ('F' to advance, 'C'/'X'
to turn) while the control page had always used them swapped, so on the real
robot follow mode rotated when it meant to drive at you and drove when it
meant to turn. Both sources agree now, and the mapping is one env var if it
still comes out wrong on the hardware - see motion.py.
"""

import os
import threading
import time

import cv2

import motion

# Resolved against this file, not the working directory, so the model is
# found however the process was started.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FACE_MODEL_PATH = os.environ.get(
    "FACE_MODEL_PATH", os.path.join(BASE_DIR, "models", "face_detection_yunet_2023mar.onnx")
)

def _env_float(name, default):
    """Tuning knobs are env-settable because none of them can be chosen
    correctly in software - they depend on the robot's rotation speed, the
    camera's field of view and latency, and how much floor it's on. Same
    reasoning as MOTION_PROFILE in motion.py: pick a sane default, let it be
    corrected without a code edit."""
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


DETECT_INTERVAL_S = 0.12     # ~8Hz - responsive enough to feel live, light enough for the Pi's CPU
DETECT_WIDTH = 320           # detector runs on a downscaled copy; full camera res is unnecessary work
CENTER_DEADZONE_FRAC = _env_float("FOLLOW_CENTER_DEADZONE", 0.15)  # +/- fraction of frame width considered "centered"

# Follow mode drives at the firmware's second, lower duty (FOLLOW_DUTY, the
# lowercase command characters - see motion.command_for(slow=True)). That is
# what makes a held turn usable here.
#
# It was previously pulsed instead: send a turn, run it for a short burst, stop,
# re-evaluate. That did bound the overshoot, but only by chopping the movement
# up, and it judders - the robot visibly steps its way around rather than
# tracking. The overshoot was never really about duration, it was about speed:
# at driving duty the robot covers a lot of arc during the ~120ms between
# decisions plus the camera's own latency, so by the time a frame says
# "centered" it has already gone past. Turning that duty down attacks the cause,
# and a slow turn can then simply be held until the face is centered, which is
# both smoother and simpler.
#
# If it now overshoots again, lower FOLLOW_DUTY in the .ino (needs a reflash);
# if it stalls or crawls, raise it.
CLOSE_ENTER_FRAC = 0.50      # face height / frame height above this -> too close, back up
CLOSE_EXIT_FRAC = 0.40       # must shrink below this before "too close" clears (hysteresis vs. flip-flop)
FAR_ENTER_FRAC = 0.20        # face height / frame height below this -> too far, drive forward
FAR_EXIT_FRAC = 0.28         # must grow above this before "too far" clears
LOST_FACE_TIMEOUT_S = 1.0    # how long a tracked face's overlay box stays drawn after detection loses it

# When a face leaves the frame it has almost always walked out of one side, and
# the robot was already turning that way. Cutting the motors the instant
# detection fails means it stops just short of catching up, and the person has
# to walk back into view to be picked up again. So keep turning the way they
# went for a moment first - usually enough to bring them back into frame on its
# own - and only then give up and stop.
LOST_COAST_S = _env_float("FOLLOW_LOST_COAST_S", 0.5)
# Which side they left by is taken from the last offset actually measured, so
# this also covers a face that vanished while still inside the deadzone but
# clearly drifting. Below this it was centered enough that the exit direction is
# a guess, and guessing means turning away from them half the time - so stop.
COAST_MIN_OFFSET_FRAC = _env_float("FOLLOW_COAST_MIN_OFFSET", 0.05)

# Flip if the robot turns AWAY from an off-center face instead of toward it.
# This one is about the camera: whether it's mounted facing the same way the
# robot drives, and whether its image is mirrored - so it only affects follow
# mode, which is the only thing reading the camera to decide a direction.
# If the robot instead turns the wrong way for everything, including the
# remote page's rotate buttons, that's the robot and not the camera - use
# MOTION_INVERT_TURN in motion.py. Neither is verifiable in software; both
# need someone watching the real robot turn.
TURN_INVERT = os.environ.get("FOLLOW_TURN_INVERT", "0") == "1"

BOX_COLOR_BGR = (94, 197, 34)   # matches the control page's --ok green (#22c55e)
BOX_THICKNESS = 2

_detector = None
_detector_error = None


def _load_detector():
    global _detector, _detector_error
    if _detector is not None or _detector_error is not None:
        return
    try:
        if not os.path.isfile(FACE_MODEL_PATH):
            raise RuntimeError(
                f"YuNet face model not found at '{FACE_MODEL_PATH}'. Download it from "
                "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
                "face_detection_yunet_2023mar.onnx, or set FACE_MODEL_PATH."
            )
        # input_size here is just a placeholder - _follow_loop calls
        # setInputSize() with each frame's actual downscaled dimensions.
        _detector = cv2.FaceDetectorYN_create(FACE_MODEL_PATH, "", (DETECT_WIDTH, DETECT_WIDTH))
        print(f"[face_follow] loaded YuNet model from {FACE_MODEL_PATH}")
    except Exception as e:
        _detector_error = str(e)
        print(f"[face_follow] disabled: {_detector_error}")


def is_available():
    _load_detector()
    return _detector is not None


def _update_distance_state(state, face_height_frac):
    """Enter/exit thresholds are deliberately different (hysteresis) so a
    face hovering right at one boundary doesn't make the robot flip-flop
    between forward and stop every other frame."""
    if state == "too_close":
        return "too_close" if face_height_frac >= CLOSE_EXIT_FRAC else "ok"
    if state == "too_far":
        return "too_far" if face_height_frac <= FAR_EXIT_FRAC else "ok"
    if face_height_frac >= CLOSE_ENTER_FRAC:
        return "too_close"
    if face_height_frac <= FAR_ENTER_FRAC:
        return "too_far"
    return "ok"


def _turn_command_for_offset(offset_frac):
    """Slow turn command that moves the robot toward a face this far
    off-center, with FOLLOW_TURN_INVERT applied."""
    turning_right = offset_frac > 0
    if TURN_INVERT:
        turning_right = not turning_right
    return motion.command_for(motion.TURN_CW if turning_right else motion.TURN_CCW, slow=True)


class FaceFollower:
    """
    Owns the background thread that turns face position into MotorLink
    commands while follow mode is on. start()/stop() are cheap and
    idempotent so both the /follow route and app.py's ESP32-trip callback
    can call stop() without coordinating with each other.
    """

    def __init__(self, camera, motor_link):
        self.camera = camera
        self.motor_link = motor_link
        self._lock = threading.Lock()
        self._running = False
        self._thread = None
        self.disabled_reason = None
        # Full-frame-pixel-coordinate box of whatever face is currently being
        # tracked, for draw_overlay() to draw on the video feed - set from
        # _follow_loop (a different thread), so it's guarded by self._lock
        # same as everything else here.
        self._last_box = None
        self._last_box_time = 0.0

    def is_available(self):
        return is_available() and self.camera.is_available()

    def is_running(self):
        return self._running

    def start(self):
        with self._lock:
            if self._running:
                return True
            if not self.is_available():
                return False
            self._running = True
            self.disabled_reason = None
            self._thread = threading.Thread(target=self._follow_loop, daemon=True)
            self._thread.start()
            return True

    def stop(self, reason=None):
        with self._lock:
            if not self._running:
                return
            self._running = False
            if reason:
                self.disabled_reason = reason
        self.motor_link.send_command(motion.command_for(motion.STOP))

    def _send(self, cmd):
        """Send one command, or report that follow mode was switched off.

        Every send from the loop goes through here so the re-check and the
        write stay atomic against stop(), which takes the same lock: a
        concurrent stop() (the /follow route, or app.py's ESP32-trip callback)
        can never have its "S" clobbered by a command this loop had already
        decided on beforehand. Returns False once stopped, which is the
        loop's cue to unwind - importantly, mid-pulse as well, so a trip
        during a turn burst does not still get its "stop the turn" write in
        after stop() already parked the motors.
        """
        with self._lock:
            if not self._running:
                return False
            self.motor_link.send_command(cmd)
            return True

    def draw_overlay(self, frame):
        """camera.py's overlay hook: draws a box around the face currently
        being tracked. No-op (returns frame unchanged) when follow is off,
        or once the tracked face has been gone longer than
        LOST_FACE_TIMEOUT_S - same staleness window _follow_loop itself uses
        before giving up and stopping, so the box's lifetime always matches
        what's actually driving the robot."""
        if not self._running:
            return frame
        with self._lock:
            box = self._last_box
            box_time = self._last_box_time
        if box is None or time.monotonic() - box_time > LOST_FACE_TIMEOUT_S:
            return frame
        x, y, w, h = box
        cv2.rectangle(frame, (int(x), int(y)), (int(x + w), int(y + h)), BOX_COLOR_BGR, BOX_THICKNESS)
        return frame

    def _follow_loop(self):
        """Crash barrier around the real loop.

        An unhandled exception in this thread is uniquely dangerous here: the
        thread dies, but nothing else notices. _running stays True so the UI
        still reports follow mode as on, and MotorLink's resend loop keeps
        re-sending whatever command was last set - so if the loop died just
        after issuing a turn, the firmware watchdog never fires (it is being
        fed) and the robot rotates until someone hits Stop. Catching here
        turns any such bug into "follow mode switches itself off and the
        motors park", which is how every other failure in this file behaves.
        """
        try:
            self._follow_loop_body()
        except Exception as exc:
            print(f"[face_follow] follow loop crashed, stopping: {exc}")
            self.stop(reason=f"Follow mode stopped after an internal error: {exc}")

    def _follow_loop_body(self):
        distance_state = "ok"
        last_seen = time.monotonic()
        # Signed horizontal offset of the last face actually detected. Kept
        # after the face is gone, which is the whole point: it is what decides
        # which way to coast. None until the first detection, so a face that is
        # never seen at all coasts nowhere.
        last_offset_frac = None

        while True:
            with self._lock:
                if not self._running:
                    return

            frame = self.camera.get_frame()
            if frame is None:
                time.sleep(DETECT_INTERVAL_S)
                continue

            h, w = frame.shape[:2]
            scale = DETECT_WIDTH / w
            small = cv2.resize(frame, (DETECT_WIDTH, int(h * scale)))
            _detector.setInputSize((small.shape[1], small.shape[0]))
            _, faces = _detector.detect(small)

            now = time.monotonic()
            if faces is None or len(faces) == 0:
                # Coast briefly the way they were last heading, then give up.
                # Note this runs every iteration while the face is missing, so
                # it re-sends the same turn character - which MotorLink and the
                # firmware both treat as a keep-alive, not a new command, so
                # the protection counters keep accumulating normally.
                if (
                    last_offset_frac is not None
                    and abs(last_offset_frac) >= COAST_MIN_OFFSET_FRAC
                    and now - last_seen <= LOST_COAST_S
                ):
                    cmd = _turn_command_for_offset(last_offset_frac)
                else:
                    cmd = motion.command_for(motion.STOP)
                    distance_state = "ok"   # reassess distance fresh once a face is reacquired
                    last_offset_frac = None  # don't coast again on the next loss
            else:
                last_seen = now
                # Largest face = whoever's closest/most prominent if several people are in frame
                fx, fy, fw, fh = max(faces, key=lambda f: f[2] * f[3])[:4]
                full_box = (fx / scale, fy / scale, fw / scale, fh / scale)
                with self._lock:
                    self._last_box = full_box
                    self._last_box_time = now
                face_center_x = (fx + fw / 2) / scale
                face_height_frac = (fh / scale) / h
                offset_frac = float((face_center_x - w / 2) / w)
                last_offset_frac = offset_frac

                if abs(offset_frac) > CENTER_DEADZONE_FRAC:
                    cmd = _turn_command_for_offset(offset_frac)
                else:
                    distance_state = _update_distance_state(distance_state, face_height_frac)
                    cmd = motion.command_for({
                        "too_close": motion.BACKWARD,
                        "too_far": motion.FORWARD,
                        "ok": motion.STOP,
                    }[distance_state], slow=True)

            # Every command here is held, not pulsed: whatever was decided this
            # iteration simply stays in force until the next one changes it,
            # kept alive against the firmware watchdog by MotorLink's resend
            # loop. That is what makes the movement continuous rather than
            # stepped - the slow duty, not a short duration, is what keeps it
            # from overshooting.
            if not self._send(cmd):
                return

            time.sleep(DETECT_INTERVAL_S)
