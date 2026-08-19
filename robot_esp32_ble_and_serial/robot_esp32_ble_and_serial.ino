/*==========================================================================
// Dual BTS7960 robot: USB-Serial control + current protection (ESP32)
//
// Commands (single chars):
//   F forward   B backward   C rotate CW   X rotate CCW   S stop
//   f/b/c/x     the same four directions at FOLLOW_DUTY instead of DUTY,
//               for face-following, which needs to move gently and
//               continuously rather than at driving speed. No lowercase 's'.
//   Hnnn        aim the head servo (GPIO 23, carries the camera) at nnn
//               degrees, always three digits: "H090" is centre. The head
//               slews there smoothly rather than snapping - see updateServo.
//               Not a motor command: it does not feed the motor watchdog.
//   P           re-run the boot-time PWM pad report, for chasing a motor that
//               does not move - see reportPads(). Also not a motor command;
//               refused while a motor is running.
//
//   - Serial: from a Raspberry Pi over USB, e.g. Serial.write('F')
//     Feeds into pendingCmd -> handleCommand() logic.
//
// Reported back, in addition to the existing STATUS:/LOG: lines:
//   CUR:<m1>,<m2>   measured motor current in amps, 5Hz - see reportCurrent()
//
// Current protection is UNCHANGED from the original version:
//   - Instant trip ignored for first 300 ms after a command
//   - Sustained trip needs 12 samples over limit (~250 ms)
//   - Either trip stops BOTH motors
//
// NEW: Command watchdog. If the last command came in and the motors are
// still running but no new command has arrived within CMD_TIMEOUT_MS,
// motors are force-stopped. This protects against a Pi crash, USB
// disconnect, or app freeze leaving the robot driving blind. Whichever
// side is actively driving must keep resending its command (even if
// unchanged) faster than this timeout - e.g. every 150-200 ms.
//==========================================================================*/

// ---------------- Pins (from the current-sensing code) ----------------
const int SenseM1 = 34;   // M1 current sense
const int SenseM2 = 35;   // M2 current sense

const int M1_RPWM = 12;   // M1 forward
const int M1_LPWM = 13;   // M1 reverse
const int M2_RPWM = 18;   // M2 forward
const int M2_LPWM = 19;   // M2 reverse

// ---------------- PWM ----------------
const uint32_t PWM_FREQ = 20000;
const uint8_t  PWM_BITS = 8;
const int      DUTY     = 75;    // 0-255

// ---------------- Protection settings ----------------
const float SENSE_R    = 1000.0;
const float K_ILIS     = 8500.0;
const float ADC_FS_V   = 3.1;

// Raised from 10/20 because follow mode kept tripping as it started a turn.
// These now mean the same current at either duty (see ampsToCounts), so the
// old effective follow-mode limits of 13.6A/27.3A are the honest baseline to
// compare against, not the nominal 10/20.
//
// Startup current is the thing being given room here: a motor breaking a
// standing robot out of rest draws several times its running current until it
// is actually turning. The grace period covers the first 300ms of that and
// SUSTAIN_MS another 250ms, so anything still over SUSTAIN_AMPS at 540ms is
// not a startup any more - it is a motor that never got moving.
//
// Set by judgement, not measurement - the motors' stall current isn't
// documented anywhere in this project. The motor current readout is how to
// replace that judgement with a number: watch what a normal follow-mode turn
// actually peaks at and leave maybe 50% headroom above it.
const float SUSTAIN_AMPS = 16.0;
const float INSTANT_AMPS = 30.0;
const int   SUSTAIN_MS   = 250;
const int   GRACE_MS     = 300;
const int   SAMPLE_MS    = 20;

const int SUSTAIN_SAMPLES = SUSTAIN_MS / SAMPLE_MS;
const int CLEAR_SAMPLES   = 3;

// ---------------- Current reporting ----------------
// The protection code above turns an amp limit into an ADC threshold and then
// only ever compares counts. This reports the same measurement back to the Pi
// as an actual current, for display - it reads the same two samples
// checkProtection() gets, and changes nothing about when the motors trip.
//
// The BTS7960's IS pin sources a mirror of the load current, I_IS =
// I_load / K_ILIS, which SENSE_R turns into a voltage. So the whole
// conversion is just ampsToCounts() run backwards; see countsToAmps().
//
// Sent every CURRENT_REPORT_MS as one line, "CUR:<m1>,<m2>" in amps. Its own
// prefix rather than folding into the STATUS: line because that one is
// edge-triggered on a mode change and this is a continuous measurement - and
// because the Pi drops CUR: lines silently instead of logging them, which a
// 5Hz line has to be.
const int CURRENT_REPORT_MS = 200;

