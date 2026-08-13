#!/usr/bin/env bash
# Fetches every model this project needs that isn't checked into the repo:
# the Piper TTS voices behind Ruby's voice picker (kiosk_api.py's
# VOICE_OPTIONS) and the YuNet face detector behind Follow me
# (face_follow.py). Run this once after cloning - models/*.onnx is gitignored
# since the voice files are ~60MB each. Re-run any time; it skips files
# already present.
#
# The Vosk speech model the robot's old voice-command feature used is
# deliberately not here any more - transcription is Ruby's job now, and she
# does it through the Gemini API rather than a local model.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# ---------------------------------------------------------------------------
# Piper voices
# ---------------------------------------------------------------------------
BASE_URL="https://huggingface.co/rhasspy/piper-voices/resolve/main"

# id -> path relative to BASE_URL (without the trailing .onnx/.onnx.json)
VOICES=(
  "en_US-hfc_female-medium=en/en_US/hfc_female/medium/en_US-hfc_female-medium"
  "en_US-lessac-medium=en/en_US/lessac/medium/en_US-lessac-medium"
  "en_GB-alan-medium=en/en_GB/alan/medium/en_GB-alan-medium"
)

for entry in "${VOICES[@]}"; do
  name="${entry%%=*}"
  remote_path="${entry#*=}"

  for ext in onnx onnx.json; do
    dest="${name}.${ext}"
    if [ -f "$dest" ]; then
      echo "skip (already present): $dest"
      continue
    fi
    echo "downloading: $dest"
    curl -L --fail --progress-bar -o "$dest" "${BASE_URL}/${remote_path}.${ext}"
  done
done

# ---------------------------------------------------------------------------
# YuNet face detector (~230KB) - Follow me needs this or the button stays
# hidden. Not a Haar cascade: the pinned opencv-python-headless build ships
# no cascade XML files at all, and YuNet holds up far better on the
# off-angle faces and uneven lighting a camera on a moving robot sees.
# ---------------------------------------------------------------------------
FACE_MODEL="face_detection_yunet_2023mar.onnx"
FACE_URL="https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/${FACE_MODEL}"

if [ -f "$FACE_MODEL" ]; then
  echo "skip (already present): $FACE_MODEL"
else
  echo "downloading: $FACE_MODEL"
  curl -L --fail --progress-bar -o "$FACE_MODEL" "$FACE_URL"
fi

echo "done."
