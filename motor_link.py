"""
motor_link.py

Owns the USB-serial connection to the ESP32. Extracted back out of app.py
for the merged package: app.py is now the wiring for two separate web
surfaces (the Ruby kiosk and the robot's remote page), and burying the
hardware link inside it would leave the one genuinely safety-critical class
in the repo sharing a file with TTC arrival parsing.

Nothing about the class itself changed in the move except that the set of
valid characters now comes from motion.py, which is also where every caller
decides *which* character to send (see that module's header for why that
indirection exists).
"""

import glob
import threading
import time

import serial

import motion

BAUD_RATE = 115200
RESEND_INTERVAL_S = 0.15    # must stay well under the ESP32's CMD_TIMEOUT_MS (500ms)
RECONNECT_COOLDOWN_S = 1.5  # see MotorLink._reconnect


def find_esp32_port():
    """Best-effort auto-detect of the ESP32's serial port on Linux. Prefers
    udev's stable /dev/serial/by-id/ symlink when one exists - same reason
    as camera.py's _candidate_devices(): immune to /dev/ttyUSB* renumbering
    across a reconnect, so a MotorLink reconnect (see _reconnect) finds the
    right port on the first try instead of however the kernel happened to
    number things this time."""
    by_id = sorted(glob.glob("/dev/serial/by-id/*"))
    if by_id:
        return by_id[0]
    candidates = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    if not candidates:
        raise RuntimeError(
            "No /dev/ttyUSB* or /dev/ttyACM* device found. "
            "Is the ESP32 plugged in? Try `ls /dev/tty*` to check."
        )
    return candidates[0]


class NullMotorLink:
    """
    Stand-in for MotorLink when the ESP32 isn't connected, so the rest of the
    system still comes up.

    This matters more after the merge than it would have before. Opening the
    serial port is an import-time side effect, and it used to be fine for it
    to be fatal: the only thing in the process was the robot server, which
    has nothing to do without motors. Now the same process is also the
    kiosk's assistant, and "the ESP32 is unplugged" should not be the reason
    Ruby won't answer questions or show the campus map.

    Deliberately not a silent no-op that swallows commands: `available` is
    False, and every caller that could actually move the robot checks it and
    reports the reason to the UI rather than showing controls that look live
    and do nothing.
    """

    available = False

    def __init__(self, reason):
        self.reason = reason
        self.port_name = None

    def send_command(self, cmd):
        pass

    @property
    def current_command(self):
        return "S"

    def stop(self):
        pass

    def close(self):
        pass


