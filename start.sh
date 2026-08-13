#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# One server for both halves - Ruby's screen and the robot's remote page (see
# CLAUDE.md's Architecture section for why they can't be separate processes).
# gunicorn, not `python app.py`: Werkzeug's dev server has no HTTP keep-alive
# in any configuration and serializes TLS handshakes behind one slow client,
# which is what caused the input lag and disconnects this replaces.
"$DIR/venv/bin/gunicorn" -c gunicorn.conf.py app:app &
SERVER_PID=$!

cleanup() {
  kill "$SERVER_PID" 2>/dev/null || true
}
trap cleanup EXIT

# -k: the server's certificate is self-signed (or Tailscale-issued for a name
# that isn't "localhost"), so curl would otherwise refuse the handshake and
# we'd loop the full 30 tries against a server that's actually up and fine.
for i in $(seq 1 60); do
  if curl -sk -o /dev/null "https://127.0.0.1:5000/"; then
    break
  fi
  sleep 0.5
done

# PipeWire races the HDMI display at boot and loses often enough to matter.
# WirePlumber probes the vc4-hdmi card for an audio profile before the monitor
# has finished handshaking, sees no valid ELD, and gives up on that card:
#   wireplumber: s-monitors: Failed to create
#     alsa_output.platform-...hdmi.hdmi-stereo: Object activation aborted
# It then falls back to "auto_null" (Dummy Output) - a sink that accepts audio
# and discards it. Everything looks healthy from userspace: playback succeeds,
# the volume control works, nothing errors. There is just no sound, forever,
# until something re-probes the card. The display itself is fine and does
# advertise stereo LPCM in its ELD once it's up.
#
# Re-probing after the display is definitely awake fixes it, and by this point
# in startup it certainly is. Only touched when the output is actually missing,
# so a working setup (or a deliberately chosen sink) is left completely alone.
if command -v pactl >/dev/null 2>&1; then
  SINK="$(pactl get-default-sink 2>/dev/null || true)"
  case "$SINK" in
    "" | auto_null | *dummy* | *Dummy*)
      echo "No real audio output (sink: ${SINK:-none}) - re-probing sound card." >&2
      systemctl --user restart wireplumber 2>/dev/null || true
      for _ in $(seq 1 20); do
        SINK="$(pactl get-default-sink 2>/dev/null || true)"
        case "$SINK" in
          "" | auto_null | *dummy* | *Dummy*) sleep 0.5 ;;
          *) break ;;
        esac
      done
      if [ -n "$SINK" ] && [ "$SINK" != "auto_null" ]; then
        # A sink recovered this way comes back muted at 0% about as often as
        # not, which is indistinguishable from the original fault to anyone
        # without a terminal. Only ever runs on the repair path.
        pactl set-sink-mute "$SINK" 0 2>/dev/null || true
        pactl set-sink-volume "$SINK" 85% 2>/dev/null || true
        echo "Audio output restored: $SINK" >&2
      else
        echo "Warning: still no audio output device." >&2
      fi
      ;;
  esac
fi

# Any Chromium-family browser will do - the only thing that matters is
# --load-extension, which loads kiosk-extension/ unpacked (no signing) so the
# headers blocking Google/YouTube get stripped. See that folder's README.
# Firefox refuses unsigned extensions, so on Firefox those tiles show an
# explanatory card and the rest of the kiosk works normally.
# Chromium is single-instance per profile: launch it while another window is
# already open on the same profile and it just hands the URL to the running
# process, silently dropping --load-extension. That fails in the most
# confusing way possible - the kiosk opens fine, but Google and friends show
# the "won't open yet" card because the extension never loaded.
#
# Giving the kiosk its own profile directory guarantees it gets its own
# process with its own extension, and keeps kiosk browsing out of any personal
# Chromium profile. The snap confines --user-data-dir to its own tree, so the
# path has to live there when we're running the snap build.
if [ -d "$HOME/snap/chromium" ]; then
  KIOSK_PROFILE="$HOME/snap/chromium/common/ruby-kiosk"