// Zero-current reading, captured once at boot with the motors provably off.
// Not assumed to be 0 counts: the ESP32's ADC has a non-zero floor at the
// bottom of its range, and the driver's IS pin has a small quiescent output
// of its own. Both are constant offsets, so measuring them once and
// subtracting is both simpler and more accurate than modelling either.
float zeroM1 = 0;
float zeroM2 = 0;
const int ZERO_SAMPLES = 64;

// Duty currently applied to the motors: DUTY, FOLLOW_DUTY, or 0 when stopped.
// Needed to interpret the sense reading at all - see countsToAmps().
int activeDuty = 0;

// Averaged over the report interval rather than reported raw. A single
// analogRead of a PWM-driven sense line is mostly noise; ten of them across
// 200ms is a number that can be read off a screen. Accumulated in amps, not
// counts, so that a command changing mid-window (which changes activeDuty)
// is handled correctly instead of blending two different scale factors.
float curSumM1 = 0;
float curSumM2 = 0;
int   curSamples = 0;
unsigned long lastCurrentReport = 0;

// ---------------- Command watchdog ----------------
const unsigned long CMD_TIMEOUT_MS = 500;   // stop if no fresh command within this window

// ---------------- Head servo ----------------
// An RDS51150 on GPIO 23, carrying the camera. Driven straight from LEDC at
// the standard 50Hz hobby-servo frame rather than pulling in a servo library,
// to match how the motor PWM is already set up (and to keep this sketch
// dependency-free - there are no libraries installed for this board).
//
// 16-bit resolution at 50Hz gives ~0.3us per step, far finer than any servo
// resolves. It is well within what LEDC can clock: at 50Hz the ceiling is
// about 20 bits.
const int      SERVO_PIN   = 23;
const uint32_t SERVO_FREQ  = 50;      // 20ms frame
const uint8_t  SERVO_BITS  = 16;
const int      SERVO_MIN_US = 500;    // pulse width at 0 deg
const int      SERVO_MAX_US = 2500;   // pulse width at 180 deg
const float    SERVO_CENTER_DEG = 90.0;

// The head slews here rather than jumping straight to whatever the Pi asked
// for, and that is the whole reason this lives in firmware. A servo commanded
// to a new angle slams to it at full speed; feeding it a fast sequence of
// small targets from the Pi would make it jerk once per detection frame. By
// holding the target and walking toward it at a fixed rate, the motion stays
// smooth no matter how often, how erratically, or how coarsely the Pi updates
// it - and it degrades gracefully if a frame is late.
//
// Raise for a snappier head, lower for a calmer one.
const float SERVO_DEG_PER_S = 40.0;
const int   SERVO_STEP_MS   = 20;     // matches the 50Hz frame; no point going finer

float servoTarget  = SERVO_CENTER_DEG;   // where the Pi wants the head
float servoCurrent = SERVO_CENTER_DEG;   // where it actually is right now
unsigned long lastServoStep = 0;

// ---------------- Slow speed (follow mode) ----------------
// Lowercase command characters ('f','b','c','x') mean "the same direction, at
// FOLLOW_DUTY instead of DUTY". Added because face-following needs a gentler
// speed than a human driving from the remote page: the protocol has no speed
// field, so the Pi's only way to soften a movement used to be to chop it into
// short bursts - which worked, but visibly juddered. A second duty lets the Pi
// hold a turn continuously and smoothly instead.
//
// Deliberately a separate character rather than a mode flag: 'c' and 'C' are
// simply different chars, so every existing mechanism keeps working untouched.
// lastAppliedCmd sees a genuine change when the speed changes (so stopAll()
// runs and the protection counters get a clean slate), keep-alive resends of
// either still collapse to a heartbeat, and the trip latch stays per-command.
//
// If follow mode stalls or crawls, raise this; if it still overshoots, lower
// it. It must stay below DUTY to mean anything.
//
// 55 -> 65: follow mode was slower than wanted, and 55/255 (21.6% duty) was
// close enough to the torque needed to break a standing robot out of rest
// that it could sit drawing near-stall current instead of accelerating away.
// More duty is counter-intuitively the gentler option on current: a motor
// that is actually turning develops back-EMF and draws far less than one
// straining against standstill.
const int FOLLOW_DUTY = 65;   // 0-255, against DUTY = 75 for the remote page

