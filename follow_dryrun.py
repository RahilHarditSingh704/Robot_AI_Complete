"""
follow_dryrun.py

Standalone diagnostic for face_follow.py: runs the real FaceFollower against
the live camera, but with a fake motor link that just prints the command it
would have sent instead of touching MotorLink/the ESP32. Lets you verify
detection and the centering/distance decision logic with the camera sitting
on a desk, before it's mounted on the assembled robot (and before the ESP32
even needs to be plugged in).

Run:
    python3 follow_dryrun.py

Move around in front of the camera; Ctrl+C to stop. Only prints when the
decision changes, not on every detection pass, so the log reads as a
timeline of state transitions rather than a firehose.

This is also the cheapest way to sanity-check a MOTION_PROFILE change
without the robot: run it, stand off to one side, and confirm the intent it
prints is the one you'd expect. It can't tell you whether the character is
right for the hardware - only the robot can - but it will tell you the
tracking logic is asking for the right thing.
"""

import time

from camera import camera
from face_follow import FaceFollower, follow_command_for, is_available

import motion

WHY = {
    motion.FORWARD: "face is far",
    motion.BACKWARD: "face is close",
    motion.TURN_CW: "face is right of center",
    motion.TURN_CCW: "face is left of center",
    motion.STOP: "centered + good distance, or face lost",
}
# Wire character -> what the follower meant by it. Keyed on what follow mode
# actually puts on the wire - via face_follow's own resolver, so it stays
# honest across a MOTION_PROFILE change or either of the follow-only invert
# flags, rather than describing whatever the defaults were when this was
# written. It has to be that resolver and not motion.WIRE_COMMANDS: follow
# mode sends the lowercase FOLLOW_DUTY characters, so a map built from the
# full-speed ones matches nothing but Stop and every line prints unlabelled.
COMMAND_NAMES = {
    follow_command_for(intent): f"{motion.LABELS[intent].lower()} ({WHY[intent]})"
    for intent in motion.INTENTS
}


class PrintMotorLink:
    def __init__(self):
        self._last = None

    def send_command(self, cmd):
        if cmd == self._last:
            return
        self._last = cmd
        print(f"[{time.strftime('%H:%M:%S')}] -> {cmd}: {COMMAND_NAMES.get(cmd, cmd)}")


def main():
    # First, before any hardware check can bail out: which mapping is live is
    # the main thing you'd run this to find out.
    print(f"Motion mapping: {motion.summary()}")
    # start() returns immediately now (the probe runs on the capture thread),
    # so this waits for a picture rather than testing a return value. Probing
    # every /dev/video* node can take a while on this Pi when the camera isn't
    # the first thing it tries - see camera.py.
    camera.start()
    print("Looking for a camera…")
    if not camera.wait_until_available():
        print("No camera found - check it's plugged in, or set CAMERA_DEVICE.")
        camera.close()
        return
    if not is_available():
        print("Face model not available - check models/face_detection_yunet_2023mar.onnx exists, or set FACE_MODEL_PATH.")
        camera.close()
        return

    follower = FaceFollower(camera, PrintMotorLink())
    follower.start()
    print("Dry run active - no motors involved. Move around in front of the camera. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        follower.stop()
        camera.close()


if __name__ == "__main__":
    main()
