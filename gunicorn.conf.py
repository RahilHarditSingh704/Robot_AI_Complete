"""
gunicorn.conf.py

Runs app.py's Flask app - both halves of it, the kiosk assistant and the
robot's remote page - under gunicorn instead of Werkzeug's dev server. See
CLAUDE.md's Running section for the full story. Short version: Werkzeug's dev
server (a) does the TLS handshake for a freshly-accepted connection
synchronously inside its single accept() loop, so one slow/stalled client
blocks every other client's requests behind it, and (b) unconditionally
sends "Connection: close" on every response - no keep-alive, ever, in any
configuration - so every single request, including routine polling, pays a
full new TCP+TLS handshake. Together those caused both the input-lag-under-
bursts and the disconnects-after-extended-use problems this replaces.

gunicorn's gthread worker fixes both: accept() immediately hands a raw
connection to a thread-pool slot, so the handshake happens in that thread
rather than blocking the accept loop; and idle keep-alive connections sit in
a poller (not a thread) until they have data, so real persistent connections
work and don't cost anything while idle.

workers=1 is required, not just a default: MotorLink (the ESP32 serial
connection) and Camera are opened once as module-level singletons when
hardware.py is imported. gunicorn imports the WSGI app fresh in each worker
process - more than one worker would mean more than one process
independently opening the same serial port / camera device, which can't
work. threads is what actually provides concurrency here, not extra workers.
Do not set max_requests (or anything else that recycles the worker) -
MotorLink.__init__ causes the ESP32 to hardware-reset on serial open, so
recycling the worker would reboot the ESP32 mid-session.

certfile/keyfile are computed here, in the arbiter, before any worker forks
- via tls_cert.ensure_self_signed_cert(), a standalone module with no other
project imports. Importing app.py or hardware.py here instead would run
their top-level code - opening the serial port, starting the camera - in the
arbiter process too, racing the actual worker for the same hardware.
"""

import os

from tls_cert import ensure_self_signed_cert

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# So models/, certs/ and data/ resolve the same way no matter where the
# process was launched from - systemd units and a shell in some other
# directory included.
chdir = BASE_DIR

# 0.0.0.0, not 127.0.0.1, because the robot's remote page has to be
# reachable from a phone. That does NOT publish the kiosk's own endpoints
# (volume, screen, power) - those are localhost-only, and the robot page is
# gated behind the Remote Control switch. All of that policy lives in
# access.py; read that file before changing this line.
bind = "0.0.0.0:5000"
workers = 1
# Raised from 8 when the two projects merged. The robot half's requests were
# all local and fast; the kiosk half adds Gemini chat/transcribe/TTS calls
# that can each hold a thread for as long as their 30s request timeout, and
# those must not be able to crowd out a Stop command sharing the pool.
threads = 16
worker_class = "gthread"
keepalive = 5       # seconds an idle connection is held open for the next request
timeout = 30        # seconds a worker may be silent before the arbiter kills it
accesslog = "-"     # stdout
errorlog = "-"
# %(D)s appended: per-request time in microseconds. Default format doesn't
# include this - without it, a slow request (e.g. one queued behind other
# threads blocked on MotorLink's lock during a serial reconnect) is
# indistinguishable in the log from a fast one, since both just show up as
# a 200 at whatever second they finished in.
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)sus'

certfile, keyfile = ensure_self_signed_cert(
    cert_path=os.path.join(BASE_DIR, "certs", "robot.crt"),
    key_path=os.path.join(BASE_DIR, "certs", "robot.key"),
)