// ---------------- State ----------------
bool m1On = false;
bool m2On = false;
unsigned long commandTime = 0;      // used for the current-protection grace period
unsigned long lastCmdReceivedTime = 0; // used for the command watchdog
unsigned long lastSample  = 0;
const char* mode = "STOP";
const char* lastPrintedMode = "";

int m1OverCount = 0;
int m2OverCount = 0;
int m1UnderRun  = 0;
int m2UnderRun  = 0;

int sustainCounts;
int instantCounts;

// Command received over Serial, handled in loop(). volatile: written
// from the serial read, read from loop().
volatile char pendingCmd = 0;

// Mid-parse state for the "H" + three digits head-angle command.
bool readingAngle = false;
int  angleAccum   = 0;
int  angleDigits  = 0;

// Tracks the command currently driving the motors, so repeated/resent
// commands (e.g. the Pi's keep-alive resends) can be told apart from a
// genuinely NEW command. This matters for current protection: resetting
// stopAll()/commandTime on every resend would wipe out the protection
// counters before they ever had a chance to trip.
char lastAppliedCmd = 0;

// Set when current protection trips. While true, resends of the SAME
// command that caused the trip are ignored - only a genuinely different
// command (e.g. the user clicking Stop, or picking a new direction)
// clears the latch and re-arms the robot. This is what makes the trip
// actually "stick" instead of the Pi's keep-alive resends undoing it
// within 150ms.
bool tripped = false;
char trippedCmd = 0;


// ================= MOTOR CONTROL (LEDC) ================================
void motor1(int fwd, int rev) { ledcWrite(M1_RPWM, fwd); ledcWrite(M1_LPWM, rev); }
void motor2(int fwd, int rev) { ledcWrite(M2_RPWM, fwd); ledcWrite(M2_LPWM, rev); }

// ================= HEAD SERVO ==========================================
void applyServo(float deg) {
  if (deg < 0)   deg = 0;
  if (deg > 180) deg = 180;
  uint32_t us = SERVO_MIN_US + (uint32_t)((SERVO_MAX_US - SERVO_MIN_US) * (deg / 180.0f));
  // Duty is the fraction of the 20ms frame the pulse is high.
  uint32_t maxDuty = (1UL << SERVO_BITS) - 1;
  ledcWrite(SERVO_PIN, (uint32_t)(((uint64_t)us * maxDuty) / 20000UL));
}

// Walk servoCurrent toward servoTarget at SERVO_DEG_PER_S. Called every
// loop(); does nothing until SERVO_STEP_MS has elapsed.
void updateServo() {
  unsigned long now = millis();
  if (now - lastServoStep < (unsigned long)SERVO_STEP_MS) return;
  float dt = (now - lastServoStep) / 1000.0f;
  lastServoStep = now;

  if (servoCurrent == servoTarget) return;

  float maxStep = SERVO_DEG_PER_S * dt;
  float diff = servoTarget - servoCurrent;
  if (fabs(diff) <= maxStep) servoCurrent = servoTarget;
  else                       servoCurrent += (diff > 0 ? maxStep : -maxStep);
  applyServo(servoCurrent);
}


void stopAll() {
  motor1(0, 0);
  motor2(0, 0);
  m1On = false;
  m2On = false;
  // Every path that cuts the motors comes through here - a Stop, the
  // watchdog, a protection trip - so this is the one place that has to
  // record "nothing is being driven" for the current reporting.
  activeDuty = 0;
  m1OverCount = 0;
  m2OverCount = 0;
  m1UnderRun  = 0;
  m2UnderRun  = 0;

  // Forget what command was "already running." This matters because
  // stopAll() is also called directly from a watchdog timeout or a
  // protection trip (not just from handleCommand()) - without clearing
  // this, a resend of the same direction afterward would be mistaken
  // for a harmless keep-alive and silently fail to re-engage the motors.
  lastAppliedCmd = 0;
}


