# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

This is the merged package: the ECE kiosk assistant ("Ruby") and the robot control stack, which used to be two separate projects (`AI_Interface/` and `Robot_Project/`) running as two servers. They are one process now. The Merge notes section at the bottom records what changed and why, including one behavioural bug the merge exposed.

## Running

```
python3.12 -m venv venv
venv/bin/pip install -r requirements.txt
sudo apt install ffmpeg
./models/download.sh          # Piper voices + the YuNet face detector
cp .env.example .env          # then put a Google AI Studio key in it
./start.sh
```

`start.sh` brings up the server and the kiosk browser together, and kills the server when the browser quits. It also repairs the sound output first — see Audio below. To run just the server:

```
venv/bin/gunicorn -c gunicorn.conf.py app:app
```

Two things to open:

- **`https://localhost:5000/`** — Ruby, the kiosk screen. This is what `start.sh` puts on the attached display.
- **`https://<magicdns-name>:5000/robot/`** — the robot's driving page, from a laptop signed in to the same tailnet. **Only reachable while Remote Control is switched on** in the kiosk's Apps → Remote Control tile; the tile shows the exact address. See Access policy and Addressing below.

`app.py` is a plain WSGI module — importing it (as gunicorn does) runs its startup side effects via `hardware.py` (opening the ESP32 serial connection, starting the camera) but it doesn't start a server or know about TLS; that's `gunicorn.conf.py` and `tls_cert.py`. `python3 app.py` just prints a reminder.

**HTTPS is required, not optional.** `navigator.mediaDevices.getUserMedia()` (Ruby's mic button) refuses to run at all on a plain-HTTP origin, even for a trusted LAN/Tailscale IP; without it the mic fails instantly with a misleading "permission denied" and no real prompt. `gunicorn.conf.py` handles this via `tls_cert.ensure_self_signed_cert()`, called once in the arbiter before any worker forks: on first run it tries `tailscale cert` (a real, browser-trusted cert for this machine's MagicDNS name — silent, no warnings), and if that isn't permitted yet, falls back to a self-signed cert under `certs/` (reused on later runs). With the self-signed cert every phone shows a one-time "connection not private" warning to click through. To get the clean trusted-cert path, run `sudo tailscale set --operator=$USER` once, then delete `certs/` and restart.

`tls_cert.py` is deliberately a standalone module with no other project imports: `gunicorn.conf.py` imports it before workers fork, and importing `app.py`/`hardware.py` there instead would open the serial port and camera in the arbiter process too, racing the actual worker for the same hardware.

The kiosk browser meets that same certificate, and nobody is standing at the Pi to click through an interstitial at boot — so `start.sh` passes Chromium `--allow-insecure-localhost`, which suppresses the warning **for localhost only**. Certificate errors from any other origin still block normally. That flag is not what makes the mic work: `localhost` counts as a "secure context" on its own merits regardless of its certificate.

Models are not in the repo (`models/*.onnx` is gitignored; the Piper voices are ~60MB each). `./models/download.sh` fetches all of them and skips what's already there. Each feature degrades on its own if its model is missing, rather than taking the app down: no YuNet model means `face_follow.is_available()` is false and Ruby hides the Follow me button, exactly as a missing camera does.

There is no test suite, linter, or build step — it's a handful of Python files run directly on the Pi.

### What happens when hardware is missing

Everything is designed to come up degraded rather than not at all, and this matters more than it did before the merge: the same process is now both halves, so "the ESP32 is unplugged" must not be the reason Ruby won't answer questions.

| Missing | Effect |
| --- | --- |
| ESP32 | `hardware.py` substitutes `NullMotorLink`. Driving and Follow me report why they're unavailable; the kiosk is unaffected. |
| Camera | `camera.is_available()` false. The video feed and Follow me are hidden. |
| YuNet model | `face_follow.is_available()` false. Follow me is hidden. |
| `GOOGLE_AI_API_KEY` | **Fatal** — `kiosk_api.py` raises at import. Inherited from the kiosk project, which always required it. If you want the robot half to survive a missing key, that's the one place to change. |

