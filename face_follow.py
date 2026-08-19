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

Two of those env vars live here rather than there, FOLLOW_DRIVE_INVERT and
FOLLOW_TURN_INVERT, for cases where follow mode alone is inverted: the robot's
wiring is a fact about the robot and belongs in motion.py, but which way the
camera looks is a fact about follow mode, and only this module reads a camera
to decide a direction. Both are applied in follow_command_for().
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


# How long to wait between detections. The loop's actual period is this plus
# the detection itself (~41ms at DETECT_WIDTH 640), so 0.06 gives roughly 10Hz.
#
# This is not just a smoothness knob, it is half the dead time in the turn
# controller below: every decision is acted on for one whole period before it
# can be revised, so the period sets the smallest turn the robot is capable of
# making, and a robot that cannot make a turn smaller than its deadzone can
# only pace back and forth across the face. It was 0.12 (a ~8Hz loop, 0.16s
# period); at 0.06 the same detector runs on a ~0.10s period, which simulation
# puts on the right side of that line for sweep rates up to ~2 frame-widths a
# second where 0.16 was not.
#
# The cost is CPU: one 41ms detection per period is ~40% of one of the Pi 5's
# four cores rather than ~25%. That is real but affordable, and it is visible
# live on the CPU readout if it ever isn't.
DETECT_INTERVAL_S = _env_float("FOLLOW_DETECT_INTERVAL_S", 0.06)

# Width the detector actually sees. This is the single biggest lever on how
# far away a face can still be found: YuNet needs roughly 10-20 pixels of face
# to fire, so halving this halves the range at which someone is detectable.
# It was 320, which is why faces dropped out at a few paces.
#
# Measured on this Pi 5 (per detection, 1280x720 source):
#     320 ->  7.0ms      480 -> 22.9ms      640 -> 40.7ms      800 -> 67.6ms
# That cost lands once per loop period, so it is now ~40% of one of the Pi 5's
# four cores rather than the ~25% it was on the slower loop (see
# DETECT_INTERVAL_S above), leaving room for the capture thread and the MJPEG
# encoder that share this process. Roughly doubles the usable range over 320.
DETECT_WIDTH = int(_env_float("FOLLOW_DETECT_WIDTH", 640))

# YuNet's own confidence floor. The library default is 0.9, which is tuned for
# not embarrassing yourself on a benchmark - it discards exactly the faint,
# small, off-angle detections a distant person produces. 0.6 keeps those. The
# cost is the occasional false positive, which matters less here than it looks:
# the loop tracks the largest face in frame, and spurious detections are almost
# always small.
SCORE_THRESHOLD = _env_float("FOLLOW_SCORE_THRESHOLD", 0.6)
NMS_THRESHOLD = _env_float("FOLLOW_NMS_THRESHOLD", 0.3)
CENTER_DEADZONE_FRAC = _env_float("FOLLOW_CENTER_DEADZONE", 0.15)  # +/- fraction of frame width considered "centered"

# --- Stopping a turn before the overshoot, not after it ---------------------
# A held turn is a bang-bang controller, and the thing it controls is behind
# it: one detection interval, plus however old the frame already was when
# get_frame() handed it over, plus the time the wheels take to actually stop.
# By the time a frame reports "centered" the robot has already swung past, so
# the next frame corrects back - and if that swing is wider than the deadzone
# (at FOLLOW_DUTY on a differential drive it easily is, a fifth of a second at
# ~80 deg/s is most of a 70 degree field of view) it never lands inside it and
# just paces back and forth across the face forever.
#
# Waiting for the offset to reach zero is the mistake: by then the command to
# stop is already too late. So the release decision is made on where the
# offset is *heading* rather than where it is - measure how fast it is closing
# between two detections, project that forward by TURN_LEAD_S, and stop when
# the projection lands centered. The robot then coasts the rest of the way in
# on its own momentum instead of driving through it.
#
# This is self-calibrating in a way a fixed threshold isn't: a fast robot
# measures a fast closing rate and brakes correspondingly earlier, and if the
# lead is set too short it degrades into short repeated pulses that still
# converge, rather than into oscillation. TURN_LEAD_S is the one to raise if
# it still swings past (and to lower if it now creeps in from one side in
# visible steps).
TURN_LEAD_S = _env_float("FOLLOW_TURN_LEAD_S", 0.28)