// ==================== CURRENT PROTECTION ====================
// No longer "UNCHANGED from the original version": see checkProtection() for
// the grace-period fix. The thresholds and the trip latch are untouched.
// Trip threshold in ADC counts for a given limit, at the duty actually being
// applied. `duty` used to be hardcoded to DUTY here, which quietly made the
// limits mean different currents in the two modes: the sense voltage scales
// with duty, so a threshold computed at DUTY=75 and compared against samples
// taken at FOLLOW_DUTY=55 only tripped at 10 * 75/55 = 13.6A.
//
// That mattered the moment both follow speed and the limits were tuned
// together, because the two changes pull opposite ways - raising FOLLOW_DUTY
// 55->65 on its own would have moved the follow-mode trip point DOWN from
// 13.6A to 11.5A, i.e. going faster would trip sooner. Passing the real duty
// in makes SUSTAIN_AMPS/INSTANT_AMPS mean the same current in both modes, so
// they can be set to a number that means something.
int ampsToCounts(float amps, int duty) {
  if (duty <= 0) return 4090;   // motors off - no threshold can be meaningful
  float volts = amps * (SENSE_R / K_ILIS) * (duty / 255.0);
  int counts = volts * 4095.0 / ADC_FS_V;
  if (counts > 4090) counts = 4090;
  return counts;
}

// ampsToCounts() backwards, for reporting rather than tripping. `counts` must
// already have the boot-time zero offset subtracted.
//
// The (255/duty) term is the part that isn't obvious. The IS pin only mirrors
// the load current while the high-side FET is conducting, so what the ADC
// integrates over a PWM period is the sense voltage scaled by the duty cycle -
// which is exactly the factor ampsToCounts() applies when it computes a trip
// threshold, and undoing it here is what makes the two agree. It also means
// the answer is the current flowing while the motor is actually driven, not
// the cycle average, which is the number that matters for a motor.
//
// Duty 0 means the bridge is off, so no current can be flowing through the
// sense path and there is nothing to scale - report zero rather than dividing
// by it.
float countsToAmps(float counts, int duty) {
  if (duty <= 0 || counts <= 0) return 0.0f;
  float volts = counts * ADC_FS_V / 4095.0f;
  return volts * (K_ILIS / SENSE_R) * (255.0f / duty);
}

// Averages the samples taken since the last report and sends one CUR: line.
// Called from loop() on its own interval, independent of the protection
// sampling that feeds it.
void reportCurrent() {
  if (curSamples <= 0) return;
  float a1 = curSumM1 / curSamples;
  float a2 = curSumM2 / curSamples;
  curSumM1 = 0;
  curSumM2 = 0;
  curSamples = 0;
  Serial.printf("CUR:%.2f,%.2f\n", a1, a2);
}

void checkProtection(int c1, int c2) {
  if (!m1On && !m2On) return;

  // GRACE_MS after a new command, nothing is judged at all. This used to
  // cover only the instant trip below, and the sustained counter further down
  // ran from the very first sample - which quietly defeated the whole point of
  // having a grace period, since inrush is exactly what the sustained path
  // then counted. The arithmetic made it certain rather than unlucky:
  // SUSTAIN_SAMPLES is SUSTAIN_MS/SAMPLE_MS = 250/20 = 12 samples = 240ms,
  // which is LESS than GRACE_MS, so a motor whose inrush outlasted ~220ms
  // tripped "sustained" before the grace period it was supposed to be
  // protected by had even expired.
  //
  // It went unnoticed while follow mode still pulsed its turns: bursts shorter
  // than 12 samples never let the counter fill. Holding a turn continuously
  // (see FOLLOW_DUTY) removed that accidental masking and follow mode started
  // tripping on essentially every turn it began.
  //
  // Earliest a sustained trip can now fire is GRACE_MS + SUSTAIN_MS = 540ms of
  // continuous overcurrent, which is a stall rather than a startup.
  if (millis() - commandTime < GRACE_MS) return;

  if (m1On && c1 >= instantCounts) {
    Serial.print("LOG: INSTANT TRIP - M1 counts="); Serial.println(c1);
    tripped = true; trippedCmd = lastAppliedCmd;
    stopAll(); mode = "TRIPPED (M1 instant)"; return;
  }
  if (m2On && c2 >= instantCounts) {
    Serial.print("LOG: INSTANT TRIP - M2 counts="); Serial.println(c2);
    tripped = true; trippedCmd = lastAppliedCmd;
    stopAll(); mode = "TRIPPED (M2 instant)"; return;
  }

  if (m1On) {
    if (c1 >= sustainCounts) {
      m1OverCount++;
      m1UnderRun = 0;
      if (m1OverCount >= SUSTAIN_SAMPLES) {
        Serial.print("LOG: SUSTAINED TRIP - M1 counts="); Serial.println(c1);
        tripped = true; trippedCmd = lastAppliedCmd;
        stopAll(); mode = "TRIPPED (M1 sustained)"; return;
      }
    } else {
      m1UnderRun++;
      if (m1UnderRun >= CLEAR_SAMPLES) {
        m1OverCount = 0;
        m1UnderRun = 0;
      }
    }
  }

  if (m2On) {
    if (c2 >= sustainCounts) {
      m2OverCount++;
      m2UnderRun = 0;
      if (m2OverCount >= SUSTAIN_SAMPLES) {
        Serial.print("LOG: SUSTAINED TRIP - M2 counts="); Serial.println(c2);
        tripped = true; trippedCmd = lastAppliedCmd;
        stopAll(); mode = "TRIPPED (M2 sustained)"; return;
      }
    } else {
      m2UnderRun++;
      if (m2UnderRun >= CLEAR_SAMPLES) {
        m2OverCount = 0;
        m2UnderRun = 0;
      }
    }
  }
}
// ================== END CURRENT PROTECTION ==============================


