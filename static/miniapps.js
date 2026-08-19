// ===========================================================================
// Built-in kiosk mini-apps. Each one is { mount(root), unmount() } and gets a
// full screen to itself; kiosk.js handles navigation and the exit pill.
//
// Anything a mini-app starts (intervals, ringing alarms, canvas listeners)
// must be torn down in unmount() - mount() is called again on every visit.
// The one deliberate exception is the radio, whose <audio> lives on
// document.body so a station keeps playing while you use the rest of the
// kiosk.
// ===========================================================================
window.KioskMiniApps = (() => {
  // ---------- shared helpers ----------

  function h(html) {
    const t = document.createElement("template");
    t.innerHTML = html.trim();
    return t.content.firstElementChild;
  }

  function pad(n) {
    return String(n).padStart(2, "0");
  }

  // Tabs: every .miniTab controls the .miniPane with the matching data-pane.
  function wireTabs(root) {
    const tabs = [...root.querySelectorAll(".miniTab")];
    const panes = [...root.querySelectorAll(".miniPane")];
    for (const tab of tabs) {
      tab.addEventListener("click", () => {
        for (const t of tabs) t.setAttribute("aria-selected", String(t === tab));
        for (const p of panes) p.hidden = p.dataset.pane !== tab.dataset.pane;
      });
    }
  }

  // Short square-wave-ish blip for timers and alarms. Its own AudioContext so
  // it never fights the lip-sync analyser in app.js.
  let beepCtx = null;
  function beep(duration = 0.18, freq = 880) {
    try {
      if (!beepCtx) beepCtx = new (window.AudioContext || window.webkitAudioContext)();
      if (beepCtx.state === "suspended") beepCtx.resume();
      const osc = beepCtx.createOscillator();
      const gain = beepCtx.createGain();
      osc.type = "triangle";
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, beepCtx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.35, beepCtx.currentTime + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, beepCtx.currentTime + duration);
      osc.connect(gain);
      gain.connect(beepCtx.destination);
      osc.start();
      osc.stop(beepCtx.currentTime + duration + 0.02);
    } catch (err) {
      console.error("beep failed", err);
    }
  }

  // =========================================================================
  // Clock: time, countdown timer, stopwatch, alarms
  // =========================================================================
  const clock = (() => {
    const ALARMS_KEY = "kioskAlarms";
    let tickTimer = null;
    let swTimer = null;
    let ringTimer = null;

    function loadAlarms() {
      try {
        return JSON.parse(localStorage.getItem(ALARMS_KEY) || "[]");
      } catch (err) {
        return [];
      }
    }

    function saveAlarms(alarms) {
      localStorage.setItem(ALARMS_KEY, JSON.stringify(alarms));
    }

    function mount(root) {
      root.appendChild(
        h(`
        <div>
          <div class="miniTabs">
            <button class="miniTab" data-pane="clock" aria-selected="true">Clock</button>
            <button class="miniTab" data-pane="timer" aria-selected="false">Timer</button>
            <button class="miniTab" data-pane="stopwatch" aria-selected="false">Stopwatch</button>
            <button class="miniTab" data-pane="alarms" aria-selected="false">Alarms</button>
          </div>

          <section class="miniPane" data-pane="clock">
            <div class="hugeReadout" id="ckNow">--:--</div>
            <div class="subReadout" id="ckDate"></div>
          </section>

          <section class="miniPane" data-pane="timer" hidden>
            <div class="hugeReadout" id="tmReadout">00:00</div>
            <div class="chipRow">
              <button class="chip" data-add="60">+1 min</button>
              <button class="chip" data-add="300">+5 min</button>
              <button class="chip" data-add="600">+10 min</button>
              <button class="chip" data-set="180">3 min</button>
              <button class="chip" data-set="900">15 min</button>
            </div>
            <div class="btnRow" style="justify-content:center">
              <button class="bigBtn primary" id="tmStart">Start</button>
              <button class="bigBtn" id="tmReset">Reset</button>
            </div>
          </section>

          <section class="miniPane" data-pane="stopwatch" hidden>
            <div class="hugeReadout" id="swReadout">00:00.0</div>
            <div class="btnRow" style="justify-content:center">
              <button class="bigBtn primary" id="swStart">Start</button>
              <button class="bigBtn" id="swReset">Reset</button>
            </div>
          </section>

          <section class="miniPane" data-pane="alarms" hidden>
            <div class="btnRow" style="margin-bottom:20px">
              <input type="time" class="timeInput" id="alTime" value="07:00" />
              <button class="bigBtn primary" id="alAdd">Add alarm</button>
            </div>
            <div class="card" id="alList"></div>
          </section>
        </div>
      `)
      );
      wireTabs(root);

      const ckNow = root.querySelector("#ckNow");
      const ckDate = root.querySelector("#ckDate");
      const tmReadout = root.querySelector("#tmReadout");
      const tmStart = root.querySelector("#tmStart");
      const swReadout = root.querySelector("#swReadout");
      const swStart = root.querySelector("#swStart");
      const alList = root.querySelector("#alList");

      // --- timer ---
      let tmRemaining = 0; // seconds
      let tmRunning = false;

      function renderTimer() {
        const m = Math.floor(Math.max(tmRemaining, 0) / 60);
        const s = Math.max(tmRemaining, 0) % 60;
        tmReadout.textContent = `${pad(m)}:${pad(s)}`;
        tmStart.textContent = tmRunning ? "Pause" : "Start";
      }

      root.querySelectorAll("[data-add]").forEach((b) =>
        b.addEventListener("click", () => {
          tmRemaining += Number(b.dataset.add);
          renderTimer();
        })
      );
      root.querySelectorAll("[data-set]").forEach((b) =>
        b.addEventListener("click", () => {
          tmRemaining = Number(b.dataset.set);
          renderTimer();
        })
      );
      tmStart.addEventListener("click", () => {
        if (tmRemaining <= 0) return;
        tmRunning = !tmRunning;
        renderTimer();
      });
      root.querySelector("#tmReset").addEventListener("click", () => {
        tmRunning = false;
        tmRemaining = 0;
        stopRinging();
        renderTimer();
      });

      // --- stopwatch ---
      let swElapsed = 0;
      let swStartedAt = 0;
      let swRunning = false;

      function renderStopwatch() {
        const total = swRunning ? swElapsed + (performance.now() - swStartedAt) : swElapsed;
        const m = Math.floor(total / 60000);
        const s = Math.floor((total % 60000) / 1000);
        const tenths = Math.floor((total % 1000) / 100);
        swReadout.textContent = `${pad(m)}:${pad(s)}.${tenths}`;
      }

      swStart.addEventListener("click", () => {
        if (swRunning) {
          swElapsed += performance.now() - swStartedAt;
          swRunning = false;
          clearInterval(swTimer);
          swTimer = null;
        } else {
          swStartedAt = performance.now();
          swRunning = true;
          swTimer = setInterval(renderStopwatch, 100);
        }
        swStart.textContent = swRunning ? "Stop" : "Start";
        renderStopwatch();
      });
      root.querySelector("#swReset").addEventListener("click", () => {
        swRunning = false;
        swElapsed = 0;
        clearInterval(swTimer);
        swTimer = null;
        swStart.textContent = "Start";
        renderStopwatch();
      });

      // --- ringing (shared by timer and alarms) ---
      function startRinging(label) {
        stopRinging();
        tmReadout.classList.add("ringing");
        beep(0.25, 990);
        ringTimer = setInterval(() => beep(0.25, 990), 900);
        const banner = h(
          `<div class="btnRow" id="ringBanner" style="justify-content:center;margin-top:18px">
             <button class="bigBtn danger">${label} — tap to stop</button>
           </div>`
        );
        banner.querySelector("button").addEventListener("click", stopRinging);
        root.firstElementChild.appendChild(banner);
      }

      function stopRinging() {
        if (ringTimer !== null) {
          clearInterval(ringTimer);
          ringTimer = null;
        }
        tmReadout.classList.remove("ringing");
        const banner = root.querySelector("#ringBanner");
        if (banner) banner.remove();
      }

      // --- alarms ---
      let alarms = loadAlarms();
      let lastFiredMinute = "";

      function renderAlarms() {
        alList.innerHTML = "";
        if (!alarms.length) {
          alList.appendChild(h(`<div class="wxMeta">No alarms set.</div>`));
          return;
        }
        alarms.forEach((alarm, i) => {
          const row = h(`
            <div class="alarmRow ${alarm.on ? "" : "off"}">
              <time>${alarm.time}</time>
              <button class="chip">${alarm.on ? "On" : "Off"}</button>
              <button class="chip">Delete</button>
            </div>
          `);
          const [toggleBtn, deleteBtn] = row.querySelectorAll("button");
          toggleBtn.addEventListener("click", () => {
            alarms[i].on = !alarms[i].on;
            saveAlarms(alarms);
            renderAlarms();
          });
          deleteBtn.addEventListener("click", () => {
            alarms.splice(i, 1);
            saveAlarms(alarms);
            renderAlarms();
          });
          alList.appendChild(row);
        });
      }

      root.querySelector("#alAdd").addEventListener("click", () => {
        const time = root.querySelector("#alTime").value;
        if (!time) return;
        alarms.push({ time, on: true });
        alarms.sort((a, b) => a.time.localeCompare(b.time));
        saveAlarms(alarms);
        renderAlarms();
      });

      // --- one ticker drives clock, timer and alarm checks ---
      function tick() {
        const now = new Date();
        ckNow.textContent = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        ckDate.textContent = now.toLocaleDateString([], {
          weekday: "long",
          day: "numeric",
          month: "long",
          year: "numeric",
        });

        if (tmRunning) {
          tmRemaining -= 1;
          if (tmRemaining <= 0) {
            tmRemaining = 0;
            tmRunning = false;
            startRinging("Timer finished");
          }
          renderTimer();
        }

        const hhmm = `${pad(now.getHours())}:${pad(now.getMinutes())}`;
        if (hhmm !== lastFiredMinute && alarms.some((a) => a.on && a.time === hhmm)) {
          lastFiredMinute = hhmm;
          startRinging(`Alarm ${hhmm}`);
        }
      }

      renderTimer();
      renderStopwatch();
      renderAlarms();
      tick();
      tickTimer = setInterval(tick, 1000);
    }

    function unmount() {
      clearInterval(tickTimer);
      clearInterval(swTimer);
      clearInterval(ringTimer);
      tickTimer = swTimer = ringTimer = null;
    }

    return { mount, unmount };
  })();

  // =========================================================================
  // Weather (Open-Meteo - no API key, no signup, CORS-friendly)
  // =========================================================================
  const weather = (() => {
    const LOC_KEY = "kioskWeatherLoc";
    // Falls back to the Pi's own timezone city until you search for another.
    const DEFAULT_LOC = { name: "Toronto", latitude: 43.6532, longitude: -79.3832 };

    // WMO weather interpretation codes -> [emoji, description]
    const WMO = {
      0: ["☀️", "Clear"], 1: ["🌤️", "Mainly clear"], 2: ["⛅", "Partly cloudy"],
      3: ["☁️", "Overcast"], 45: ["🌫️", "Fog"], 48: ["🌫️", "Rime fog"],
      51: ["🌦️", "Light drizzle"], 53: ["🌦️", "Drizzle"], 55: ["🌦️", "Heavy drizzle"],
      56: ["🌧️", "Freezing drizzle"], 57: ["🌧️", "Freezing drizzle"],
      61: ["🌧️", "Light rain"], 63: ["🌧️", "Rain"], 65: ["🌧️", "Heavy rain"],
      66: ["🌧️", "Freezing rain"], 67: ["🌧️", "Freezing rain"],
      71: ["🌨️", "Light snow"], 73: ["🌨️", "Snow"], 75: ["❄️", "Heavy snow"],
      77: ["❄️", "Snow grains"], 80: ["🌦️", "Showers"], 81: ["🌦️", "Showers"],
      82: ["⛈️", "Violent showers"], 85: ["🌨️", "Snow showers"], 86: ["🌨️", "Snow showers"],
      95: ["⛈️", "Thunderstorm"], 96: ["⛈️", "Thunderstorm, hail"], 99: ["⛈️", "Thunderstorm, hail"],
    };

    function describe(code) {
      return WMO[code] || ["❔", "Unknown"];
    }

    function getLoc() {
      try {
        return JSON.parse(localStorage.getItem(LOC_KEY)) || DEFAULT_LOC;
      } catch (err) {
        return DEFAULT_LOC;
      }
    }

    function mount(root) {
      root.appendChild(
        h(`
        <div>
          <h1 class="miniTitle" id="wxPlace">Weather</h1>
          <div id="wxBody"><div class="wxMeta">Loading…</div></div>
          <div class="btnRow" style="margin-top:26px">
            <input id="wxSearch" class="timeInput" style="font-size:1.05rem;flex:1 1 240px"
                   placeholder="Change location…" />
            <button class="bigBtn" id="wxGo">Search</button>
          </div>
          <div class="chipRow" id="wxResults" style="justify-content:flex-start"></div>
        </div>
      `)
      );

      const body = root.querySelector("#wxBody");
      const place = root.querySelector("#wxPlace");
      const results = root.querySelector("#wxResults");

      async function load() {
        const loc = getLoc();
        place.textContent = loc.name;
        body.innerHTML = `<div class="wxMeta">Loading…</div>`;
        try {
          const url =
            `https://api.open-meteo.com/v1/forecast?latitude=${loc.latitude}` +
            `&longitude=${loc.longitude}` +
            `&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m` +
            `&hourly=temperature_2m,weather_code` +
            `&daily=weather_code,temperature_2m_max,temperature_2m_min` +
            `&timezone=auto&forecast_days=4`;
          const resp = await fetch(url);
          if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
          const d = await resp.json();
          render(d);
        } catch (err) {
          console.error("weather failed", err);
          body.innerHTML = `<div class="wxMeta">Couldn't reach the weather service.</div>`;
        }
      }

      function render(d) {
        const [icon, desc] = describe(d.current.weather_code);
        const unit = d.current_units.temperature_2m;

        // Hourly strip starts at the current hour, not midnight.
        const startIdx = Math.max(
          d.hourly.time.findIndex((t) => new Date(t) > new Date()) - 1,
          0
        );
        const hours = d.hourly.time.slice(startIdx, startIdx + 12).map((t, i) => {
          const j = startIdx + i;
          const [hIcon] = describe(d.hourly.weather_code[j]);
          return `<div class="wxCell">
              <div class="l">${new Date(t).toLocaleTimeString([], { hour: "numeric" })}</div>
              <div class="i">${hIcon}</div>
              <div class="t">${Math.round(d.hourly.temperature_2m[j])}°</div>
            </div>`;
        });

        const days = d.daily.time.map((t, i) => {
          const [dIcon] = describe(d.daily.weather_code[i]);
          return `<div class="wxCell">
              <div class="l">${i === 0 ? "Today" : new Date(t).toLocaleDateString([], { weekday: "short" })}</div>
              <div class="i">${dIcon}</div>
              <div class="t">${Math.round(d.daily.temperature_2m_max[i])}° <span class="l">${Math.round(d.daily.temperature_2m_min[i])}°</span></div>
            </div>`;
        });

        body.innerHTML = `
          <div class="wxNow">
            <div class="wxIcon">${icon}</div>
            <div>
              <div class="wxTemp">${Math.round(d.current.temperature_2m)}${unit}</div>
              <div class="wxMeta">${desc}</div>
            </div>
            <div class="wxMeta">
              Feels like ${Math.round(d.current.apparent_temperature)}${unit}<br />
              Humidity ${d.current.relative_humidity_2m}%<br />
              Wind ${Math.round(d.current.wind_speed_10m)} ${d.current_units.wind_speed_10m}
            </div>
          </div>
          <div class="wxStrip">${hours.join("")}</div>
          <div class="wxStrip">${days.join("")}</div>
        `;
      }

      async function search() {
        const q = root.querySelector("#wxSearch").value.trim();
        if (!q) return;
        results.innerHTML = `<span class="wxMeta">Searching…</span>`;
        try {
          const resp = await fetch(
            `https://geocoding-api.open-meteo.com/v1/search?count=5&name=${encodeURIComponent(q)}`
          );
          const d = await resp.json();
          results.innerHTML = "";
          if (!d.results || !d.results.length) {
            results.innerHTML = `<span class="wxMeta">No matches.</span>`;
            return;
          }
          for (const r of d.results) {
            const label = [r.name, r.admin1, r.country_code].filter(Boolean).join(", ");
            const chip = h(`<button class="chip">${label}</button>`);
            chip.addEventListener("click", () => {
              localStorage.setItem(
                LOC_KEY,
                JSON.stringify({ name: r.name, latitude: r.latitude, longitude: r.longitude })
              );
              results.innerHTML = "";
              root.querySelector("#wxSearch").value = "";
              load();
            });
            results.appendChild(chip);
          }
        } catch (err) {
          results.innerHTML = `<span class="wxMeta">Search failed.</span>`;
        }
      }

      root.querySelector("#wxGo").addEventListener("click", search);
      root.querySelector("#wxSearch").addEventListener("keydown", (e) => {
        if (e.key === "Enter") search();
      });

      load();
    }

    return { mount, unmount() {} };
  })();

  // =========================================================================
  // Radio. The <audio> element lives on document.body rather than inside the
  // mini-app, so a station keeps playing when you leave for another app.
  // =========================================================================
  const radio = (() => {
    const VOL_KEY = "kioskRadioVolume";
    const STATIONS = [
      { id: "gs", name: "Groove Salad", genre: "SomaFM · downtempo", url: "https://ice1.somafm.com/groovesalad-128-mp3" },
      { id: "lush", name: "Lush", genre: "SomaFM · vocal chill", url: "https://ice2.somafm.com/lush-128-mp3" },
      { id: "drone", name: "Drone Zone", genre: "SomaFM · ambient", url: "https://ice1.somafm.com/dronezone-128-mp3" },
      { id: "rp", name: "Radio Paradise", genre: "Eclectic mix", url: "https://stream.radioparadise.com/mp3-192" },
      { id: "rprock", name: "Radio Paradise Rock", genre: "Rock", url: "https://stream.radioparadise.com/rock-192" },
      { id: "classic", name: "Classic FM", genre: "Classical", url: "https://media-ssl.musicradio.com/ClassicFMMP3" },
    ];

    let audio = null;
    let playingId = null;

    function getAudio() {
      if (!audio) {
        audio = new Audio();
        audio.preload = "none";
        const saved = Number(localStorage.getItem(VOL_KEY));
        audio.volume = Number.isFinite(saved) && saved > 0 ? saved : 0.8;
        document.body.appendChild(audio);
      }
      return audio;
    }

    function mount(root) {
      root.appendChild(
        h(`
        <div>
          <h1 class="miniTitle">Radio</h1>
          <div class="stationGrid" id="rdGrid"></div>
          <div class="card">
            <div id="rdNow" class="wxMeta" style="margin-bottom:10px">Nothing playing</div>
            <div class="sliderRow" style="margin:0">
              <label for="rdVol">Volume</label>
              <input type="range" class="bigRange" id="rdVol" min="0" max="100" />
              <output id="rdVolOut"></output>
            </div>
            <div class="btnRow" style="margin-top:16px">
              <button class="bigBtn danger" id="rdStop">Stop</button>
            </div>
          </div>
        </div>
      `)
      );

      const grid = root.querySelector("#rdGrid");
      const now = root.querySelector("#rdNow");
      const vol = root.querySelector("#rdVol");
      const volOut = root.querySelector("#rdVolOut");

      function renderNow() {
        const station = STATIONS.find((s) => s.id === playingId);
        now.textContent = station ? `▶ ${station.name} — ${station.genre}` : "Nothing playing";
        grid.querySelectorAll(".station").forEach((btn) => {
          btn.setAttribute("aria-current", String(btn.dataset.id === playingId));
        });
      }

      for (const station of STATIONS) {
        const btn = h(
          `<button class="station" data-id="${station.id}">
             <div class="n">${station.name}</div><div class="g">${station.genre}</div>
           </button>`
        );
        btn.addEventListener("click", async () => {
          const a = getAudio();
          if (playingId === station.id) {
            a.pause();
            playingId = null;
            renderNow();
            return;
          }
          now.textContent = `Connecting to ${station.name}…`;
          a.src = station.url;
          try {
            await a.play();
            playingId = station.id;
          } catch (err) {
            console.error("stream failed", err);
            playingId = null;
            now.textContent = `Couldn't play ${station.name}.`;
            return;
          }
          renderNow();
        });
        grid.appendChild(btn);
      }

      vol.value = String(Math.round(getAudio().volume * 100));
      volOut.textContent = `${vol.value}%`;
      vol.addEventListener("input", () => {
        const v = Number(vol.value) / 100;
        getAudio().volume = v;
        localStorage.setItem(VOL_KEY, String(v));
        volOut.textContent = `${vol.value}%`;
      });

      root.querySelector("#rdStop").addEventListener("click", () => {
        if (audio) audio.pause();
        playingId = null;
        renderNow();
      });

      renderNow();
    }

    return { mount, unmount() {} };
  })();

  // =========================================================================
  // Notes: a typed scratch pad and a finger-drawing pad, both persisted
  // server-side (see /api/notes) so they survive a reboot or a cleared
  // browser profile.
  // =========================================================================
  const notes = (() => {
    let saveTimer = null;
    let state = { text: "", drawing: "" };

    async function flush() {
      if (saveTimer !== null) {
        clearTimeout(saveTimer);
        saveTimer = null;
      }
      try {
        await fetch("/api/notes", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(state),
        });
      } catch (err) {
        console.error("saving notes failed", err);
      }
    }

    function queueSave(hint) {
      if (hint) hint.textContent = "Saving…";
      clearTimeout(saveTimer);
      saveTimer = setTimeout(async () => {
        await flush();
        if (hint) hint.textContent = "Saved";
      }, 800);
    }

    function mount(root) {
      root.appendChild(
        h(`
        <div>
          <div class="miniTabs">
            <button class="miniTab" data-pane="note" aria-selected="true">Note</button>
            <button class="miniTab" data-pane="draw" aria-selected="false">Draw</button>
          </div>
          <section class="miniPane" data-pane="note">
            <textarea id="noteText" placeholder="Shopping list, phone numbers, anything…"></textarea>
            <div class="btnRow" style="margin-top:14px">
              <span class="saveHint" id="noteHint"></span>
            </div>
          </section>
          <section class="miniPane" data-pane="draw" hidden>
            <canvas id="drawCanvas"></canvas>
            <div class="btnRow" style="margin-top:14px">
              <button class="penDot" data-colour="#4fd1ff" aria-pressed="true" style="background:#4fd1ff"></button>
              <button class="penDot" data-colour="#7dffb3" aria-pressed="false" style="background:#7dffb3"></button>
              <button class="penDot" data-colour="#ffd75e" aria-pressed="false" style="background:#ffd75e"></button>
              <button class="penDot" data-colour="#ff4d7d" aria-pressed="false" style="background:#ff4d7d"></button>
              <button class="penDot" data-colour="#eaf4fb" aria-pressed="false" style="background:#eaf4fb"></button>
              <button class="bigBtn danger" id="drawClear">Clear</button>
              <span class="saveHint" id="drawHint"></span>
            </div>
          </section>
        </div>
      `)
      );
      wireTabs(root);

      const textarea = root.querySelector("#noteText");
      const noteHint = root.querySelector("#noteHint");
      const drawHint = root.querySelector("#drawHint");
      const canvas = root.querySelector("#drawCanvas");
      const ctx = canvas.getContext("2d");
      let colour = "#4fd1ff";

      textarea.addEventListener("input", () => {
        state.text = textarea.value;
        queueSave(noteHint);
      });

      // Canvas is sized in device pixels so strokes aren't blurry on the Pi's
      // 1080p panel; CSS keeps the on-screen size.
      function sizeCanvas() {
        const dpr = window.devicePixelRatio || 1;
        const rect = canvas.getBoundingClientRect();
        canvas.width = Math.round(rect.width * dpr);
        canvas.height = Math.round(rect.height * dpr);
        ctx.scale(dpr, dpr);
        ctx.lineCap = "round";
        ctx.lineJoin = "round";
        ctx.lineWidth = 4;
      }

      function restoreDrawing() {
        if (!state.drawing) return;
        const img = new Image();
        img.onload = () => {
          const rect = canvas.getBoundingClientRect();
          ctx.drawImage(img, 0, 0, rect.width, rect.height);
        };
        img.src = state.drawing;
      }

      // The Draw pane starts hidden, so the canvas has no size until you open
      // it - measuring it before then yields a 0x0 bitmap you can't draw on.
      // Size and repopulate it the first time it's actually on screen.
      let loaded = false;
      let canvasReady = false;
      function ensureCanvas() {
        if (canvasReady || !loaded) return;
        if (!canvas.getBoundingClientRect().width) return;
        canvasReady = true;
        sizeCanvas();
        restoreDrawing();
      }
      root
        .querySelector('.miniTab[data-pane="draw"]')
        .addEventListener("click", () => requestAnimationFrame(ensureCanvas));

      let drawing = false;
      canvas.addEventListener("pointerdown", (e) => {
        canvas.setPointerCapture(e.pointerId);
        drawing = true;
        const r = canvas.getBoundingClientRect();
        ctx.strokeStyle = colour;
        ctx.beginPath();
        ctx.moveTo(e.clientX - r.left, e.clientY - r.top);
      });
      canvas.addEventListener("pointermove", (e) => {
        if (!drawing) return;
        const r = canvas.getBoundingClientRect();
        ctx.lineTo(e.clientX - r.left, e.clientY - r.top);
        ctx.stroke();
      });
      function endStroke() {
        if (!drawing) return;
        drawing = false;
        state.drawing = canvas.toDataURL("image/png");
        queueSave(drawHint);
      }
      canvas.addEventListener("pointerup", endStroke);
      canvas.addEventListener("pointercancel", endStroke);

      root.querySelectorAll(".penDot").forEach((dot) =>
        dot.addEventListener("click", () => {
          colour = dot.dataset.colour;
          root.querySelectorAll(".penDot").forEach((d) =>
            d.setAttribute("aria-pressed", String(d === dot))
          );
        })
      );

      root.querySelector("#drawClear").addEventListener("click", () => {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        state.drawing = "";
        queueSave(drawHint);
      });

      (async () => {
        try {
          const resp = await fetch("/api/notes");
          state = await resp.json();
        } catch (err) {
          console.error("loading notes failed", err);
        }
        textarea.value = state.text || "";
        loaded = true;
        ensureCanvas();
      })();
    }

    function unmount() {
      // Don't lose the last few keystrokes on the way out.
      if (saveTimer !== null) flush();
    }

    return { mount, unmount };
  })();

  // =========================================================================
  // Transit: live TTC arrivals for the stops around St. George campus.
  // Data comes via /api/ttc (proxied server-side - the feed has no CORS
  // headers). Streetcars and buses only; see the note on TTC_STOPS in
  // server.py for why the subway isn't here.
  // =========================================================================
  const transit = (() => {
    const REFRESH_MS = 30000;
    let refreshTimer = null;

    function mount(root) {
      root.appendChild(
        h(`
        <div>
          <h1 class="miniTitle">Transit — next arrivals</h1>
          <div id="ttcBody"><div class="wxMeta">Loading…</div></div>
          <p class="emgFooter" id="ttcFoot"></p>
        </div>
      `)
      );

      const body = root.querySelector("#ttcBody");
      const foot = root.querySelector("#ttcFoot");

      function renderStop(stop) {
        if (stop.error) {
          return `<div class="card"><div class="ttcStop">${stop.label}</div>
                  <div class="wxMeta">Couldn't reach the TTC feed.</div></div>`;
        }
        if (!stop.routes.length) {
          return `<div class="card"><div class="ttcStop">${stop.label}</div>
                  <div class="wxMeta">Nothing scheduled right now.</div></div>`;
        }
        const rows = stop.routes
          .map(
            (r) => `
            <div class="ttcRow">
              <span class="ttcRoute">${r.route}</span>
              <span class="ttcDir">${r.direction}</span>
              <span class="ttcMins">${r.minutes
                .map(
                  (m, i) =>
                    `<b class="${i === 0 ? "soon" : ""}">${m === 0 ? "now" : m}</b>`
                )
                .join("<i>·</i>")}</span>
            </div>`
          )
          .join("");
        return `<div class="card"><div class="ttcStop">${stop.label}</div>${rows}</div>`;
      }

      async function load() {
        try {
          const resp = await fetch("/api/ttc");
          const data = await resp.json();
          body.innerHTML = `<div class="ttcGrid">${data.stops.map(renderStop).join("")}</div>`;
          foot.textContent = `Minutes until arrival · updated ${new Date().toLocaleTimeString(
            [],
            { hour: "2-digit", minute: "2-digit", second: "2-digit" }
          )} · refreshes every 30s`;
        } catch (err) {
          console.error("ttc failed", err);
          body.innerHTML = `<div class="wxMeta">Couldn't load arrivals.</div>`;
        }
      }

      load();
      refreshTimer = setInterval(load, REFRESH_MS);
    }

    function unmount() {
      clearInterval(refreshTimer);
      refreshTimer = null;
    }

    return { mount, unmount };
  })();

  // =========================================================================
  // Buildings: turn a U of T building code into a name you can act on.
  //
  // Local data, so it answers instantly and works offline - the campus map
  // site is slow on a touchscreen and needs a network. Addresses appear only
  // where they were verified against U of T's own pages; the rest are left
  // blank rather than guessed, with the campus map a tap away.
  // =========================================================================
  const buildings = (() => {
    const BUILDINGS = [
      { code: "BA", name: "Bahen Centre for Information Technology", address: "40 St. George St.", eng: true },
      { code: "SF", name: "Sandford Fleming Building", address: "10 King's College Rd.", eng: true, note: "Home of ECE" },
      { code: "GB", name: "Galbraith Building", address: "35 St. George St.", eng: true },
      { code: "MY", name: "Myhal Centre for Engineering Innovation & Entrepreneurship", address: "55 St. George St.", eng: true },
      { code: "MC", name: "Mechanical Engineering Building", eng: true },
      { code: "WB", name: "Wallberg Memorial Building", eng: true },
      { code: "HA", name: "Haultain Building", eng: true },
      { code: "PT", name: "D.L. Pratt Building", eng: true },
      { code: "RS", name: "Rosebrugh Building", eng: true },
      { code: "EX", name: "Exam Centre", eng: true },
      { code: "SS", name: "Sidney Smith Hall" },
      { code: "MP", name: "McLennan Physical Laboratories" },
      { code: "LM", name: "Lash Miller Chemical Laboratories" },
      { code: "ES", name: "Earth Sciences Centre" },
      { code: "UC", name: "University College" },
      { code: "KP", name: "Koffler Student Services Centre" },
      { code: "RW", name: "Ramsay Wright Laboratories" },
      { code: "OI", name: "Ontario Institute for Studies in Education (OISE)" },
      { code: "MS", name: "Medical Sciences Building" },
      { code: "HS", name: "Health Sciences Building" },
      { code: "RB", name: "Robarts Library" },
      { code: "CG", name: "Convocation Hall" },
    ];

    function mount(root) {
      root.appendChild(
        h(`
        <div>
          <h1 class="miniTitle">Buildings</h1>
          <input id="bdSearch" class="timeInput" style="font-size:1.1rem;width:100%;max-width:520px"
                 placeholder="Type a code or name — BA, Myhal, Sandford…" />
          <div class="miniTabs" style="margin-top:18px">
            <button class="miniTab" data-filter="eng" aria-selected="true">Engineering</button>
            <button class="miniTab" data-filter="all" aria-selected="false">All</button>
          </div>
          <div id="bdList"></div>
          <p class="emgFooter">
            Addresses are shown only where they were confirmed against U of T's own
            pages. For anything else, open the Campus Map tile.
          </p>
        </div>
      `)
      );

      const list = root.querySelector("#bdList");
      const search = root.querySelector("#bdSearch");
      let filter = "eng";

      function render() {
        const q = search.value.trim().toLowerCase();
        const matches = BUILDINGS.filter((b) => {
          if (!q && filter === "eng" && !b.eng) return false;
          if (!q) return true;
          return (
            b.code.toLowerCase().startsWith(q) ||
            b.name.toLowerCase().includes(q)
          );
        });

        if (!matches.length) {
          list.innerHTML = `<div class="wxMeta">No building matches “${search.value}”.</div>`;
          return;
        }

        list.innerHTML = matches
          .map(
            (b) => `
          <div class="bdRow">
            <span class="bdCode">${b.code}</span>
            <span class="bdName">${b.name}${
              b.note ? `<span class="bdNote">${b.note}</span>` : ""
            }</span>
            <span class="bdAddr">${b.address || "—"}</span>
          </div>`
          )
          .join("");
      }

      search.addEventListener("input", render);
      root.querySelectorAll(".miniTab").forEach((tab) =>
        tab.addEventListener("click", () => {
          filter = tab.dataset.filter;
          root
            .querySelectorAll(".miniTab")
            .forEach((t) => t.setAttribute("aria-selected", String(t === tab)));
          render();
        })
      );

      render();
    }

    return { mount, unmount() {} };
  })();

  // =========================================================================
  // Emergency contacts, U of T St. George.
  //
  // Entirely local data - no fetch, no iframe - so it still works when the
  // network is down, which is exactly when someone might need it. Numbers
  // below were taken from campussafety.utoronto.ca and
  // studentlife.utoronto.ca. If you edit them, re-check against those pages
  // rather than from memory: a wrong number here is worse than no number.
  // =========================================================================
  const emergency = (() => {
    // Set this to where the kiosk physically stands, e.g.
    // "Bahen Centre, 40 St. George St., Room 1200". It's shown on screen so a
    // caller can tell a dispatcher exactly where they are.
    const KIOSK_LOCATION = "";

    const SECTIONS = [
      {
        title: "In an emergency",
        critical: true,
        entries: [
          { name: "Police · Fire · Ambulance", number: "911", note: "Life-threatening emergencies" },
          { name: "Campus Safety — St. George", number: "416-978-2222", note: "24/7 · U of T Special Constable Service" },
        ],
      },
      {
        title: "Non-urgent",
        entries: [
          { name: "Campus Safety — non-urgent", number: "416-978-2323", note: "Reports and general enquiries" },
        ],
      },
      {
        title: "Health",
        entries: [
          { name: "Health & Wellness Centre", number: "416-978-8030", note: "St. George · Mon–Fri, 9 a.m.–5 p.m." },
          {
            name: "U of T Telus Health Student Support",
            number: "1-844-451-9700",
            note: "24/7 · 146 languages · outside North America: 001-416-380-6578",
          },
        ],
      },
      {
        title: "Crisis lines · 24/7",
        entries: [
          { name: "Suicide Crisis Helpline", number: "9-8-8", note: "Call or text" },
          { name: "Good2Talk Student Helpline", number: "1-866-925-5454" },
          { name: "Gerstein Centre Crisis Line", number: "416-929-5200" },
          { name: "Distress Centres of Toronto", number: "416-408-4357" },
          { name: "Toronto Rape Crisis Centre", number: "416-597-8808" },
          { name: "LGBTQ Youthline", number: "1-800-268-9688" },
        ],
      },
    ];

    function mount(root) {
      const sections = SECTIONS.map(
        (section) => `
        <h2 class="emgSection">${section.title}</h2>
        <div class="emgGrid">
          ${section.entries
            .map(
              (e) => `
            <div class="emgCard ${section.critical ? "critical" : ""}">
              <div class="emgName">${e.name}</div>
              <div class="emgNum">${e.number}</div>
              ${e.note ? `<div class="emgNote">${e.note}</div>` : ""}
            </div>`
            )
            .join("")}
        </div>`
      ).join("");

      const location = KIOSK_LOCATION
        ? `<div class="emgWhere"><span class="k">You are at</span> ${KIOSK_LOCATION}</div>`
        : `<div class="emgWhere"><span class="k">Tip</span> set KIOSK_LOCATION in miniapps.js so callers can state exactly where they are.</div>`;

      root.appendChild(
        h(`
        <div>
          <div class="emgBanner">
            <div class="emgBannerTitle">🚨 Emergency contacts</div>
            <div class="emgBannerSub">If someone is in immediate danger, call 911 first.</div>
          </div>
          ${location}
          ${sections}
          <p class="emgFooter">
            Blue-light emergency phones are located across campus and connect
            straight to Campus Safety. U of T also runs UTAlert for campus-wide
            emergency notifications.
          </p>
        </div>
      `)
      );
    }

    return { mount, unmount() {} };
  })();

  // =========================================================================
  // Remote Control: publishes the robot's driving page to the network.
  //
  // The robot's control page and its endpoints live in the same server as
  // this kiosk, but refuse every non-local request unless this switch is on
  // (see access.py). So this isn't starting or stopping a process - it's
  // opening and closing the door to one that's always running, which is why
  // it takes effect instantly and can't fail to "boot".
  //
  // Off on every boot by design, and not remembered: a robot that reboots on
  // its own shouldn't come back with its motors reachable from the network
  // because of a switch someone flipped days ago.
  // =========================================================================
  const remote = (() => {
    let currentTimer = null;

    function mount(root) {
      root.appendChild(
        h(`
        <div>
          <h1 class="miniTitle">Remote Control</h1>

          <div class="card rcCard">
            <div class="rcState">
              <span class="rcDot" id="rcDot"></span>
              <div>
                <div class="rcStateLabel" id="rcStateLabel">Checking…</div>
                <div class="rcStateSub" id="rcStateSub">
                  Lets someone drive the robot from their own phone.
                </div>
              </div>
            </div>
            <button class="bigBtn primary" id="rcToggle" disabled>…</button>
          </div>

          <!-- Motor current. Shown whether or not remote access is on: it's a
               fact about the robot, not about the network door this app
               opens, and it's just as worth seeing while Follow me is
               driving. Hidden only when there's no reading to show. -->
          <div class="card rcCurrent" id="rcCurrent" hidden>
            <div class="rcCurrentCell" id="rcCellM1">
              <div class="rcCurrentLabel">Motor 1 current =</div>
              <div class="rcCurrentValue" id="rcM1">—</div>
            </div>
            <div class="rcCurrentCell" id="rcCellM2">
              <div class="rcCurrentLabel">Motor 2 current =</div>
              <div class="rcCurrentValue" id="rcM2">—</div>
            </div>
            <div class="rcCurrentCell" id="rcCellCpu">
              <div class="rcCurrentLabel">CPU usage =</div>
              <div class="rcCurrentValue" id="rcCpu">—</div>
            </div>
          </div>

          <div class="card rcAddress" id="rcAddress" hidden>
            <div class="rcAddressLabel" id="rcAddressLabel"></div>
            <div class="rcUrl" id="rcUrl"></div>
            <div class="rcAlso" id="rcAlso"></div>
            <div class="rcNote">
              The robot uses its own security certificate, so the browser will warn
              you once that the connection isn't private. Choose <strong>Advanced</strong>,
              then <strong>Proceed</strong> — it's this robot, on your own tailnet.
            </div>
          </div>

          <div class="rcExplain">
            <p>
              While this is on, anyone who can reach the robot on this network can open
              that page and drive it. There's no password. Turn it off when you're done.
            </p>
            <p>
              <strong>Follow me</strong> isn't affected either way — that button stays on
              Ruby's screen, and stays available whether this is on or off. People on the
              remote page can see that the robot is following someone, and press Stop to
              take over, but can't start it themselves.
            </p>
          </div>
        </div>
      `)
      );

      const dot = root.querySelector("#rcDot");
      const stateLabel = root.querySelector("#rcStateLabel");
      const stateSub = root.querySelector("#rcStateSub");
      const toggle = root.querySelector("#rcToggle");
      const addressCard = root.querySelector("#rcAddress");
      const addressLabel = root.querySelector("#rcAddressLabel");
      const urlEl = root.querySelector("#rcUrl");
      const alsoEl = root.querySelector("#rcAlso");

      let enabled = false;

      function render(data) {
        enabled = !!data.enabled;
        dot.classList.toggle("on", enabled);
        stateLabel.textContent = enabled ? "On" : "Off";
        stateSub.textContent = enabled
          ? "The robot can be driven from the tailnet right now."
          : "Lets you drive the robot from a laptop on the same tailnet.";
        toggle.textContent = enabled ? "Turn off" : "Turn on";
        toggle.classList.toggle("primary", !enabled);
        toggle.classList.toggle("danger", enabled);
        toggle.disabled = false;

        // No address at all means no route out. Saying so beats printing a
        // URL nobody can reach.
        addressCard.hidden = !enabled;
        if (!enabled) return;

        urlEl.textContent = data.url || "No address — is Tailscale up?";
        urlEl.classList.toggle("rcUrlMissing", !data.url);

        // "via" distinguishes the address this is meant for (the tailnet)
        // from the fallback. Worth being explicit: the fallback is a
        // different audience - anyone on the local Wi-Fi, rather than only
        // devices signed in to the tailnet.
        const onTailnet = data.via === "tailscale";
        addressLabel.textContent = onTailnet
          ? "Open this on a device signed in to the same tailnet"
          : "⚠ Tailscale isn't up — this is a local network address";
        addressLabel.classList.toggle("rcWarn", !onTailnet);

        // The MagicDNS name is what's shown; the raw Tailscale IP is kept
        // alongside it for when DNS isn't cooperating on the other end.
        alsoEl.textContent =
          onTailnet && data.tailscale_ip && data.host !== data.tailscale_ip
            ? `or ${data.tailscale_ip}`
            : "";
      }

      function failed(message) {
        stateLabel.textContent = "Unavailable";
        stateSub.textContent = message;
        toggle.disabled = true;
        addressCard.hidden = true;
      }

      (async () => {
        try {
          render(await (await fetch("/api/remote")).json());
        } catch (err) {
          failed("Couldn't reach the robot server.");
        }
      })();

      toggle.addEventListener("click", async () => {
        toggle.disabled = true;
        try {
          const resp = await fetch("/api/remote", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled: !enabled }),
          });
          render(await resp.json());
        } catch (err) {
          failed("Couldn't reach the robot server.");
        }
      });

      // --- motor current and CPU ---
      // Motor current is measured on the ESP32 and pushed up the serial link;
      // /robot/motor_current just hands back the last reading, so neither poll
      // touches hardware. Polled only while this app is open - see unmount() -
      // unlike Ruby's own readout, which is always on screen and always polling.
      const currentCard = root.querySelector("#rcCurrent");
      const cellM1 = root.querySelector("#rcCellM1");
      const cellM2 = root.querySelector("#rcCellM2");
      const cellCpu = root.querySelector("#rcCellCpu");
      const m1El = root.querySelector("#rcM1");
      const m2El = root.querySelector("#rcM2");
      const cpuEl = root.querySelector("#rcCpu");

      function formatAmps(value) {
        return typeof value === "number" && isFinite(value) ? `${value.toFixed(2)} A` : "—";
      }

      async function pollStats() {
        const [current, cpu] = await Promise.all([
          fetch("/robot/motor_current").then((r) => r.json()).catch(() => null),
          fetch("/robot/cpu_status").then((r) => r.json()).catch(() => null),
        ]);
        // Cells hide individually, and the card only when they all would.
        // Hidden rather than frozen when the ESP32 is absent or has gone
        // quiet: a stale number here looks exactly like a live one. CPU is a
        // fact about the Pi, so it stays up whatever the ESP32 is doing.
        const haveCurrent = !!(current && current.available);
        const haveCpu = !!(cpu && cpu.available);
        cellM1.hidden = !haveCurrent;
        cellM2.hidden = !haveCurrent;
        cellCpu.hidden = !haveCpu;
        currentCard.hidden = !(haveCurrent || haveCpu);
        if (haveCurrent) {
          m1El.textContent = formatAmps(current.m1);
          m2El.textContent = formatAmps(current.m2);
        }
        if (haveCpu) cpuEl.textContent = `${cpu.percent.toFixed(1)}%`;
      }

      pollStats();
      currentTimer = setInterval(pollStats, 1000);
    }

    // Was a no-op until this app started polling. Without clearing the timer
    // it would keep firing after the app is closed, and mounting it again
    // would stack a second one on top - the same pattern the System app's
    // vitals timer already follows.
    function unmount() {
      clearInterval(currentTimer);
      currentTimer = null;
    }

    return { mount, unmount };
  })();

  // =========================================================================
  // System: speaker volume, screen dimming, Pi vitals, power.
  // =========================================================================
  const system = (() => {
    const DIM_KEY = "kioskDim";
    let infoTimer = null;

    // Applied at load too (below), so a dimmed screen stays dimmed after a
    // restart rather than blinding you at 2am.
    function applyDim(value) {
      document.getElementById("dimScrim").style.opacity = String(value);
      localStorage.setItem(DIM_KEY, String(value));
    }

    function savedDim() {
      const v = Number(localStorage.getItem(DIM_KEY));
      return Number.isFinite(v) ? Math.min(Math.max(v, 0), 0.8) : 0;
    }

    function mount(root) {
      root.appendChild(
        h(`
        <div>
          <h1 class="miniTitle">System</h1>

          <div class="card" style="margin-bottom:20px">
            <div class="sliderRow" style="margin-top:0">
              <label for="sysVol">Speaker</label>
              <input type="range" class="bigRange" id="sysVol" min="0" max="100" />
              <output id="sysVolOut">—</output>
            </div>
            <div class="sliderRow" style="margin-bottom:0">
              <label for="sysDim">Screen dim</label>
              <input type="range" class="bigRange" id="sysDim" min="0" max="80" />
              <output id="sysDimOut">0%</output>
            </div>
          </div>

          <div class="infoGrid" id="sysInfo"></div>

          <div class="btnRow">
            <button class="bigBtn danger" id="sysReboot">Restart</button>
            <button class="bigBtn danger" id="sysShutdown">Shut down</button>
            <span class="saveHint" id="sysPowerHint"></span>
          </div>
        </div>
      `)
      );

      const volInput = root.querySelector("#sysVol");
      const volOut = root.querySelector("#sysVolOut");
      const dimInput = root.querySelector("#sysDim");
      const dimOut = root.querySelector("#sysDimOut");
      const infoGrid = root.querySelector("#sysInfo");
      const powerHint = root.querySelector("#sysPowerHint");

      // --- speaker volume (PipeWire, via the server) ---
      (async () => {
        try {
          const resp = await fetch("/api/system/volume");
          const d = await resp.json();
          volInput.value = String(d.volume);
          volOut.textContent = `${d.volume}%`;
        } catch (err) {
          volOut.textContent = "n/a";
        }
      })();

      let volTimer = null;
      volInput.addEventListener("input", () => {
        volOut.textContent = `${volInput.value}%`;
        clearTimeout(volTimer);
        volTimer = setTimeout(() => {
          fetch("/api/system/volume", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ volume: Number(volInput.value) }),
          }).catch((err) => console.error("volume failed", err));
        }, 120);
      });

      // --- screen dim (software scrim; HDMI has no backlight control) ---
      dimInput.value = String(Math.round(savedDim() * 100));
      dimOut.textContent = `${dimInput.value}%`;
      dimInput.addEventListener("input", () => {
        applyDim(Number(dimInput.value) / 100);
        dimOut.textContent = `${dimInput.value}%`;
      });

      // --- vitals ---
      async function loadInfo() {
        try {
          const resp = await fetch("/api/system/info");
          const d = await resp.json();
          infoGrid.innerHTML = [
            ["Host", d.hostname],
            ["Address", d.ip],
            ["CPU temp", d.cpu_temp],
            ["Uptime", d.uptime],
            ["Memory", d.memory],
            ["Disk", d.disk],
          ]
            .map(([k, v]) => `<div class="infoCell"><div class="k">${k}</div><div class="v">${v}</div></div>`)
            .join("");
        } catch (err) {
          infoGrid.innerHTML = `<div class="wxMeta">System info unavailable.</div>`;
        }
      }
      loadInfo();
      infoTimer = setInterval(loadInfo, 5000);

      // --- power, with a confirm tap so a stray touch can't kill the Pi ---
      function armPowerButton(btn, action, label) {
        let armed = false;
        let armTimer = null;
        btn.addEventListener("click", async () => {
          if (!armed) {
            armed = true;
            btn.textContent = `Tap again to ${label.toLowerCase()}`;
            powerHint.textContent = "Cancels in 5s";
            armTimer = setTimeout(() => {
              armed = false;
              btn.textContent = label;
              powerHint.textContent = "";
            }, 5000);
            return;
          }
          clearTimeout(armTimer);
          armed = false;
          btn.textContent = label;
          powerHint.textContent = `${label}…`;
          try {
            await fetch("/api/system/power", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ action }),
            });
          } catch (err) {
            powerHint.textContent = "Failed";
          }
        });
      }
      armPowerButton(root.querySelector("#sysReboot"), "reboot", "Restart");
      armPowerButton(root.querySelector("#sysShutdown"), "shutdown", "Shut down");
    }

    function unmount() {
      clearInterval(infoTimer);
      infoTimer = null;
    }

    // Restore the saved dim level as soon as the page loads.
    document.addEventListener("DOMContentLoaded", () => applyDim(savedDim()));

    return { mount, unmount };
  })();

  return { clock, weather, radio, notes, system, emergency, transit, buildings, remote };
})();
