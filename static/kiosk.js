(() => {
  // =========================================================================
  // Kiosk shell: the launcher grid, the full-screen app view, and the
  // draggable pill that gets you back out.
  //
  // Aimed at U of T St. George, Faculty of Applied Science & Engineering, and
  // ECE in particular.
  //
  // NOTHING HERE MAY REQUIRE A LOGIN. This is a shared machine in a public
  // space: anyone who signs into ACORN, Quercus, Magellan or webmail and
  // walks away leaves their student record open to the next person. Every
  // tile below is a public page that needs no UTORid. Keep it that way when
  // adding apps - if it prompts for credentials, it doesn't belong here.
  //
  // Two kinds of tile:
  //   type: "web"  - a real website, loaded in a full-screen iframe.
  //   type: "mini" - a built-in touch UI from miniapps.js, no network needed.
  //
  // "unframe: true" marks sites that send X-Frame-Options / CSP
  // frame-ancestors and so refuse to load in an iframe unless
  // kiosk-extension/ is stripping those headers (see that folder's README).
  // Verified per-site with curl; the U of T sites mostly frame fine.
  //
  // To add an app: append an entry to a group. Nothing else needs touching.
  // =========================================================================
  const APP_GROUPS = [
    {
      name: "Electrical & Computer Engineering",
      apps: [
        { id: "ece", label: "ECE Home", icon: "⚡", type: "web", url: "https://www.ece.utoronto.ca/" },
        { id: "iris", label: "ECE Iris", icon: "🕸️", type: "web", url: "https://ececourses.ece.utoronto.ca/" },
        { id: "eceResources", label: "ECE Resources", icon: "🧰", type: "web", url: "https://www.ece.utoronto.ca/undergraduate-students/resources/" },
        { id: "eceClub", label: "ECE Club", icon: "🎓", type: "web", url: "https://ece.skule.ca/" },
      ],
    },
    {
      name: "Engineering",
      apps: [
        { id: "fase", label: "Faculty of Eng", icon: "🏛️", type: "web", url: "https://www.engineering.utoronto.ca/" },
        { id: "undergrad", label: "Undergrad Office", icon: "📋", type: "web", url: "https://undergrad.engineering.utoronto.ca/" },
        { id: "calendar", label: "Academic Calendar", icon: "📖", type: "web", url: "https://engineering.calendar.utoronto.ca/", unframe: true },
        { id: "careers", label: "Career Centre", icon: "💼", type: "web", url: "https://engineeringcareers.utoronto.ca/" },
        { id: "ecp", label: "Communication", icon: "✍️", type: "web", url: "https://ecp.engineering.utoronto.ca/" },
      ],
    },
    {
      name: "Campus",
      apps: [
        { id: "emergency", label: "Emergency", icon: "🚨", type: "mini" },
        { id: "buildings", label: "Buildings", icon: "🏢", type: "mini" },
        { id: "transit", label: "Transit", icon: "🚋", type: "mini" },
        { id: "map", label: "Campus Map", icon: "🧭", type: "web", url: "https://map.utoronto.ca/" },
        { id: "ttb", label: "Timetable", icon: "🗓️", type: "web", url: "https://ttb.utoronto.ca/" },
        { id: "library", label: "Library Search", icon: "📚", type: "web", url: "https://onesearch.library.utoronto.ca/", unframe: true },
        { id: "wellness", label: "Health & Wellness", icon: "💚", type: "web", url: "https://studentlife.utoronto.ca/department/health-wellness/" },
        { id: "safety", label: "Campus Safety", icon: "🛡️", type: "web", url: "https://www.campussafety.utoronto.ca/" },
        { id: "skule", label: "Skule", icon: "⚙️", type: "web", url: "https://skule.ca/" },
      ],
    },
    {
      name: "General",
      apps: [
        { id: "google", label: "Google", icon: "🔍", type: "web", url: "https://www.google.com", unframe: true },
        { id: "youtube", label: "YouTube", icon: "▶️", type: "web", url: "https://www.youtube.com", unframe: true },
        { id: "gmaps", label: "Google Maps", icon: "🗺️", type: "web", url: "https://www.google.com/maps", unframe: true },
        { id: "news", label: "News", icon: "📰", type: "web", url: "https://news.google.com", unframe: true },
        { id: "wikipedia", label: "Wikipedia", icon: "🌐", type: "web", url: "https://en.m.wikipedia.org" },
      ],
    },
    {
      name: "Tools",
      apps: [
        { id: "clock", label: "Clock", icon: "⏰", type: "mini" },
        { id: "weather", label: "Weather", icon: "⛅", type: "mini" },
        { id: "radio", label: "Radio", icon: "📻", type: "mini" },
        { id: "notes", label: "Notes", icon: "📝", type: "mini" },
        // The two device controls, kept together at the end: everything
        // above is content, these two change what the machine does.
        { id: "remote", label: "Remote Control", icon: "🕹️", type: "mini" },
        { id: "system", label: "System", icon: "🔧", type: "mini" },
      ],
    },
  ];

  const appEl = document.getElementById("app");
  const kioskEl = document.getElementById("kiosk");
  const appViewEl = document.getElementById("appView");
  const appFrame = document.getElementById("appFrame");
  const miniAppEl = document.getElementById("miniApp");
  const appBlockedEl = document.getElementById("appBlocked");
  const tileGrid = document.getElementById("tileGrid");
  const kioskClock = document.getElementById("kioskClock");
  const kioskDate = document.getElementById("kioskDate");
  const assistantClock = document.getElementById("assistantClock");
  const assistantDate = document.getElementById("assistantDate");
  const tapHint = document.getElementById("tapHint");
  const exitPill = document.getElementById("exitPill");
  const dragShield = document.getElementById("dragShield");

  let clockTimer = null;
  let activeMiniApp = null;

  // The extension sets this attribute on our page at document_start. Checked
  // lazily (not cached at load) so installing the extension and reloading is
  // all it takes - no code change needed.
  function unframingActive() {
    return document.documentElement.dataset.kioskExt === "1";
  }

  // ---------- View switching ----------

  // showHint is set when nobody asked to come back here - first boot and the
  // idle timeout - so the next passer-by gets told the screen is interactive.
  // Tapping "back to Ruby" yourself doesn't need the invitation.
  function showAssistant({ showHint = false } = {}) {
    closeApp();
    kioskEl.hidden = true;
    appEl.hidden = false;
    tapHint.hidden = !showHint;
    document.dispatchEvent(new CustomEvent("kiosk:exit"));
  }

  function showKiosk() {
    closeApp();
    appEl.hidden = true;
    kioskEl.hidden = false;
    tapHint.hidden = true;
    // Dispatched last, after the view flags are set: listeners check them.
    // app.js drops the mic and stops any reply mid-sentence (leaving Ruby
    // talking to an empty room while you browse is jarring), and the idle
    // timer below arms itself only once the assistant is actually hidden.
    document.dispatchEvent(new CustomEvent("kiosk:enter"));
  }

  function openApp(app) {
    if (app.type === "mini") return openMiniApp(app);
    return openWebApp(app);
  }

  function openWebApp(app) {
    if (app.unframe && !unframingActive()) return showBlockedCard(app);
    miniAppEl.hidden = true;
    appBlockedEl.hidden = true;
    appFrame.hidden = false;
    appFrame.src = app.url;
    enterAppView();
  }

  function openMiniApp(app) {
    const mini = window.KioskMiniApps && window.KioskMiniApps[app.id];
    if (!mini) {
      console.error("no mini-app registered for", app.id);
      return;
    }
    appFrame.hidden = true;
    appFrame.src = "about:blank";
    appBlockedEl.hidden = true;
    miniAppEl.hidden = false;
    miniAppEl.innerHTML = "";
    activeMiniApp = mini;
    mini.mount(miniAppEl);
    enterAppView();
  }

  function showBlockedCard(app) {
    appFrame.hidden = true;
    appFrame.src = "about:blank";
    miniAppEl.hidden = true;
    appBlockedEl.hidden = false;
    // Deliberately specific about the likeliest cause. The header-stripping
    // extension only loads when Chromium is started with --load-extension, and
    // Chromium drops that flag when it hands the URL to an instance that's
    // already running - so "it worked yesterday" usually means a stray
    // Chromium is holding the profile, not that anything is misconfigured.
    appBlockedEl.innerHTML = `
      <h2>${app.label} won't open in the kiosk</h2>
      <p>
        ${app.label} sends an <strong>X-Frame-Options</strong> header telling the browser
        to refuse to display it inside another page. The kiosk extension strips that
        header, but it isn't loaded right now.
      </p>
      <p>
        Most often that's because Chromium was already running when the kiosk
        started, so it reused that process and ignored the extension. Close every
        Chromium window and relaunch:
        <code>pkill -9 chromium; ./start.sh</code>
      </p>
      <p>Everything else — Wikipedia, the U of T tiles, Clock, Weather, Radio, Notes — works regardless.</p>
    `;
    enterAppView();
  }

  function enterAppView() {
    appViewEl.hidden = false;
    placePillFromStorage();
  }

  function closeApp() {
    if (activeMiniApp) {
      if (activeMiniApp.unmount) activeMiniApp.unmount();
      activeMiniApp = null;
    }
    miniAppEl.innerHTML = "";
    miniAppEl.hidden = true;
    appBlockedEl.hidden = true;
    // Blanking the src stops video/audio and frees the renderer; leaving a
    // YouTube tab alive in the background eats CPU on the Pi.
    appFrame.src = "about:blank";
    appFrame.hidden = false;
    appViewEl.hidden = true;
  }

  // ---------- Launcher ----------

  for (const group of APP_GROUPS) {
    // Headings span the whole grid row so the tiles below them stay aligned
    // to the same columns as every other group.
    const heading = document.createElement("h2");
    heading.className = "groupTitle";
    heading.textContent = group.name;
    tileGrid.appendChild(heading);

    for (const app of group.apps) {
      const tile = document.createElement("button");
      tile.className = "tile";
      tile.type = "button";
      tile.innerHTML = `<span class="tileIcon">${app.icon}</span><span class="tileLabel">${app.label}</span>`;
      if (app.id === "emergency") tile.classList.add("tileEmergency");
      tile.addEventListener("click", () => openApp(app));
      tileGrid.appendChild(tile);
    }
  }

  // One ticker drives the clock on both screens - the launcher header and the
  // faint one over Ruby's face. It runs for the life of the page rather than
  // starting and stopping with the view: a 10s interval writing four strings
  // costs nothing next to the bookkeeping of pausing it.
  function tickClock() {
    const now = new Date();
    const time = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    const date = now.toLocaleDateString([], {
      weekday: "long",
      day: "numeric",
      month: "long",
    });
    kioskClock.textContent = time;
    kioskDate.textContent = date;
    assistantClock.textContent = time;
    assistantDate.textContent = date;
  }

  function startClock() {
    tickClock();
    if (clockTimer === null) clockTimer = setInterval(tickClock, 10000);
  }

  document.getElementById("kioskOpenBtn").addEventListener("click", showKiosk);
  document.getElementById("kioskBackBtn").addEventListener("click", showAssistant);

  // ---------- Draggable exit pill ----------
  //
  // Pointer capture is what makes this work over a full-screen iframe: without
  // it the first pointermove that crosses into the frame is delivered to the
  // embedded site instead of us, and the pill sticks to the finger's starting
  // point. #dragShield is a second line of defence for the same problem.
  //
  // A press only counts as "go back to the launcher" if the finger stayed
  // within DRAG_THRESHOLD - past that it's a drag, and the pill snaps to
  // whichever side of the screen it ended up nearest, remembering that spot.

  const PILL_KEY = "kioskPillPos";
  const DRAG_THRESHOLD = 8;
  const PILL_MARGIN = 14;

  let drag = null;

  function clampPill(left, top) {
    const w = exitPill.offsetWidth;
    const h = exitPill.offsetHeight;
    return {
      left: Math.min(Math.max(left, PILL_MARGIN), window.innerWidth - w - PILL_MARGIN),
      top: Math.min(Math.max(top, PILL_MARGIN), window.innerHeight - h - PILL_MARGIN),
    };
  }

  function placePill(left, top) {
    const p = clampPill(left, top);
    exitPill.style.left = `${p.left}px`;
    exitPill.style.top = `${p.top}px`;
  }

  function placePillFromStorage() {
    let saved = null;
    try {
      saved = JSON.parse(localStorage.getItem(PILL_KEY) || "null");
    } catch (err) {
      saved = null;
    }
    if (saved && Number.isFinite(saved.left) && Number.isFinite(saved.top)) {
      placePill(saved.left, saved.top);
    } else {
      // Default: right edge, vertically centred - same spot as the Apps tab
      // you just pressed, so your thumb is already there.
      placePill(window.innerWidth, window.innerHeight / 2 - exitPill.offsetHeight / 2);
    }
  }

  exitPill.addEventListener("pointerdown", (e) => {
    exitPill.setPointerCapture(e.pointerId);
    exitPill.classList.remove("settling");
    const rect = exitPill.getBoundingClientRect();
    drag = {
      id: e.pointerId,
      grabX: e.clientX - rect.left,
      grabY: e.clientY - rect.top,
      startX: e.clientX,
      startY: e.clientY,
      moved: false,
    };
  });

  exitPill.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    if (!drag.moved) {
      const dist = Math.hypot(e.clientX - drag.startX, e.clientY - drag.startY);
      if (dist <= DRAG_THRESHOLD) return;
      drag.moved = true;
      exitPill.classList.add("dragging");
      dragShield.hidden = false;
    }
    placePill(e.clientX - drag.grabX, e.clientY - drag.grabY);
  });

  function endDrag(e) {
    if (!drag || e.pointerId !== drag.id) return;
    const wasDrag = drag.moved;
    drag = null;
    exitPill.classList.remove("dragging");
    dragShield.hidden = true;

    if (!wasDrag) {
      showKiosk();
      return;
    }

    const rect = exitPill.getBoundingClientRect();
    const snapLeft =
      rect.left + rect.width / 2 < window.innerWidth / 2
        ? PILL_MARGIN
        : window.innerWidth - rect.width - PILL_MARGIN;
    exitPill.classList.add("settling");
    placePill(snapLeft, rect.top);
    localStorage.setItem(
      PILL_KEY,
      JSON.stringify({ left: snapLeft, top: clampPill(snapLeft, rect.top).top })
    );
  }

  exitPill.addEventListener("pointerup", endDrag);
  exitPill.addEventListener("pointercancel", endDrag);

  window.addEventListener("resize", () => {
    if (!appViewEl.hidden) placePillFromStorage();
  });

  // Escape steps back one level - handy when a keyboard is plugged in.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!appViewEl.hidden) showKiosk();
    else if (!kioskEl.hidden) showAssistant();
  });

  // ---------- Idle reset ----------
  //
  // A shared kiosk shouldn't hand the next person whatever the last one left
  // on screen. After IDLE_MS without interaction we warn with a countdown -
  // never yank the page out from under someone mid-read - and then return to
  // Ruby, blanking the frame on the way.
  //
  // Only armed once you've left the assistant view: Ruby's face IS the idle
  // screen, so there's nothing to reset to while it's showing.
  //
  // Activity inside an embedded site can't be seen from here - events in a
  // cross-origin iframe don't reach this document. kiosk-extension's
  // activity.js is injected into every frame and posts a message up to us, so
  // scrolling YouTube counts as being present. Without the extension the
  // countdown still appears and one tap dismisses it.

  const IDLE_MS = 5 * 60 * 1000;
  const COUNTDOWN_S = 30;

  const idlePrompt = document.getElementById("idlePrompt");
  const idleCount = document.getElementById("idleCount");
  let idleTimer = null;
  let countdownTimer = null;

  function clearIdleTimers() {
    clearTimeout(idleTimer);
    clearInterval(countdownTimer);
    idleTimer = countdownTimer = null;
  }

  function hideIdlePrompt() {
    idlePrompt.hidden = true;
    clearInterval(countdownTimer);
    countdownTimer = null;
  }

  function armIdleTimer() {
    clearIdleTimers();
    hideIdlePrompt();
    // Nothing to reset to while the assistant is already up.
    if (!appEl.hidden) return;
    idleTimer = setTimeout(startCountdown, IDLE_MS);
  }

  function startCountdown() {
    let remaining = COUNTDOWN_S;
    idleCount.textContent = String(remaining);
    idlePrompt.hidden = false;
    countdownTimer = setInterval(() => {
      remaining -= 1;
      idleCount.textContent = String(Math.max(remaining, 0));
      if (remaining <= 0) {
        clearIdleTimers();
        hideIdlePrompt();
        wipeBrowsingData();
        showAssistant({ showHint: true });
      }
    }, 1000);
  }

  function noteActivity() {
    // While the prompt is up, any touch anywhere counts as "yes I'm here".
    // The same touch retires the "tap the screen" invitation - it has served
    // its purpose the moment someone touches anything.
    tapHint.hidden = true;
    armIdleTimer();
  }

  // Clears cookies, history, cache and saved form entries so the next person
  // doesn't inherit the last one's session - notably a site's own search box
  // offering up whatever was typed into it. Done only on an idle timeout (the
  // person has left), not when someone walks back to Ruby themselves.
  //
  // The work happens in kiosk-extension/background.js; flag.js relays this
  // message to it. Silently does nothing when the extension isn't loaded, and
  // the kiosk's own origin is excluded so its settings survive.
  function wipeBrowsingData() {
    window.postMessage({ type: "kiosk-wipe" }, window.location.origin);
  }

  for (const evt of ["pointerdown", "keydown", "wheel"]) {
    document.addEventListener(evt, noteActivity, { passive: true });
  }

  // The scrim consumes the tap that dismisses it: stopPropagation keeps the
  // event off the document, and because the scrim is the hit target the mic,
  // send and Apps controls underneath never see it. Tapping to wake the kiosk
  // shouldn't also press whatever happened to be under your finger.
  tapHint.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    e.stopPropagation();
    tapHint.hidden = true;
    armIdleTimer();
  });

  // Pings from activity.js running inside embedded sites, plus the extension's
  // report on whether a wipe actually succeeded.
  window.addEventListener("message", (e) => {
    if (!e.data) return;
    if (e.data.type === "kiosk-activity") noteActivity();
    if (e.data.type === "kiosk-wipe-result") {
      if (e.data.ok) console.log("kiosk: browsing data wiped");
      else console.error("kiosk: browsing data wipe FAILED", e.data.error);
    }
  });

  document.getElementById("idleStay").addEventListener("click", noteActivity);

  // Re-arm whenever the view changes, so the timer reflects where we now are.
  document.addEventListener("kiosk:enter", armIdleTimer);
  document.addEventListener("kiosk:exit", armIdleTimer);

  // ---------- Boot ----------

  startClock();
  // First boot is the other moment nobody has touched anything yet.
  tapHint.hidden = false;
})();
