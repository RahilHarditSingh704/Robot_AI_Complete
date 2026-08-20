# Robot AI Complete — Architecture & Code Guide

> A comprehensive guide to how every part of this project works, from the high-level Python web interface and AI kiosk down to the ESP32 motor controller firmware.

---

## Table of Contents

- [System Overview](#system-overview)
- [High-Level Architecture](#high-level-architecture)
- [Python Backend (Raspberry Pi 5)](#python-backend-raspberry-pi-5)
  - [app.py — Application Entry Point](#apppy--application-entry-point)
  - [robot_api.py — Robot Control API](#robot_apipy--robot-control-api)
  - [kiosk_api.py — Kiosk Mode API (Ruby)](#kiosk_apipy--kiosk-mode-api-ruby)
  - [motor_link.py — ESP32 Serial Communication](#motor_linkpy--esp32-serial-communication)
  - [camera.py — Camera Capture & Streaming](#camerapy--camera-capture--streaming)
  - [face_follow.py — Autonomous Face Tracking](#face_followpy--autonomous-face-tracking)
  - [hardware.py — Hardware Manager & Watchdog](#hardwarepy--hardware-manager--watchdog)
  - [motion.py — Motion Profile Mapping](#motionpy--motion-profile-mapping)
  - [access.py — Network Security Gate](#accesspy--network-security-gate)
  - [tls_cert.py — TLS Certificate Manager](#tls_certpy--tls-certificate-manager)
  - [gunicorn.conf.py — Production Server Config](#gunicornconfpy--production-server-config)
  - [start.sh — System Bootstrap Script](#startsh--system-bootstrap-script)
  - [follow_dryrun.py — Face-Follow Test Harness](#follow_dryrunpy--face-follow-test-harness)
- [ESP32 Firmware](#esp32-firmware)
  - [Overview & Architecture](#overview--architecture)
  - [Communication Protocol](#communication-protocol)
  - [Pin Assignments](#pin-assignments)
  - [Motor Control (BTS7960 Dual H-Bridge)](#motor-control-bts7960-dual-h-bridge)
  - [Servo Control (RDS51150 Camera Pan)](#servo-control-rds51150-camera-pan)
  - [Dual-Tier Current Protection](#dual-tier-current-protection)
  - [Safety Features](#safety-features)
  - [State Management](#state-management)
  - [Firmware Lifecycle (setup / loop)](#firmware-lifecycle-setup--loop)
- [Frontend & Web Interfaces](#frontend--web-interfaces)
  - [robot.html — Remote Driving Dashboard](#robothtml--remote-driving-dashboard)
  - [Kiosk Mode Interface (Ruby AI Assistant)](#kiosk-mode-interface-ruby-ai-assistant)
  - [Mini-Apps Framework](#mini-apps-framework)
  - [Kiosk Chrome Extension](#kiosk-chrome-extension)
- [Data Flow Diagrams](#data-flow-diagrams)
- [Configuration & Environment](#configuration--environment)
- [Dependencies](#dependencies)

---

## System Overview

This project is a **full-stack robot platform** built for U of T Electrical & Computer Engineering. It consists of three layers:

| Layer | Technology | Role |
|-------|-----------|------|
| **Web Interface** | HTML/CSS/JavaScript | AI assistant kiosk ("Ruby") and remote driving dashboard |
| **Python Backend** | Flask + Gunicorn on Raspberry Pi 5 | API server, Gemini AI integration, camera, face tracking |
| **ESP32 Firmware** | Arduino C++ on ESP32 | Real-time motor/servo/sensor control with current protection |

The Raspberry Pi 5 acts as the "brain" — running the web server, processing camera frames with OpenCV YuNet, making Gemini AI calls, and sending motor commands to the ESP32. The ESP32 is the "body" — directly driving dual BTS7960 motor drivers and an RDS51150 camera pan servo, while monitoring motor current draw and enforcing hardware safety limits. They communicate over **USB Serial at 115200 baud**.

```
┌─────────────────────────────────────────────────────┐
│                 User's Browser                      │
│   (Remote D-Pad Control  or  Kiosk Touchscreen)     │
└────────────────────────┬────────────────────────────┘
                         │ HTTPS (REST API + MJPEG)
                         ▼
┌─────────────────────────────────────────────────────┐
│              Raspberry Pi 5 (Python)                │
│                                                     │
│  ┌──────────┐ ┌──────────┐ ┌────────────────────┐  │
│  │ Robot    │ │ Kiosk    │ │ Face Follow /      │  │
│  │ API      │ │ API      │ │ Camera / AI        │  │
│  └────┬─────┘ └────┬─────┘ └────────┬───────────┘  │
│       └─────────────┴────────┬───────┘              │
│                              ▼                      │
│                    ┌───────────────────┐             │
│                    │    MotorLink      │             │
│                    │ (USB Serial +     │             │
│                    │  Keep-Alive +     │             │
│                    │  DTR/RTS Reset)   │             │
│                    └────────┬──────────┘             │
└─────────────────────────────┼───────────────────────┘
                              │ USB Serial 115200 baud
                              ▼
┌─────────────────────────────────────────────────────┐
│              ESP32 Microcontroller                  │
│                                                     │
│  ┌──────────────┐ ┌───────────┐ ┌────────────────┐ │
│  │ Dual BTS7960 │ │ RDS51150  │ │ Current Sense  │ │
│  │ Motor Driver │ │ Pan Servo │ │ ADC (20ms)     │ │
│  │ (20kHz PWM)  │ │ (50Hz)    │ │ 30A / 16A Trip │ │
│  └──────────────┘ └───────────┘ └────────────────┘ │
└─────────────────────────────────────────────────────┘
```

---

## High-Level Architecture

### Two Modes of Operation

1. **Robot Control Dashboard** (`/robot/`) — A remote network-accessible D-pad interface for driving the robot, with live video, motor current telemetry, CPU stats, and Wi-Fi signal monitoring.
2. **Kiosk Mode** (`/kiosk/` via `/`) — A local-only touchscreen AI assistant ("Ruby") with Gemini chat, sentiment-driven facial expressions, TTS/STT, mini-apps (transit, weather, notes, system controls), and autonomous face-following.

Both modes share the same backend and ESP32 firmware. Network access is controlled by a centralized security gate (`access.py`) — kiosk endpoints are localhost-only, while robot endpoints require the "Remote Control" toggle to be enabled from the kiosk.

---

## Python Backend (Raspberry Pi 5)

The backend runs as a **single-process WSGI application** under Gunicorn with 1 worker and 16 threads (`gthread` mode), serving everything over HTTPS on port 5000.

### `app.py` — Application Entry Point

**File:** [`app.py`](file:///Robot_AI_Complete/app.py)

The main WSGI entry point imported by Gunicorn. Responsibilities:
- Configures `stdout` line-buffering for reliable logging under Gunicorn.
- Loads `.env` before any project imports via `python-dotenv`.
- Creates the Flask `app` and registers blueprints (`kiosk_api`, `robot_api`).
- Installs the `access.py` security gate via `access.install(app)`.
- Registers `atexit` cleanup for camera and serial port shutdown.

---

### `robot_api.py` — Robot Control API

**File:** [`robot_api.py`](file:///Robot_AI_Complete/robot_api.py)

Flask blueprint providing the remote driving interface and telemetry under `/robot/`:

| Route | Method | Description |
|-------|--------|-------------|
| `/robot/` | GET | Serves the driving dashboard (`robot.html`), injecting wire command mappings from `motion.WIRE_COMMANDS` |
| `/robot/command` | POST | Sends drive commands (`{"cmd": "F"}`). Rejects non-STOP commands during face-follow (HTTP 409) |
| `/robot/heartbeat` | POST | Feeds the server-side safety watchdog (3s timeout) |
| `/robot/follow` | POST | Starts/stops face-following (`@local_only` — kiosk only) |
| `/robot/follow_status` | GET | Returns follow mode state, availability, and disabled reason |
| `/robot/motor_current` | GET | M1/M2 current in Amps, ESP32 link health, trip state |
| `/robot/video_feed` | GET | MJPEG stream with per-frame `stream_allowed()` check |
| `/robot/video_resolution` | GET/POST | Dynamic stream quality switching (720p/360p/240p/off) |
| `/robot/camera_status` | GET | Camera hardware availability |
| `/robot/wifi_status` | GET | Wi-Fi RSSI in dBm and quality level |
| `/robot/cpu_status` | GET | Pi CPU usage percentage |

Key safety feature: The video feed sets a 10-second socket timeout on the Gunicorn socket to prevent thread exhaustion from disconnected clients, and checks `access.stream_allowed()` per-frame to immediately terminate video when Remote Control is disabled.

---

### `kiosk_api.py` — Kiosk Mode API (Ruby)

**File:** [`kiosk_api.py`](file:///Robot_AI_Complete/kiosk_api.py)

The largest API surface — powers the Ruby touchscreen AI assistant for U of T ECE visitors:

**AI & Speech:**
| Route | Method | Description |
|-------|--------|-------------|
| `/api/chat` | POST | Sends conversation history to **Google Gemini**, returns reply + emotion |
| `/api/transcribe` | POST | Speech-to-text via Gemini (receives base64 audio) |
| `/api/tts` | POST | Text-to-speech — tries Gemini Cloud TTS, falls back to local **Piper ONNX** |
| `/api/tts/phrase/<key>` | GET | Cached canned audio phrases (saves API costs) |
| `/api/tts/voices` | GET | Available TTS voice list |
| `/api/tts/voice` | POST | Switch active TTS voice |

**Sentiment Analysis:** Uses `vaderSentiment` with regex patterns to map chat text to facial expressions (`happy`, `elated`, `blush`, `neutral`).

**System & Mini-App Bridges:**
| Route | Method | Description |
|-------|--------|-------------|
| `/api/remote` | GET/POST | Toggle Remote Control network access |
| `/api/notes` | GET/POST | Persistent notes scratchpad |
| `/api/system/volume` | GET/POST | PipeWire audio volume via `wpctl` |
| `/api/system/info` | GET | CPU temp, uptime, memory, disk from `/proc` |
| `/api/system/power` | POST | `systemctl reboot` / `systemctl poweroff` |
| `/api/ttc` | GET | Live TTC transit predictions (UmoIQ proxy) |

**Ruby's Personality:** The system prompt defines Ruby as a quirky assistant at U of T ECE, with verified campus context (buildings BA, SF, GB, MY; emergency numbers) and a constraint to never hallucinate room numbers.

---

### `motor_link.py` — ESP32 Serial Communication

**File:** [`motor_link.py`](file:///Robot_AI_Complete/motor_link.py)

Safety-critical hardware driver managing the USB serial link to the ESP32.

#### Key Class: `MotorLink`

| Method | Description |
|--------|-------------|
| `send_command(cmd)` | Validates against `motion.VALID_WIRE_COMMANDS`, writes to serial under `threading.RLock` |
| `set_head_angle(deg)` | Sends fixed-width `H<deg:03d>` (e.g., `H090`) for servo pan |
| `stop()` | Sends `S` (stop) command |
| `get_motor_currents()` | Returns latest M1/M2 current readings in Amps |
| `trip_state()` | Returns overcurrent trip status |
| `link_health()` | Returns `ok`, `stale`, or `unknown` |

**Critical Background Threads:**

1. **Keep-Alive Resend Loop** (`_resend_loop()`) — Resends the active command every **150ms** to feed the ESP32's 500ms watchdog timer. Without this, motors would stop during sustained key holds.

2. **Reader Loop** (`_reader_loop()`) — Reads incoming telemetry from ESP32:
   - `CUR:<m1>,<m2>` — Motor current at 5Hz
   - `STATUS:<mode>` — State change reports
   - `LOG: ... TRIP` — Overcurrent trip events (fires `_on_trip` callback)

3. **Silent Link Revival** (`_revive_if_silent()`) — Detects when an open serial connection stops sending telemetry (common after 5V rail brownouts) and pulses DTR/RTS to reset the ESP32's EN pin.

**Port Auto-Detection:** Prioritizes stable `/dev/serial/by-id/*` symlinks before falling back to `/dev/ttyUSB*` / `/dev/ttyACM*`.

**Fallback:** `NullMotorLink` — Mock instance used when no ESP32 is connected, allowing the kiosk to boot cleanly while marking `available = False`.

---

### `camera.py` — Camera Capture & Streaming

**File:** [`camera.py`](file:///Robot_AI_Complete/camera.py)

Threaded USB camera capture manager using OpenCV with V4L2 backend.

#### Key Class: `Camera`

| Method | Description |
|--------|-------------|
| `start()` | Launches async capture thread (non-blocking startup) |
| `get_frame()` | Thread-safe copy of raw BGR frame for face detection |
| `set_overlay(fn)` | Registers callback to draw on frames before JPEG encoding |
| `set_stream_resolution(name)` | Dynamic quality: `720p`, `360p`, `240p`, or `off` |
| `mjpeg_generator()` | Yields `multipart/x-mixed-replace` chunks for `<img>` tags |
| `close()` | Stops thread and releases V4L2 device |

**Smart Device Probing:** Checks `/dev/v4l/by-id/*` first, bypassing dummy Raspberry Pi 5 hardware ISP/HEVC nodes (`/dev/video23-26`) that block for 10 seconds on verification reads.

- Default capture: 1280×720 at 30 FPS in native MJPEG mode.
- Thread-safe frame access via `threading.Lock`.

---

### `face_follow.py` — Autonomous Face Tracking

**File:** [`face_follow.py`](file:///Robot_AI_Complete/face_follow.py)

Real-time face detection and tracking using **OpenCV YuNet DNN** (`face_detection_yunet_2023mar.onnx`).

#### Key Class: `FaceFollower`

**Two Tracking Modes:**

| Mode | Behavior |
|------|----------|
| **Body** (`MODE_BODY`) | Drives chassis using slow-speed commands (`f`, `b`, `c`, `x`) to follow the face |
| **Head** (`MODE_HEAD`) | Adjusts pan servo angle (`H<deg>`) while keeping drivetrain stationary |

**Face Detection:** YuNet ONNX model at 640px detection width, score threshold 0.6, NMS threshold 0.3. Runs at 10Hz.

**Turn Controller (`_TurnController`):**
- **Early Lead Release** — Projects face offset forward in time (`projected = offset + rate × TURN_LEAD_S`, where `TURN_LEAD_S = 0.28s`) to release turning *before* crossing the center line, preventing overshoot oscillation.
- **Adaptive Deadzone** — Measures the robot's physical sweep rate (frame-widths/sec) and dynamically widens the deadzone (up to 0.35) to match the drivetrain's minimum achievable turn angle.

**Distance Hysteresis (`_update_distance_state`):**
| State | Enter Threshold | Exit Threshold |
|-------|----------------|----------------|
| Too close (back up) | Face height ≥ 50% frame | < 40% |
| Too far (drive forward) | Face height ≤ 28% frame | > 36% |
| OK (hold position) | In between | — |

**Lost Face Behavior:** Coasts in the direction of the lost face for 0.5 seconds (`FOLLOW_LOST_COAST_S`) before stopping.

**Head Mode Servo Control:** Proportional gain (`correction = offset × 70° × 0.5`) clamped to 0°–180°.

---

### `hardware.py` — Hardware Manager & Watchdog

**File:** [`hardware.py`](file:///Robot_AI_Complete/hardware.py)

Singleton registry managing hardware instances and safety:

- **Global Singletons:** `link` (MotorLink), `follower` (FaceFollower), `camera` (Camera).
- **Connection Watchdog** (`_connection_watchdog_loop()`) — Runs every 0.5s. If manual driving is active and no `/robot/heartbeat` has arrived within 3 seconds, commands immediate `link.stop()`.
- **Trip Handler** (`_handle_trip()`) — Callback from `MotorLink`. Automatically stops `FaceFollower` if the ESP32's hardware current protection trips.
- **System Stats:**
  - `get_wifi_signal()` — Reads `/proc/net/wireless` directly (no subprocess).
  - `get_cpu_usage()` — Samples `/proc/stat` CPU jiffy deltas over 1.0s shared window.

---

### `motion.py` — Motion Profile Mapping

**File:** [`motion.py`](file:///Robot_AI_Complete/motion.py)

Abstraction layer mapping semantic movement intents to ESP32 wire characters, accounting for the physical motor mounting configuration.

**Wire Command Set:** `VALID_WIRE_COMMANDS = {'F', 'B', 'C', 'X', 'S', 'f', 'b', 'c', 'x'}`
- Uppercase = full speed (`DUTY = 75`)
- Lowercase = slow/gentle speed (`FOLLOW_DUTY = 65`, used by face tracking)

**Motion Profiles:**

| Profile | Forward | Backward | Turn CW | Turn CCW | Stop |
|---------|---------|----------|---------|----------|------|
| `mirrored` (default) | `X` | `C` | `F` | `B` | `S` |
| `direct` | `F` | `B` | `C` | `X` | `S` |

The `mirrored` profile handles differential drives where motors are mirror-mounted (a common physical configuration).

**Key Functions:**
- `command_for(intent, slow=False)` — Returns the exact wire character for an intent.
- `describe(wire_cmd)` — Friendly UI label for status displays.
- `MOTION_INVERT_DRIVE` / `MOTION_INVERT_TURN` — Environment-variable flags for hardware-level direction fixes.

---

### `access.py` — Network Security Gate

**File:** [`access.py`](file:///Robot_AI_Complete/access.py)

Centralized security policy resolving conflicting access requirements:

- **Kiosk endpoints** (system commands, power, volume) → **localhost only**
- **Robot endpoints** (driving, video) → **network-accessible only when Remote Control is toggled on**

| Function | Description |
|----------|-------------|
| `install(app)` | Registers global `@app.before_request` hook |
| `remote_enabled()` / `set_remote_enabled()` | In-memory toggle (always `False` on boot) |
| `@local_only` | Decorator preventing remote access even when Remote Control is on |
| `stream_allowed()` | Per-frame check for MJPEG generators — instantly severs video when Remote Control is toggled off |

---

### `tls_cert.py` — TLS Certificate Manager

**File:** [`tls_cert.py`](file:///Robot_AI_Complete/tls_cert.py)

Generates TLS certificates for HTTPS (required by browsers for microphone access, secure WebSocket contexts, etc.):

1. Attempts to issue a publicly trusted cert via **Tailscale** (`tailscale cert <MagicDNS>`).
2. Falls back to a self-signed RSA-2048 certificate with SANs for `localhost`, `127.0.0.1`, Tailscale MagicDNS name, and local LAN IPs.

Kept standalone with zero project imports so it can run safely in Gunicorn's master arbiter process without prematurely initializing hardware singletons.

---

### `gunicorn.conf.py` — Production Server Config

**File:** [`gunicorn.conf.py`](file:///Robot_AI_Complete/gunicorn.conf.py)

| Setting | Value | Rationale |
|---------|-------|-----------|
| `bind` | `0.0.0.0:5000` | All interfaces (security enforced by `access.py`) |
| `workers` | `1` | **Mandatory** — prevents multiple processes claiming USB serial port and camera |
| `threads` | `16` | Long Gemini API calls never block emergency Stop commands or MJPEG streaming |
| `worker_class` | `gthread` | Threaded concurrency |
| `keepalive` | `5` | Persistent connections eliminate repetitive TLS handshakes |

---

### `start.sh` — System Bootstrap Script

**File:** [`start.sh`](file:///Robot_AI_Complete/start.sh)

Bash bootstrap script for launching the full robot stack on boot:

1. Starts Gunicorn in the background and sets an `EXIT` trap to kill it on exit.
2. Polls `curl -sk https://127.0.0.1:5000/` until the web server is ready.
3. Detects and fixes PipeWire/WirePlumber HDMI audio issues (restarts `wireplumber` if detecting dummy sink).
4. Manages a dedicated Chromium profile (`~/.config/ruby-kiosk`) and terminates lingering profile locks.
5. Launches Chromium in `--kiosk` mode with `--load-extension=kiosk-extension` and `--allow-insecure-localhost`.

---

### `follow_dryrun.py` — Face-Follow Test Harness

**File:** [`follow_dryrun.py`](file:///Robot_AI_Complete/follow_dryrun.py)

Standalone CLI tool for testing face tracking without the ESP32:
- `PrintMotorLink` — Mock motor link that logs timestamped command transitions to stdout.
- Opens camera, initializes `FaceFollower`, prints motion summary, and runs until Ctrl+C.
- Useful for tuning PID parameters, deadzone thresholds, and verifying YuNet detection quality.

---

## ESP32 Firmware

**File:** [`robot_esp32_ble_and_serial.ino`](file:///Robot_AI_Complete/robot_esp32_ble_and_serial/robot_esp32_ble_and_serial.ino)

### Overview & Architecture

The ESP32 firmware is a **deterministic, non-blocking, real-time controller** that receives single-character commands over USB Serial, drives motors and servos, monitors current draw, and enforces hardware-level safety limits.

**Design Philosophy:**
- **Zero external library dependencies** — Uses core ESP32 Arduino HAL (`ledcAttach`, `ledcWrite`, `analogRead`, etc.) for predictable timing and low footprint.
- **Strictly reactive** — All intelligence lives on the Pi. The ESP32 only executes commands and enforces safety.
- **Non-blocking main loop** — Coordinates command polling, servo slewing, ADC sampling (20ms), current reporting (200ms), and watchdog expiration (500ms) without any blocking delays.

> **Note:** While the filename retains `ble_and_serial` for legacy tracking, BLE has been intentionally removed in favor of a deterministic, lower-latency USB Serial pipeline.

---

### Communication Protocol

**Transport:** USB Serial at 115200 baud (CP2102 bridge).

**Commands (Host → ESP32):** Single-character commands, newline-terminated:

| Command | Format | Description |
|---------|--------|-------------|
| `F` | `F` | Forward (full speed, `DUTY = 75`) |
| `B` | `B` | Backward (full speed) |
| `C` | `C` | Rotate clockwise (full speed) |
| `X` | `X` | Rotate counter-clockwise (full speed) |
| `f` | `f` | Forward (slow, `FOLLOW_DUTY = 65`) |
| `b` | `b` | Backward (slow) |
| `c` | `c` | Rotate clockwise (slow) |
| `x` | `x` | Rotate counter-clockwise (slow) |
| `S` | `S` | **Stop** — all motors halted |
| `H` | `Hnnn` | Head pan servo — fixed 3 ASCII digits (e.g., `H090` = 90°, range 0–180) |
| `P` | `P` | Pin/pad diagnostic test (refused if motors running) |

**Telemetry (ESP32 → Host):**

| Message | Format | Frequency |
|---------|--------|-----------|
| Motor current | `CUR:<m1>,<m2>\n` | 5 Hz (every 200ms) |
| Status change | `STATUS:<mode>\n` | Edge-triggered |
| Diagnostics/Trips | `LOG:<message>\n` | On event |

---

### Pin Assignments

```
┌──────────────────────────────────────────────────────────────┐
│                     ESP32 DevKit Pinout                      │
├───────────────┬─────────┬────────────────────────────────────┤
│ Function      │ GPIO    │ Hardware                           │
├───────────────┼─────────┼────────────────────────────────────┤
│ M1_RPWM       │ GPIO 12 │ Motor 1 Forward PWM (LEDC Timer)  │
│ M1_LPWM       │ GPIO 13 │ Motor 1 Reverse PWM (LEDC Timer)  │
│ M2_RPWM       │ GPIO 18 │ Motor 2 Forward PWM (LEDC Timer)  │
│ M2_LPWM       │ GPIO 19 │ Motor 2 Reverse PWM (LEDC Timer)  │
│ SERVO_PIN     │ GPIO 23 │ Head Pan Servo (50Hz, 16-bit PWM) │
│ SenseM1       │ GPIO 34 │ Motor 1 Current ADC (input only)  │
│ SenseM2       │ GPIO 35 │ Motor 2 Current ADC (input only)  │
└───────────────┴─────────┴────────────────────────────────────┘
```

---

### Motor Control (BTS7960 Dual H-Bridge)

The robot uses **dual BTS7960 high-power half-bridge drivers** in a differential-drive configuration.

**PWM Configuration:**
- Frequency: **20 kHz** (ultrasonic — eliminates audible motor whine)
- Resolution: 8-bit (0–255 duty cycle steps)

**Speed Modes:**
| Mode | Duty | Usage |
|------|------|-------|
| Full speed | `DUTY = 75` (~29.4%) | Manual teleoperation (uppercase commands) |
| Follow speed | `FOLLOW_DUTY = 65` (~25.5%) | Autonomous face tracking (lowercase commands) |

**Deadzone:** Speeds below a threshold are treated as 0 to prevent motor stall whine at very low PWM.

**Low-Level Functions:**
- `motor1(fwd, rev)` — Writes PWM to `M1_RPWM` and `M1_LPWM`
- `motor2(fwd, rev)` — Writes PWM to `M2_RPWM` and `M2_LPWM`
- `stopAll()` — Zeroes all PWM channels, resets protection counters, clears active command

---

### Servo Control (RDS51150 Camera Pan)

Controls an **RDS51150 digital robotic servo** carrying the camera on GPIO 23.

| Parameter | Value |
|-----------|-------|
| PWM frequency | 50 Hz (standard 20ms period) |
| PWM resolution | 16-bit (65,536 counts, ~0.3 µs/count) |
| Pulse range | 500 µs (0°) to 2500 µs (180°) |
| Angle range | 0° to 180° (clamped) |

**Smooth Slew Rate Limiter:** `updateServo()` steps `servoCurrent` toward `servoTarget` at a maximum rate of **40 degrees/second**, evaluated every 20ms. This prevents camera jitter and mechanical whiplash from discrete frame-by-frame bounding box updates.

---

### Dual-Tier Current Protection

The firmware implements a sophisticated dual-tier overcurrent protection system via `checkProtection()`:

**ADC Configuration:**
- Sense resistor: R_sense = 1000 Ω
- K_ILIS = 8500.0 (BTS7960 current sensing ratio)
- ADC full-scale: 3.1V, 12-bit (4095 counts)
- Sample period: 20ms
- Boot-time baseline calibration: 64 samples averaged with motors guaranteed off

**Protection Tiers:**

| Tier | Threshold | Window | Purpose |
|------|-----------|--------|---------|
| **Instantaneous** | 30A | Single sample | Catches dead shorts, stall locks |
| **Sustained** | 16A | 12 consecutive samples (250ms) | Catches prolonged overload |

**Grace Period:** All trip evaluations are suppressed for the first **300ms** after a new movement starts to tolerate high motor inrush currents.

**Clear Filter:** 3 consecutive samples below the limit reset the overcurrent counter.

**Trip Action:** `stopAll()` instantly halts both motors, emits `LOG: ... TRIP`, latches `tripped = true`, and sets `mode = "TRIPPED (...)"`. The tripped command is rejected on resend — only a new command or Stop clears the latch.

---

### Safety Features

> [!IMPORTANT]
> Multiple layers of safety ensure the robot stops moving if anything goes wrong.

1. **Command Watchdog Timer** — If no command is received within **500ms** (`CMD_TIMEOUT_MS`), the ESP32 automatically stops all motors and logs `WATCHDOG TIMEOUT`. This protects against host crashes, USB disconnections, and UI lockups.

2. **Keep-Alive Resend (Pi side)** — `MotorLink._resend_loop()` retransmits the active command every 150ms, keeping the 500ms watchdog fed during sustained button holds.

3. **Connection Watchdog (Pi side)** — `hardware._connection_watchdog_loop()` stops motors if no browser heartbeat arrives within 3 seconds.

4. **Browser Fail-Safe** — `robot.html` uses `navigator.sendBeacon` on `beforeunload` to guarantee a Stop command if the browser tab closes.

5. **Dual-Tier Current Protection** — Hardware-level 30A instantaneous and 16A sustained overcurrent detection with automatic motor shutdown.

6. **Silent Link Revival** — `MotorLink._revive_if_silent()` detects USB brownouts and pulses DTR/RTS to reset the ESP32.

7. **Speed Clamping** — All duty values are clamped regardless of input.

8. **Servo Angle Clamping** — Prevents driving servo beyond 0°–180° mechanical limits.

---

### State Management

| Variable | Type | Purpose |
|----------|------|---------|
| `pendingCmd` | `volatile char` | Latest valid command from serial (decouples I/O from execution) |
| `lastAppliedCmd` | `char` | Active command (distinguishes state changes from keep-alive resends) |
| `tripped` / `trippedCmd` | `bool` / `char` | Overcurrent latch — resends of faulted command rejected |
| `activeDuty` | `int` | Active duty (0, `DUTY`, or `FOLLOW_DUTY`) for current scaling |
| `servoTarget` / `servoCurrent` | `float` | Servo slew rate limiter state |
| `zeroM1` / `zeroM2` | `float` | Boot-time ADC baseline calibration offsets |
| `readingAngle` / `angleAccum` / `angleDigits` | — | Mid-parse state machine for `Hnnn` protocol |

---

### Firmware Lifecycle (setup / loop)

#### `setup()`
1. Initialize Serial at 115200 baud.
2. Configure motor PWM channels (20 kHz, 8-bit) via `ledcAttach`.
3. Configure servo PWM (50 Hz, 16-bit) and set to center.
4. Configure current sense ADC pins (GPIO 34, 35 as input-only).
5. Run ADC baseline calibration (64 samples, motors off).
6. Run pad diagnostic test (`reportPads()`).
7. Print startup message with firmware version.

#### `loop()`
1. **Check Serial** — Non-blocking character stream parser in `checkSerialCommand()`. Whitespace/newlines silently dropped to prevent desynchronization.
2. **Handle Command** — Execute pending command: set motor PWM, update servo target, or run diagnostics.
3. **Servo Slewing** — `updateServo()` steps current angle toward target at max 40°/sec.
4. **ADC Sampling** — Every 20ms, read current sense pins.
5. **Current Protection** — `checkProtection()` evaluates instantaneous/sustained limits.
6. **Current Reporting** — Every 200ms, send `CUR:<m1>,<m2>` telemetry.
7. **Watchdog Check** — If `millis() - lastCmdReceivedTime > 500`, stop motors.
8. **Status Reporting** — Edge-triggered: only report on actual state changes.

#### Self-Diagnostic Pad Testing

The firmware includes hardware integrity verification:
- `padState(pin)` — Tests with internal pullup/pulldown to classify as `"held low"`, `"held high"`, or `"floating"`.
- `padDriveTest(pin)` — Momentarily drives HIGH/LOW for 200µs to detect `"follows"`, `"STUCK LOW"`, or `"STUCK HIGH"`.
- `reportPads()` — Runs on boot and on `P` command for hardware verification without physical disassembly.

---

## Frontend & Web Interfaces

### `robot.html` — Remote Driving Dashboard

**File:** [`robot.html`](file:///Robot_AI_Complete/templates/robot.html)

A standalone Jinja2 template served at `/robot/` for remote robot operation over the network.

**Input & Controls:**
- **3×3 D-Pad** — Forward, Rotate CCW, Stop, Rotate CW, Backward buttons supporting both pointer and keyboard.
- **Keyboard Mapping** — Arrow keys and L/R for directional control, dispatches Stop on key-up or window blur.
- **Command Coalescing** — `dispatchCommand()` with `cmdInFlight` flag, `pendingCmd` queue, and 1500ms `AbortController` timeout prevents request pile-up.

**Telemetry Dashboard:**
- Live MJPEG video feed with dynamic resolution switching (720p/360p/240p/off).
- Motor 1 & Motor 2 current readings (Amps).
- CPU usage percentage.
- Wi-Fi signal strength (5-bar meter with dBm).
- ESP32 link health indicator and trip alert banners.

**Safety Features:**
- **Heartbeat** — Fires every 750ms to feed the server-side watchdog.
- **`beforeunload` Beacon** — Uses `navigator.sendBeacon` to guarantee a Stop command if the tab closes.
- **Follow Lock** — Disables directional controls (but keeps Stop) when face-following is active.

**Styled by:** [`style.css`](file:///Robot_AI_Complete/static/style.css) — Dark theme, responsive layout.

---

### Kiosk Mode Interface (Ruby AI Assistant)

**Files:**
- [`index.html`](file:///Robot_AI_Complete/static/index.html) — SPA shell with layered views
- [`app.js`](file:///Robot_AI_Complete/static/app.js) — Core AI assistant logic
- [`kiosk.js`](file:///Robot_AI_Complete/static/kiosk.js) — Kiosk shell management
- [`style.css`](file:///Robot_AI_Complete/static/style.css) — Assistant face styles
- [`kiosk.css`](file:///Robot_AI_Complete/static/kiosk.css) — Kiosk launcher and mini-app styles

A full-screen, touch-optimized interface for the robot's mounted tablet display.

**Ruby's Expressive SVG Face:**
- Inline vector graphic with layered facial features (eyes, mouth, blush circles, sparkle stars).
- CSS keyframe animations: `blink`, `breathe`, `eyeSway` (thinking), `eyeBounce` / `smileBounce` (happy), `blushPulse` / `sparkleTwinkle` (blush), `errorFill` (error).
- Attribute-driven state machine: `data-state` (`idle`, `listening`, `thinking`, `speaking`, `error`) and `data-expression` (`neutral`, `happy`, `elated`, `blush`).

**`app.js` — Core Logic:**
- **Audio-Reactive Lip-Sync** — Web Audio API `AnalyserNode` samples frequency bands and applies non-linear `scaleY` transforms to SVG mouth paths in real-time.
- **Voice Activity Detection (VAD)** — Computes RMS time-domain audio energy to detect speech start and automatically finalize recording after 1200ms of trailing silence or a 20s safety limit.
- **Chat Pipeline** — `sendMessage()` posts conversation history (capped at 20 turns) to `/api/chat`, updates captions and expression, initiates TTS.
- **Token Optimization** — Distinguishes dynamic Gemini replies from pre-cached canned phrases (`speakPhrase()`) to save API costs.
- **Follow-Me Management** — Configures body vs. head tracking modes, manages camera preview, polls `/robot/follow_status` every 1.5s.

**`kiosk.js` — Shell Management:**
- **View Navigation** — `showAssistant()`, `showKiosk()`, `enterAppView()` with custom lifecycle events (`kiosk:enter`, `kiosk:exit`).
- **App Registry** — `APP_GROUPS` array organizing apps into categories (ECE, Engineering, Campus, General, Tools).
- **Extension Detection** — Checks `dataset.kioskExt === "1"` to determine if iframe header-stripping extension is loaded. Shows `showBlockedCard()` for unframeable sites when missing.
- **Draggable Exit Pill** — Full pointer capture with drag/tap distinction (8px threshold), iframe event shielding, edge-snapping, and localStorage position persistence.
- **Privacy & Idle Timeout** — 5-minute inactivity timer → 30-second countdown → `wipeBrowsingData()` + return to assistant.

---

### Mini-Apps Framework

**File:** [`miniapps.js`](file:///Robot_AI_Complete/static/miniapps.js)

Self-contained touch applications registered under `window.KioskMiniApps`, each implementing `{ mount(root), unmount() }`:

| App | Description |
|-----|-------------|
| **Clock** | Live time/date, countdown timer with presets (+1m/5m/10m), 100ms stopwatch, multi-alarm scheduler |
| **Weather** | Open-Meteo API — current conditions, 12-hour hourly strip, 4-day daily forecast |
| **Radio** | SomaFM / Radio Paradise / Classic FM internet streams (persists across app switches) |
| **Notes** | Text scratchpad + multi-color touch drawing canvas, debounced auto-save to `/api/notes` |
| **Transit** | Live TTC bus/streetcar arrival predictions for U of T campus stops (polled every 30s) |
| **Buildings** | Searchable offline directory of U of T building codes, names, addresses, departments |
| **Emergency** | Offline directory — 911, Campus Safety, Health & Wellness, 24/7 crisis hotlines |
| **Remote** | Toggle LAN/Tailscale remote driving, display connection URLs, live motor/CPU telemetry |
| **System** | PipeWire volume, screen dimming, hardware telemetry (CPU temp, uptime, memory, disk), two-tap confirmed reboot/shutdown |

---

### Kiosk Chrome Extension

**Directory:** [`kiosk-extension/`](file:///Robot_AI_Complete/kiosk-extension)

Chrome Manifest V3 extension ("Ruby Kiosk Frame Unlocker") enabling third-party websites to render in the kiosk iframe and providing privacy wiping:

| File | Purpose |
|------|---------|
| [`manifest.json`](file:///Robot_AI_Complete/kiosk-extension/manifest.json) | Declares permissions (`declarativeNetRequest`, `browsingData`, `<all_urls>`) and script injection |
| [`rules.json`](file:///Robot_AI_Complete/kiosk-extension/rules.json) | Strips `X-Frame-Options`, `Content-Security-Policy`, and `frame-options` from subframe responses |
| [`background.js`](file:///Robot_AI_Complete/kiosk-extension/background.js) | Privacy wipe via `chrome.browsingData.remove()`, preserving kiosk origin data while clearing external cookies/cache/history |
| [`flag.js`](file:///Robot_AI_Complete/kiosk-extension/flag.js) | Sets `dataset.kioskExt = "1"` for extension detection; origin-validated message relay between page and background worker |
| [`activity.js`](file:///Robot_AI_Complete/kiosk-extension/activity.js) | Injected into all frames — throttled (2s) heartbeat beacon (`postMessage("kiosk-activity")`) for idle timer across cross-origin iframes |

---

## Data Flow Diagrams

### Manual Drive Command Flow

```
User presses Forward in browser
        │
        ▼
JavaScript dispatchCommand() → POST /robot/command {"cmd":"F"}
  (coalesced: max 1 in-flight + 1 pending)
        │
        ▼
robot_api.py validates → hardware.link.send_command("F")
        │
        ▼
MotorLink validates against motion.VALID_WIRE_COMMANDS
  Writes "F\n" over USB Serial under threading.RLock
        │
        ├──▶ _resend_loop() re-sends "F\n" every 150ms (keep-alive)
        │
        ▼
ESP32 checkSerialCommand() reads 'F'
  Sets targetDuty=75, motor1(75,0), motor2(75,0)
  Resets watchdog timer, reports STATUS:FWD
        │
        ▼
PWM signals drive BTS7960 H-bridges → motors spin
        │
        ▼ (every 20ms)
ESP32 reads current ADC, checkProtection()
        │
        ▼ (every 200ms)
ESP32 sends "CUR:3.45,3.62\n" to Serial
        │
        ▼
MotorLink._reader_loop() parses → get_motor_currents()
```

### AI Chat Flow (Kiosk)

```
Visitor speaks into microphone
        │
        ▼
Browser MediaRecorder captures Opus audio
  monitorSilence() detects 1200ms trailing silence → auto-stops
        │
        ▼
app.js POST /api/transcribe (FormData with audio blob)
        │
        ▼
kiosk_api.py → Gemini API transcription → text
        │
        ▼
app.js POST /api/chat {history: [...20 turns]}
        │
        ▼
kiosk_api.py → Gemini API (with Ruby persona system prompt)
  detect_expression() → vaderSentiment → "happy" / "blush" / etc.
        │
        ▼
{reply: "...", emotion: "happy"} returned to browser
        │
        ├──▶ Chat bubble rendered, caption updated
        ├──▶ SVG face: data-expression="happy" → eyeBounce animation
        ├──▶ POST /api/tts {text: "..."} → WAV audio → playback
        └──▶ Web Audio AnalyserNode → animateMouth() lip-sync
```

### Face Following Flow

```
FaceFollower._follow_loop_body() runs at 10Hz
        │
        ▼
camera.get_frame() → raw BGR numpy array
        │
        ▼
YuNet DNN face detection (640px, score > 0.6, NMS 0.3)
        │
        ├── No face → coast 0.5s in last direction → stop
        │
        └── Face detected:
              │
              ├── Calculate offset_frac (face center vs frame center)
              │
              ├── [Body Mode]
              │     │
              │     ├── _TurnController evaluates:
              │     │     projected = offset + rate × 0.28s (lead release)
              │     │     adaptive deadzone based on measured sweep rate
              │     │     → turn command or release
              │     │
              │     ├── _update_distance_state (hysteresis):
              │     │     face height ≥ 50% → back up
              │     │     face height ≤ 28% → drive forward
              │     │     → forward/backward/hold
              │     │
              │     └── motion.command_for(intent, slow=True) → "f"/"c"/"x"
              │           motor_link.send_command() → ESP32
              │
              └── [Head Mode]
                    correction = offset × 70° × 0.5
                    motor_link.set_head_angle(deg) → "H090" → ESP32
                    → servo slews at max 40°/sec
```

---

## Configuration & Environment

Configuration is managed through environment variables (see [`.env.example`](file:///Robot_AI_Complete/.env.example)):

**AI & Speech:**
| Variable | Description |
|----------|-------------|
| `GOOGLE_AI_API_KEY` | Gemini API key |
| `LLM_MODEL` | Gemini model name |
| `TTS_MODEL` / `TTS_VOICE` / `TTS_VOICE_ID` | TTS engine configuration |
| `TRANSCRIBE_MODEL` | Speech-to-text model |

**Motion & Driving:**
| Variable | Description |
|----------|-------------|
| `MOTION_PROFILE` | `mirrored` (default) or `direct` |
| `MOTION_INVERT_DRIVE` / `MOTION_INVERT_TURN` | Direction fix flags |

**Face Tracking:**
| Variable | Description |
|----------|-------------|
| `FOLLOW_CENTER_DEADZONE` | Center deadzone width |
| `FOLLOW_TURN_DEADZONE_MAX` | Maximum adaptive deadzone |
| `FOLLOW_TURN_LEAD_S` | Lead release time (0.28s default) |
| `FOLLOW_CLOSE_ENTER` / `FOLLOW_CLOSE_EXIT` | Distance hysteresis thresholds |
| `FOLLOW_FAR_ENTER` / `FOLLOW_FAR_EXIT` | Distance hysteresis thresholds |
| `FOLLOW_LOST_COAST_S` | Coast time after losing face (0.5s) |
| `FOLLOW_DETECT_WIDTH` / `FOLLOW_SCORE_THRESHOLD` | YuNet parameters |

**Head Servo:**
| Variable | Description |
|----------|-------------|
| `HEAD_CENTER_DEG` / `HEAD_MIN_DEG` / `HEAD_MAX_DEG` | Servo angle limits |
| `HEAD_CAMERA_FOV_DEG` / `HEAD_GAIN` / `HEAD_DEADZONE` | Control parameters |

**Hardware:**
| Variable | Description |
|----------|-------------|
| `CAMERA_DEVICE` / `CAMERA_WIDTH` / `CAMERA_HEIGHT` / `CAMERA_FPS` | Camera configuration |
| `CAMERA_STREAM_RESOLUTION` | Default streaming quality |
| `WIFI_INTERFACE` | Network interface for signal monitoring |

---

## Dependencies

**Python** ([`requirements.txt`](file:///Robot_AI_Complete/requirements.txt)):

| Package | Purpose |
|---------|---------|
| `Flask` | Web framework |
| `gunicorn` | Production WSGI server |
| `pyserial` | USB Serial communication with ESP32 |
| `opencv-python-headless` | Camera capture, YuNet face detection |
| `requests` | HTTP client |
| `python-dotenv` | Environment variable loading |
| `piper-tts` | Offline TTS synthesis (ONNX models) |
| `vaderSentiment` | Sentiment analysis for facial expressions |

**ML Models** (downloaded by [`models/download.sh`](file:///Robot_AI_Complete/models/download.sh)):
- `face_detection_yunet_2023mar.onnx` — OpenCV Zoo face detector
- `en_US-hfc_female-medium.onnx` — Piper TTS voice
- `en_US-lessac-medium.onnx` — Piper TTS voice
- `en_GB-alan-medium.onnx` — Piper TTS voice

**ESP32 Firmware:**
- ESP32 Arduino Core (HAL functions only — no external servo/motor libraries)