else
  KIOSK_PROFILE="$HOME/.config/ruby-kiosk"
fi

CHROMIUM_FLAGS=(
  --kiosk
  --user-data-dir="$KIOSK_PROFILE"
  --no-first-run
  --load-extension="$DIR/kiosk-extension"
  --autoplay-policy=no-user-gesture-required
  --overscroll-history-navigation=0
  --noerrdialogs
  --disable-session-crashed-bubble
  --disable-features=TranslateUI
  --check-for-update-interval=31536000
  # The server is HTTPS now because the robot's remote page has to be
  # reachable from a phone, and that means the kiosk browser meets the same
  # self-signed certificate every other device does - except it can't be
  # asked to click through an interstitial, since nobody is standing at the
  # Pi with a mouse when it boots. This suppresses the warning for localhost
  # only; certificate errors from any other origin still block normally.
  #
  # Note this is NOT what makes the microphone work: getUserMedia needs a
  # "secure context", and localhost counts as one on its own merits whatever
  # its certificate looks like.
  --allow-insecure-localhost
)
URL="https://localhost:5000/"

# Chromium is single-instance per profile, and that failure mode is nastier
# here than it looks. If one is still running on this profile - including one
# merely still shutting down from a previous run - a fresh launch does not
# start a browser: it hands the URL to the existing process, prints "Opening
# in existing browser session." and exits immediately. Two consequences:
# --load-extension is silently dropped (so Google/YouTube show the "won't
# open" card), and because the browser runs in the foreground below, that
# instant exit fires the EXIT trap and tears the server down with it -
# leaving a dead kiosk that looks like the server crashed.
#
# So make sure the profile is genuinely free first. Matching on the profile
# path means this only ever touches the kiosk's own browser, never a personal
# Chromium window someone left open on another profile.
PROFILE_PATTERN="user-data-dir=$KIOSK_PROFILE"
if pgrep -f "$PROFILE_PATTERN" >/dev/null 2>&1; then
  echo "Kiosk browser already running - closing it so the extension reloads." >&2
  pkill -f "$PROFILE_PATTERN" 2>/dev/null || true
  for _ in $(seq 1 40); do
    pgrep -f "$PROFILE_PATTERN" >/dev/null 2>&1 || break
    sleep 0.25
  done
  # Wouldn't go quietly; snap Chromium can take a while to release the
  # profile lock, and launching before it does reproduces the whole problem.
  if pgrep -f "$PROFILE_PATTERN" >/dev/null 2>&1; then
    pkill -9 -f "$PROFILE_PATTERN" 2>/dev/null || true
    sleep 1.5
  fi
fi

BROWSER=""
for candidate in chromium chromium-browser brave-browser vivaldi-stable google-chrome microsoft-edge-stable; do
  if command -v "$candidate" >/dev/null 2>&1; then
    BROWSER="$candidate"
    break
  fi
done

# Deliberately not exec'd: the EXIT trap above has to survive the browser
# quitting so the server gets cleaned up with it.
if [ -n "$BROWSER" ]; then
  "$BROWSER" "${CHROMIUM_FLAGS[@]}" "$URL"
elif flatpak info org.chromium.Chromium >/dev/null 2>&1; then
  # Flatpak's sandbox can't see the extension folder until you grant it:
  #   flatpak override --user --filesystem="$DIR/kiosk-extension:ro" org.chromium.Chromium
  flatpak run org.chromium.Chromium "${CHROMIUM_FLAGS[@]}" "$URL"
else
  echo "No Chromium-family browser found - falling back to firefox." >&2
  echo "Google/YouTube/Gmail/Maps/News tiles will show a 'won't open yet' card," >&2
  echo "and you'll have to accept the certificate warning once by hand." >&2
  firefox --kiosk --new-instance "$URL"
fi
