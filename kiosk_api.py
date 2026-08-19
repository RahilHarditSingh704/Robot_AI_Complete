"""
kiosk_api.py

Ruby: the touchscreen assistant and everything her UI calls. Chat,
transcription and TTS via the Gemini API, local Piper voices, the kiosk
mini-apps' backends (notes, volume, screen, vitals, power, TTC), and the
Remote Control switch that publishes the robot's page to the network.

Every route here is localhost-only, enforced once in access.py. Several of
them shell out to the desktop session and can power the Pi off; that was
safe when this app bound 127.0.0.1 and it stays safe now that the merged
process has to bind 0.0.0.0 for the robot half. Keep it that way.
"""

import base64
import io
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import wave

import requests
from flask import Blueprint, Response, current_app, jsonify, request, send_from_directory
from piper import PiperVoice
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

import access
from hardware import follower, link

kiosk = Blueprint("kiosk", __name__)

API_KEY = os.environ.get("GOOGLE_AI_API_KEY")
if not API_KEY:
    raise RuntimeError("GOOGLE_AI_API_KEY is not set in .env")

CHAT_MODEL = os.environ.get("LLM_MODEL", "gemini-3.5-flash-lite")
TRANSCRIBE_MODEL = os.environ.get("TRANSCRIBE_MODEL", "gemini-3.5-flash-lite")
TTS_MODEL = os.environ.get("TTS_MODEL", "gemini-2.5-flash-preview-tts")
TTS_VOICE = os.environ.get("TTS_VOICE", "Kore")
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(BASE_DIR, "models")
DATA_DIR = os.path.join(BASE_DIR, "data")
NOTES_PATH = os.path.join(DATA_DIR, "notes.json")

# ---------------------------------------------------------------------------
# Selectable TTS voices. "gemini" is the cloud voice; the rest are local
# Piper voices (each lazy-loaded on first use, see get_piper_voice()). Add
# more by dropping a Piper .onnx/.onnx.json pair in models/ and adding an
# entry here - it'll show up in the voice picker automatically.
# ---------------------------------------------------------------------------
VOICE_OPTIONS = [
    {"id": "gemini", "label": "Gemini (cloud)", "type": "gemini"},
    {
        "id": "piper-hfc_female",
        "label": "Piper - American Female",
        "type": "piper",
        "model": "en_US-hfc_female-medium.onnx",
    },
    {
        "id": "piper-lessac",
        "label": "Piper - American Male",
        "type": "piper",
        "model": "en_US-lessac-medium.onnx",
    },
    {
        "id": "piper-alan",
        "label": "Piper - British Male",
        "type": "piper",
        "model": "en_GB-alan-medium.onnx",
    },
]
VOICE_OPTIONS_BY_ID = {v["id"]: v for v in VOICE_OPTIONS}

# The Piper voice used as a fallback when the preferred voice is "gemini"
# and the Gemini API call fails (e.g. a rate limit).
FALLBACK_PIPER_VOICE_ID = "piper-hfc_female"

_piper_voice_cache = {}


def get_piper_voice(voice_id):
    if voice_id not in _piper_voice_cache:
        model_filename = VOICE_OPTIONS_BY_ID[voice_id]["model"]
        _piper_voice_cache[voice_id] = PiperVoice.load(
            os.path.join(MODELS_DIR, model_filename)
        )
    return _piper_voice_cache[voice_id]


# Which voice is currently preferred - one of the ids in VOICE_OPTIONS.
# Switchable at runtime via GET/POST /api/tts/voice (see the picker in the UI).
tts_voice_id = os.environ.get("TTS_VOICE_ID", "gemini")

# ---------------------------------------------------------------------------
# Assistant persona: edit PERSONA_PROMPT to change who the assistant is / how
# it talks. RESPONSE_CONSTRAINTS below is kept separate so the TTS/screen
# formatting rules stay in place no matter what persona is set here.
# ---------------------------------------------------------------------------
PERSONA_PROMPT = (
    "You are an adorable assistant named Ruby - quirky and helpful. You live on a "
    "touchscreen kiosk in the Department of Electrical & Computer Engineering at "
    "the University of Toronto's St. George campus in downtown Toronto. The people "
    "talking to you are mostly ECE and Faculty of Applied Science & Engineering "
    "undergrads, plus visitors trying to find their way around. You are also mounted "
    "on a small wheeled robot, so you can physically follow someone around."
)

