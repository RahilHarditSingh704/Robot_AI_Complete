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
| ESP32 | `hardware.py` substitutes `NullMotorLink`. Driving and Follow me report why they're unavailable; the motor current readout hides itself; the kiosk is unaffected. |
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
| `FOLLOW_DRIVE_INVERT=1` | follow mode only — backs away from a distant face and closes in on a near one |

**Both drive inverts are currently off**, which is the state both motors were confirmed working in. Getting there was confused by a wiring fault (see When a motor doesn't move below) and the episode is worth remembering, because it will read as a direction bug again: while motor 1 was disconnected the robot could only pivot, so "forward" was not observable at all, and the two candidate mappings differ *only* in which way M1 turns — `F` drives both motors the same way, `X` drives them opposite. Every direction flag set during that period was a coin flip on unobservable evidence, and the two ended up opposite, which is worse than either: the D-pad sent one family and follow mode the other.

**So before touching a direction flag, confirm both motors actually draw current.** `/robot/motor_current` answers that in one glance, and it is the cheaper question by a long way.

If pressing Forward makes the robot spin on the spot, you want the other `MOTION_PROFILE`. The live mapping is printed at startup (`[motion] profile=...`) and `follow_dryrun.py` prints it too.

**The bottom two are deliberately not the same flag as the two above them.** `MOTION_INVERT_*` describes the robot — which way it moves when told to move — so it applies to every control source at once, D-pad included. The `FOLLOW_*` pair describes the camera: which way it faces and whether its image is mirrored, which only matters to the one thing that reads a picture to choose a direction. Fixing follow mode with the robot-level flag would flip the D-pad along with it, so the rule is: **wrong everywhere → `MOTION_INVERT_*`; wrong only while following → `FOLLOW_*`.** Both were set at one point, from the D-pad appearing reversed and then follow mode appearing to drive away from people — which cancelled out on the drive axis and was really two readings of one broken motor. They are both off now. If a genuine need for one ever appears, note that setting both is not automatically redundant: they record two independent facts, so they can legitimately coexist.

`face_follow.follow_command_for()` is where the follow-only pair is applied — one place, so what body mode puts on the wire has a single answer, which is what `follow_dryrun.py` labels its output from. That map is keyed on the **lowercase** `FOLLOW_DUTY` characters follow mode actually sends; keyed on `motion.WIRE_COMMANDS` (the full-speed set) it matched nothing but Stop and every line printed unlabelled.

### Follow me

A button on Ruby's own screen (bottom left of the input bar). `POST /robot/follow` with `{"enabled": true, "mode": "body"|"head"}`, polled via `GET /robot/follow_status`. Tapping it while stopped opens a chooser rather than starting anything — "follow me" alone no longer says what the robot will do. Tapping it while running is an unambiguous stop, so that stays one press with no menu in the way.

| Mode | What moves | What doesn't |
| --- | --- | --- |
| **Body** | Wheels: turns toward you and holds distance | Head stays where it is |
| **Head** | Servo on GPIO 23 only — the head turns to watch you | Wheels never move at all |

Both share everything except what they actuate: capture, detection, choosing the largest face, the deadzone, and the lost-face coast are one code path, and `_step_body()` / `_step_head()` differ only in what they command. `mode` defaults to `body` when absent so an older client sending just `{"enabled": true}` keeps working.

**Head mode is a closed loop for the same reason body mode is** — the camera rides on the servo, so a face drifting right means turn the head right, which brings the face back toward the middle of the frame. Nothing needs to know the head's angle relative to the body, or the robot's heading. `_step_head()` converts the offset to degrees via `HEAD_CAMERA_FOV_DEG`, applies `HEAD_GAIN` (below 1.0 so it converges instead of hunting), and commands an absolute angle. Simulated against the real loop, a person 25° off centre is acquired in three steps with no overshoot.

Head mode gets its own, tighter deadzone (`HEAD_DEADZONE`, 0.06 vs the body's 0.15). The body's is sized to absorb gear lash and the momentum of a robot that can't stop instantly; at a 70° field of view that would leave the head sitting 10° off you, which reads as not quite looking at you. A servo has neither problem.

The head **re-centres when follow stops**, so the robot doesn't sit staring off to one side. The firmware slews, so that's a smooth sweep back rather than a snap.

**Smoothing lives in the firmware, not here.** A servo commanded to a new angle slams to it at full speed, so feeding it a fresh target every detection frame would make it jerk ~8 times a second. Instead the `.ino` holds `servoTarget` and walks `servoCurrent` toward it at `SERVO_DEG_PER_S` (40°/s) every 20ms. The motion stays smooth however often, however erratically, or however coarsely the Pi updates it, and it degrades gracefully when a frame is late. The corollary: to change how fast the head sweeps you edit the `.ino` and reflash — it is not an env var.

`MotorLink.set_head_angle()` sends `H` plus exactly three digits (`H090` is centre). Fixed width, so it needs no terminator on a protocol that has no framing. It is deliberately **not** resent the way motor commands are: a servo holds position by itself and the firmware slews on its own clock, so repeating it would be pure serial noise. If the ESP32 resets, the head re-centres and the target is lost — which is fine, because the follow loop re-commands it on its next frame.

- **`face_follow.py`** — `is_available()`/lazy model loading mirrors the same pattern used elsewhere (module-level `_load_detector()`, called once at startup). Detection is `cv2.FaceDetectorYN` (YuNet), a small ONNX DNN — not `CascadeClassifier`/Haar, which the pinned `opencv-python-headless` build ships no cascade XML files for at all. YuNet is also just more robust to the off-angle faces and uneven lighting a camera bouncing around on a moving robot sees. The `FaceFollower` background thread repeatedly grabs the latest frame via `camera.get_frame()`, runs detection on a downscaled copy, and calls `link.send_command()` to rotate toward the largest face and hold a comfortable distance — with hysteresis on the distance thresholds so a face sitting right at a boundary doesn't flip-flop. If no face has been seen for `LOST_FACE_TIMEOUT_S` it sends Stop rather than keep driving blind.
- **Ruby's UI** — while following, she shows a live preview of the robot's camera in the top right with the tracked face boxed (the box is drawn server-side by `draw_overlay`, so the preview is a plain `<img>` with no per-frame JS). The preview stream is opened only while follow is on and dropped when you open the Apps launcher, so the Pi isn't encoding frames for a hidden element. Follow mode itself keeps running there — you might well browse while the robot walks you somewhere.
- **On the remote page** there is no toggle, only a banner saying follow is running and that Stop takes over. `/robot/command` rejects every manual command except Stop while the follower drives, so the D-pad's direction buttons grey out — but **Stop deliberately stays live**, since with the toggle now on the kiosk it's the only way somebody out there can take back control.
- **Follow mode moves at a second, slower duty** — and this is why the firmware has two. The protocol is one character with no speed field, so originally the ESP32 ran every command at its single fixed `DUTY`, and the only thing the Pi could vary was *how long* the motors ran. Follow mode therefore pulsed its turns (turn, brief burst, stop, re-evaluate). That did bound the overshoot, but only by chopping the movement into steps, and it visibly juddered.

  The overshoot was never really about duration — it was about speed. At driving duty the robot covers a lot of arc during the ~120ms between decisions plus the camera's own latency, so by the time a frame reports "centered" it has already swung past, and the next frame starts correcting back. Turning the duty down attacks the cause, and a slow turn can then simply be *held* until the face is centered: smoother, and less code.

  **Lowering the duty was not enough on its own, and it could not have been.** It made each overshoot smaller without making it smaller than the deadzone, so the robot went on pacing back and forth across the face at a slower speed — which is how it was reported: "turns too much past the person and ends up moving back and forth". See Why follow mode paced below for what actually fixed it. The lesson to carry: **a tracking complaint is not a duty problem.** `FOLLOW_DUTY` is now the knob for follow mode stalling or crawling, and lowering it to improve centering makes the robot more likely to sit near stall for no benefit.

  So the `.ino` gained `FOLLOW_DUTY` (65, against `DUTY` 75 — it started at 55) reached through **lowercase** command characters — `f`/`b`/`c`/`x` are the same four directions at the lower duty. There is no lowercase `s`; stop is stop. Deliberately a separate character rather than a mode flag, because `'c'` and `'C'` are simply different chars, so every existing mechanism keeps working untouched: `lastAppliedCmd` sees a genuine change when the speed changes (so `stopAll()` runs and the protection counters get a clean slate), keep-alive resends of either still collapse to a heartbeat, and the trip latch stays per-command. On the Pi side it's `motion.command_for(intent, slow=True)`.

  **`MotorLink.send_command()` must never `.upper()` its argument again.** It used to, and doing so silently promotes every follow-mode command to full driving speed — the slow path then looks like it simply doesn't work, with no error anywhere. Validation is unchanged in strictness; `VALID_WIRE_COMMANDS` just lists both cases now.

  Only follow mode uses the slow set. The remote page always drives at full `DUTY` — a person steering in real time wants the response.

  To change the follow speed you must edit `FOLLOW_DUTY` in the `.ino` and reflash (`arduino-cli compile && arduino-cli upload -p /dev/ttyUSB0 --fqbn esp32:esp32:esp32:UploadSpeed=115200 .` from `robot_esp32_ble_and_serial/` — pin the upload speed, this CP2102 corrupts data at 460800+). Raise it if follow mode stalls or crawls; lower it if it overshoots again.

- **Losing the face coasts before stopping** — when someone leaves the frame they have almost always walked out of one side, and the robot was already turning that way. Cutting the motors the instant detection fails stops it just short of catching up, and the person has to walk back into view to be re-acquired. So the loop keeps turning the way they went for `FOLLOW_LOST_COAST_S` first, which is usually enough to bring them back into frame on its own, and only then stops. Direction comes from the last offset actually measured, so it also covers a face that vanished while still inside the deadzone but clearly drifting; below `FOLLOW_COAST_MIN_OFFSET` it was centered enough that the exit direction would be a guess, and guessing means turning away from them half the time, so it just stops. Note the coast is quantised to the loop period, so it overruns its target by up to that (~0.1s).

  | Variable | Default | Effect |
  | --- | --- | --- |
  | `FOLLOW_CENTER_DEADZONE` | `0.15` | ± fraction of frame width treated as centered, **body mode**. A *floor* — see Why follow mode paced |
  | `FOLLOW_TURN_DEADZONE_MAX` | `0.35` | ceiling on the self-measured widening of that deadzone |
  | `FOLLOW_TURN_LEAD_S` | `0.28` | how far ahead the face's movement is projected to decide when to stop turning. Raise if it still swings past |
  | `FOLLOW_TURN_RELEASE` | `0.05` | how centered that projection has to land before the turn is released |
  | `FOLLOW_DETECT_INTERVAL_S` | `0.06` | gap between detections; the loop's period is this + ~41ms of detection |
  | `FOLLOW_FAR_ENTER` / `FOLLOW_FAR_EXIT` | `0.28` / `0.36` | face height ÷ frame height below which you're "too far" and it drives forward, and how big the face must grow before it stops |
  | `FOLLOW_CLOSE_ENTER` / `FOLLOW_CLOSE_EXIT` | `0.50` / `0.40` | the same pair for "too close, back up" |
  | `HEAD_DEADZONE` | `0.06` | same, **head mode** — tighter, see above |
  | `FOLLOW_LOST_COAST_S` | `0.5` | how long to keep turning after losing the face |
  | `FOLLOW_COAST_MIN_OFFSET` | `0.05` | below this the exit direction is a guess, so stop instead of coasting |
  | `FOLLOW_DRIVE_INVERT=1` | off | body mode drives *away* from a distant face instead of toward it — see Which way is forward |
  | `HEAD_INVERT=1` | off | head turns *away* from you instead of toward you |
  | `HEAD_CAMERA_FOV_DEG` | `70` | camera's horizontal FOV, used to convert offset → degrees |
  | `HEAD_GAIN` | `0.5` | fraction of the error corrected per detection; above ~0.8 it hunts |
  | `HEAD_CENTER_DEG` / `HEAD_MIN_DEG` / `HEAD_MAX_DEG` | `90` / `0` / `180` | neutral angle and allowed travel |

- **Why follow mode paced back and forth, and what stopped it.** The held slow turn above is a bang-bang controller — wheels on at `FOLLOW_DUTY` or off — reading a picture from a moment ago and committing every decision for a whole loop period. That dead time is the whole problem, and it needs two separate fixes, both in `face_follow._TurnController`.

  **Release the turn early.** Waiting for the *measured* offset to reach the middle means the stop is commanded after the robot is already there, so it sails past. Instead the loop differences the face's horizontal offset between detections to get a closing rate, projects that `FOLLOW_TURN_LEAD_S` ahead, and releases the turn once the *projection* lands centered — the robot then coasts the last of the way in. The lead is standing in for the real reaction delay (camera latency + one loop period + the wheels' coast-down), which is why it is the knob to raise if it still swings past.

  **Never ask for a correction smaller than the robot can make.** Releasing early bounds the overshoot but there is a hard floor under it: once the wheels start they run for at least a full period plus their stopping distance. If that smallest possible swing is wider than the deadzone, *every* correction lands outside it on the far side, and the robot oscillates forever no matter how well-timed the release — the deadzone is demanding a precision the drivetrain doesn't have. So the loop measures its own sweep rate (it is already computing that closing rate) and widens the deadzone to one dead time's worth of it, capped at `FOLLOW_TURN_DEADZONE_MAX`. **`FOLLOW_CENTER_DEADZONE` is therefore a floor, not the operating value.** Hitting the cap is the signal that the robot genuinely swings too fast per decision, and *that* is the one case where lowering `FOLLOW_DUTY` is the answer.

  Both were verified by driving the real `_TurnController` through a model of the loop (camera lag, one period of quantisation, a coasting stop) across sweep rates from 0.4 to 2.8 frame-widths per second. The old fixed deadzone hunted from 0.8 upward — 62 direction reversals and ±0.5 of frame width at the top end; this settles cleanly with zero reversals through 2.0, and only the extreme 2.8 case still paces. The learned sweep rate recovers the true one exactly. It also tracks a person walking across the frame at 0.35 frame-widths/s without reversing.

  The third lever was the loop period itself: `DETECT_INTERVAL_S` went 0.12 → **0.06**, so with a ~41ms detection the period is ~0.10s instead of ~0.16s. It is not a smoothness knob — it is half the dead time, and it directly sets that smallest-possible-swing floor. Simulation puts 0.16 on the wrong side of the line for sweep rates the robot plausibly has. It costs CPU (~40% of one core rather than ~25%), which is visible live on the CPU readout.

- **How far it lets you get before following.** Distance is inferred from face height ÷ frame height, the only cue one camera gives; the fraction is inversely proportional to distance (roughly 0.28 at arm's length, 0.20 at a bit over a metre on this camera). The gap between the two *enter* thresholds is what you can move within before anything happens, and because the relationship is inverse, **that gap is much bigger than the numbers suggest**: `FAR_ENTER` 0.20 against `CLOSE_ENTER` 0.50 is a 2.5× span in distance — half a metre to well over one — which is why following someone who walked away only started once they were most of a room off. `FOLLOW_FAR_ENTER` is 0.28 now and `FOLLOW_FAR_EXIT` 0.36 with it, so it sets off while they're still leaving and closes to a sensible gap. All four are env-settable; keep the ordering `FAR_ENTER < FAR_EXIT < CLOSE_EXIT < CLOSE_ENTER` or the hysteresis is meaningless.

  Worth knowing: this interacts with the turn controller, because `_step_body` only evaluates distance once the face is *inside* the deadzone. While the robot was pacing it was rarely centered for long enough to look, so "it takes forever to start following" was partly the oscillation above and not only these thresholds.

- **"Okay, I'll follow you!" is a recording, not an API call.** The fixed lines Ruby says on a button press never vary, so paying Gemini to read the same sentence out several times a day was pure waste. `kiosk_api.CANNED_PHRASES` holds the wording; `GET /api/tts/phrase/<key>` synthesizes it **once**, writes the WAV under `data/tts_cache/`, and plays it from disk forever after. The frontend's `speakPhrase(key)` sends only the key — the text lives server-side precisely so what she says and what was recorded can't drift apart.

  Recordings are filed **per voice** (`follow-body.gemini.wav`), because the voice picker switches at runtime and a recording in the wrong voice is worse than no cache. They are also filed under the voice that *actually spoke* rather than the one requested: if Gemini was preferred but rate-limited and Piper covered, caching that as "gemini" would make one bad minute permanent. Switching voices and back reuses the earlier recording rather than re-spending. The synthesis lock is held across the API call, not just the file write, so several quick taps on a cold cache cost one call. To re-record after editing the wording, delete the file — `data/` is gitignored, so the cache is disposable and a fresh clone pays one call per phrase.

- **Detection range** — this is set by how big the detector's input is, since YuNet needs roughly 10-20 pixels of face to fire. `FOLLOW_DETECT_WIDTH` was 320, which is why faces dropped out after a few paces; it is 640 now, roughly doubling usable range. Measured per detection on this Pi 5: 320 → 7.0ms, 480 → 22.9ms, **640 → 40.7ms**, 800 → 67.6ms. That cost is paid once per loop period, so at the current `FOLLOW_DETECT_INTERVAL_S` it is ~40% of one of the four cores (it was ~25% on the old, slower loop), leaving room for the capture thread and MJPEG encoder sharing this process. `FOLLOW_SCORE_THRESHOLD` is 0.6 against OpenCV's default 0.9, which discards exactly the faint, small, off-angle detections a distant person produces; the cost is occasional false positives, which matter little because the loop tracks the *largest* face and spurious ones are almost always small.

  These can't be derived in software — they depend on the robot's rotation speed, the camera's field of view and latency, and the floor surface. Same reasoning as `MOTION_PROFILE`: sane default, correctable without a code edit.
- **The follow thread has a crash barrier** (`_follow_loop` wraps `_follow_loop_body`). An unhandled exception there is uniquely dangerous: the thread dies but nothing notices — `_running` stays true so the UI still shows follow as on, and `MotorLink`'s resend loop keeps re-sending the last command, so if the loop died just after issuing a turn the firmware watchdog never fires (it is being fed) and the robot rotates until someone hits Stop. This was not hypothetical: while follow mode was still pulsed, its burst-length helper returned a `numpy.float32` (derived from the detector's array — and unlike `float64`, *not* a `float` subclass), which `time.sleep()` rejects with `TypeError`. The barrier turns any such bug into "follow switches itself off and the motors park", matching every other failure path in the file.
- **Trip safety** — `MotorLink` takes an `on_trip` callback, invoked from `_reader_loop` on the edge-triggered `LOG: ... TRIP` lines (not the repeating `STATUS: TRIPPED` line) the ESP32 prints when current protection trips. `hardware.py` wires this to `follower.stop()`. `FaceFollower.stop()` and its detection loop share one lock so a trip can never be raced by a command the loop already decided on — whichever side's `send_command()` is still in flight, the other's Stop always lands last. This is a Pi-side reaction to a trip that already happened on the ESP32; the trip detection/latching itself lives entirely in the `.ino` and is untouched.
- **`follow_dryrun.py`** — runs the real `FaceFollower` against the live camera with a fake motor link that prints the command it would have sent. This is how to verify detection and the centering/distance logic with the camera on a desk, before anything is assembled, and the cheapest way to sanity-check a `MOTION_PROFILE` change without the robot.

### Reconnecting to the ESP32

**Opening the serial port does not reset the ESP32**, and code here assumed for a long time that it did (`time.sleep(2.0)  # ESP32 resets on USB serial open; let it boot`). On this board it demonstrably doesn't: pyserial asserts both DTR and RTS on open, and the devkit's two-transistor auto-reset circuit only pulls EN or IO0 when the two lines *differ* — both asserted cancels out and the chip is left in whatever state it was already in. A plain open produces no boot banner at all.

That assumption cost a debugging session. Unplugging a USB keyboard re-enumerated the bus; the CP2102 dropped and came back as a new device four seconds later:

```
12:13:09  usb 2-1: USB disconnect, device number 6     <- keyboard
12:16:14  usb 4-2: USB disconnect, device number 5     <- the ESP32's CP2102
12:16:14  cp210x ttyUSB0: failed set request 0x7 status: -19
12:16:18  cp210x converter now attached to ttyUSB0
```

`_reconnect()` reopened the port, logged `Reconnected to ESP32`, and **the link was dead in both directions**. No telemetry came back and no command got through — but writes into a reopened-but-dead port don't raise, so `/robot/command` answered `200` to 89 consecutive commands while the robot sat still. Nothing anywhere reported a fault.

So `MotorLink._reset_esp32()` now pulses EN on every connect and reconnect: RTS asserted pulls EN low, releasing it boots, DTR stays de-asserted so IO0 stays high and it boots the application rather than the download stub (esptool's classic reset minus the bootloader entry). This is what turns "the port opened" into "the firmware is running". Safe at any time — the firmware's `setup()` calls `stopAll()` first, so a reset parks the motors, which is the right outcome for a link that just failed anyway.

**The trigger is a power problem, and it will keep happening.** The kernel logs the whole story:

```
usb usb4-port1: over-current change #2   \
usb usb2-port1: over-current change #2    |  all six ports at once =
usb usb5-port1: over-current change #2    |  the 5V rail collapsed, not
usb usb3-port1: over-current change #2    |  one device misbehaving
usb usb4-port2: over-current change #2    |
usb usb2-port2: over-current change #2   /
hwmon hwmon1: Undervoltage detected!
hwmon hwmon1: Voltage normalised          (2 seconds later)
```

This Pi 5 is on a **5V/3A (15W)** supply (`/proc/device-tree/chosen/power/max_current`= 3000, and `usbpd_power_data_objects` is all zeros — no PD contract was negotiated), with `usb_max_current_enable = 0`. That caps *total* USB draw at **600mA**, against ~800mA of declared demand: touchscreen 400, keyboard 100, hub 100, camera 100, this link 100 — and the camera's descriptor badly understates what it draws while streaming MJPEG with its mic open. The camera and the ESP32 are both on bus 4, which is why they always drop together.

Fixing it properly is a hardware change: a **powered USB hub** for the camera and the ESP32 (works with the existing supply), or the official **27W 5V/5A** supply plus `usb_max_current_enable=1` in `/boot/firmware/config.txt`. Do **not** set that flag on the 3A supply — it lifts the USB cap to 1.6A on a rail that cannot deliver it, which turns a USB brownout into a whole-board one.

So `_revive_if_silent()` in `_reader_loop` treats it as a recoverable event rather than a fault: once telemetry has been seen and then stops, it pulses EN (rate-limited by `REVIVE_INTERVAL_S`) until the ESP32 answers again. Verified against real hardware by holding EN low to genuinely silence the chip — stale at 2.0s, reset pulsed, link back at 3.3s with no restart. It lives in the reader thread deliberately: verifying a link means watching for telemetry, that thread is the only one that reads the port, and a `_reconnect()` that tried to read for itself would either race this loop for the same bytes or wait on telemetry this loop is blocked from collecting, since it would be sitting on the lock `_reconnect()` holds.

Two related things worth knowing when this bites again:

- **A wedged CP2102 ignores the baud rate.** Reopening the port after that outage produced 181,605 bytes of `0x0C` in 3 seconds — 60KB/s, five times what 115200 can physically carry. That rate-independent flood is the adapter's signature, not the firmware's; it has happened twice now. A `USBDEVFS_RESET` ioctl clears it, and so does the EN pulse above.
- **Killing gunicorn may need `-9`.** SIGTERM stops the listener but a worker blocked on a dead serial read can hang; the master then survives holding `/dev/ttyUSB0`, and the next start fails to bind.

### Motor current and CPU

Live current draw through each motor plus the Pi's CPU usage, shown on all three surfaces: Ruby's screen (top left, under the logo), Apps → Remote Control, and the driving page. Each polls `GET /robot/motor_current` → `{"available", "m1", "m2", "link", "tripped"}` and `GET /robot/cpu_status` → `{"available", "percent"}` together at 1Hz, and renders both in one pass so the readout never updates one half on one tick and the other half on the next.

**Rows hide individually; the block hides only when they all would.** The currents are facts about the ESP32 and CPU is a fact about the Pi, so unplugging the ESP32 must not take the CPU readout down with it. On the driving page this also meant deleting the old `#cpu-indicator` from the status row rather than leaving CPU displayed twice; Wi-Fi stays up there on its own 4s poll. CPU picked up the 1Hz cadence in the move — it was a slow diagnostic sitting next to signal strength, but read beside motor current while driving it's the other half of "what is this robot doing right now".

`get_cpu_usage()` had to change to survive that. It diffs `/proc/stat` jiffies against a baseline, and it used to roll that baseline forward on *every* call — fine when the driving page was the only caller (one poller, one fixed 4s window), wrong the moment three surfaces poll it independently: each call then measures "since whoever happened to call last", windows shrink to a third, and two requests landing in the same jiffy give `delta_total == 0` and an intermittent `available: false` that makes the readout flicker. The baseline now only advances once `CPU_SAMPLE_INTERVAL_S` (1s) has actually elapsed and everyone in between shares the last figure computed, under a lock. Verified with three threads polling concurrently for 4s: 12 readings, no errors, none unavailable.

**The ESP32 measures it; nothing on the Pi does.** It already samples both `IS` pins every 20ms for its overcurrent protection — the reading exists whether or not anything displays it. So the firmware converts those same samples to amps and pushes one `CUR:<m1>,<m2>` line up the serial link every 200ms; `MotorLink` keeps the latest and the endpoint hands it back. No request causes a measurement, which is what makes it safe to poll from several places at once.

The conversion is `ampsToCounts()` run backwards (`countsToAmps()` in the `.ino`), so the displayed number and the trip threshold are the same arithmetic in opposite directions — a round-trip through both returns the input exactly. Two parts of it are worth knowing:

- **The `255/duty` term.** The BTS7960's `IS` pin only mirrors load current while the high-side FET conducts, so what the ADC integrates over a PWM period is the sense voltage scaled by the duty cycle. Undoing that is what makes the reading mean "current while the motor is actually driven" rather than a cycle average — and it's the same factor `ampsToCounts()` applies, which is why the two agree. Duty 0 (stopped) reports 0.00 A rather than dividing by zero: the bridge is off, so no current can flow through the sense path.
- **The zero offset** is measured once at boot, in `setup()`, where `stopAll()` has just guaranteed the bridges are off. The ESP32's ADC has a non-zero floor at the bottom of its range and the driver has a small quiescent `IS` output; both are constants, so measuring beats modelling. On this hardware it came out at exactly 0.0 counts on both channels (`LOG: Current sense zero:` at boot) — the subtraction is currently a no-op, but it's insurance against a board where it isn't.

Resolution is 0.022 A per ADC count at `DUTY`, full scale ~90 A, so two decimals is real precision rather than decoration.

**Reporting changed nothing about tripping.** The samples are read before `checkProtection()` and reused unmodified; the duty is captured before it too, because a trip inside `checkProtection()` calls `stopAll()` and zeroes `activeDuty` — reading it afterwards would report 0.00 A for the very sample that caused the trip.

**The trip thresholds are per-duty.** `ampsToCounts(amps, duty)` used to hardcode `DUTY`, which quietly made the limits mean different currents in the two modes — the sense voltage scales with duty, so a threshold computed at `DUTY`=75 and compared against samples taken at `FOLLOW_DUTY`=55 only tripped at 10 × 75/55 = 13.6 A.

That stopped being a curiosity the moment follow speed and the limits were tuned together, because **the two pull opposite ways**: raising `FOLLOW_DUTY` 55→65 on its own would have moved the follow-mode trip point *down* from 13.6 A to 11.5 A, so going faster would have tripped sooner. `handleCommand()` now re-scales `sustainCounts`/`instantCounts` to whichever duty it just applied (two multiplications, on a genuine command change only — keep-alive resends return long before that line), so `SUSTAIN_AMPS`/`INSTANT_AMPS` mean the same current at either speed and can be set to a number that means something. `setup()` prints both sets at boot.

### When a motor doesn't move

Motor 1 didn't turn at all for a while. **It was a wiring fault on driver 1's signal side, and both motors run correctly now** — but the diagnostics built to find it are still in the firmware, because they turn a day of pulling connectors into two printed lines.

The current readout localised it first: **0.00 A on every one of the four commands while motor 2 drew 4–9 A on the same ones**. Both M1 half-bridges equally dead is a whole driver not switching, not a blown bridge — and it rules out the MCU, the firmware, the serial link and the protection logic in one measurement, since the same command moved the other motor.

`setup()` then reports what the four PWM nets look like, and `P` re-runs that report on demand. Each pin gets two tests:

```
LOG: PWM pads: M1_RPWM(12)=floating/follows  M1_LPWM(13)=floating/follows
LOG:           M2_RPWM(18)=held low/follows  M2_LPWM(19)=held low/follows
```

- **Before the first `ledcAttach()`, with only the ESP32's internal pull-up/pull-down** — what the *outside* does to a pin nothing is driving. A BTS7960 input on a wired-up driver holds the pin down through its input pull-down (`held low`, motor 2 above). A pin that instead follows whichever internal resistor is enabled is connected to nothing (`floating`, motor 1 above — the fault).
- **Driven high, then low, reading the pad back** — whether the pin's own output driver still works. `follows` is healthy; `STUCK LOW` is a short to ground or a dead pad, `STUCK HIGH` a short to a rail. This is what said the fault was *not* GPIO 12/13, which is worth knowing before anyone starts moving motor 1 to a spare pin. Reading an output pin works because Arduino-ESP32's `OUTPUT` leaves the input path enabled (`GPIO_MODE_INPUT_OUTPUT`).

**Read one pin's `floating` on its own and it means nothing** — some BTS7960 boards pull their inputs down, some leave them high-impedance, and an unconnected pin is indistinguishable from a high-Z input. Read as a comparison between two motors wired the same way, it's conclusive, which is why all four pins print together and why motor 2 is the reference. Both pins of one motor reading identical is itself informative: a single loose wire shows one `floating` and one `held low`.

`P` exists so this can be watched live rather than rebooted between attempts — wiggle a connector and the line flips to `held low` the moment contact is made. It's refused while a motor is running, since the test calls `pinMode()`, which drops the pin out of the GPIO matrix and would kill the PWM mid-drive; `reportPads()` re-attaches all four and calls `stopAll()` when it's done. Like `H` it is not a motor command and never feeds the watchdog.

`ledcAttach()` returns a bool that nothing used to check. Unchecked, a failed attach is the same silent-success failure as writing into a dead serial port: the pin never outputs, the motor never moves, and every layer above reports success. It's checked now and warns.

### The grace period, and why follow mode tripped instantly

`GRACE_MS` (300ms) exists to ignore the current spike when a motor starts. It only ever guarded the **instant** trip; the sustained counter ran from the very first sample. And the arithmetic made that fatal rather than merely sloppy:

```
SUSTAIN_SAMPLES = SUSTAIN_MS / SAMPLE_MS = 250 / 20 = 12 samples = 240ms
GRACE_MS                                                        = 300ms
```

240ms < 300ms, so a motor whose inrush outlasted ~220ms tripped on "sustained overcurrent" **before the grace period protecting it had expired**. Simulated against a faithful port of `checkProtection()`: a 240ms inrush, a 400ms inrush and a genuine stall all tripped at exactly t=220ms.

It went unnoticed for as long as it did because **follow mode used to pulse its turns** — bursts shorter than 12 samples never let the counter fill. Switching to a slow continuous hold (see `FOLLOW_DUTY` above) removed that accidental masking, and follow mode began tripping on essentially every turn it started. The remote page suffers it less because a human holds one direction while the robot actually accelerates away, rather than starting and reversing eight times a second.

The fix is one early return covering both paths: nothing is judged at all until `GRACE_MS` has passed. Earliest a sustained trip can now fire is `GRACE_MS + SUSTAIN_MS` = 540ms of *continuous* overcurrent, which is a stall rather than a startup. Verified by simulation: inrush of 200/240/400/520ms → no trip; genuine stall → still trips, at 520ms.

**If it still trips after that, the motors are genuinely stalling, and the fix is to raise `FOLLOW_DUTY`, not lower it.** A stalled motor draws locked-rotor current for as long as you leave it energised; one that is actually turning develops back-EMF and draws far less. This is why the answer to "it trips as it starts a turn" was to go *faster*: 55/255 is 21.6% duty, close enough to the torque needed to break a standing robot out of rest that it could sit straining instead of accelerating away.

It was tripping on startup even after the grace-period fix, so both knobs moved together:

| | was | now | why |
| --- | --- | --- | --- |
| `FOLLOW_DUTY` | 55 | **65** | +18% speed; 87% of remote-control speed rather than 73%. Also less likely to sit near stall. |
| `SUSTAIN_AMPS` | 10 | **16** | headroom for startup current. Against the *effective* old follow-mode limit of 13.6 A, not the nominal 10. |
| `INSTANT_AMPS` | 20 | **30** | same, against an effective 27.3 A. |

Both limits now mean the same current at either duty (see above), so remote control moved from 10 A/20 A to 16 A/30 A as well — a real loosening there, and worth knowing.

**These are judgement, not measurement.** The motors' stall current isn't documented anywhere in this project. The motor current readout is how to replace that judgement with a number: watch what a normal follow-mode turn actually peaks at, and leave roughly 50% headroom above it. A stall reads high and flat; a startup spikes and decays.

### Protection applies to remote control identically

Verified in the firmware rather than assumed: `checkProtection()` is called unconditionally from `loop()` every `SAMPLE_MS`, gated only on `m1On || m2On`. All eight motor commands set those — `F`/`B`/`C`/`X` from the remote page and `f`/`b`/`c`/`x` from follow mode alike — and nothing inside `checkProtection()` branches on the command source or its case. The only place `lastAppliedCmd` appears is the trip latch.

What was *not* equal was visibility. `hardware._handle_trip()` stops the follower, which is the whole story when Follow me is driving and **nothing at all** when somebody is driving from the remote page: the robot stopped dead, the ESP32 quietly ignored resends of the command that tripped it, and `/robot/command` carried on answering `200` to a D-pad that no longer moved anything — the same class of silent-success failure as the dead serial link above. `MotorLink.trip_state()` now reads the latch back off the `STATUS:` line and `/robot/motor_current` reports it as `tripped`, so the driving page shows an amber banner naming the motor and trip type, and saying that any other direction (or Stop) re-arms it. Amber rather than red: unlike a dead link, this is the hardware working as designed.

The endpoint also returns `link`: `"ok"` / `"stale"` / `"unknown"` (`MotorLink.link_health()`). This telemetry is the first thing on the serial link that makes "is the ESP32 actually there?" observable at all — see Reconnecting to the ESP32 above for what that was worth finding out the hard way. The driving page shows a red banner on `"stale"` only: that means an ESP32 we have heard from has gone quiet, which is a real fault. `"unknown"` means we never heard from it, which is also what firmware predating the `CUR:` line looks like, and a permanent banner on a working rig just teaches people to ignore banners. The D-pad deliberately stays live either way — the link can come back on its own, and disabling Stop because the robot is unreachable would be exactly backwards.

Every path reports `available: false` rather than a stale number when there's no reading: no ESP32, an ESP32 that has gone quiet (`MOTOR_CURRENT_STALE_S`, 2s = ten missed reports), or one running firmware old enough to predate the `CUR:` line. All three UIs then hide the readout entirely. A number that has silently stopped updating is worse than no number, because nothing about it looks wrong.

`CUR:` lines are dropped by `_reader_loop` **before** its `print()` — at 5Hz they would bury every `STATUS:`/`LOG:` line worth seeing under 300 lines a minute. That is also why the display polls at 1Hz and not the firmware's 5Hz: this is a readout somebody glances at, not a control loop, and the thing that has to react fast to a current spike is the protection logic on the ESP32, which never involves the Pi at all.

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

Single ASCII byte, no framing: `F` `B` `C` `X` `S`, plus `f` `b` `c` `x` for the same four directions at the firmware's lower `FOLLOW_DUTY` (see Follow me). The one exception is `H` + exactly three digits (`H090`), which aims the head servo; fixed width so it needs no terminator, and a non-digit arriving mid-number abandons the number and is re-handled as an ordinary command, so a stray `H` can never swallow a Stop. Head commands do not feed the motor watchdog — they aren't motor commands, and the watchdog only fires while a motor is actually on. `P` is the other non-motor character: it re-prints the PWM pad report for chasing a motor that doesn't move (see When a motor doesn't move), is refused while a motor is running, and likewise never touches the watchdog. Nothing on the Pi sends it — it's for a serial monitor or a bench script, which is why it isn't in `VALID_WIRE_COMMANDS`. **Case is significant** — nothing on this path may upper- or lower-case a command in passing. `MotorLink.send_command()` is the only validator; get the character from `motion.command_for(intent, slow=...)` rather than spelling it out. Status/log lines coming back (`STATUS:`/`LOG:` prefixes) are read by `_reader_loop` and printed; `LOG:` lines containing `TRIP` also fire the `on_trip` callback.

Coming back the other way there is a third prefix, `CUR:<m1>,<m2>` — measured motor current in amps, every 200ms (see Motor current above). `_reader_loop` parses it into `MotorLink._motor_amps` and returns **before** the `print()` the other two get, since 5Hz of telemetry in the log would bury everything else. A malformed one is dropped rather than raised on: this link has no framing, so a line truncated by a reconnect is an ordinary event, and losing the reader thread would also lose trip detection.

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