// ==================== COMMAND HANDLING (SERIAL) ==========================
// F/B move both motors together. C/X rotate by driving the motors opposite.
void handleCommand(char cmd) {
  // Trip latch: if we're currently tripped, refuse to re-engage on a
  // resend of the SAME command that caused the trip (that's just the
  // Pi's keep-alive, not a real user action). Any DIFFERENT command -
  // Stop, or a new direction - clears the latch and is processed normally.
  if (tripped) {
    if (cmd == trippedCmd) {
      lastCmdReceivedTime = millis();   // still "alive", just refuse to move
      return;
    }
    tripped = false;   // a genuinely different command clears the fault
  }

  // Keep-alive resend of the command already running: just prove to the
  // watchdog that we're still hearing from the controller. Do NOT touch
  // motors, stopAll(), or commandTime here - that would erase the
  // in-progress current-protection counters and grace period every time,
  // which is why sustained/instant trips previously never fired.
  if (cmd == lastAppliedCmd) {
    lastCmdReceivedTime = millis();
    return;
  }

  stopAll();   // clean slate; also resets protection counters - only on a REAL change

  switch (cmd) {
    case 'F': motor1(DUTY, 0); motor2(DUTY, 0); m1On = m2On = true; mode = "FWD";  break;
    case 'B': motor1(0, DUTY); motor2(0, DUTY); m1On = m2On = true; mode = "BACK"; break;
    case 'C': motor1(DUTY, 0); motor2(0, DUTY); m1On = m2On = true; mode = "CW";   break;
    case 'X': motor1(0, DUTY); motor2(DUTY, 0); m1On = m2On = true; mode = "CCW";  break;
    // Lowercase: same directions at FOLLOW_DUTY. Used by follow mode only.
    case 'f': motor1(FOLLOW_DUTY, 0); motor2(FOLLOW_DUTY, 0); m1On = m2On = true; mode = "FWD (slow)";  break;
    case 'b': motor1(0, FOLLOW_DUTY); motor2(0, FOLLOW_DUTY); m1On = m2On = true; mode = "BACK (slow)"; break;
    case 'c': motor1(FOLLOW_DUTY, 0); motor2(0, FOLLOW_DUTY); m1On = m2On = true; mode = "CW (slow)";   break;
    case 'x': motor1(0, FOLLOW_DUTY); motor2(FOLLOW_DUTY, 0); m1On = m2On = true; mode = "CCW (slow)";  break;
    case 'S': mode = "STOP";   break;
    default:  return;   // ignore stray characters, don't restamp timers
  }

  // Which duty the switch above actually applied. stopAll() ran a few lines
  // up and zeroed this, so 'S' and the default case need nothing here.
  // Derived from the case rather than tracked inside each one so the two
  // can't drift apart when a command is added.
  if (m1On || m2On) {
    activeDuty = (cmd >= 'a' && cmd <= 'z') ? FOLLOW_DUTY : DUTY;
    // Re-scale the trip thresholds to this duty - see ampsToCounts(). Done
    // here rather than once in setup() because the duty is per-command now.
    // Two multiplications on a genuine command change only (keep-alive
    // resends returned long before reaching this line).
    sustainCounts = ampsToCounts(SUSTAIN_AMPS, activeDuty);
    instantCounts = ampsToCounts(INSTANT_AMPS, activeDuty);
  }

  commandTime = millis();
  lastCmdReceivedTime = commandTime;   // feeds the watchdog
  lastAppliedCmd = cmd;
}