# ---------------------------------------------------------------------------
# Campus knowledge handed to the model on every turn. Only facts verified
# against U of T's own pages are listed here - buildings whose street address
# wasn't confirmed are named without one on purpose, because the model will
# repeat whatever it's given as fact. Same reason for the closing paragraph:
# without it, an LLM asked "what room is the ECE office in?" will invent a
# plausible room number, which is worse than "I'm not sure, ask at the desk".
# ---------------------------------------------------------------------------
CAMPUS_CONTEXT = (
    "Campus knowledge you can rely on. Engineering building codes: BA is the Bahen "
    "Centre for Information Technology at 40 St. George Street; SF is the Sandford "
    "Fleming Building at 10 King's College Road, which is where ECE is based; GB is "
    "the Galbraith Building at 35 St. George Street; MY is the Myhal Centre for "
    "Engineering Innovation & Entrepreneurship at 55 St. George Street. Other codes "
    "you'll hear, whose addresses you should not guess at: MC (Mechanical "
    "Engineering Building), WB (Wallberg), HA (Haultain), PT (Pratt), RS "
    "(Rosebrugh), EX (Exam Centre), SS (Sidney Smith). "
    "ECE Iris is the department's curriculum visualization tool for seeing how "
    "courses connect across the flexible curriculum. "
    "Emergencies: Campus Safety is 416-978-2222, staffed 24/7, or 911 if someone is "
    "in immediate danger; 416-978-2323 is the non-urgent line. The Health & Wellness "
    "Centre is 416-978-8030, open weekdays 9 to 5. "
    "This kiosk has an Apps button on the right edge of the screen. Behind it are "
    "tiles for ECE Home, ECE Iris, ECE Resources, ECE Club, the Faculty of "
    "Engineering, the Undergrad Office, the Academic Calendar, the Career Centre, "
    "Engineering Communication, Emergency contacts, the Campus Map, Timetable "
    "Builder, Library Search, Health & Wellness, Campus Safety, Skule, Transit, "
    "Buildings, and tools like Clock, Weather, Radio and Notes. Point people at the "
    "right tile when it would help. Nothing on the kiosk asks anyone to log in, and "
    "you should never ask someone to type a password or UTORid into it. "
    "There is also a Remote Control tile, which switches on a web page other people "
    "on the same network can open to drive the robot from their own phone. It is off "
    "unless someone turns it on at this screen. "
    "The Follow me button at the bottom left of your own screen makes the robot use "
    "its camera to find a face and drive after that person; press it again to stop. "
    "If someone asks you to follow them, tell them to tap it - you cannot press it "
    "yourself. "
    "If you are not certain about a room number, an office's hours, a deadline, a "
    "course's details or someone's contact information, say plainly that you're not "
    "sure and send them to the relevant tile or the department office. Never invent "
    "a room number, phone number or address."
)

RESPONSE_CONSTRAINTS = (
    "Your replies are read aloud by text-to-speech and shown on a small screen, "
    "so keep answers short and conversational (usually 1-3 sentences) unless the "
    "user clearly asks for detail. Avoid markdown, bullet points, or code blocks "
    "since they will be spoken aloud."
)


def build_system_prompt():
    # Built fresh per request (not cached) so it always reflects whichever
    # voice is currently selected, since that can change at runtime via the
    # picker in the UI.
    voice_label = VOICE_OPTIONS_BY_ID[tts_voice_id]["label"]
    voice_note = f'You are currently speaking through the "{voice_label}" voice.'
    return f"{PERSONA_PROMPT} {RESPONSE_CONSTRAINTS} {CAMPUS_CONTEXT} {voice_note}"