# How close the *projected* offset has to land before the turn is released.
# Deliberately tighter than CENTER_DEADZONE_FRAC, which stays the threshold
# for starting a turn: turning until nearly centered and then not moving again
# until the face is clearly off-centre is ordinary hysteresis, and it is what
# stops a face parked near the edge of the deadzone from twitching the wheels.
TURN_RELEASE_FRAC = _env_float("FOLLOW_TURN_RELEASE", 0.05)

# A closing rate is only meaningful between two detections close enough
# together that the robot was doing the same thing throughout. Across a longer
# gap (detection dropped the face for a moment, or it was just re-acquired)
# there is no usable slope, and _TurnController falls back to the plain
# deadzone rather than braking for a number it made up.
RATE_MAX_GAP_S = _env_float("FOLLOW_RATE_MAX_GAP_S", 0.5)

# Ceiling on the self-measured deadzone (see _TurnController.deadzone). Past
# this the robot is genuinely swinging further per decision than any deadzone
# worth having, and widening it further stops being "don't chase noise" and
# becomes "give up on tracking" - a face could sit a third of the frame off
# centre and be considered fine. Hitting the cap is the signal that the fix is
# a lower FOLLOW_DUTY in the .ino, which is the one lever that shrinks the
# swing itself.
TURN_DEADZONE_MAX = _env_float("FOLLOW_TURN_DEADZONE_MAX", 0.35)

# Peak-hold decay for that measurement, per detection. Peak-hold rather than a
# plain average because what matters is the widest swing the robot makes, not
# its typical one; the decay is what lets the estimate come back down after a
# fast turn on a slippery floor, or after FOLLOW_DUTY is lowered.
SWEEP_DECAY = _env_float("FOLLOW_SWEEP_DECAY", 0.85)

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
# Lowering the duty was not on its own enough - it made each overshoot smaller
# without making it smaller than the deadzone, so the pacing back and forth
# survived at a slower speed. Releasing the turn early (TURN_LEAD_S above) is
# what removes it, and it is the knob to reach for first because it costs no
# reflash. FOLLOW_DUTY is still the one to raise if follow mode stalls or
# crawls; note that lowering it does not help centering any more and does risk
# sitting near stall, so it should not be the answer to a tracking complaint.

# How near is near enough, measured as the face's height over the frame's.
# Face size is the only distance cue a single camera gives, and it is a decent
# one: the fraction is inversely proportional to distance, so on this camera
# (1280x720, ~43 degrees vertical) a face reads roughly 0.23/metres - about
# 0.28 at arm's length and 0.20 at a bit over a metre.
#
# That inverse relationship is why the *span* between the two enter thresholds
# matters more than either number: 0.20 to 0.50 sounds narrow but is a 2.5x
# ratio in distance, i.e. anywhere from half a metre to well over one, and
# inside it the robot holds still. Following someone who steps away therefore
# meant waiting until they were most of a room away before anything happened.
# FAR_ENTER is raised so it reacts while they are still leaving rather than
# already gone, and FAR_EXIT with it so the gap it closes to stays sensible.
#
# Env-settable for the same reason every other threshold in this file is: this
# depends on the camera's field of view and on how much of a head YuNet's box
# actually covers, and the only way to land on a number is to walk away from
# the robot and watch. Keep the ordering FAR_ENTER < FAR_EXIT < CLOSE_EXIT <
# CLOSE_ENTER - the hysteresis is meaningless otherwise.
CLOSE_ENTER_FRAC = _env_float("FOLLOW_CLOSE_ENTER", 0.50)  # face height / frame height above this -> too close, back up
CLOSE_EXIT_FRAC = _env_float("FOLLOW_CLOSE_EXIT", 0.40)    # must shrink below this before "too close" clears (hysteresis vs. flip-flop)
FAR_ENTER_FRAC = _env_float("FOLLOW_FAR_ENTER", 0.28)      # face height / frame height below this -> too far, drive forward
FAR_EXIT_FRAC = _env_float("FOLLOW_FAR_EXIT", 0.36)        # must grow above this before "too far" clears
LOST_FACE_TIMEOUT_S = 1.0    # how long a tracked face's overlay box stays drawn after detection loses it

# When a face leaves the frame it has almost always walked out of one side, and
# the robot was already turning that way. Cutting the motors the instant
# detection fails means it stops just short of catching up, and the person has
# to walk back into view to be picked up again. So keep turning the way they
# went for a moment first - usually enough to bring them back into frame on its
# own - and only then give up and stop.
LOST_COAST_S = _env_float("FOLLOW_LOST_COAST_S", 0.5)

