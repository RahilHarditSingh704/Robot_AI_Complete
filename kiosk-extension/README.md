# Ruby Kiosk Frame Unlocker

Google, YouTube, Gmail and most large sites send `X-Frame-Options: SAMEORIGIN`
(and a CSP `frame-ancestors` directive). Both tell the browser to refuse to
render the page inside another page — which is exactly what the kiosk does when
you tap a tile. Without something stripping those headers you get a blank
frame, so `static/kiosk.js` shows an explanatory card instead.

This extension removes those headers, **but only from sub-frame responses**
(`"resourceTypes": ["sub_frame"]` in `rules.json`). Top-level pages keep their
headers untouched, so this doesn't weaken normal browsing.

## Why Chromium and not Firefox

Firefox refuses to permanently install an extension that isn't signed by
Mozilla, and the only Firefox add-on that does this job is unmaintained
third-party code from 2020 with permission to read every page. Chromium loads
an unpacked extension straight from disk with `--load-extension`, so the code
doing the header stripping is the four small files in this folder — nothing
external, nothing to trust.

## Setup

```bash
sudo snap install chromium
./start.sh
```

`start.sh` picks Chromium automatically when it's installed and falls back to
Firefox otherwise. There is nothing to install *into* the browser: the
extension is loaded from this folder at launch and disappears when Chromium
closes.

## Checking it's working

The content script sets `data-kiosk-ext="1"` on the kiosk page's `<html>`
element, and `kiosk.js` reads that to decide whether to open a site or show the
"won't open yet" card. So: if the Google tile opens Google, it's working.

## Tightening the scope

To strip headers *only* for frames the kiosk itself opens, add this to the
`condition` block in `rules.json`:

```json
"initiatorDomains": ["127.0.0.1"]
```

That's stricter, but frames nested inside an embedded site (ads, embedded
players, sign-in popups) then keep their original headers and may fail to
render. The default is the more forgiving option.