# ---------------------------------------------------------------------------
# Expression detection: the model is never told about this - we just look at
# whatever it naturally says and pattern-match/sentiment-score for emotional
# cues, so the face's expression (see static/app.js's setExpression())
# reflects the actual reply text rather than a cooperative flag.
#
# Blush stays specific phrase-matching (it's about a particular social cue -
# flattery/bashfulness - not general positivity). Happy/elated are driven
# primarily by VADER, a local lexicon+rule sentiment scorer: unlike a fixed
# keyword list it understands negation ("not thrilled" won't score
# positive), degree modifiers ("so much fun"), and punctuation/caps
# emphasis, so it catches genuinely upbeat replies that don't happen to
# contain one of our exact keywords. HAPPY_PATTERN is kept as a secondary
# net for short assistant-specific slang VADER's general-purpose lexicon
# has no opinion on (e.g. "beep boop", compound 0.00). It only fires when
# VADER's compound score is near zero (no signal either way) - NOT merely
# "not negative" - because VADER's negation handling dampens a negated
# word's valence rather than always flipping its sign ("not thrilled"
# still comes out mildly positive), so a wider gate let negated phrases
# slip through as false positives.
#
# Happy has two tiers: "happy" (mild-to-moderately positive - smile only)
# and "elated" (strongly positive - smile plus the ">-<" bounced eyes).
# The eyes were triggering on everyday pleasant replies, so they're now
# reserved for the more emphatic end of the sentiment range.
# ---------------------------------------------------------------------------
BLUSH_PATTERN = re.compile(
    r"blush|flatt(er|ery)|bashful|you'?re (so|too) (kind|sweet)|"
    r"so sweet of you|aw+,? (thanks|thank you|shucks)|teehee|\*giggles?\*",
    re.IGNORECASE,
)

HAPPY_PATTERN = re.compile(
    r"\b(yay|woo+hoo?|awesome|wonderful|fantastic|amazing|excited|exciting|"
    r"delighted|thrilled|so much fun|haha|beep boop|cute|adorable|cool|neat|"
    r"love (that|this|it)|how fun|great fun)\b|!!",
    re.IGNORECASE,
)

HAPPY_VADER_THRESHOLD = 0.5
ELATED_VADER_THRESHOLD = 0.85
NEUTRAL_VADER_BAND = 0.1

_sentiment_analyzer = SentimentIntensityAnalyzer()


def detect_expression(text):
    if BLUSH_PATTERN.search(text):
        current_app.logger.info("expression=blush (phrase match) | %r", text)
        return "blush"

    compound = _sentiment_analyzer.polarity_scores(text)["compound"]
    if compound >= ELATED_VADER_THRESHOLD:
        expression = "elated"
    elif compound >= HAPPY_VADER_THRESHOLD:
        expression = "happy"
    elif abs(compound) <= NEUTRAL_VADER_BAND and HAPPY_PATTERN.search(text):
        expression = "happy (keyword fallback)"
    else:
        expression = "neutral"
    current_app.logger.info(
        "expression=%s (compound=%.3f) | %r", expression, compound, text
    )
    return expression.split(" ")[0]


