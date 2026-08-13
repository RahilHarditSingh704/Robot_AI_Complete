// Keeps the kiosk's idle timer honest.
//
// Events inside a cross-origin iframe never reach the kiosk page, so from its
// point of view someone reading Wikipedia or scrolling YouTube looks idle and
// would get the "Still there?" prompt every few minutes. This runs in every
// frame and posts a ping up to the top window, which static/kiosk.js treats as
// a sign of life.
//
// Deliberately minimal: it reads nothing from the page, sends no page content,
// and only ever posts the one fixed message. Throttled so a scroll doesn't
// fire thousands of postMessage calls.
const THROTTLE_MS = 2000;
let lastPing = 0;

function ping() {
  const now = Date.now();
  if (now - lastPing < THROTTLE_MS) return;
  lastPing = now;
  try {
    window.top.postMessage({ type: "kiosk-activity" }, "*");
  } catch (err) {
    // Cross-origin restrictions on window.top can throw in odd framing
    // setups; an unreported ping is harmless, so stay quiet.
  }
}

for (const evt of ["pointerdown", "keydown", "scroll", "wheel"]) {
  window.addEventListener(evt, ping, { passive: true, capture: true });
}
