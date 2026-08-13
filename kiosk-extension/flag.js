// Tells the kiosk page that header stripping is active, so it can open sites
// like Google and YouTube instead of showing the "won't open yet" card.
// Read in static/kiosk.js as document.documentElement.dataset.kioskExt.
function raiseFlag() {
  if (document.documentElement) document.documentElement.dataset.kioskExt = "1";
}

raiseFlag();
document.addEventListener("DOMContentLoaded", raiseFlag);

// Relays the kiosk page's wipe request to background.js, which holds the
// chrome.browsingData permission that a web page can't reach.
//
// The source check matters: without it, any site embedded in the kiosk could
// postMessage up to the top window and trigger a wipe. Requiring
// event.source === window means only the kiosk page itself qualifies - a
// message from an iframe carries that frame's window as its source.
window.addEventListener("message", (event) => {
  if (event.source !== window || event.origin !== window.location.origin) return;
  if (!event.data || event.data.type !== "kiosk-wipe") return;
  chrome.runtime.sendMessage({ type: "kiosk-wipe" }, (response) => {
    // Reported back so a silently failing wipe is visible in the console
    // rather than being assumed to have worked.
    window.postMessage(
      {
        type: "kiosk-wipe-result",
        ok: Boolean(response && response.ok),
        error: (response && response.error) || chrome.runtime.lastError?.message,
      },
      window.location.origin
    );
  });
});
