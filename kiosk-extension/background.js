// Wipes browsing state when the kiosk resets itself after an idle timeout, so
// the next person doesn't inherit the last one's cookies, history or typed
// search terms. Triggered by static/kiosk.js, relayed through flag.js (a web
// page can't reach chrome.* APIs directly).
//
// The kiosk's own origin is excluded: its settings - alarms, weather location,
// radio volume, where you dragged the exit pill - live in localStorage there,
// and wiping them every few minutes would be its own kind of broken.

// https, not http: the kiosk is served over TLS now that the same server also
// hosts the robot's remote page (a phone needs HTTPS for it). These strings
// have to track the scheme and port the kiosk is actually served on - if they
// drift, the wipe stops excluding the kiosk and quietly erases its own
// localStorage on every idle reset: alarms, weather location, radio volume,
// where the exit pill was dragged.
const KIOSK_ORIGINS = ["https://127.0.0.1:5000", "https://localhost:5000"];

async function wipe() {
  // Two calls because Chrome only accepts excludeOrigins for storage-shaped
  // data types. Passing it alongside history or cache throws.
  await chrome.browsingData.remove(
    { since: 0, excludeOrigins: KIOSK_ORIGINS },
    {
      cookies: true,
      localStorage: true,
      indexedDB: true,
      serviceWorkers: true,
      cacheStorage: true,
    }
  );

  // formData is the one that matters most here: it's what makes a site's own
  // search box offer up whatever the previous person typed into it.
  await chrome.browsingData.remove(
    { since: 0 },
    { history: true, cache: true, formData: true }
  );
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.type !== "kiosk-wipe") return undefined;

  wipe()
    .then(() => {
      console.log("kiosk: browsing data wiped");
      sendResponse({ ok: true });
    })
    .catch((err) => {
      console.error("kiosk: wipe failed", err);
      sendResponse({ ok: false, error: String(err) });
    });

  return true; // keep the message channel open for the async reply
});