def call_gemini(model, body):
    url = f"{GEMINI_BASE}/{model}:generateContent"
    resp = requests.post(
        url,
        headers={"x-goog-api-key": API_KEY, "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        reason = data.get("promptFeedback", {}).get("blockReason", "no response")
        raise ValueError(f"Model returned no candidates ({reason})")
    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    return text


def synthesize_speech_gemini(text):
    url = f"{GEMINI_BASE}/{TTS_MODEL}:generateContent"
    body = {
        "contents": [{"parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": TTS_VOICE}}
            },
        },
    }
    resp = requests.post(
        url,
        headers={"x-goog-api-key": API_KEY, "Content-Type": "application/json"},
        json=body,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    candidates = data.get("candidates") or []
    if not candidates:
        reason = data.get("promptFeedback", {}).get("blockReason", "no response")
        raise ValueError(f"TTS model returned no candidates ({reason})")
    parts = candidates[0].get("content", {}).get("parts") or []
    inline = next((p.get("inlineData") for p in parts if p.get("inlineData")), None)
    if not inline:
        raise ValueError("TTS model returned no audio")

    pcm_bytes = base64.b64decode(inline["data"])
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(24000)
        wav_file.writeframes(pcm_bytes)
    return wav_buffer.getvalue()


def synthesize_speech_piper(text, voice_id):
    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as wav_file:
        get_piper_voice(voice_id).synthesize_wav(text, wav_file)
    return wav_buffer.getvalue()


def synthesize_speech(text):
    """Returns (wav_bytes, voice_id_used)."""
    if tts_voice_id != "gemini":
        return synthesize_speech_piper(text, tts_voice_id), tts_voice_id

    try:
        return synthesize_speech_gemini(text), "gemini"
    except Exception:
        current_app.logger.exception("gemini tts failed, falling back to piper")
        return (
            synthesize_speech_piper(text, FALLBACK_PIPER_VOICE_ID),
            FALLBACK_PIPER_VOICE_ID,
        )


# ---------------------------------------------------------------------------
# Canned phrases.
#
# The fixed lines Ruby says on a button press rather than in reply to
# anything. They never vary, so paying Gemini to read the same sentence out
# every time somebody taps Follow me is pure waste - it's the same wording, in
# the same voice, at the same length, several times a day. Each one is
# synthesized once, written to disk, and played from there forever after.
#
# Filed per voice, because the picker can switch between Gemini and the local
# Piper voices at runtime and a recording in the wrong voice would be worse
# than no cache at all. Switching back reuses the earlier recording rather than
# re-spending on it.
#
# The text lives here rather than in the frontend so that what Ruby says and
# what was recorded cannot drift apart: the browser only ever sends the key.
# To re-record after editing one, delete its file - data/tts_cache/ is
# disposable, and the whole directory is already gitignored along with data/.
# ---------------------------------------------------------------------------
PHRASE_CACHE_DIR = os.path.join(DATA_DIR, "tts_cache")

CANNED_PHRASES = {
    "follow-body": "Okay, I'll follow you!",
    "follow-head": "Okay, I'll keep an eye on you!",
}

# Held across the synthesis, not just the file write, so two taps in quick
# succession on a cold cache make one API call rather than two.
_phrase_lock = threading.Lock()


def _phrase_cache_path(key, voice_id):
    return os.path.join(PHRASE_CACHE_DIR, f"{key}.{voice_id}.wav")


def _read_cached_phrase(key, voice_id):
    try:
        with open(_phrase_cache_path(key, voice_id), "rb") as f:
            return f.read()
    except OSError:
        return None


def synthesize_phrase(key):
    """Returns (wav_bytes, voice_id_used) for a canned phrase, going out to
    the TTS engine at most once per phrase per voice."""
    cached = _read_cached_phrase(key, tts_voice_id)
    if cached is not None:
        return cached, tts_voice_id

    with _phrase_lock:
        # Re-check: another request may have recorded it while we queued.
        cached = _read_cached_phrase(key, tts_voice_id)
        if cached is not None:
            return cached, tts_voice_id

        wav_bytes, engine_used = synthesize_speech(CANNED_PHRASES[key])
        # Filed under the voice that actually spoke it rather than the one
        # asked for. If Gemini was preferred but failed and Piper covered for
        # it, caching that under "gemini" would make one rate limit permanent;
        # this way the next press tries Gemini again.
        path = _phrase_cache_path(key, engine_used)
        try:
            os.makedirs(PHRASE_CACHE_DIR, exist_ok=True)
            tmp = f"{path}.part"
            with open(tmp, "wb") as f:
                f.write(wav_bytes)
            os.replace(tmp, path)  # atomic, so a reader never gets half a wav
        except OSError:
            # A cache that can't be written is a slow cache, not a broken
            # feature - she should still say the line.
            current_app.logger.exception("could not cache phrase %r", key)
        return wav_bytes, engine_used


@kiosk.route("/")
def index():
    return send_from_directory(current_app.static_folder, "index.html")


@kiosk.route("/api/chat", methods=["POST"])
def chat():
    payload = request.get_json(force=True, silent=True) or {}
    history = payload.get("history", [])

    contents = [
        {"role": turn.get("role"), "parts": [{"text": turn.get("text", "")}]}
        for turn in history
        if turn.get("text")
    ]
    if not contents:
        return jsonify({"error": "empty history"}), 400

    body = {
        "systemInstruction": {"parts": [{"text": build_system_prompt()}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 512},
    }

    try:
        reply = call_gemini(CHAT_MODEL, body)
    except Exception as exc:
        current_app.logger.exception("chat request failed")
        return jsonify({"error": str(exc)}), 502

    if not reply:
        reply = "Sorry, I didn't quite catch that. Could you try again?"

    return jsonify({"reply": reply, "emotion": detect_expression(reply)})


@kiosk.route("/api/transcribe", methods=["POST"])
def transcribe():
    audio_file = request.files.get("audio")
    if audio_file is None:
        return jsonify({"error": "no audio file provided"}), 400

    mime_type = audio_file.mimetype or "audio/ogg"
    audio_b64 = base64.b64encode(audio_file.read()).decode("ascii")

    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"inlineData": {"mimeType": mime_type, "data": audio_b64}},
                    {
                        "text": (
                            "Transcribe this audio verbatim. Reply with only the "
                            "raw transcript, no labels, quotes, or commentary. If "
                            "the audio is silent or unintelligible, reply with "
                            "nothing."
                        )
                    },
                ],
            }
        ],
        "generationConfig": {"temperature": 0.0},
    }

    try:
        text = call_gemini(TRANSCRIBE_MODEL, body)
    except Exception as exc:
        current_app.logger.exception("transcription failed")
        return jsonify({"error": str(exc)}), 502

    return jsonify({"text": text})