### Audio

Ruby speaks through the browser, so all the Pi has to do is present a working output device. There is no analog jack on a Pi 5 and the webcam is capture-only (`arecord -l` lists the EMEET C950; `aplay -l` does not), so the **only** playback path is HDMI to the attached touchscreen — which does have speakers and advertises them in its ELD:

```
/proc/asound/card0/eld#0:  monitor_name PM161QT
                           speakers [0x1] FL/FR
                           sad0_coding_type [0x1] LPCM   sad0_channels 2
```

**PipeWire loses a race against that display at boot, often.** WirePlumber probes the `vc4-hdmi` card before the monitor has finished handshaking, finds no valid ELD, and abandons the card:

```
wireplumber: s-monitors: Failed to create alsa_output.platform-107c701400.hdmi.hdmi-stereo:
             Object activation aborted: PipeWire proxy destroyed
```

It then falls back to `auto_null` — "Dummy Output", a sink that accepts audio and discards it. This is a nasty failure to diagnose from the desk, because *nothing reports an error*: playback succeeds, the volume control works, `paplay` exits 0. There is simply no sound. Confirm it with `pactl get-default-sink` — if that says `auto_null`, this is what happened.

`start.sh` repairs it before launching the browser: if the default sink is missing or `auto_null` it restarts WirePlumber (by then the display is definitely awake, so the re-probe sees a valid ELD), waits for a real sink, then unmutes and sets a sane volume — a sink recovered this way comes back muted at 0% about as often as not, which to anyone without a terminal looks identical to the original fault. A working setup is left completely alone, so a deliberately chosen sink or volume is never overridden.

Note `alsa_card.platform-107c701400.hdmi` is **card 0 / `vc4hdmi0`**, the connected port; `107c706400` is `vc4hdmi1`, the empty one. The numbering is easy to get backwards — check `readlink -f /sys/class/sound/card0/device` rather than assuming.

## Architecture

The Pi runs the high-level code and is wired directly over USB to an ESP32 running the motor firmware in `robot_esp32_ble_and_serial/`, which owns the motors and the current-sense protection logic.

One process, one server, two surfaces:

```
                        gunicorn (1 worker, 16 threads, TLS, 0.0.0.0:5000)
                                          |
                    app.py  ── access.py (one before_request gate)
                       |
        ┌──────────────┴───────────────┐
   kiosk_api.py                   robot_api.py
   "/" and /api/*                 /robot/*
   localhost only                 network, while Remote Control is on
        └──────────────┬───────────────┘
                       |
                  hardware.py
          link (MotorLink) · camera · follower
                       |
              motion.py → single ASCII char → USB serial → ESP32
```

**Why one process and not two.** The camera and the serial port can each be held by exactly one process. Ruby's Follow me needs both (it watches for a face and drives toward it); the robot's remote page needs both (live video and the D-pad). Running them as two servers would mean one of them proxying hardware access to the other over HTTP, on the latency-sensitive path that stops the motors. So they share a process, and `workers = 1` in `gunicorn.conf.py` is load-bearing rather than a tuning choice — gunicorn imports the WSGI app fresh in each worker, so a second worker would be a second process opening the same serial port and camera.

