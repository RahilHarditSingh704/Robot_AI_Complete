"""
access.py

Who is allowed to talk to what. This is the one file to read (and the one to
be careful editing) to understand the merged app's exposure, so the whole
policy lives here rather than being spread across route decorators.

The problem this solves. Before the merge the two halves had opposite
network postures, each correct for what it was:

  - The kiosk assistant bound 127.0.0.1 only, on purpose: its endpoints shell
    out to the desktop session and can set the volume, dim the screen or
    power the Pi off. Nothing on the network could reach any of that.
  - The robot control server bound 0.0.0.0, also on purpose: driving the
    robot from a phone is the entire point of it.

Merging them means one process on one socket, and it has to bind 0.0.0.0 for
the robot half to work at all. Left alone, that would silently publish the
kiosk's power and volume controls to the whole LAN - a real regression, and
exactly the kind that is invisible until someone finds it. So the bind is
network-wide and the *policy* is enforced here instead:

  /robot/...   reachable from the network, but only while Remote Control is
               switched on at the kiosk (see the Remote Control mini-app).
               Always reachable from the Pi itself, since that is Ruby
               pressing her own Follow me button.
  everything   localhost only, always - Ruby's UI, her chat/TTS endpoints,
  else         and the system controls that were never network-facing.

Remote access is off on every boot and is deliberately not persisted: a Pi
that reboots unattended should not come back up with its motors reachable
from the network because of a switch someone flipped days ago. Turning it on
is one tap at the kiosk.
"""

import threading
from functools import wraps

from flask import abort, request

# IPv4 loopback, IPv6 loopback, and the IPv4-mapped-IPv6 form a dual-stack
# listener reports for a v4 client. gunicorn is not behind a proxy here, so
# remote_addr is the real peer and no X-Forwarded-For handling is wanted -
# trusting that header would let any client claim to be local.
LOCAL_ADDRESSES = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})

ROBOT_BLUEPRINT = "robot"

_lock = threading.Lock()
_remote_enabled = False


def is_local_request():
    return request.remote_addr in LOCAL_ADDRESSES


def remote_enabled():
    with _lock:
        return _remote_enabled


def set_remote_enabled(enabled):
    """Returns the new state. Callers that need to react to the transition
    (stopping the motors when switching off) compare against the old one."""
    global _remote_enabled
    with _lock:
        _remote_enabled = bool(enabled)
        return _remote_enabled


def local_only(view):
    """For the handful of /robot routes that stay kiosk-only even when remote
    access is on - currently just toggling Follow me, which is Ruby's button
    and has no counterpart on the remote page."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not is_local_request():
            abort(403)
        return view(*args, **kwargs)

    return wrapper


def stream_allowed():
    """Whether an already-open streaming response (the MJPEG video feed) may
    keep sending. Evaluated per frame rather than once at request time,
    because a response that started while remote access was on would
    otherwise keep streaming video to the network for as long as the client
    held the connection after it was switched off. Captured at request time
    via a closure over is_local_request(), since a generator runs outside the
    request context."""
    local = is_local_request()
    return lambda: local or remote_enabled()


def install(app):
    """Single gate, in front of every request the app serves - including
    Flask's built-in static handler, which serves Ruby's HTML/JS/CSS and is
    not part of any blueprint."""

    @app.before_request
    def _enforce():
        if is_local_request():
            return None
        if request.blueprint == ROBOT_BLUEPRINT and remote_enabled():
            return None
        abort(403)
