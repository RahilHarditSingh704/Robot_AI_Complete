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

# How old the last CUR: reading may be before get_motor_currents() calls it
# unavailable rather than returning it. The firmware sends one every 200ms, so
# this is ten missed reports - long enough that a momentary hiccup doesn't
# blank the readout, short enough that a frozen number is never mistaken for a
# live one. Also what makes an ESP32 running older firmware (which sends no
# CUR: lines at all) report "no reading" instead of nothing at all.
MOTOR_CURRENT_STALE_S = 2.0

# EN held low for this long, then this long to boot, in _reset_esp32().
# The pulse only has to outlast the reset circuit's RC; esptool uses 100ms and
# 50ms for the same job. The boot wait is longer than the firmware needs
# (setup() takes 64 ADC samples and prints three lines) because it also covers
# the ROM bootloader's own startup before the application runs at all.
RESET_PULSE_S = 0.15
RESET_BOOT_S = 1.0

# Minimum gap between revive attempts (see MotorLink._revive_if_silent). Long
# enough that a brownout has time to finish and the rail to settle - the one
# observed here took ~2s from "Undervoltage detected" to "Voltage normalised" -
# and that a genuinely absent ESP32 is retried at a sane pace rather than being
# held in reset by a pulse every few hundred milliseconds.
REVIVE_INTERVAL_S = 5.0


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

    def set_head_angle(self, degrees):
        pass

    def get_motor_currents(self):
        return {"available": False, "m1": None, "m2": None}

    def link_health(self):
        # Not "stale": there is no ESP32 to have gone quiet, and the UI
        # already says so via `available` on every robot endpoint. A fault
        # banner on top of that would be reporting the same absence twice.
        return "unknown"

    def trip_state(self):
        return None

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

        # Latest "CUR:<m1>,<m2>" reading from the ESP32, as
        # (m1_amps, m2_amps, monotonic_timestamp) - or None until the first
        # one lands. Must be set before _reader_loop starts, since that thread
        # is what writes it.
        #
        # Deliberately not guarded by self._lock, unlike everything else here.
        # It's written at 5Hz by the reader thread and read by request threads,
        # and both sides touch it exactly once as a whole tuple - a single
        # atomic attribute store and load, so a reader can never see a half-
        # updated value or a timestamp from a different reading than its amps.
        # Taking the lock would be correct but would also make a 5Hz display
        # readout contend with the serial writes that actually move the robot,
        # and would block this thread for the several seconds _reconnect()
        # holds that lock.
        self._motor_amps = None
        # Rate limit for _revive_if_silent(). Only the reader thread touches it.
        self._last_revive_time = 0.0
        # Latest STATUS: text from the ESP32 ("FWD", "STOP", "TRIPPED (M1
        # sustained)"...). Same single-atomic-assignment discipline as
        # _motor_amps above: written by the reader thread, read by request
        # threads, never mutated in place.
        self._esp32_status = None

        # Opening the port is not enough to know what state the ESP32 is in -
        # see _reset_esp32(). Do it before the reader thread starts, so the
        # boot banner is the first thing that thread sees.
        self._reset_esp32()

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

    def set_head_angle(self, degrees):
        """Aim the head servo (which carries the camera) at an absolute angle.

        Sent as "H" plus exactly three digits, which is what the firmware's
        parser expects - fixed width, so it needs no terminator.

        Deliberately NOT resent like send_command()'s motor commands. A servo
        holds its last position by itself and the firmware slews toward the
        target on its own clock, so repeating this would be pure serial noise
        at the detection rate. The corollary is that if the ESP32 resets, the
        head re-centres and the target is lost - which is fine, because the
        only thing that sets it is the follow loop, and that re-commands it
        on its very next frame.
        """
        deg = int(round(max(0.0, min(180.0, float(degrees)))))
        with self._lock:
            self._write(f"H{deg:03d}")

    def get_motor_currents(self):
        """Latest measured current through each motor, in amps.

        The measurement is the ESP32's - it owns the sense pins and already
        samples them for its current protection (see checkProtection() in the
        .ino), so this is a readout of a number that exists whether or not
        anything displays it, not a second measurement path.

        Returns `available: False` rather than a stale reading if none has
        arrived recently - see MOTOR_CURRENT_STALE_S. That covers an ESP32
        that has stopped talking, and equally one running firmware old enough
        not to send CUR: lines at all, so the UIs show "no reading" instead of
        a number frozen at whatever it last was.
        """
        reading = self._motor_amps      # single atomic read - see __init__
        if reading is None or self.link_health() != "ok":
            return {"available": False, "m1": None, "m2": None}
        m1, m2, _ = reading
        return {"available": True, "m1": m1, "m2": m2}

    def trip_state(self):
        """The ESP32's protection trip, as its own description, or None.

        Current protection is entirely the firmware's - it samples the sense
        pins every 20ms and latches on its own, for every command source
        equally (see checkProtection() in the .ino). But the only Pi-side
        reaction was hardware._handle_trip(), which stops the follower. That
        is the whole story when Follow me is driving and *nothing at all* when
        somebody is driving from the remote page: the robot stops dead, the
        ESP32 quietly ignores resends of the command that tripped it, and
        /robot/command carries on answering 200 to a D-pad that no longer
        moves anything. Reading it back off the STATUS: line is what lets the
        page say so.

        Cleared by the ESP32 itself: any genuinely different command clears
        the latch and the next STATUS: line reports the new mode.
        """
        status = self._esp32_status     # single atomic read
        if status and status.startswith("TRIPPED"):
            return status
        return None

    def link_health(self):
        """Whether the ESP32 is actually answering, as "ok"/"stale"/"unknown".

        This exists because "the serial port is open" turned out to be no
        evidence at all that anything is on the other end of it - writes into
        a reopened-but-dead port succeed silently, so /robot/command happily
        returned 200 for every one of 89 commands while the robot sat still
        (see _reset_esp32() for the full story). The continuous CUR: telemetry
        is the first thing on this link that makes the difference observable,
        so the check is simply whether it is still arriving.

        The three-way answer matters. "stale" means we have seen this ESP32
        talking and it has now stopped - something is definitely wrong and it
        is worth interrupting somebody about. "unknown" means we have never
        heard from it, which is also what an ESP32 running firmware older than
        the CUR: line looks like; treating that as a fault would put a
        permanent red banner on a rig that is working fine.
        """
        reading = self._motor_amps      # single atomic read - see __init__
        if reading is None:
            return "unknown"
        return "ok" if time.monotonic() - reading[2] <= MOTOR_CURRENT_STALE_S else "stale"

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

    def _reset_esp32(self):
        """Pulse the ESP32's EN line, so it is running its firmware from a
        known state rather than whatever state it happened to be left in.

        **Opening the serial port does not do this**, despite what the code
        here used to assume ("ESP32 resets on USB serial open; let it boot").
        On this board it demonstrably does not: pyserial asserts both DTR and
        RTS on open, and the devkit's two-transistor auto-reset circuit only
        pulls EN or IO0 when the two lines *differ* - both asserted cancels
        out and the chip is left untouched. Confirmed by watching the link: a
        plain open produces no boot banner at all.

        That mattered the day the keyboard was unplugged. The USB bus
        re-enumerated, the CP2102 came back as a new device, _reconnect()
        reopened it and logged "Reconnected to ESP32" - and the link was dead
        in both directions. No telemetry came back and no command got through,
        but every write succeeded (bytes into an open port with nobody
        listening never error), so /robot/command answered 200 to 89
        consecutive commands while the robot sat still. Pulsing EN here is
        what turns "the port opened" into "the firmware is running".

        The sequence is esptool's classic reset minus the bootloader entry:
        RTS asserted pulls EN low (chip held in reset), releasing it lets the
        chip boot, and DTR stays de-asserted throughout so IO0 stays high and
        it boots the application rather than the download stub.

        Safe to do at any time: the firmware's setup() calls stopAll() before
        anything else, so a reset parks the motors. That is also the right
        outcome - this only ever runs when the link is being (re)established,
        and a robot whose control link just failed should not be driving.
        """
        try:
            self.ser.dtr = False    # IO0 high - boot the application
            self.ser.rts = True     # EN low  - hold in reset
            time.sleep(RESET_PULSE_S)
            self.ser.rts = False    # EN high - boot
        except (OSError, serial.SerialException) as e:
            # Not fatal on its own: the caller has an open port and may still
            # get a working link out of it. Worth saying out loud, though,
            # because if it didn't work the ESP32's state is unknown.
            print(f"[motor_link] Could not pulse ESP32 reset: {e}")
            return

        # The chip is rebooting, so whatever it last reported - including a
        # latched trip - is no longer true of the thing now running.
        self._esp32_status = None

        try:
            # Flushed here, between releasing EN and waiting for the boot,
            # rather than after it. Anything queued at this instant is from
            # before the reset - stale telemetry, or the garbage a wedged
            # CP2102 spews - and none of it describes the firmware now
            # starting. Flushing after the wait instead would also swallow the
            # boot banner, which is the one place the firmware states its trip
            # points and sense-zero calibration, and the clearest proof of
            # life there is.
            self.ser.reset_input_buffer()
        except (OSError, serial.SerialException):
            pass

        time.sleep(RESET_BOOT_S)

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
                    # The step this used to be missing, and the reason a
                    # "Reconnected" line could be followed by a completely
                    # dead link: reopening the port says nothing about what
                    # the chip on the far end is doing. See _reset_esp32().
                    self._reset_esp32()
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
                # Current readings arrive 5 times a second forever, so they're
                # taken before the print below rather than after it - logging
                # them would bury every STATUS:/LOG: line worth seeing under
                # 300 lines a minute of telemetry.
                if line.startswith("CUR:"):
                    self._record_currents(line)
                    continue
                print(f"[esp32] {line}")
                if line.startswith("STATUS:"):
                    self._esp32_status = line[len("STATUS:"):].strip()
                # "LOG: INSTANT TRIP..." / "LOG: SUSTAINED TRIP..." - the
                # one-shot edge event for a trip (unlike the STATUS: line,
                # which keeps reporting "TRIPPED" every 200ms while latched).
                if self._on_trip is not None and line.startswith("LOG:") and "TRIP" in line:
                    self._on_trip()

            self._revive_if_silent()

    def _revive_if_silent(self):
        """Pulse EN if the port is open but the ESP32 has stopped answering.

        This is the self-heal for a USB brownout, which on this rig is not a
        rare event: the Pi 5 is on a 5V/3A supply with usb_max_current_enable
        off, so the whole USB budget is 600mA against ~800mA of declared draw
        (touchscreen 400, keyboard 100, hub 100, camera 100, this link 100 -
        and the camera's descriptor understates it badly). When it trips, all
        six ports drop at once and the kernel logs "Undervoltage detected!".

        The port comes back on its own - udev recreates the device node and
        _reconnect() reopens it - but the ESP32 on the far end does not
        necessarily come back running, and reopening a port says nothing
        about the chip behind it (see _reset_esp32). The one time this
        happened after reconnect handling was added, the rail was back to
        normal two seconds later, so a retry a few seconds on would have
        recovered it with nobody noticing. Instead it stayed dead until the
        server was restarted by hand.

        Deliberately lives in the reader thread rather than in _reconnect().
        Verifying a link means watching for telemetry, this thread is the only
        one that reads the port, and a _reconnect() that tried to read for
        itself would be racing this loop for the same bytes - or waiting on
        telemetry this loop is blocked from collecting, since it would be
        sitting on the lock _reconnect() holds. Here there is no race at all:
        one thread, and _reset_esp32() only touches modem control lines.

        Only ever runs once telemetry has been seen at least once, so an ESP32
        running firmware older than the CUR: line is never reset in a loop for
        the crime of being quiet.
        """
        if self._motor_amps is None or self.link_health() != "stale":
            return
        now = time.monotonic()
        if now - self._last_revive_time < REVIVE_INTERVAL_S:
            return
        self._last_revive_time = now
        print("[motor_link] ESP32 has gone silent - pulsing reset to revive it")
        # Under the lock so a concurrent send_command() can't be writing a
        # command into the middle of the reset pulse.
        with self._lock:
            self._reset_esp32()

    def _record_currents(self, line):
        """Parse one "CUR:<m1>,<m2>" line into self._motor_amps.

        A malformed line is dropped rather than raised on: this runs in the
        reader thread, and there is no framing on this link, so a line
        truncated by a reconnect or corrupted in transit is an ordinary event
        - not a reason to lose the thread that also detects protection trips.
        The next reading is 200ms away regardless.
        """
        try:
            m1_text, m2_text = line[len("CUR:"):].split(",", 1)
            m1, m2 = float(m1_text), float(m2_text)
        except ValueError:
            return
        self._motor_amps = (m1, m2, time.monotonic())