- **`app.py`** — loads `.env`, builds the Flask app, installs the access gate, registers the two blueprints, wires `atexit`. Nothing else.
- **`hardware.py`** — owns the singletons (`link`, `camera`, `follower`), the Pi's WiFi/CPU readouts, and the browser heartbeat watchdog. Importing it opens the serial port and starts the camera.
- **`motor_link.py`** — `MotorLink`, the USB-serial connection, plus `NullMotorLink` for when there's no ESP32. `send_command()` is the single chokepoint every command source goes through (D-pad, keyboard, follow tracker; MQTT or similar later). Adding a control surface should never require touching its internals or the firmware.
- **`motion.py`** — which character means "forward". Read this one before touching anything that moves; see Which way is forward below.
- **`camera.py`** — USB-webcam capture. `Camera.start()` probes `/dev/video*` in order and opens the first device that actually yields a frame, since the Pi 5 also exposes `/dev/video*` nodes for its own ISP/HEVC decoder that open successfully but never produce a frame — set `CAMERA_DEVICE` to pin the exact path and skip the probe (the internal nodes block for a real ~10s each on the verification read, so a mid-session reconnect can otherwise take over a minute). Prefers udev's stable `/dev/v4l/by-id/` symlinks, which point straight at the real webcam regardless of its number this time around. Runs its own capture thread so the feed always serves the most recent JPEG rather than blocking a request on `cap.read()`. Requests the camera's onboard MJPEG compression (`CAP_PROP_FOURCC` = MJPG), not raw YUYV — on the EMEET SmartCam C950 this was built against, MJPEG sustains ~30fps up to 1080p while raw tops out near 640x480. Capture always runs at full resolution (1280x720 default) so the face detector sees full frames regardless of stream settings; what goes over the wire is separately controlled by `set_stream_resolution()` (720p/360p/240p, plus `"off"`, which skips overlay/resize/encode entirely).
- **`access.py`** — the whole network policy, in one file. See below.
- **`kiosk_api.py`** — Ruby: Gemini chat/transcription/TTS, local Piper voices, expression detection, and the mini-app backends (notes, volume, vitals, power, TTC, Remote Control).
- **`robot_api.py`** — the `/robot` surface: the driving page, `/robot/command`, the video feed, follow status, diagnostics, heartbeat.
- **`templates/robot.html`** — the driving page. In `templates/`, not `static/`, on purpose: `static/` is served at the root by Flask's own handler and would bypass the gate.

### Access policy

Read `access.py` before changing anything about what's reachable. The short version:

The two halves had opposite and individually-correct network postures. The kiosk bound `127.0.0.1` deliberately — its endpoints shell out to the desktop session and can set volume, dim the screen, or power the Pi off. The robot server bound `0.0.0.0` deliberately — driving from a phone is the point. Merging means one socket, and it has to be `0.0.0.0`. Left alone that would have silently published the kiosk's power controls to the LAN, so the bind is network-wide and the policy is enforced in one `before_request`:

- `/robot/*` — reachable from the network **only while Remote Control is on**; always reachable from the Pi itself, since that's Ruby pressing her own buttons.
- Everything else — localhost only, always. Including Flask's static handler, which isn't part of any blueprint.
- `POST /robot/follow` — localhost only even when Remote Control is on, via `@access.local_only`. Starting Follow me is Ruby's button; the remote page can only observe it and press Stop.

Remote access is **off on every boot and deliberately not persisted**: a robot that reboots unattended shouldn't come back with its motors reachable because of a switch someone flipped days ago. `X-Forwarded-For` is intentionally not honoured — there's no proxy in front of gunicorn, so trusting it would let any client claim to be local.

Two consequences worth knowing:

- Switching Remote Control off **stops the motors** (unless the follower is driving, which is the kiosk's own business). A phone holding a direction button has already sent that command once, and `MotorLink`'s resend thread would keep re-affirming it to the ESP32 forever with nobody able to press Stop.
- The video feed's generator re-checks the switch **per frame**, because a response that started while remote access was on would otherwise keep streaming for as long as the client held the connection.

### Which way is forward

`motion.py` is the only place that maps an intent (forward, rotate CW) onto the character the firmware gets. This exists because the firmware's names and the robot's actual behaviour don't agree, and before the merge the two control sources disagreed with each other about it:

- The `.ino` calls `F` forward (both motors driven the same way) and `C` rotate-CW (motors driven opposite).
- The web control page had always sent `X` for Forward and `F` for Rotate CW — the firmware's names swapped — and its status labels agreed with the swap.
- `face_follow.py` used the firmware's names literally.

Both can't be right. On a differential drive the motors are mounted mirror-imaged, so driving them "the same way" electrically spins the robot and driving them opposite makes it travel — i.e. the firmware's labels are the inverted ones, and the page's mapping is what somebody arrived at by driving the real robot. **The consequence is that follow mode was mis-driving: rotating when it meant to advance, and advancing when it meant to turn.** Both sources now share `motion.py`, defaulting to the page's mapping.

None of this is verifiable in software — only by watching the robot move. So the plausible wiring mistakes are one env var each, applied everywhere at once:

| Variable | Effect |
| --- | --- |
| `MOTION_PROFILE=mirrored` | default; the D-pad's long-standing mapping |
| `MOTION_PROFILE=direct` | the firmware's own names taken literally |
| `MOTION_INVERT_DRIVE=1` | robot goes backward when told forward |
| `MOTION_INVERT_TURN=1` | robot turns CCW when told CW |
| `FOLLOW_TURN_INVERT=1` | follow mode only — turns *away* from the face (camera mounting/mirroring, not the robot) |

If pressing Forward makes the robot spin on the spot, you want the other `MOTION_PROFILE`. The live mapping is printed at startup (`[motion] profile=...`) and `follow_dryrun.py` prints it too.

### Follow me

A button on Ruby's own screen (bottom left of the input bar). `POST /robot/follow`, polled via `GET /robot/follow_status`. The robot drives toward whoever's face the mounted camera sees.

- **`face_follow.py`** — `is_available()`/lazy model loading mirrors the same pattern used elsewhere (module-level `_load_detector()`, called once at startup). Detection is `cv2.FaceDetectorYN` (YuNet), a small ONNX DNN — not `CascadeClassifier`/Haar, which the pinned `opencv-python-headless` build ships no cascade XML files for at all. YuNet is also just more robust to the off-angle faces and uneven lighting a camera bouncing around on a moving robot sees. The `FaceFollower` background thread repeatedly grabs the latest frame via `camera.get_frame()`, runs detection on a downscaled copy, and calls `link.send_command()` to rotate toward the largest face and hold a comfortable distance — with hysteresis on the distance thresholds so a face sitting right at a boundary doesn't flip-flop. If no face has been seen for `LOST_FACE_TIMEOUT_S` it sends Stop rather than keep driving blind.
- **Ruby's UI** — while following, she shows a live preview of the robot's camera in the top right with the tracked face boxed (the box is drawn server-side by `draw_overlay`, so the preview is a plain `<img>` with no per-frame JS). The preview stream is opened only while follow is on and dropped when you open the Apps launcher, so the Pi isn't encoding frames for a hidden element. Follow mode itself keeps running there — you might well browse while the robot walks you somewhere.
- **On the remote page** there is no toggle, only a banner saying follow is running and that Stop takes over. `/robot/command` rejects every manual command except Stop while the follower drives, so the D-pad's direction buttons grey out — but **Stop deliberately stays live**, since with the toggle now on the kiosk it's the only way somebody out there can take back control.
- **Follow mode moves at a second, slower duty** — and this is why the firmware has two. The protocol is one character with no speed field, so originally the ESP32 ran every command at its single fixed `DUTY`, and the only thing the Pi could vary was *how long* the motors ran. Follow mode therefore pulsed its turns (turn, brief burst, stop, re-evaluate). That did bound the overshoot, but only by chopping the movement into steps, and it visibly juddered.

  The overshoot was never really about duration — it was about speed. At driving duty the robot covers a lot of arc during the ~120ms between decisions plus the camera's own latency, so by the time a frame reports "centered" it has already swung past, and the next frame starts correcting back. Turning the duty down attacks the cause, and a slow turn can then simply be *held* until the face is centered: smoother, and less code.

  So the `.ino` gained `FOLLOW_DUTY` (55, against `DUTY` 75) reached through **lowercase** command characters — `f`/`b`/`c`/`x` are the same four directions at the lower duty. There is no lowercase `s`; stop is stop. Deliberately a separate character rather than a mode flag, because `'c'` and `'C'` are simply different chars, so every existing mechanism keeps working untouched: `lastAppliedCmd` sees a genuine change when the speed changes (so `stopAll()` runs and the protection counters get a clean slate), keep-alive resends of either still collapse to a heartbeat, and the trip latch stays per-command. On the Pi side it's `motion.command_for(intent, slow=True)`.

  **`MotorLink.send_command()` must never `.upper()` its argument again.** It used to, and doing so silently promotes every follow-mode command to full driving speed — the slow path then looks like it simply doesn't work, with no error anywhere. Validation is unchanged in strictness; `VALID_WIRE_COMMANDS` just lists both cases now.

  Only follow mode uses the slow set. The remote page always drives at full `DUTY` — a person steering in real time wants the response.

  To change the follow speed you must edit `FOLLOW_DUTY` in the `.ino` and reflash (`arduino-cli compile && arduino-cli upload -p /dev/ttyUSB0 --fqbn esp32:esp32:esp32:UploadSpeed=115200 .` from `robot_esp32_ble_and_serial/` — pin the upload speed, this CP2102 corrupts data at 460800+). Raise it if follow mode stalls or crawls; lower it if it overshoots again.

- **Losing the face coasts before stopping** — when someone leaves the frame they have almost always walked out of one side, and the robot was already turning that way. Cutting the motors the instant detection fails stops it just short of catching up, and the person has to walk back into view to be re-acquired. So the loop keeps turning the way they went for `FOLLOW_LOST_COAST_S` first, which is usually enough to bring them back into frame on its own, and only then stops. Direction comes from the last offset actually measured, so it also covers a face that vanished while still inside the deadzone but clearly drifting; below `FOLLOW_COAST_MIN_OFFSET` it was centered enough that the exit direction would be a guess, and guessing means turning away from them half the time, so it just stops. Note the coast is quantised to the detection interval, so it overruns its target by up to `DETECT_INTERVAL_S` (0.5s measures ~0.55s).

  | Variable | Default | Effect |
  | --- | --- | --- |
  | `FOLLOW_CENTER_DEADZONE` | `0.15` | ± fraction of frame width treated as centered. Raise if it fidgets while you stand still |
  | `FOLLOW_LOST_COAST_S` | `0.5` | how long to keep turning after losing the face |
  | `FOLLOW_COAST_MIN_OFFSET` | `0.05` | below this the exit direction is a guess, so stop instead of coasting |

  These can't be derived in software — they depend on the robot's rotation speed, the camera's field of view and latency, and the floor surface. Same reasoning as `MOTION_PROFILE`: sane default, correctable without a code edit.
- **The follow thread has a crash barrier** (`_follow_loop` wraps `_follow_loop_body`). An unhandled exception there is uniquely dangerous: the thread dies but nothing notices — `_running` stays true so the UI still shows follow as on, and `MotorLink`'s resend loop keeps re-sending the last command, so if the loop died just after issuing a turn the firmware watchdog never fires (it is being fed) and the robot rotates until someone hits Stop. This was not hypothetical: while follow mode was still pulsed, its burst-length helper returned a `numpy.float32` (derived from the detector's array — and unlike `float64`, *not* a `float` subclass), which `time.sleep()` rejects with `TypeError`. The barrier turns any such bug into "follow switches itself off and the motors park", matching every other failure path in the file.
- **Trip safety** — `MotorLink` takes an `on_trip` callback, invoked from `_reader_loop` on the edge-triggered `LOG: ... TRIP` lines (not the repeating `STATUS: TRIPPED` line) the ESP32 prints when current protection trips. `hardware.py` wires this to `follower.stop()`. `FaceFollower.stop()` and its detection loop share one lock so a trip can never be raced by a command the loop already decided on — whichever side's `send_command()` is still in flight, the other's Stop always lands last. This is a Pi-side reaction to a trip that already happened on the ESP32; the trip detection/latching itself lives entirely in the `.ino` and is untouched.
- **`follow_dryrun.py`** — runs the real `FaceFollower` against the live camera with a fake motor link that prints the command it would have sent. This is how to verify detection and the centering/distance logic with the camera on a desk, before anything is assembled, and the cheapest way to sanity-check a `MOTION_PROFILE` change without the robot.