// Reads any pending bytes from the USB serial link (i.e. from the Pi).
// Accepts only the known command characters; everything else (newlines,
// stray bytes) is silently ignored so the Pi can send "F\n", "F", etc.
void checkSerialCommand() {
  while (Serial.available() > 0) {
    char c = Serial.read();

    // Head-angle capture: 'H' followed by exactly three ASCII digits, e.g.
    // "H090" for centre. Fixed width so it needs no terminator - the third
    // digit ends it - which keeps it immune to the desync a missing
    // terminator would otherwise cause on a protocol with no framing.
    // A non-digit arriving mid-number abandons the number and is then
    // re-handled as an ordinary command below, so a stray 'H' can never
    // swallow a Stop.
    if (readingAngle) {
      if (c >= '0' && c <= '9') {
        angleAccum = angleAccum * 10 + (c - '0');
        if (++angleDigits >= 3) {
          servoTarget = constrain(angleAccum, 0, 180);
          readingAngle = false;
        }
        continue;
      }
      readingAngle = false;   // malformed - drop it, then fall through
    }
    if (c == 'H') {
      readingAngle = true;
      angleAccum = 0;
      angleDigits = 0;
      continue;
    }

    // 'P' re-runs the boot-time pad report on demand, so a wiring fault can be
    // chased live - wiggle a wire and watch the state change - instead of
    // rebooting between attempts. Not a motor command: it never touches
    // pendingCmd or the watchdog. Refused while a motor is on, because the
    // test reconfigures the PWM pins and would drop the drive mid-command.
    if (c == 'P') {
      if (m1On || m2On) Serial.println("LOG: pad test skipped - motors running");
      else reportPads();
      continue;
    }

    if (c == 'F' || c == 'B' || c == 'C' || c == 'X' || c == 'S' ||
        c == 'f' || c == 'b' || c == 'c' || c == 'x') {
      pendingCmd = c;
    }
    // anything else (e.g. '\n', '\r') is ignored. Note there is no lowercase
    // 's': stop is stop, and having one spelling of it keeps the trip latch
    // and the watchdog's stop path unambiguous.
    //
    // Head commands deliberately do NOT touch lastCmdReceivedTime: they are
    // not motor commands, and the motor watchdog only ever fires while a
    // motor is actually on. Letting them feed it would mean a robot in head
    // mode - wheels stopped, head panning - was silently keeping the wheel
    // watchdog alive for no reason.
  }
}
// ================== END COMMAND HANDLING ================================


// What the pad looks like with nothing driving it, using only the ESP32's
// internal pull-up/pull-down. Run before the LEDC output is attached, so it
// reports the *net* rather than what we are driving onto it:
//
//   "held low"  something outside is holding it down - a driver input with a
//               pull-down on it, i.e. a wire that goes somewhere
//   "held high" something outside is holding it up
//   "floating"  the pin follows whichever internal resistor is enabled, which
//               means nothing is connected to it
//
// On its own that is ambiguous (a BTS7960 input is high-impedance on some
// boards and pulled down on others). Compared side by side across the four
// pins it is not: two motors wired the same way must read the same, so a pin
// that disagrees with its opposite number is a disconnected signal wire.
const char *padState(int pin) {
  pinMode(pin, INPUT_PULLUP);
  delayMicroseconds(50);
  bool withPullup = digitalRead(pin);
  pinMode(pin, INPUT_PULLDOWN);
  delayMicroseconds(50);
  bool withPulldown = digitalRead(pin);
  pinMode(pin, INPUT);
  if (withPullup && !withPulldown) return "floating";
  if (!withPullup && !withPulldown) return "held low";
  if (withPullup && withPulldown)   return "held high";
  return "unstable";
}