@kiosk.route("/api/tts", methods=["POST"])
def tts():
    payload = request.get_json(force=True, silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return jsonify({"error": "no text provided"}), 400

    try:
        wav_bytes, engine_used = synthesize_speech(text)
    except Exception as exc:
        current_app.logger.exception("tts request failed")
        return jsonify({"error": str(exc)}), 502

    resp = Response(wav_bytes, mimetype="audio/wav")
    resp.headers["X-TTS-Engine"] = engine_used
    return resp


@kiosk.route("/api/tts/phrase/<key>", methods=["GET"])
def tts_phrase(key):
    """One of Ruby's fixed lines, from the recording rather than the API.

    See CANNED_PHRASES. `key` is only ever looked up in that dict, so it can't
    reach the filesystem on its own.
    """
    if key not in CANNED_PHRASES:
        return jsonify({"error": "unknown phrase"}), 404

    try:
        wav_bytes, engine_used = synthesize_phrase(key)
    except Exception as exc:
        current_app.logger.exception("canned phrase tts failed")
        return jsonify({"error": str(exc)}), 502

    resp = Response(wav_bytes, mimetype="audio/wav")
    resp.headers["X-TTS-Engine"] = engine_used
    # The URL doesn't name a voice but the answer depends on which one is
    # selected, so the browser must not keep a copy of its own - it would go
    # on playing the old voice after a switch. Re-reading it from disk here
    # costs nothing next to the API call this is avoiding.
    resp.headers["Cache-Control"] = "no-store"
    return resp


@kiosk.route("/api/tts/voices", methods=["GET"])
def get_tts_voices():
    return jsonify(
        {
            "voices": [{"id": v["id"], "label": v["label"]} for v in VOICE_OPTIONS],
            "selected": tts_voice_id,
        }
    )


@kiosk.route("/api/tts/voice", methods=["POST"])
def set_tts_voice():
    global tts_voice_id
    payload = request.get_json(force=True, silent=True) or {}
    voice_id = payload.get("voice")
    if voice_id not in VOICE_OPTIONS_BY_ID:
        return jsonify({"error": "unknown voice id"}), 400
    tts_voice_id = voice_id
    return jsonify({"voice": tts_voice_id})


# ---------------------------------------------------------------------------
# Remote Control: publishes the robot's driving page (/robot) to the network.
#
# The switch itself lives in access.py, which is also where it's enforced.
# This is just the mini-app's view of it, plus the address to type into a
# phone - the kiosk has no keyboard, so the page has to be able to show a
# reachable URL rather than expecting someone to know the Pi's IP.
# ---------------------------------------------------------------------------
def read_ip():
    # Opens no traffic - connect() on a UDP socket just picks the route the
    # kernel would use, which gives us the address this Pi is reachable on.
    # This is the *default route's* address, i.e. the local network - which is
    # deliberately not what the Remote Control tile advertises; see below.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("192.0.2.1", 1))
            return sock.getsockname()[0]
        except OSError:
            return None


_tailscale_identity = None


def tailscale_identity():
    """This node's (MagicDNS name, IPv4) on the tailnet, or (None, None).

    The robot is driven from a laptop on the same tailnet, not from whatever
    happens to be on the local Wi-Fi, so this - not read_ip() - is the address
    the Remote Control tile shows. Two further reasons it can't be the local
    address here: this Pi's Wi-Fi network hands out addresses in 100.64.0.0/10,
    the same CGNAT range Tailscale uses, so the two are indistinguishable by
    address alone; and that access point appears to have client isolation on
    (a connection to the Pi's own Wi-Fi address times out), so the local
    address wouldn't have worked anyway.

    Prefers the MagicDNS name over the IP. It's stable across re-registration,
    far easier to read off a screen and type, and it's the only address that
    can ever be certificate-clean - see the note in the tile.

    Cached on success only, so this doesn't pay the subprocess on every poll
    but still picks Tailscale up if it starts later."""
    global _tailscale_identity
    if _tailscale_identity is not None:
        return _tailscale_identity

    try:
        out = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        self_node = json.loads(out).get("Self", {})
        name = (self_node.get("DNSName") or "").rstrip(".") or None
        ipv4 = next(
            (ip for ip in self_node.get("TailscaleIPs") or [] if ":" not in ip), None
        )
        if name or ipv4:
            _tailscale_identity = (name, ipv4)
            return _tailscale_identity
    except Exception:
        current_app.logger.info("tailscale status unavailable", exc_info=True)
    return (None, None)


def remote_state():
    # Port from the request rather than a constant: this is by definition the
    # same server the browser is already talking to, so it can't drift out of
    # sync with whatever gunicorn.conf.py binds.
    _, _, port = request.host.partition(":")
    port = port or ("443" if request.scheme == "https" else "80")

    name, tailscale_ip = tailscale_identity()
    host = name or tailscale_ip
    via = "tailscale" if host else None

    # Tailscale down or not installed. Fall back to the local address rather
    # than showing nothing, but label it, because it's a different audience
    # (anyone on that Wi-Fi) than the tailnet this is meant for.
    if host is None:
        host = read_ip()
        via = "lan" if host else None

    return {
        "enabled": access.remote_enabled(),
        "host": host,
        "via": via,
        "tailscale_ip": tailscale_ip,
        "url": f"{request.scheme}://{host}:{port}/robot/" if host else None,
    }


@kiosk.route("/api/remote", methods=["GET"])
def get_remote():
    return jsonify(remote_state())


@kiosk.route("/api/remote", methods=["POST"])
def set_remote():
    payload = request.get_json(force=True, silent=True) or {}
    enabled = bool(payload.get("enabled"))
    was_enabled = access.remote_enabled()
    access.set_remote_enabled(enabled)
    current_app.logger.info("remote control %s", "enabled" if enabled else "disabled")

    # Switching off has to stop the robot, not just close the door on it: a
    # phone holding a direction button has already sent that command once,
    # and MotorLink's resend thread will keep re-affirming it to the ESP32
    # forever with nobody able to press Stop any more. Leave the follower
    # alone though - that's Ruby's own button, driven from this screen, and
    # has nothing to do with who can reach the network page.
    if was_enabled and not enabled and not follower.is_running():
        link.stop()

    return jsonify(remote_state())


# ---------------------------------------------------------------------------
# Kiosk mini-app backends (see static/miniapps.js).
# ---------------------------------------------------------------------------
SINK = "@DEFAULT_AUDIO_SINK@"


def run_cmd(args, timeout=5):
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=True
    ).stdout.strip()