### Remote Control

Apps → Remote Control on the kiosk. It isn't starting or stopping a process — the robot's page and endpoints are always running in this same server; the switch opens and closes the door to them (`access.py`), which is why it takes effect instantly and can't fail to boot. The tile shows the address to type, since the kiosk has no keyboard, and warns that the page has no password.

#### Addressing

The tile advertises the **tailnet** address (`kiosk_api.tailscale_identity()`), not the local one `read_ip()` returns. The robot is driven from a laptop on the tailnet, and two things make the local address actively wrong here:

- This Pi's Wi-Fi network hands out addresses in **100.64.0.0/10 — the same CGNAT range Tailscale uses**, so tailnet and local addresses can't be told apart by address alone. Don't be tempted to range-filter on it.
- That access point appears to have client isolation: a connection to the Pi's own Wi-Fi address times out, so the local address wouldn't have worked anyway.

The MagicDNS name is preferred over the raw Tailscale IP — stable across re-registration, far easier to read off a screen, and the only address that can ever be certificate-clean. The IP is shown underneath it as a fallback for when DNS isn't cooperating. If Tailscale is down, the tile falls back to the local address and **says so**, because that's a different audience (anyone on that Wi-Fi) than the tailnet this is meant for.

**Getting rid of the certificate warning.** `tailscale cert` currently fails here with `Access denied: cert access denied`, so `tls_cert.py` falls back to self-signed and the laptop shows a one-time "not private" warning. To fix, once:

```
sudo tailscale set --operator=$USER
rm -rf certs/ && ./start.sh
```

That issues a genuinely browser-trusted cert for the MagicDNS name — which is why the tile shows that name rather than the IP; a Tailscale cert can never cover a bare IP. Two caveats: the cert then covers only that name (fine — the kiosk itself is exempted by `--allow-insecure-localhost`), and Tailscale certs expire in ~90 days while `ensure_self_signed_cert()` only generates when the files are *absent*, so it will not renew on its own. Deleting `certs/` and restarting re-issues it.

**Restricting to the tailnet only.** Right now the bind is `0.0.0.0`, so while the switch is on, anything that can route to the Pi could reach `/robot` — today that's just the tailnet in practice, because of the client isolation above, but that's a property of the network, not a guarantee. Binding `gunicorn.conf.py` to `["127.0.0.1:5000", "<tailscale-ip>:5000"]` would enforce it properly. Not done by default deliberately: if Tailscale hasn't come up yet at boot, that bind fails and the whole kiosk fails to start with it.

### The watchdog/resend contract

The firmware force-stops the motors if it doesn't see a fresh command within ~500ms (protects against the Pi process crashing, USB unplugging, etc). To satisfy this while a command is held, `MotorLink` runs a background thread re-sending `_current_cmd` every `RESEND_INTERVAL_S` (0.15s — must stay well under 500ms). Stop is never resent.

This is load-bearing for the firmware's protection logic: the ESP32 treats a repeat of the same command as a heartbeat (refresh watchdog only) versus a genuinely new command (reset protection counters/grace timer). If the resend interval or the single-char protocol changes, that firmware-side distinction breaks.

Separately, a **browser** heartbeat (`POST /robot/heartbeat`, every 750ms) backstops the case the ESP32's watchdog can't see: the browser losing its connection to the Pi while a manual command is held. `MotorLink`'s resend thread keeps re-affirming that command regardless of whether any browser can still reach the Pi, and held commands are sent exactly once on button-down — so "time since last `/command`" can't be the liveness signal without misfiring on every ordinary long hold. `hardware._connection_watchdog_loop` skips follow mode entirely: it drives off the camera, not a browser, and already has its own lost-face stop.

### Serial protocol

