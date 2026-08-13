/*==========================================================================
// Dual BTS7960 robot: USB-Serial control + current protection (ESP32)
//
// Commands (single chars):
//   F forward   B backward   C rotate CW   X rotate CCW   S stop
//   f/b/c/x     the same four directions at FOLLOW_DUTY instead of DUTY,
//               for face-following, which needs to move gently and
//               continuously rather than at driving speed. No lowercase 's'.
//
//   - Serial: from a Raspberry Pi over USB, e.g. Serial.write('F')
//     Feeds into pendingCmd -> handleCommand() logic.
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

const float SUSTAIN_AMPS = 10.0;
const float INSTANT_AMPS = 20.0;
const int   SUSTAIN_MS   = 250;
const int   GRACE_MS     = 300;
const int   SAMPLE_MS    = 20;

const int SUSTAIN_SAMPLES = SUSTAIN_MS / SAMPLE_MS;
const int CLEAR_SAMPLES   = 3;

// ---------------- Command watchdog ----------------
const unsigned long CMD_TIMEOUT_MS = 500;   // stop if no fresh command within this window

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
const int FOLLOW_DUTY = 55;   // 0-255, against DUTY = 75 for the remote page

// ---------------- State ----------------
bool m1On = false;
bool m2On = false;
unsigned long commandTime = 0;      // used for the current-protection grace period
unsigned long lastCmdReceivedTime = 0; // used for the command watchdog
unsigned long lastSample  = 0;
String mode = "STOP";
String lastPrintedMode = "";

int m1OverCount = 0;
int m2OverCount = 0;
int m1UnderRun  = 0;
int m2UnderRun  = 0;

int sustainCounts;
int instantCounts;

// Command received over Serial, handled in loop(). volatile: written
// from the serial read, read from loop().
volatile char pendingCmd = 0;

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

void stopAll() {
  motor1(0, 0);
  motor2(0, 0);
  m1On = false;
  m2On = false;
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


// ==================== CURRENT PROTECTION (UNCHANGED) ====================
int ampsToCounts(float amps) {
  float volts = amps * (SENSE_R / K_ILIS) * (DUTY / 255.0);
  int counts = volts * 4095.0 / ADC_FS_V;
  if (counts > 4090) counts = 4090;
  return counts;
}

void checkProtection(int c1, int c2) {
  if (!m1On && !m2On) return;

  bool pastGrace = (millis() - commandTime >= GRACE_MS);

  if (pastGrace) {
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
    if (c == 'F' || c == 'B' || c == 'C' || c == 'X' || c == 'S' ||
        c == 'f' || c == 'b' || c == 'c' || c == 'x') {
      pendingCmd = c;
    }
    // anything else (e.g. '\n', '\r') is ignored. Note there is no lowercase
    // 's': stop is stop, and having one spelling of it keeps the trip latch
    // and the watchdog's stop path unambiguous.
  }
}
// ================== END COMMAND HANDLING ================================


void setup() {
  Serial.begin(115200);

  ledcAttach(M1_RPWM, PWM_FREQ, PWM_BITS);
  ledcAttach(M1_LPWM, PWM_FREQ, PWM_BITS);
  ledcAttach(M2_RPWM, PWM_FREQ, PWM_BITS);
  ledcAttach(M2_LPWM, PWM_FREQ, PWM_BITS);

  stopAll();

  sustainCounts = ampsToCounts(SUSTAIN_AMPS);
  instantCounts = ampsToCounts(INSTANT_AMPS);

  Serial.print("LOG: Trip points: sustained=");
  Serial.print(sustainCounts);
  Serial.print("   instant=");
  Serial.println(instantCounts);

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

  // Every SAMPLE_MS, check current draw against limits.
  if (millis() - lastSample >= SAMPLE_MS) {
    lastSample = millis();
    int c1 = analogRead(SenseM1);
    int c2 = analogRead(SenseM2);
    checkProtection(c1, c2);
  }

  // Command watchdog: if motors are running but nothing new has arrived
  // recently, force stop. Protects against Pi crash, USB unplug, or app
  // freeze leaving the robot driving with no oversight.
  if ((m1On || m2On) && (millis() - lastCmdReceivedTime > CMD_TIMEOUT_MS)) {
    Serial.println("LOG: WATCHDOG TIMEOUT - no command received, stopping");
    stopAll();
    mode = "STOP (timeout)";
  }

  // Status line on actual state changes only (not a fixed-interval heartbeat -
  // that used to print unconditionally every 200ms, which meant "STATUS:STOP"
  // forever while idle: pure noise, and on the Pi side each line becomes a
  // print() call sharing the same process as Flask's request handling, so a
  // constant 5Hz stream of them was a real, avoidable source of overhead).
  // Prefixed "STATUS:" so it's easy to distinguish from the "LOG:" lines above.
  if (mode != lastPrintedMode) {
    lastPrintedMode = mode;
    Serial.print("STATUS:");
    Serial.println(mode);
  }
}