// Can the pin drive its net at all? padState() above says what the outside
// world does to a pin we are not driving, which cannot tell "nothing attached"
// from "attached to a high-impedance input". This says whether the ESP32's
// output driver still works and whether anything is fighting it:
//
//   "follows"    the pad goes where it is driven - the pin is healthy
//   "STUCK LOW"  driven high, still reads low: shorted to ground, or the pad
//                is damaged
//   "STUCK HIGH" driven low, still reads high: shorted to 3V3/5V
//
// Reads its own output pin, which works because Arduino-ESP32's OUTPUT mode
// leaves the input path enabled (GPIO_MODE_INPUT_OUTPUT). Each pin is driven
// for 200us and put back to a safe state before the next one, far too brief to
// turn a motor and never with both halves of one bridge high at once.
const char *padDriveTest(int pin) {
  pinMode(pin, OUTPUT);
  digitalWrite(pin, HIGH);
  delayMicroseconds(200);
  bool drivenHigh = digitalRead(pin);
  digitalWrite(pin, LOW);
  delayMicroseconds(200);
  bool drivenLow = digitalRead(pin);
  pinMode(pin, INPUT);
  if (drivenHigh && !drivenLow) return "follows";
  if (!drivenHigh && !drivenLow) return "STUCK LOW";
  if (drivenHigh && drivenLow)   return "STUCK HIGH";
  return "unstable";
}

// Both pad tests on all four PWM pins, then put the pins back to work. The
// tests use pinMode(), which takes the pin out of the GPIO matrix and so
// detaches the LEDC output - hence the re-attach and stopAll() at the end.
// Never call this with a motor running: it would drop the PWM mid-drive.
void reportPads() {
  Serial.printf("LOG: PWM pads: M1_RPWM(%d)=%s/%s M1_LPWM(%d)=%s/%s\n",
                M1_RPWM, padState(M1_RPWM), padDriveTest(M1_RPWM),
                M1_LPWM, padState(M1_LPWM), padDriveTest(M1_LPWM));
  Serial.printf("LOG:           M2_RPWM(%d)=%s/%s M2_LPWM(%d)=%s/%s\n",
                M2_RPWM, padState(M2_RPWM), padDriveTest(M2_RPWM),
                M2_LPWM, padState(M2_LPWM), padDriveTest(M2_LPWM));
  ledcAttach(M1_RPWM, PWM_FREQ, PWM_BITS);
  ledcAttach(M1_LPWM, PWM_FREQ, PWM_BITS);
  ledcAttach(M2_RPWM, PWM_FREQ, PWM_BITS);
  ledcAttach(M2_LPWM, PWM_FREQ, PWM_BITS);
  stopAll();
}

void setup() {
  Serial.begin(115200);

  // Motor 1 drew exactly 0.00A on both half-bridges while motor 2 drew 4-7A on
  // the same commands, which is the signature of a whole driver not switching
  // rather than one blown bridge. These two lines are what tell you which side
  // of the ESP32's pins that fault is on, without unscrewing anything.
  reportPads();   // also attaches the four motor PWM pins

  // ledcAttach returns false if the pin cannot be attached (no free timer, or
  // a pin that cannot output). Unchecked, that failure is completely silent:
  // the motor simply never moves and every layer above reports success, which
  // is the same class of failure as writes into a dead serial port.
  bool pwmOk = true;
  pwmOk &= ledcAttach(M1_RPWM, PWM_FREQ, PWM_BITS);
  pwmOk &= ledcAttach(M1_LPWM, PWM_FREQ, PWM_BITS);
  pwmOk &= ledcAttach(M2_RPWM, PWM_FREQ, PWM_BITS);
  pwmOk &= ledcAttach(M2_LPWM, PWM_FREQ, PWM_BITS);
  if (!pwmOk) Serial.println("LOG: WARNING - a motor PWM pin failed to attach");

  // Separate LEDC timer from the motors (20kHz/8-bit vs 50Hz/16-bit). There
  // are four timers available, so this does not contend with them.
  if (!ledcAttach(SERVO_PIN, SERVO_FREQ, SERVO_BITS))
    Serial.println("LOG: WARNING - servo pin failed to attach");
  applyServo(SERVO_CENTER_DEG);
  lastServoStep = millis();

  stopAll();

  // Zero the current sense now, while stopAll() above guarantees the bridges
  // are off and no load current can be flowing. Anything the ADC reads here
  // is offset - the converter's own floor plus the driver's quiescent IS
  // output - and subtracting it is what stops an idle robot reporting a
  // couple of phantom amps.
  long z1 = 0, z2 = 0;
  for (int i = 0; i < ZERO_SAMPLES; i++) {
    z1 += analogRead(SenseM1);
    z2 += analogRead(SenseM2);
  }
  zeroM1 = (float)z1 / ZERO_SAMPLES;
  zeroM2 = (float)z2 / ZERO_SAMPLES;

  // Armed at DUTY so they are never left at 0 (which would trip on the first
  // sample). handleCommand() re-scales them to whichever duty each command
  // actually applies - see ampsToCounts().
  sustainCounts = ampsToCounts(SUSTAIN_AMPS, DUTY);
  instantCounts = ampsToCounts(INSTANT_AMPS, DUTY);

  Serial.printf("LOG: Current sense zero: M1=%.1f M2=%.1f counts\n", zeroM1, zeroM2);

  // Both duties printed, because the thresholds are per-duty now and "which
  // counts mean 16A" is the first thing worth knowing when a trip is being
  // chased.
  Serial.printf("LOG: Trip points: %.0fA sustained / %.0fA instant\n",
                SUSTAIN_AMPS, INSTANT_AMPS);
  Serial.printf("LOG:   at DUTY=%d        sustained=%d instant=%d counts\n",
                DUTY, ampsToCounts(SUSTAIN_AMPS, DUTY), ampsToCounts(INSTANT_AMPS, DUTY));
  Serial.printf("LOG:   at FOLLOW_DUTY=%d sustained=%d instant=%d counts\n",
                FOLLOW_DUTY, ampsToCounts(SUSTAIN_AMPS, FOLLOW_DUTY),
                ampsToCounts(INSTANT_AMPS, FOLLOW_DUTY));

  Serial.println("LOG: Ready - USB serial commands accepted");
}