Single ASCII byte, no framing: `F` `B` `C` `X` `S`, plus `f` `b` `c` `x` for the same four directions at the firmware's lower `FOLLOW_DUTY` (see Follow me). **Case is significant** — nothing on this path may upper- or lower-case a command in passing. `MotorLink.send_command()` is the only validator; get the character from `motion.command_for(intent, slow=...)` rather than spelling it out. Status/log lines coming back (`STATUS:`/`LOG:` prefixes) are read by `_reader_loop` and printed; `LOG:` lines containing `TRIP` also fire the `on_trip` callback.

Because the protocol is a single repeated character with no sequence number or timestamp, the Pi cannot distinguish "user pressed the same button again" from "this is the keep-alive resend" — that disambiguation, and any trip-latching after a fault, lives entirely in the firmware.

### Why gunicorn, not Werkzeug

**This does not run on Werkzeug's dev server**, and that isn't a style preference. Werkzeug's dev server does the handshake for a freshly-accepted connection *inside* its single accept loop (raw accept + `wrap_socket()`, one blocking call) rather than in a per-connection thread, so one client with a stalled handshake wedged accept() forever and every other client queued behind it — "server hangs for everyone". Worse, and not fixable from the app at all: it unconditionally sends `Connection: close` on every response, in every configuration — no HTTP keep-alive, full stop (see `werkzeug/serving.py`'s `run_wsgi()`; intentional upstream, not a bug). Every 1.5s status poll and every button press paid a full fresh TCP+TLS handshake, serialized through that same accept loop. That is what actually caused the input-lag-under-bursts and disconnects-after-extended-use problems an earlier custom handshake timeout could only band-aid.

gunicorn's `gthread` worker fixes the architecture: `accept()` immediately hands a raw connection to a thread-pool slot, so a slow handshake ties up one thread rather than the accept loop, and idle keep-alive connections sit in a poller until they have data. `threads = 16` (up from 8 pre-merge) is what provides concurrency — the robot half's requests were all local and fast, while Ruby's Gemini calls can each hold a thread for up to their 30s timeout, and those must not crowd out a Stop sharing the pool. Do not set `max_requests` or anything else that recycles the worker: `MotorLink.__init__` hardware-resets the ESP32 on serial open, so a recycle would reboot it mid-session.

The `/robot/video_feed` route sets a socket timeout via `environ["gunicorn.socket"]`. gthread's `send()` has no default timeout, so a client that goes dark mid-stream leaves that thread blocked forever, never returning to the pool; repeat it a few times and every thread is stuck on a dead socket, wedging the server for unrelated requests too.

## Merge notes

What changed relative to the two original projects, beyond the wiring above:

**Removed.** The robot's LLM conversation feature (`llm_chat.py`, `/llm_reply`, `/llm_status`, the reply box and browser `SpeechSynthesis` playback) — Ruby handles conversation now. Its offline voice pipeline went with it (`voice_transcribe.py`, `transcript_link.py`, `transcript_viewer.py`, `voice_config.py`, the Vosk model and the `vosk`/`dearpygui` dependencies); transcription is Ruby's job via Gemini. The Follow toggle came off the remote page. `image_white.png` (5.8MB, referenced nowhere) wasn't carried over.

**Extracted.** `MotorLink` moved back out of `app.py` into `motor_link.py`. The pre-merge note said it was deliberately merged *in*; that made sense when `app.py` was only the robot server, but with `app.py` now wiring two web surfaces it would have left the one safety-critical class sharing a file with TTC arrival parsing. The robot page moved from a Python string to `templates/robot.html` for the same reason.

**Fixed in passing.** The motion mapping contradiction above (a real bug, not a tidy-up). Model paths now resolve against the source file rather than the working directory, and `gunicorn.conf.py` sets `chdir`, so nothing depends on where the process was launched from. `sys.stdout.reconfigure(line_buffering=True)` in `app.py` — Python block-buffers stdout under gunicorn, so startup diagnostics sat unwritten for minutes while stderr's OpenCV warnings appeared immediately, which reads as if they never ran.

**Verified.** The access matrix (18 cases, both switch states, from a real non-loopback address), the degraded-hardware paths, Piper TTS, and one live Gemini chat round trip. Not verified, because it needs the assembled robot: anything that moves.