@kiosk.route("/api/notes", methods=["GET"])
def get_notes():
    try:
        with open(NOTES_PATH, encoding="utf-8") as f:
            return jsonify(json.load(f))
    except (OSError, ValueError):
        return jsonify({"text": "", "drawing": ""})


@kiosk.route("/api/notes", methods=["POST"])
def save_notes():
    payload = request.get_json(force=True, silent=True) or {}
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(NOTES_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {
                "text": payload.get("text", ""),
                "drawing": payload.get("drawing", ""),
            },
            f,
        )
    return jsonify({"saved": True})


@kiosk.route("/api/system/volume", methods=["GET"])
def get_volume():
    try:
        # wpctl prints e.g. "Volume: 0.55" or "Volume: 0.55 [MUTED]".
        out = run_cmd(["wpctl", "get-volume", SINK])
        parts = out.split()
        return jsonify(
            {"volume": round(float(parts[1]) * 100), "muted": "[MUTED]" in out}
        )
    except Exception as exc:
        current_app.logger.exception("reading volume failed")
        return jsonify({"error": str(exc)}), 500


@kiosk.route("/api/system/volume", methods=["POST"])
def set_volume():
    payload = request.get_json(force=True, silent=True) or {}
    try:
        level = max(0, min(100, int(payload.get("volume", 50))))
    except (TypeError, ValueError):
        return jsonify({"error": "volume must be a number"}), 400

    try:
        run_cmd(["wpctl", "set-volume", SINK, f"{level / 100:.2f}"])
    except Exception as exc:
        current_app.logger.exception("setting volume failed")
        return jsonify({"error": str(exc)}), 500
    return jsonify({"volume": level})