# --- Follow modes ----------------------------------------------------------
# Two ways to keep a face centered, picked by the user when they tap Follow me.
MODE_BODY = "body"   # drive the wheels: the whole robot turns and holds distance
MODE_HEAD = "head"   # pan the servo only: the wheels never move
MODES = (MODE_BODY, MODE_HEAD)

# --- Head mode -------------------------------------------------------------
# The camera rides on the servo, so head mode closes the loop exactly the way
# body mode does: a face drifting right means turn the head right, which brings
# the face back toward the middle of the frame. Nothing here needs to know the
# robot's heading or the head's position relative to the body.
HEAD_CENTER_DEG = _env_float("HEAD_CENTER_DEG", 90.0)
HEAD_MIN_DEG = _env_float("HEAD_MIN_DEG", 0.0)
HEAD_MAX_DEG = _env_float("HEAD_MAX_DEG", 180.0)
# Horizontal field of view, used to convert "face is 20% of the frame off
# centre" into degrees. It does not have to be exact - being wrong just scales
# the loop gain, which HEAD_GAIN already damps - but the closer it is, the
# faster the head settles.
HEAD_FOV_DEG = _env_float("HEAD_CAMERA_FOV_DEG", 70.0)
# Fraction of the measured error to correct per detection. Below 1.0 on
# purpose: correcting the whole error every frame, on top of the ~120ms
# detection interval and the camera's own latency, is how a tracker starts
# hunting back and forth. Halving it converges in a few frames with no
# overshoot.
HEAD_GAIN = _env_float("HEAD_GAIN", 0.5)
# Head mode gets its own, tighter deadzone. CENTER_DEADZONE_FRAC is sized for
# the wheels, where it absorbs gear lash and the momentum of a robot that
# cannot stop instantly - at a 70 degree field of view its 0.15 is over 10
# degrees, which on a head reads as not quite looking at you. A servo has
# neither problem, so it can be held far closer to centre without hunting.
HEAD_DEADZONE_FRAC = _env_float("HEAD_DEADZONE", 0.06)
# Don't bother re-commanding for sub-degree changes; it is below what the
# servo resolves and just adds serial traffic.
HEAD_MIN_STEP_DEG = _env_float("HEAD_MIN_STEP_DEG", 0.8)
# Set HEAD_INVERT=1 if the head turns away from you instead of toward you.
# Which way the servo's angle increases depends on how it is mounted and
# geared, and like FOLLOW_TURN_INVERT it can only be settled by watching it.
HEAD_INVERT = os.environ.get("HEAD_INVERT", "0") == "1"
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

# The same idea for the other axis: flip if body mode backs away from someone
# standing too far off and closes in on someone already too near, while the
# remote page's Forward button drives the right way. Separate from
# MOTION_INVERT_DRIVE for exactly the reason TURN_INVERT is separate from
# MOTION_INVERT_TURN - that one is a fact about the robot's wiring and applies
# to every control source at once, so setting it to fix follow mode would flip
# the D-pad along with it. If Forward on the remote page is wrong too, the
# robot is what's inverted and MOTION_INVERT_DRIVE is the flag to change; the
# two cancel out here if both are set.
DRIVE_INVERT = os.environ.get("FOLLOW_DRIVE_INVERT", "0") == "1"

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
        _detector = cv2.FaceDetectorYN_create(
            FACE_MODEL_PATH, "", (DETECT_WIDTH, DETECT_WIDTH),
            SCORE_THRESHOLD, NMS_THRESHOLD, 5000,
        )
        print(
            f"[face_follow] loaded YuNet model from {FACE_MODEL_PATH} "
            f"(detect width {DETECT_WIDTH}, score threshold {SCORE_THRESHOLD})"
        )
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