void loop() {
  // Pull in any new command from Serial (Pi), then handle it if present.
  checkSerialCommand();

  if (pendingCmd != 0) {
    char cmd = pendingCmd;
    pendingCmd = 0;
    handleCommand(cmd);
  }

  // Head servo slews toward its target independently of everything else -
  // deliberately outside the motor watchdog and the trip latch, since the
  // head carries only the camera and cannot stall the drive or draw through
  // the sense resistors.
  updateServo();

  // Every SAMPLE_MS, check current draw against limits.
  if (millis() - lastSample >= SAMPLE_MS) {
    lastSample = millis();
    int c1 = analogRead(SenseM1);
    int c2 = analogRead(SenseM2);

    // Read before checkProtection(), because a trip inside it calls stopAll()
    // and zeroes activeDuty - and these two samples were taken while the
    // motors were still driving at the old duty. Scaling them by the new zero
    // would report 0.0A for the one sample that actually caused the trip.
    int sampledDuty = activeDuty;

    checkProtection(c1, c2);

    // Same two readings, converted for reporting. Deliberately reuses
    // checkProtection()'s untouched values: reporting must never be able to
    // change when the motors trip.
    curSumM1 += countsToAmps(c1 - zeroM1, sampledDuty);
    curSumM2 += countsToAmps(c2 - zeroM2, sampledDuty);
    curSamples++;
  }

  if (millis() - lastCurrentReport >= (unsigned long)CURRENT_REPORT_MS) {
    lastCurrentReport = millis();
    reportCurrent();
  }

  // Command watchdog: if motors are running but nothing new has arrived
  // recently, force stop. Protects against Pi crash, USB unplug, or app
  // freeze leaving the robot driving with no oversight.
  if ((m1On || m2On) && (millis() - lastCmdReceivedTime > CMD_TIMEOUT_MS)) {
    Serial.println("LOG: WATCHDOG TIMEOUT - no command received, stopping");
    stopAll();
    mode = "STOP (timeout)";
  }

  // Status line on actual state changes only, never as a fixed-interval
  // heartbeat. Prefixed "STATUS:" to distinguish it from the "LOG:" lines.
  //
  // mode/lastPrintedMode are const char* rather than Arduino String. Every
  // value assigned to mode is a string literal with static storage, so this
  // is safe, keeps heap allocation out of a loop that runs thousands of times
  // a second, and strcmp is an honest content comparison. printf also makes
  // the line a single write instead of print()+println()'s two.
  //
  // Worth recording why this was touched, so nobody "fixes" it back: it was
  // changed while chasing an apparent flood of truncated ":STOP" lines on the
  // serial link. That turned out NOT to be this code - the CP2102 adapter had
  // wedged, and was replaying a fixed pattern at ~240KB/s, which is 20x what
  // 115200 baud can physically carry and was identical at every baud rate we
  // asked for. A USB-level reset cleared it and this loop went silent, as it
  // was always supposed to. If you ever see repeated garbage that ignores the
  // baud rate, reset the adapter before suspecting the firmware.
  if (strcmp(mode, lastPrintedMode) != 0) {
    lastPrintedMode = mode;
    Serial.printf("STATUS:%s\n", mode);
  }
}