class MotorLink:
    """
    Owns the USB-serial connection to the ESP32 and exposes ONE function,
    send_command(), that anything on the Pi can call to drive the robot -
    the remote page's D-pad, the face-follow tracker, or an MQTT subscriber
    later. Centralizing this here means adding new command sources never
    requires touching the ESP32 firmware or this class's internals.

    See NullMotorLink above for what stands in when there's no ESP32 to open.

    Why the background resend thread exists:
    The ESP32 has a command watchdog - if it doesn't see a fresh command
    within ~500ms it force-stops the motors (protects against this process
    crashing, USB unplugging, etc). So while a command is "held" (e.g. a
    joystick button is down), this class keeps resending it underneath so
    the ESP32 never sees a gap, without every caller needing to know that.
    """

    available = True

    def __init__(self, port=None, baud=BAUD_RATE, on_trip=None):
        self.port_name = port or find_esp32_port()
        self.baud = baud
        self.ser = serial.Serial(self.port_name, baud, timeout=0.2)
        time.sleep(2.0)  # ESP32 resets on USB serial open; let it boot

        # RLock, not Lock: _write()'s reconnect path (see _reconnect) takes
        # this same lock, but _write() runs both under send_command()'s lock
        # (already held by the calling thread) and unlocked from
        # _resend_loop() - a plain Lock would deadlock the first case.
        self._lock = threading.RLock()
        self._current_cmd = "S"
        self._running = True
        # Guards against a wasted, fully-redundant second 5-attempt/~5s
        # reconnect round: _reader_loop's readline() and a concurrent
        # _write() (from a /command request) can both hit SerialException
        # around the same moment and each call _reconnect() independently -
        # the RLock only serializes them, it doesn't tell the second caller
        # "someone already just tried this and failed a moment ago", so
        # without this they stack into back-to-back full rounds and roughly
        # double the real outage. See _reconnect() below.
        self._last_reconnect_finish_time = 0.0
        self._last_reconnect_succeeded = False
        # Called from _reader_loop whenever the ESP32 reports a current-
        # protection trip (see checkProtection() in the .ino). Lets a
        # control source that's actively driving - currently only
        # face_follow's FaceFollower - react by switching itself off,
        # without MotorLink needing to know that source exists. Tripping
        # logic itself lives entirely on the ESP32 and is untouched here.
        self._on_trip = on_trip

        self._resend_thread = threading.Thread(target=self._resend_loop, daemon=True)
        self._resend_thread.start()

        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

        print(f"[motor_link] Connected to ESP32 on {self.port_name}")

    def send_command(self, cmd: str):
        """
        The one function every control source should call. `cmd` is a wire
        character - get it from motion.command_for(), don't spell it out at
        the call site.
        """
        # strip() but deliberately NOT upper(): case is significant on this
        # wire now. Lowercase means the firmware's slower FOLLOW_DUTY (see
        # motion.command_for(slow=True)), so upper-casing here would quietly
        # promote every follow-mode command to full driving speed and the
        # slow path would look like it simply didn't work. Validation below
        # is unchanged in strictness - VALID_WIRE_COMMANDS lists both cases,
        # and anything outside it still raises rather than being coerced.
        cmd = cmd.strip()
        if cmd not in motion.VALID_WIRE_COMMANDS:
            raise ValueError(
                f"Unknown command {cmd!r}, expected one of {sorted(motion.VALID_WIRE_COMMANDS)}"
            )

        with self._lock:
            self._current_cmd = cmd
            self._write(cmd)

    @property
    def current_command(self):
        """The command currently being held/resent - lets an external
        watchdog (see hardware._connection_watchdog_loop) check whether the
        robot is actually driving without reaching into _current_cmd
        directly, keeping this class's internals private to it."""
        with self._lock:
            return self._current_cmd

    def stop(self):
        self.send_command(motion.command_for(motion.STOP))

    def close(self):
        self.stop()
        self._running = False
        time.sleep(0.05)
        self.ser.close()

    # ---------------- internal ----------------

    def _write(self, cmd: str):
        try:
            self.ser.write(cmd.encode("ascii"))
        except serial.SerialException:
            # The ESP32's serial device node can vanish and come back under
            # a new /dev/ttyUSB* number - confirmed on this hardware as a
            # whole-board USB brownout (simultaneous over-current on every
            # port, re-enumerating the camera, this link, and the WiFi
            # adapter all at once; WiFi survives it because NetworkManager
            # actively recovers it, nothing did that for this link before).
            # Re-probe and retry this one write; if that also fails, let it
            # raise so this request reports the error instead of hanging.
            print("[motor_link] Write failed, attempting reconnect...")
            if self._reconnect():
                self.ser.write(cmd.encode("ascii"))
            else:
                raise

    def _reconnect(self):
        """Re-probe for the ESP32 and reopen the serial connection. Retries
        a few times with a short pause since the new device node can take a
        moment to appear after the old one disappears (see _write).

        _reader_loop and a concurrent _write() (from a /command request)
        can each hit SerialException around the same moment and both call
        this - the RLock means they never literally overlap, but without
        the cooldown check below, the second caller would still burn a
        full fresh 5-attempt/~5s round immediately after the first one just
        finished, roughly doubling the real outage for no benefit (the
        device's state can't have changed in the few milliseconds between
        them). Reusing the just-finished outcome instead - success or
        failure - fixes that without weakening retry persistence: a
        genuinely persistent outage still gets retried, just paced by the
        cooldown instead of immediately stacked back-to-back."""
        with self._lock:
            if time.time() - self._last_reconnect_finish_time < RECONNECT_COOLDOWN_S:
                return self._last_reconnect_succeeded

            try:
                self.ser.close()
            except Exception:
                pass
            for attempt in range(5):
                try:
                    new_port = find_esp32_port()
                    self.ser = serial.Serial(new_port, self.baud, timeout=0.2)
                    time.sleep(2.0)  # ESP32 resets on open; let it boot
                    self.port_name = new_port
                    print(f"[motor_link] Reconnected to ESP32 on {self.port_name}")
                    self._last_reconnect_finish_time = time.time()
                    self._last_reconnect_succeeded = True
                    return True
                except Exception as e:
                    print(f"[motor_link] Reconnect attempt {attempt + 1}/5 failed: {e}")
                    time.sleep(1.0)
            self._last_reconnect_finish_time = time.time()
            self._last_reconnect_succeeded = False
            return False

    def _resend_loop(self):
        """Keeps re-sending the current command so the ESP32's watchdog
        never trips while a command is being held (e.g. joystick button down)."""
        stop_cmd = motion.command_for(motion.STOP)
        while self._running:
            time.sleep(RESEND_INTERVAL_S)
            with self._lock:
                cmd = self._current_cmd
            if cmd != stop_cmd:   # no need to spam stop commands
                try:
                    self._write(cmd)
                except serial.SerialException:
                    pass  # _write already tried to reconnect and failed; next tick retries

    def _reader_loop(self):
        """Prints STATUS:/LOG: lines coming back from the ESP32. Useful for
        debugging; swap the print() for real logging/telemetry storage later.
        Reconnects on a dropped connection instead of exiting - previously
        this broke out of the loop permanently on the first disconnect,
        which meant a single USB brownout silently killed trip detection
        for the rest of the process's life even after the ESP32 came back."""
        while self._running:
            try:
                line = self.ser.readline().decode("utf-8", errors="ignore").strip()
            except serial.SerialException:
                if not self._reconnect():
                    time.sleep(1.0)
                continue
            if line:
                print(f"[esp32] {line}")
                # "LOG: INSTANT TRIP..." / "LOG: SUSTAINED TRIP..." - the
                # one-shot edge event for a trip (unlike the STATUS: line,
                # which keeps reporting "TRIPPED" every 200ms while latched).
                if self._on_trip is not None and line.startswith("LOG:") and "TRIP" in line:
                    self._on_trip()