class _TurnController:
    """Body mode's turn decision, and the two things it has to remember.

    Turning toward a face is a bang-bang decision - the wheels are either on
    at FOLLOW_DUTY or off - taken by a loop that is looking at a picture from
    a moment ago and whose command will run for a whole detection period
    before it can be revised. That dead time is what made follow mode pace
    back and forth across a face instead of settling on it, and it takes two
    separate corrections to fix, which is why this is a small object rather
    than a function:

    **Release early.** Waiting for the measured offset to reach the middle
    means commanding the stop after the robot is already there, so it sails
    past and the next frame corrects back. Instead the offset's closing rate
    is measured between detections and projected TURN_LEAD_S ahead, and the
    turn is released once the *projection* lands centered. The robot then
    coasts the last of the way in rather than driving through.

    **Don't ask for a correction smaller than the robot can make.** Releasing
    early bounds the overshoot but there is still a floor under it: once the
    wheels start, they run for at least a full period plus however long they
    take to stop. If that smallest possible swing is wider than the deadzone,
    every correction lands outside it on the far side and the robot oscillates
    forever, however well-timed the release was - the deadzone is asking for a
    precision the drivetrain does not have. So the sweep rate is measured
    while turning and the deadzone widened to match, up to TURN_DEADZONE_MAX.

    Simulated against a model of the loop (camera lag, one period of
    quantisation, and a stop that coasts) across sweep rates from 0.4 to 2.8
    frame-widths per second: the old fixed deadzone hunted from 0.8 upward,
    this settles cleanly to 2.0 and only the extreme 2.8 case still paces.
    """

    def __init__(self):
        self.turning = False
        # Frame-widths per second the robot sweeps a face across the frame
        # while turning. Measured rather than configured: it depends on
        # FOLLOW_DUTY, the floor, the battery, and the camera's field of view,
        # and the loop is already computing the number it needs. 0 until the
        # first turn, where the plain deadzone applies.
        self.sweep_rate = 0.0

    def deadzone(self):
        """How far off centre a face has to be before starting a turn.

        CENTER_DEADZONE_FRAC is the floor, not the answer: see the class
        docstring for why one dead time's worth of sweep is the real lower
        bound on what is worth asking for.
        """
        if self.sweep_rate <= 0.0:
            return CENTER_DEADZONE_FRAC
        return min(TURN_DEADZONE_MAX, max(CENTER_DEADZONE_FRAC, self.sweep_rate * TURN_LEAD_S))

    def update(self, offset_frac, offset_rate):
        """Should the wheels be turning toward the face? Also learns the sweep
        rate, so call this once per detection and only with a live one.

        `offset_rate` is the measured closing rate in frame-widths per second,
        or None when the last two detections were too far apart to difference
        honestly.
        """
        if offset_frac is None:
            self.turning = False
            return False

        # Only learn while the wheels were actually turning for the whole
        # interval this rate was measured over - otherwise the number being
        # peak-held is the speed the person walks, which is not what the
        # deadzone needs to cover.
        if self.turning and offset_rate is not None:
            self.sweep_rate = max(self.sweep_rate * SWEEP_DECAY, abs(offset_rate))

        if not self.turning:
            # Starting is a judgement about where the face actually is. A
            # projection is only trustworthy while something is already
            # moving, and reading one here would have the robot chasing people
            # who are walking back toward the middle on their own.
            self.turning = abs(offset_frac) > self.deadzone()
            return self.turning

        if offset_rate is None:
            # No usable rate this frame: brake on the plain deadzone rather
            # than aim for a tighter target with no idea how far the coast
            # will carry.
            self.turning = abs(offset_frac) > CENTER_DEADZONE_FRAC
            return self.turning

        projected = offset_frac + offset_rate * TURN_LEAD_S
        if projected * offset_frac <= 0:
            # Already crossing the middle, or projected to. Stopping a frame
            # early leaves the face slightly off centre; stopping a frame late
            # is the overshoot this exists to remove.
            self.turning = False
        else:
            self.turning = abs(projected) > TURN_RELEASE_FRAC
        return self.turning

    def coast(self):
        """The face is gone and the loop is holding its last turn - keep the
        state consistent with what the wheels are doing without judging an
        offset there is no live measurement for."""
        self.turning = True


# Both follow-only inverts, as intent -> intent. Applying them by swapping the
# intent (rather than by picking a different branch at each call site) is what
# lets follow_command_for below be a single honest answer to "what does body
# mode send when it means this?", which is what follow_dryrun.py labels its
# output from.
_INVERT_DRIVE_INTENT = {motion.FORWARD: motion.BACKWARD, motion.BACKWARD: motion.FORWARD}
_INVERT_TURN_INTENT = {motion.TURN_CW: motion.TURN_CCW, motion.TURN_CCW: motion.TURN_CW}

# Which way to drive for each distance verdict. "ok" is the whole point of the
# hysteresis in _update_distance_state: near enough, hold still.
_DISTANCE_INTENTS = {
    "too_close": motion.BACKWARD,
    "too_far": motion.FORWARD,
    "ok": motion.STOP,
}