def read_uptime():
    with open("/proc/uptime", encoding="utf-8") as f:
        seconds = int(float(f.read().split()[0]))
    days, rem = divmod(seconds, 86400)
    hours, minutes = divmod(rem // 60, 60)
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def read_cpu_temp():
    with open("/sys/class/thermal/thermal_zone0/temp", encoding="utf-8") as f:
        return f"{int(f.read()) / 1000:.0f} °C"


def read_memory():
    values = {}
    with open("/proc/meminfo", encoding="utf-8") as f:
        for line in f:
            key, _, rest = line.partition(":")
            values[key] = int(rest.split()[0])  # kB
    used_gb = (values["MemTotal"] - values["MemAvailable"]) / 1048576
    total_gb = values["MemTotal"] / 1048576
    return f"{used_gb:.1f} / {total_gb:.1f} GB"


@kiosk.route("/api/system/info", methods=["GET"])
def system_info():
    def safe(fn, fallback="—"):
        try:
            return fn()
        except Exception:
            current_app.logger.exception("system info field failed")
            return fallback

    usage = shutil.disk_usage("/")
    return jsonify(
        {
            "hostname": socket.gethostname(),
            "ip": safe(lambda: read_ip() or "offline"),
            "cpu_temp": safe(read_cpu_temp),
            "uptime": safe(read_uptime),
            "memory": safe(read_memory),
            "disk": f"{(usage.total - usage.free) / 1e9:.0f} / {usage.total / 1e9:.0f} GB",
        }
    )


# ---------------------------------------------------------------------------
# TTC real-time arrivals, proxied because the feed sends no CORS headers so the
# browser can't call it directly.
#
# Stop IDs were looked up from the routeConfig feed and verified to return live
# predictions. Add a stop by finding its numeric stopId at:
#   https://webservices.umoiq.com/service/publicJSONFeed?command=routeConfig&a=ttc&r=<route>
#
# Only surface routes here, not the subway: TTC's separate next-train API
# returned nothing for every station id tried, so there's no honest way to show
# subway times yet.
# ---------------------------------------------------------------------------
TTC_FEED = "https://webservices.umoiq.com/service/publicJSONFeed"
TTC_STOPS = [
    {"id": 7347, "label": "Spadina at College — northbound"},
    {"id": 8120, "label": "Spadina at College — southbound"},
    {"id": 845, "label": "College at St. George"},
    {"id": 843, "label": "College at Spadina"},
]


def as_list(value):
    """The feed drops the array when there's exactly one of something."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def fetch_ttc_stop(stop):
    resp = requests.get(
        TTC_FEED,
        params={"command": "predictions", "a": "ttc", "stopId": stop["id"]},
        timeout=8,
    )
    resp.raise_for_status()
    data = resp.json()

    routes = []
    for entry in as_list(data.get("predictions")):
        for direction in as_list(entry.get("direction")):
            minutes = [
                int(p["minutes"])
                for p in as_list(direction.get("prediction"))
                if "minutes" in p
            ]
            if not minutes:
                continue
            routes.append(
                {
                    "route": entry.get("routeTag", "?"),
                    "direction": direction.get("title", ""),
                    "minutes": sorted(minutes)[:4],
                }
            )
    routes.sort(key=lambda r: r["minutes"][0])
    return {"label": stop["label"], "routes": routes}


@kiosk.route("/api/ttc", methods=["GET"])
def ttc_arrivals():
    stops = []
    for stop in TTC_STOPS:
        try:
            stops.append(fetch_ttc_stop(stop))
        except Exception:
            current_app.logger.exception("ttc stop %s failed", stop["id"])
            stops.append({"label": stop["label"], "routes": [], "error": True})
    return jsonify({"stops": stops})


@kiosk.route("/api/system/power", methods=["POST"])
def system_power():
    payload = request.get_json(force=True, silent=True) or {}
    action = payload.get("action")
    commands = {"reboot": ["systemctl", "reboot"], "shutdown": ["systemctl", "poweroff"]}
    if action not in commands:
        return jsonify({"error": "action must be 'reboot' or 'shutdown'"}), 400

    current_app.logger.info("system power action requested: %s", action)
    # Popen rather than run() so the response reaches the browser before
    # systemd starts tearing the session down.
    subprocess.Popen(commands[action])
    return jsonify({"action": action})
