"""
app.py

The WSGI app gunicorn imports, and nothing else: it loads .env, builds the
Flask object, installs the access policy, and mounts the two halves of the
system. All the substance is in the modules it pulls in.

    kiosk_api.py   Ruby - the touchscreen assistant, her mini-apps, and the
                   Remote Control switch.  Localhost only.
    robot_api.py   /robot - the remote driving page and its endpoints.
                   Network-reachable while Remote Control is on.
    hardware.py    the ESP32 link, the camera, the face follower. Importing
                   it opens the serial port and starts the camera.
    access.py      who may reach which of the above.

Run:
    gunicorn -c gunicorn.conf.py app:app

`python3 app.py` deliberately does not start a server - see CLAUDE.md's
Running section for why Werkzeug's dev server is unusable here.
"""

import atexit
import os
import sys

from dotenv import load_dotenv

# Python block-buffers stdout when it isn't a terminal, which under gunicorn
# means the startup diagnostics (which motion profile is live, whether the
# ESP32 was found, which camera node won the probe) can sit unwritten for
# minutes - exactly the lines you're tailing the log for when something's
# wrong. stderr is unbuffered, so without this the OpenCV warnings show up
# promptly and our own messages don't, which reads as if they never ran.
sys.stdout.reconfigure(line_buffering=True)

# Must happen before the project imports below, not alongside them: motion,
# camera, face_follow and kiosk_api all read their configuration from
# os.environ at import time, so a .env loaded after them would be ignored.
# Resolved against this file rather than the working directory so it doesn't
# matter where the process was started from. An environment variable set
# another way (shell profile, systemd EnvironmentFile) always wins - by
# default load_dotenv() does not override what's already set, and a stray
# .env should never silently beat an operator's explicit export.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

from flask import Flask  # noqa: E402

import access  # noqa: E402
import hardware  # noqa: E402
from kiosk_api import kiosk  # noqa: E402
from robot_api import robot  # noqa: E402

app = Flask(__name__, static_folder="static", static_url_path="")
app.logger.setLevel("INFO")

# Installed before the blueprints purely for readability - it's a
# before_request hook either way, so ordering has no effect on behaviour.
access.install(app)

app.register_blueprint(kiosk)
app.register_blueprint(robot)

# Release the serial port and camera however this worker process ends.
# atexit rather than a try/finally around a serve_forever() call, because
# this module doesn't run its own server - gunicorn owns the lifecycle.
atexit.register(hardware.close)


if __name__ == "__main__":
    print(
        "[app] This module is a WSGI app, not a standalone server - run it with:\n\n"
        "    gunicorn -c gunicorn.conf.py app:app\n\n"
        "or just ./start.sh, which also brings up the kiosk browser. See "
        "CLAUDE.md's Running section for why (Werkzeug's dev server has no "
        "HTTP keep-alive at all and serializes TLS handshakes behind one slow "
        "client)."
    )