def follow_command_for(intent):
    """The wire character body mode sends when it means `intent`: the slow
    (FOLLOW_DUTY) variant from motion.py, with follow mode's own
    FOLLOW_DRIVE_INVERT/FOLLOW_TURN_INVERT applied on top. Stop has no
    direction, so neither flag touches it."""
    if DRIVE_INVERT:
        intent = _INVERT_DRIVE_INTENT.get(intent, intent)
    if TURN_INVERT:
        intent = _INVERT_TURN_INTENT.get(intent, intent)
    return motion.command_for(intent, slow=True)


def _turn_command_for_offset(offset_frac):
    """Slow turn command that moves the robot toward a face this far
    off-center."""
    return follow_command_for(motion.TURN_CW if offset_frac > 0 else motion.TURN_CCW)


def _drive_command_for_state(distance_state):
    """Slow drive command that closes the gap to the face."""
    return follow_command_for(_DISTANCE_INTENTS[distance_state])


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
        # Which of MODES is running. Meaningless while stopped, but kept
        # rather than cleared so the UI can show what was last used.
        self.mode = MODE_BODY
        # Where the head has been commanded to. Tracked here, not read back
        # from the ESP32, because the firmware slews toward this target on its
        # own clock - so it is behind this value most of the time by design,
        # and steering off the actual position would fight that ramp.
        self._head_angle = HEAD_CENTER_DEG

    def is_available(self):
        return is_available() and self.camera.is_available()

    def is_running(self):
        return self._running

    def start(self, mode=MODE_BODY):
        if mode not in MODES:
            raise ValueError(f"Unknown follow mode {mode!r}, expected one of {MODES}")
        with self._lock:
            if self._running:
                return True
            if not self.is_available():
                return False
            self._running = True
            self.mode = mode
            self._head_angle = HEAD_CENTER_DEG
            self.disabled_reason = None
            self._thread = threading.Thread(target=self._follow_loop, daemon=True)
            self._thread.start()
            return True

    def stop(self, reason=None):
        with self._lock:
            if not self._running:
                return
            was_head = self.mode == MODE_HEAD
            self._running = False
            if reason:
                self.disabled_reason = reason
        # Stop the wheels unconditionally, including in head mode where they
        # should already be stopped - this is the path a current-protection
        # trip comes through, and "make sure the motors are parked" is not
        # something to make conditional on believing our own state.
        self.motor_link.send_command(motion.command_for(motion.STOP))
        if was_head:
            # Return the head to neutral so the robot doesn't sit staring off
            # to one side. The firmware slews, so this is a smooth sweep back,
            # not a snap.
            self._head_angle = HEAD_CENTER_DEG
            self.motor_link.set_head_angle(HEAD_CENTER_DEG)

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

    def _send_head(self, degrees):
        """set_head_angle() under the same guard _send() uses, for the same
        reason: a stop() landing mid-decision must win."""
        with self._lock:
            if not self._running:
                return False
            self.motor_link.set_head_angle(degrees)
            return True

    def _step_head(self, offset_frac):
        """Head mode: pan the servo toward the face, wheels untouched.

        offset_frac is None when there is no face and nothing to coast toward.
        """
        if offset_frac is None or abs(offset_frac) <= HEAD_DEADZONE_FRAC:
            return self._still_running()

        # offset_frac is a fraction of frame width, so multiplying by the
        # horizontal field of view converts it straight into "the face is this
        # many degrees off the middle of the picture". Because the camera is on
        # the head, that is also exactly how far the head has to move.
        correction = offset_frac * HEAD_FOV_DEG * HEAD_GAIN
        if HEAD_INVERT:
            correction = -correction

        target = max(HEAD_MIN_DEG, min(HEAD_MAX_DEG, self._head_angle + correction))
        if abs(target - self._head_angle) < HEAD_MIN_STEP_DEG:
            return self._still_running()
        self._head_angle = target
        return self._send_head(target)

    def _step_body(self, turn, offset_frac, offset_rate, face_height_frac,
                   distance_state):
        """Body mode: drive the wheels. Returns (distance_state, still_running).

        `turn` is the _TurnController, which carries its own state across
        calls; `distance_state` is still passed through because it is a plain
        two-threshold verdict with nothing to remember beyond itself.
        """
        if offset_frac is None:
            turn.update(None, None)
            return distance_state, self._send(motion.command_for(motion.STOP))

        if face_height_frac is None:
            # Coasting toward a face that's gone: hold the turn rather than
            # reassessing a distance we can no longer measure. Deliberately
            # not put to the turn controller either - there is no live
            # measurement left to project, and the point of the coast is to
            # keep going the way they went until they come back into frame.
            turn.coast()
            return distance_state, self._send(_turn_command_for_offset(offset_frac))

        if turn.update(offset_frac, offset_rate):
            return distance_state, self._send(_turn_command_for_offset(offset_frac))

        distance_state = _update_distance_state(distance_state, face_height_frac)
        return distance_state, self._send(_drive_command_for_state(distance_state))

    def _still_running(self):
        with self._lock:
            return self._running

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
        if self.mode == MODE_HEAD:
            # Park the wheels once up front. Head mode never issues a motor
            # command afterwards, so without this it would inherit whatever
            # the last manual command happened to leave held - and the resend
            # loop would keep driving it.
            if not self._send(motion.command_for(motion.STOP)):
                return
            if not self._send_head(self._head_angle):
                return

        distance_state = "ok"
        turn = _TurnController()
        last_seen = time.monotonic()
        # Signed horizontal offset of the last face actually detected. Kept
        # after the face is gone, which is the whole point: it is what decides
        # which way to coast. None until the first detection, so a face that is
        # never seen at all coasts nowhere.
        last_offset_frac = None
        # The previous *live* detection, for differencing into a closing rate.
        # Separate from last_offset_frac because that one deliberately outlives
        # the face and this one must not: a slope measured across the gap where
        # detection lost someone describes nothing that happened.
        prev_offset_frac = None
        prev_offset_time = 0.0

        while True:
            with self._lock:
                if not self._running:
                    return

            frame = self.camera.get_frame()
            if frame is None:
                time.sleep(DETECT_INTERVAL_S)
                continue

            h, w = frame.shape[:2]
            # Never upscale: if DETECT_WIDTH is at or above the camera's own
            # width there is no detail to recover, only work to invent.
            if DETECT_WIDTH < w:
                scale = DETECT_WIDTH / w
                small = cv2.resize(frame, (DETECT_WIDTH, int(h * scale)))
            else:
                scale = 1.0
                small = frame
            _detector.setInputSize((small.shape[1], small.shape[0]))
            _, faces = _detector.detect(small)

            now = time.monotonic()
            # face_height_frac stays None whenever there's no live measurement
            # this frame - i.e. while coasting - so the distance controller
            # knows not to act on a stale size.
            face_height_frac = None
            if faces is None or len(faces) == 0:
                # Coast briefly the way they were last heading, then give up.
                # In body mode this re-sends the same turn character every
                # iteration, which MotorLink and the firmware both treat as a
                # keep-alive rather than a new command, so the protection
                # counters keep accumulating normally.
                # No live measurement, so no honest slope either - the coast
                # holds its turn without consulting one anyway.
                offset_rate = None
                if (
                    last_offset_frac is not None
                    and abs(last_offset_frac) >= COAST_MIN_OFFSET_FRAC
                    and now - last_seen <= LOST_COAST_S
                ):
                    offset_frac = last_offset_frac
                else:
                    offset_frac = None
                    distance_state = "ok"    # reassess distance fresh once a face is reacquired
                    last_offset_frac = None  # don't coast again on the next loss
                    prev_offset_frac = None
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

                # How fast the face is closing on the middle of the frame, in
                # frame-widths per second. This is what lets the turn be
                # released before the overshoot rather than after it - see
                # _TurnController. It measures the robot's own swing and the
                # person's walking together, which is the sum that matters.
                dt = now - prev_offset_time
                if prev_offset_frac is not None and 0 < dt <= RATE_MAX_GAP_S:
                    offset_rate = (offset_frac - prev_offset_frac) / dt
                else:
                    offset_rate = None
                prev_offset_frac = offset_frac
                prev_offset_time = now
                last_offset_frac = offset_frac

            # The two modes share everything above - capture, detection,
            # picking a face, the deadzone, and the coast - and differ only in
            # what they move. Head mode never touches the wheels at all.
            if self.mode == MODE_HEAD:
                if not self._step_head(offset_frac):
                    return
            else:
                distance_state, running = self._step_body(
                    turn, offset_frac, offset_rate, face_height_frac, distance_state
                )
                if not running:
                    return

            time.sleep(DETECT_INTERVAL_S)
