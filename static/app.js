(() => {
  const faceWrap = document.getElementById("faceWrap");
  const captionText = document.getElementById("captionText");
  const textInput = document.getElementById("textInput");
  const sendBtn = document.getElementById("sendBtn");
  const micBtn = document.getElementById("micBtn");
  const ttsToggle = document.getElementById("ttsToggle");
  const ttsVoiceSelect = document.getElementById("ttsVoiceSelect");
  const ttsEngineDot = document.getElementById("ttsEngineDot");

  const MAX_HISTORY = 20;
  let history = [];
  let ttsEnabled = localStorage.getItem("ttsEnabled") !== "false";
  let ttsEnginePreference = "gemini";
  let isRecording = false;
  let mediaRecorder = null;
  let audioChunks = [];
  let busy = false; // true while waiting on chat or transcription
  let currentAudio = null;
  let currentAudioUrl = null;
  const mouthEl = document.querySelector(".mouth");
  const mouthSmileEl = document.querySelector(".mouthSmile");

  // ---------- Audio-reactive mouth ----------

  let audioCtx = null;
  let analyser = null;
  let freqData = null;
  let mouthRafId = null;
  let mouthLevel = 0;

  function getAudioContext() {
    if (!audioCtx) {
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
    if (audioCtx.state === "suspended") {
      audioCtx.resume();
    }
    return audioCtx;
  }
  document.getElementById("app").addEventListener("pointerdown", getAudioContext, {
    once: true,
  });

  // Release the shared audio session (and whatever hold it has on the
  // system's audio device) whenever we're not actively speaking or
  // recording, instead of leaving it running in the background at all times.
  function maybeSuspendAudioContext() {
    if (audioCtx && audioCtx.state === "running" && !currentAudio && !isRecording) {
      audioCtx.suspend();
    }
  }

  function attachAnalyser(audioEl) {
    try {
      const ctx = getAudioContext();
      const source = ctx.createMediaElementSource(audioEl);
      analyser = ctx.createAnalyser();
      analyser.fftSize = 256;
      analyser.smoothingTimeConstant = 0.7;
      freqData = new Uint8Array(analyser.frequencyBinCount);
      source.connect(analyser);
      analyser.connect(ctx.destination);
    } catch (err) {
      console.error("audio analyser unavailable", err);
      analyser = null;
    }
  }

  function animateMouth() {
    if (analyser) {
      analyser.getByteFrequencyData(freqData);
      // Speech energy concentrates in the lower bins; average those for a
      // volume-like signal instead of a fixed loop unrelated to the audio.
      const bandEnd = Math.min(48, freqData.length);
      let sum = 0;
      for (let i = 0; i < bandEnd; i++) sum += freqData[i];
      const level = sum / bandEnd / 255;
      mouthLevel = mouthLevel * 0.55 + level * 0.45;
      // Require a genuinely loud peak to reach full openness, and compress
      // the curve (exponent > 1) so typical speech shows graduated movement
      // instead of clipping to the max shape most of the time.
      const normalized = Math.min(mouthLevel / 0.6, 1);
      const eased = Math.pow(normalized, 1.8);
      const scale = 0.25 + eased * 2.0;
      const transform = `scaleY(${scale.toFixed(3)})`;
      // Applied to both mouth shapes since either the flat mouth or the
      // smile may be the visible one depending on the current expression -
      // the invisible one's transform simply has no visual effect.
      mouthEl.style.transform = transform;
      mouthSmileEl.style.transform = transform;
    }
    mouthRafId = requestAnimationFrame(animateMouth);
  }

  function startMouthVisualizer() {
    if (mouthRafId === null) animateMouth();
  }

  function stopMouthVisualizer() {
    if (mouthRafId !== null) {
      cancelAnimationFrame(mouthRafId);
      mouthRafId = null;
    }
    mouthLevel = 0;
    mouthEl.style.transform = "";
    mouthSmileEl.style.transform = "";
  }

  function setFaceState(state) {
    faceWrap.dataset.state = state;
  }

  function setExpression(expression) {
    faceWrap.dataset.expression = expression || "neutral";
  }

  function setCaption(text) {
    captionText.textContent = text;
  }

  function updateTtsButton() {
    ttsToggle.textContent = ttsEnabled ? "🔊" : "🔇";
    ttsToggle.classList.toggle("muted", !ttsEnabled);
  }

  function stopSpeaking() {
    stopMouthVisualizer();
    if (currentAudio) {
      currentAudio.pause();
      currentAudio.src = "";
      currentAudio = null;
    }
    if (currentAudioUrl) {
      URL.revokeObjectURL(currentAudioUrl);
      currentAudioUrl = null;
    }
    maybeSuspendAudioContext();
  }

  ttsToggle.addEventListener("click", () => {
    ttsEnabled = !ttsEnabled;
    localStorage.setItem("ttsEnabled", String(ttsEnabled));
    updateTtsButton();
    if (!ttsEnabled) {
      stopSpeaking();
      if (faceWrap.dataset.state === "speaking") setFaceState("idle");
    }
  });
  updateTtsButton();

  // ---------- TTS voice picker ----------

  function setEngineDot(usedFallback) {
    ttsEngineDot.classList.toggle("fallback", usedFallback);
  }

  async function loadTtsVoices() {
    try {
      const resp = await fetch("/api/tts/voices");
      const data = await resp.json();
      ttsVoiceSelect.innerHTML = "";
      for (const voice of data.voices) {
        const option = document.createElement("option");
        option.value = voice.id;
        option.textContent = voice.label;
        ttsVoiceSelect.appendChild(option);
      }
      ttsEnginePreference = data.selected;
      ttsVoiceSelect.value = ttsEnginePreference;
    } catch (err) {
      console.error("failed to load tts voices", err);
    }
  }
  loadTtsVoices();

  ttsVoiceSelect.addEventListener("change", async () => {
    const next = ttsVoiceSelect.value;
    try {
      const resp = await fetch("/api/tts/voice", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ voice: next }),
      });
      const data = await resp.json();
      ttsEnginePreference = data.voice;
      setEngineDot(false);
    } catch (err) {
      console.error("failed to switch tts voice", err);
    }
  });

  // ---------- Speech (server-side TTS) ----------

  async function speak(text) {
    if (!text) {
      setFaceState("idle");
      return;
    }
    await playSpeech(() =>
      fetch("/api/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      })
    );
  }

  // Ruby's fixed lines - the ones she says on a button press rather than in
  // reply to something - come from a recording the server made once and kept,
  // so tapping Follow me doesn't spend Gemini TTS tokens reading the same
  // sentence out again every time. The wording lives server-side in
  // kiosk_api.CANNED_PHRASES; only the key travels from here, which is what
  // stops what she says and what was recorded from drifting apart.
  async function speakPhrase(key) {
    await playSpeech(() => fetch("/api/tts/phrase/" + encodeURIComponent(key)));
  }

  // Shared by both: fetch some WAV, play it, and drive the face while it does.
  async function playSpeech(request) {
    if (!ttsEnabled) {
      setFaceState("idle");
      return;
    }
    stopSpeaking();

    try {
      const resp = await request();
      if (!resp.ok) {
        const data = await resp.json().catch(() => ({}));
        throw new Error(data.error || "tts request failed");
      }
      const engineUsed = resp.headers.get("X-TTS-Engine");
      if (engineUsed) setEngineDot(engineUsed !== ttsEnginePreference);
      const blob = await resp.blob();
      currentAudioUrl = URL.createObjectURL(blob);
      currentAudio = new Audio(currentAudioUrl);
      attachAnalyser(currentAudio);
      currentAudio.addEventListener("play", () => {
        setFaceState("speaking");
        startMouthVisualizer();
      });
      currentAudio.addEventListener("ended", () => {
        setFaceState("idle");
        stopSpeaking();
      });
      currentAudio.addEventListener("error", () => {
        setFaceState("idle");
        stopSpeaking();
      });
      await currentAudio.play();
    } catch (err) {
      console.error("speech playback failed:", err);
      setFaceState("error");
      setTimeout(() => setFaceState("idle"), 1200);
    }
  }

  // ---------- Chat ----------

  function pushHistory(role, text) {
    history.push({ role, text });
    if (history.length > MAX_HISTORY) history = history.slice(-MAX_HISTORY);
  }

  async function sendMessage(rawText) {
    const text = (rawText || "").trim();
    if (!text || busy) return;

    busy = true;
    textInput.value = "";
    pushHistory("user", text);
    setCaption(text);
    setFaceState("thinking");
    setExpression("neutral");

    try {
      const resp = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ history }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "request failed");

      pushHistory("model", data.reply);
      setCaption(data.reply);
      setExpression(data.emotion);
      speak(data.reply);
    } catch (err) {
      console.error(err);
      setCaption("Sorry, I ran into a problem reaching the assistant.");
      setFaceState("error");
      setTimeout(() => setFaceState("idle"), 2000);
    } finally {
      busy = false;
    }
  }

  sendBtn.addEventListener("click", () => sendMessage(textInput.value));
  textInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendMessage(textInput.value);
  });

  // ---------- Microphone ----------

  // Voice-activity detection: auto-stops recording after this much silence
  // following detected speech, so you don't have to tap the mic button
  // again. Raise SILENCE_RMS_THRESHOLD if it cuts off too early in a noisy
  // room, lower it if it never detects silence. MAX_RECORDING_MS is a safety
  // cap in case silence is never detected at all.
  const SILENCE_RMS_THRESHOLD = 0.02;
  const SILENCE_DURATION_MS = 1200;
  const MAX_RECORDING_MS = 20000;

  let micSource = null;
  let micAnalyser = null;
  let micTimeData = null;
  let micRafId = null;
  let micSpeechDetected = false;
  let micSilenceStart = null;
  let micRecordingStart = 0;

  function startSilenceDetection(stream) {
    const ctx = getAudioContext();
    micSource = ctx.createMediaStreamSource(stream);
    micAnalyser = ctx.createAnalyser();
    micAnalyser.fftSize = 512;
    micTimeData = new Uint8Array(micAnalyser.fftSize);
    // Intentionally not connected onward to ctx.destination - we only want
    // to measure the mic level, not play it back through the speakers.
    micSource.connect(micAnalyser);

    micSpeechDetected = false;
    micSilenceStart = null;
    micRecordingStart = performance.now();
    monitorSilence();
  }

  function monitorSilence() {
    if (!micAnalyser) return;
    micAnalyser.getByteTimeDomainData(micTimeData);

    let sumSquares = 0;
    for (let i = 0; i < micTimeData.length; i++) {
      const normalized = (micTimeData[i] - 128) / 128;
      sumSquares += normalized * normalized;
    }
    const rms = Math.sqrt(sumSquares / micTimeData.length);
    const now = performance.now();

    if (rms > SILENCE_RMS_THRESHOLD) {
      micSpeechDetected = true;
      micSilenceStart = null;
    } else if (micSpeechDetected && micSilenceStart === null) {
      micSilenceStart = now;
    }

    const silentLongEnough =
      micSilenceStart !== null && now - micSilenceStart > SILENCE_DURATION_MS;
    const tookTooLong = now - micRecordingStart > MAX_RECORDING_MS;

    if (silentLongEnough || tookTooLong) {
      stopRecording();
      return;
    }

    micRafId = requestAnimationFrame(monitorSilence);
  }

  function stopSilenceDetection() {
    if (micRafId !== null) {
      cancelAnimationFrame(micRafId);
      micRafId = null;
    }
    if (micSource) {
      micSource.disconnect();
      micSource = null;
    }
    if (micAnalyser) {
      micAnalyser.disconnect();
    }
    micAnalyser = null;
    micTimeData = null;
  }

  function pickAudioMimeType() {
    const candidates = [
      "audio/ogg;codecs=opus",
      "audio/ogg",
      "audio/webm;codecs=opus",
      "audio/webm",
    ];
    return candidates.find((t) => MediaRecorder.isTypeSupported(t)) || "";
  }

  async function startRecording() {
    if (busy) return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mimeType = pickAudioMimeType();
      mediaRecorder = mimeType
        ? new MediaRecorder(stream, { mimeType })
        : new MediaRecorder(stream);
      audioChunks = [];

      mediaRecorder.addEventListener("dataavailable", (e) => {
        if (e.data.size > 0) audioChunks.push(e.data);
      });
      mediaRecorder.addEventListener("stop", () => {
        stream.getTracks().forEach((t) => t.stop());
        handleRecordingStopped(mediaRecorder.mimeType || mimeType);
      });

      mediaRecorder.start();
      isRecording = true;
      micBtn.classList.add("recording");
      setFaceState("listening");
      setCaption("Listening…");
      startSilenceDetection(stream);
    } catch (err) {
      console.error(err);
      setCaption("Couldn't access the microphone.");
      setFaceState("error");
      setTimeout(() => setFaceState("idle"), 2000);
    }
  }

  function stopRecording() {
    stopSilenceDetection();
    if (mediaRecorder && isRecording) {
      mediaRecorder.stop();
    }
    isRecording = false;
    micBtn.classList.remove("recording");
    maybeSuspendAudioContext();
  }

  async function handleRecordingStopped(mimeType) {
    if (!audioChunks.length) {
      setFaceState("idle");
      return;
    }
    busy = true;
    setFaceState("thinking");
    setCaption("Transcribing…");

    try {
      const blob = new Blob(audioChunks, { type: mimeType || "audio/ogg" });
      const form = new FormData();
      form.append("audio", blob, "speech");

      const resp = await fetch("/api/transcribe", { method: "POST", body: form });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "transcription failed");

      const text = (data.text || "").trim();
      if (!text) {
        setCaption("I didn't catch that. Try again?");
        setFaceState("idle");
        busy = false;
        return;
      }
      busy = false;
      await sendMessage(text);
    } catch (err) {
      console.error(err);
      setCaption("Sorry, I couldn't transcribe that.");
      setFaceState("error");
      setTimeout(() => setFaceState("idle"), 2000);
      busy = false;
    }
  }

  micBtn.addEventListener("click", () => {
    if (isRecording) {
      stopRecording();
    } else {
      startRecording();
    }
  });

  // ---------- Follow me ----------
  //
  // Turns the robot's camera into a tracker: the server detects the largest
  // face and drives to keep it centred and at arm's length (face_follow.py).
  // Everything here is one POST to toggle it plus a poll, because the robot
  // can stop following without anybody touching this screen - a motor
  // protection trip switches it off, and the follower stops the motors by
  // itself the moment it loses the face for a second.
  //
  // /robot/follow is localhost-only server-side: this button is the only way
  // to start it, deliberately. The remote driving page can only see that
  // it's running and press Stop.

  const followWrap = document.getElementById("followWrap");
  const followBtn = document.getElementById("followBtn");
  const followMenu = document.getElementById("followMenu");
  const followPreview = document.getElementById("followPreview");
  const followPreviewImg = document.getElementById("followPreviewImg");

  const FOLLOW_POLL_MS = 1500;
  let followEnabled = false;
  let followMode = null;
  let followBusy = false;

  // The preview is a live MJPEG connection, so it's opened only while follow
  // is actually on and closed the moment it isn't - including when you walk
  // off into the Apps launcher, where this screen isn't even visible. Left
  // open it would keep the Pi encoding and sending frames to a hidden
  // element for as long as the tab lives.
  function startPreview() {
    followPreview.hidden = false;
    // Cache-buster: an <img> re-shown with the same src can reuse the dead
    // connection from last time rather than opening a fresh stream.
    followPreviewImg.src = "/robot/video_feed?t=" + Date.now();
  }

  function stopPreview() {
    followPreview.hidden = true;
    followPreviewImg.removeAttribute("src");
  }

  // No camera, or no face model on disk - hide the preview but leave follow
  // running, since the tracking itself is the server's job and doesn't need
  // this picture. Better a missing thumbnail than a broken-image icon.
  followPreviewImg.addEventListener("error", () => {
    followPreview.hidden = true;
  });

  // What each mode is called on screen and how Ruby announces it. Keyed by the
  // same strings face_follow.MODES uses, so the wire value and the label can't
  // drift apart. `phrase` is a key into kiosk_api.CANNED_PHRASES rather than
  // the sentence itself - she says these several times a day and they never
  // change, so they're played from a recording instead of re-synthesized (see
  // speakPhrase). The caption is separate because it goes on to say what to
  // tap to stop, which is worth reading but not worth listening to.
  const FOLLOW_MODES = {
    body: {
      label: "Following",
      phrase: "follow-body",
      caption: "Okay, I'll follow you! Tap Following to stop.",
    },
    head: {
      label: "Watching",
      phrase: "follow-head",
      caption: "I'll turn my head to watch you. Tap Watching to stop.",
    },
  };

  function setMenuOpen(open) {
    followMenu.hidden = !open;
    followBtn.setAttribute("aria-expanded", String(open));
  }

  function setFollowUI(enabled, mode, { error = false } = {}) {
    followEnabled = enabled;
    followMode = enabled ? mode : null;
    followBtn.setAttribute("aria-pressed", String(enabled));
    followBtn.classList.toggle("error", error);
    followBtn.querySelector(".pillLabel").textContent =
      enabled ? (FOLLOW_MODES[mode] || FOLLOW_MODES.body).label : "Follow me";
    // Head mode moves the camera, so the preview is arguably more useful there
    // than in body mode - show it for both.
    if (enabled) startPreview();
    else stopPreview();
    if (enabled) setMenuOpen(false);
  }

  // Pressing the button while stopped only opens the chooser; nothing moves
  // until a mode is picked. While running it's a plain stop.
  function onFollowButton() {
    if (followEnabled) {
      setMenuOpen(false);
      requestFollow(false, null);
    } else {
      setMenuOpen(followMenu.hidden);
    }
  }

  async function requestFollow(enabled, mode) {
    if (followBusy) return;
    followBusy = true;
    try {
      const resp = await fetch("/robot/follow", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(enabled ? { enabled: true, mode } : { enabled: false }),
      });
      const data = await resp.json();
      if (!resp.ok || !data.ok) throw new Error(data.error || "follow request failed");

      setFollowUI(data.enabled, data.mode);
      if (data.enabled) {
        const copy = FOLLOW_MODES[data.mode] || FOLLOW_MODES.body;
        setExpression("happy");
        speakPhrase(copy.phrase);
        setCaption(copy.caption);
      } else {
        setExpression("neutral");
        setCaption("Stopped following.");
      }
    } catch (err) {
      console.error("follow request failed", err);
      setFollowUI(false, null, { error: true });
      setCaption("I couldn't start following - my camera might not be connected.");
    } finally {
      followBusy = false;
    }
  }

  followBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    onFollowButton();
  });

  followMenu.querySelectorAll(".followOpt").forEach((opt) => {
    opt.addEventListener("click", (e) => {
      e.stopPropagation();
      setMenuOpen(false);
      requestFollow(true, opt.dataset.mode);
    });
  });

  // Tap anywhere else to dismiss. Without this the menu would be a trap on a
  // touchscreen with no Escape key and nothing else to click.
  document.addEventListener("click", () => {
    if (!followMenu.hidden) setMenuOpen(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !followMenu.hidden) setMenuOpen(false);
  });

  // Polled rather than trusted from the toggle alone: the server turns follow
  // off on its own after a motor-protection trip, and this screen has to
  // notice that without anyone touching it.
  async function pollFollowStatus() {
    try {
      const resp = await fetch("/robot/follow_status");
      const data = await resp.json();

      followWrap.hidden = !data.available;
      if (!data.available) {
        setMenuOpen(false);
        return;
      }

      if (data.enabled === followEnabled && data.mode === followMode) return;

      // Stopped by the server rather than by this button - say why, once, on
      // the transition. disabled_reason lingers server-side until the next
      // start(), so reacting to it on every poll would pin a stale message
      // to the screen long after the fact.
      const stoppedByServer = followEnabled && !data.enabled;
      setFollowUI(data.enabled, data.mode, {
        error: stoppedByServer && !!data.disabled_reason,
      });
      if (stoppedByServer && data.disabled_reason) {
        setExpression("neutral");
        setCaption(`I had to stop following: ${data.disabled_reason.toLowerCase()}.`);
      }
    } catch (err) {
      // The kiosk and the robot share one process, so this failing means the
      // whole server is down - app.js's other calls will report that loudly
      // enough without this poll piling on every 1.5 seconds.
    }
  }

  pollFollowStatus();
  setInterval(pollFollowStatus, FOLLOW_POLL_MS);

  // ---------- Motor current ----------
  //
  // Measured on the ESP32's sense pins and pushed up the serial link at 5Hz;
  // this just reads whatever the last reading was (/robot/motor_current
  // touches no hardware, so polling it costs nothing but the request).
  //
  // Polled at 1Hz rather than the firmware's 5Hz on purpose. This is a
  // readout on a screen somebody glances at, not a control loop - five
  // requests a second would be four wasted, and the number would flicker too
  // fast to actually read. Nothing here needs to be fast: the thing that has
  // to react quickly to a current spike is the protection logic on the ESP32
  // itself, which never involves this screen at all.

  const motorCurrentEl = document.getElementById("motorCurrent");
  const mcRowM1 = document.getElementById("mcRowM1");
  const mcRowM2 = document.getElementById("mcRowM2");
  const mcRowCpu = document.getElementById("mcRowCpu");
  const mcM1 = document.getElementById("mcM1");
  const mcM2 = document.getElementById("mcM2");
  const mcCpu = document.getElementById("mcCpu");
  const STATS_POLL_MS = 1000;

  function formatAmps(value) {
    return typeof value === "number" && isFinite(value) ? `${value.toFixed(2)} A` : "—";
  }

  // Both readouts on one timer, fetched together and rendered once, rather
  // than two independent pollers writing into the same block - otherwise the
  // two halves update on separate ticks and the block can be mid-way through
  // appearing while the other half still says nothing.
  async function pollStats() {
    const [current, cpu] = await Promise.all([
      fetch("/robot/motor_current").then((r) => r.json()).catch(() => null),
      fetch("/robot/cpu_status").then((r) => r.json()).catch(() => null),
    ]);

    // Missing readings hide their own row rather than freezing: a number that
    // has silently stopped updating is worse than no number, because nothing
    // about it looks wrong. The ESP32 half goes away when it is unplugged or
    // has stopped answering; CPU only when the server itself is unreachable.
    const haveCurrent = !!(current && current.available);
    const haveCpu = !!(cpu && cpu.available);

    mcRowM1.hidden = !haveCurrent;
    mcRowM2.hidden = !haveCurrent;
    mcRowCpu.hidden = !haveCpu;
    motorCurrentEl.hidden = !(haveCurrent || haveCpu);

    if (haveCurrent) {
      mcM1.textContent = formatAmps(current.m1);
      mcM2.textContent = formatAmps(current.m2);
    }
    if (haveCpu) mcCpu.textContent = `${cpu.percent.toFixed(1)}%`;
  }

  pollStats();
  setInterval(pollStats, STATS_POLL_MS);

  // Leaving for kiosk mode: drop the mic and cut any reply off mid-sentence,
  // rather than leaving Ruby talking to an empty room while you browse.
  // Fired by kiosk.js (see static/kiosk.js).
  //
  // Follow mode itself deliberately keeps running - you might well open the
  // Apps launcher while the robot walks you somewhere - but its preview
  // stream is dropped, since this screen is hidden behind the launcher.
  document.addEventListener("kiosk:enter", () => {
    if (isRecording) stopRecording();
    stopSpeaking();
    setFaceState("idle");
    stopPreview();
  });

  document.addEventListener("kiosk:exit", () => {
    if (followEnabled) startPreview();
  });
})();
