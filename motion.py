"""
motion.py

The one place that maps *what we want the robot to do* - drive forward,
rotate clockwise - onto the single ASCII characters the ESP32 firmware
accepts. Every control source goes through here: the remote page's D-pad,
its keyboard bindings, and face_follow's tracking loop.

WHY THIS EXISTS
---------------
The firmware's command names and the robot's actual behaviour do not agree,
and before these two projects were merged the two control sources disagreed
with each other about it:

  - The .ino calls 'F' forward (both motors driven the same way) and 'C'
    rotate-CW (motors driven opposite).
  - The web control page has always sent 'X' for its Forward button and 'F'
    for Rotate CW - the firmware's names swapped - and its on-screen status
    labels agree with the swap.
  - face_follow.py used the firmware's names literally.

Both cannot be right on one robot. On a differential drive the two motors
are mounted mirror-imaged, so driving them "the same way" electrically spins
the robot in place and driving them opposite makes it travel - i.e. the
firmware's labels are the ones that are inverted, and the web page's mapping
is what someone actually arrived at by driving the real robot. That is the
default here ("mirrored"). The consequence for the old code is that follow
mode was mis-driving: rotating when it meant to advance, and advancing when
it meant to turn.

None of this can be verified in software - only by watching the physical
robot move. So rather than hardcode a guess, all three plausible wiring
mistakes are one env var each, applied to every control source at once:

    MOTION_PROFILE=mirrored   drive/rotate as the web D-pad has always meant
                              them (default)
    MOTION_PROFILE=direct     take the firmware's own names literally
    MOTION_INVERT_DRIVE=1     robot goes backward when told to go forward
    MOTION_INVERT_TURN=1      robot turns CCW when told to turn CW

Set them in .env. If the robot rotates when you press Forward, you want the
other MOTION_PROFILE; if it drives the right axis but the wrong way round,
you want one of the invert flags.
"""

import os

# ---------------------------------------------------------------------------
# Semantic intents. Control sources speak in these; only this module knows
# which wire character each one currently corresponds to.
# ---------------------------------------------------------------------------
FORWARD = "forward"
BACKWARD = "backward"
TURN_CW = "turn_cw"
TURN_CCW = "turn_ccw"
STOP = "stop"

INTENTS = (FORWARD, BACKWARD, TURN_CW, TURN_CCW, STOP)

LABELS = {
    FORWARD: "Forward",
    BACKWARD: "Backward",
    TURN_CW: "Rotate CW",
    TURN_CCW: "Rotate CCW",
    STOP: "Stop",
}

# Every character the firmware's checkSerialCommand() will accept. This is the
# wire protocol and is not affected by any of the options below - those only
# change which intent maps to which character.
#
# Lowercase means "same direction, at the firmware's FOLLOW_DUTY instead of its
# DUTY" - a second, gentler speed. There is no lowercase 's': stop is stop.
# Only follow mode uses the slow set; the remote page always drives at full
# speed, which is why this is a separate character rather than global state.
VALID_WIRE_COMMANDS = frozenset({"F", "B", "C", "X", "S", "f", "b", "c", "x"})

_PROFILES = {
    # Motors mounted mirror-imaged (the normal differential-drive layout):
    # "both motors the same way" spins, "motors opposite" travels. Matches
    # what the control page's D-pad has always sent.
    "mirrored": {FORWARD: "X", BACKWARD: "C", TURN_CW: "F", TURN_CCW: "B", STOP: "S"},
    # The firmware's own labels taken at face value.
    "direct": {FORWARD: "F", BACKWARD: "B", TURN_CW: "C", TURN_CCW: "X", STOP: "S"},
}

PROFILE = os.environ.get("MOTION_PROFILE", "mirrored").strip().lower()
if PROFILE not in _PROFILES:
    print(f"[motion] unknown MOTION_PROFILE {PROFILE!r}, falling back to 'mirrored'")
    PROFILE = "mirrored"

INVERT_DRIVE = os.environ.get("MOTION_INVERT_DRIVE", "0") == "1"
INVERT_TURN = os.environ.get("MOTION_INVERT_TURN", "0") == "1"


def _resolve():
    wire = dict(_PROFILES[PROFILE])
    if INVERT_DRIVE:
        wire[FORWARD], wire[BACKWARD] = wire[BACKWARD], wire[FORWARD]
    if INVERT_TURN:
        wire[TURN_CW], wire[TURN_CCW] = wire[TURN_CCW], wire[TURN_CW]
    return wire


# intent -> wire character, with the env overrides above already applied.
WIRE_COMMANDS = _resolve()

# wire character -> human label, for status text and logs. Built by inverting
# WIRE_COMMANDS so it can never drift out of step with it.
WIRE_LABELS = {char: LABELS[intent] for intent, char in WIRE_COMMANDS.items()}


def command_for(intent, slow=False):
    """The wire character to send for a semantic intent. Raises on an unknown
    intent rather than defaulting to anything - a typo'd intent silently
    becoming Stop (or worse, Forward) is not a failure mode worth having in
    something that moves.

    slow=True picks the firmware's FOLLOW_DUTY variant (the lowercase
    character) for everything except Stop, which has only one spelling. The
    direction mapping - profile and invert flags - is applied first, so a
    slow command is always the same direction as its full-speed counterpart.
    """
    try:
        cmd = WIRE_COMMANDS[intent]
    except KeyError:
        raise ValueError(f"Unknown motion intent {intent!r}, expected one of {INTENTS}")
    return cmd.lower() if slow and intent != STOP else cmd


def describe(wire_cmd):
    """Human label for a wire character, e.g. for the page's status line."""
    if wire_cmd in WIRE_LABELS:
        return WIRE_LABELS[wire_cmd]
    upper = wire_cmd.upper()
    if upper in WIRE_LABELS:
        return f"{WIRE_LABELS[upper]} (slow)"
    return wire_cmd


def summary():
    """One line for the startup log, so which mapping is live is visible in
    the journal rather than something you have to infer from behaviour."""
    flags = []
    if INVERT_DRIVE:
        flags.append("drive inverted")
    if INVERT_TURN:
        flags.append("turn inverted")
    suffix = f" ({', '.join(flags)})" if flags else ""
    pairs = " ".join(f"{LABELS[i]}={WIRE_COMMANDS[i]}" for i in INTENTS if i != STOP)
    return f"profile={PROFILE}{suffix}: {pairs}"
