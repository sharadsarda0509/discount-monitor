# Apple IN Guest Checkout Autofill

A Chrome extension (Manifest V3) that fills your saved guest-checkout details on
`apple.com/in` fast, so you can check out in seconds when stock appears. It
**does not** place the order or submit payment — you review and complete the bank
OTP + "Place Order" yourself. That human-in-the-loop is intentional.

## Why an extension (and not a bot/automation)
Apple detects automated browsers via `navigator.webdriver` and CDP/Selenium
fingerprints, and **blocks the cart and cancels orders** placed that way. A
content-script extension runs inside your **normal** browser, so `webdriver` is
`false` and there's no automation fingerprint — you look like a real buyer.
Anything that *drives* the browser (Selenium/Puppeteer/DevTools "auto-checkout")
gets flagged; don't use those for the actual purchase.

> Note: guest checkout itself is the most common cause of Apple order
> cancellations (no account history + fraud checks). Filling faster doesn't
> change that. For fewest cancellations: sign in with an Apple ID, pay by UPI,
> and make billing name/address exactly match your card.

## Install (unpacked)
1. Chrome → `chrome://extensions` → toggle **Developer mode** (top-right).
2. **Load unpacked** → select this `chrome-extension/` folder.
3. Pin the extension. Open its **Options** and fill in your config → **Save**.

## Use
1. Add the iPhone to your bag and go to guest checkout (or click **Open buy page**
   in the popup to jump to your configured SKU).
2. On each checkout step, press **Alt+Shift+F** (or popup → **Autofill this page**).
3. Review, complete **OTP / Place Order** yourself.

The popup status shows `filled: N, missed: [...]`. Anything in `missed` needs a
selector override (below).

## Reset & open fresh (fresh client per attempt)
Popup → **Reset & open fresh** (or **Alt+Shift+R**) does, in order:
1. Rotates a new persona **seed** (resets the fingerprint spoof).
2. Clears **all `apple.com` cookies** (every subdomain, via `chrome.cookies`) and
   per-origin **localStorage / IndexedDB / CacheStorage / service workers / cache**
   (via `chrome.browsingData`), plus session/localStorage of the current tab.
3. Navigates to your configured **buy URL** so the next attempt starts clean.

This runs *before* the session on purpose — clearing or spoofing *after* autofill
(mid-checkout) would wipe the live cart/session and change nothing already sent.

**Canvas/WebGL spoof** (Options → Behaviour toggle, default on but inert until you
first use Reset): injects a *consistent, seeded* canvas + WebGL fingerprint at
`document_start` in the page's MAIN world. It's seeded (not per-call random) so the
persona is stable within a session and changes only on the next Reset.

> ⚠️ **Honest caveat.** This extension's whole value (see above) is looking like a
> *clean, real* browser. A spoofed fingerprint on an otherwise-real client can read
> as **more** anomalous to Apple's anti-fraud, not less — mismatch/noise is itself a
> signal. Cookie/storage clearing is the reliable lever; the fingerprint spoof is
> best-effort and easy to turn off. **A separate clean Chrome profile (or container)
> per attempt is more robust** than any in-page spoof.

## Config
Set once in **Options** (stored in `chrome.storage.local`, this browser only):
contact, fulfillment (pickup store / pickup person), delivery address, payment
(UPI recommended), and the buy URL.

## Fixing a field that won't fill (selector overrides)
Apple's field `id`s change over time, so defaults match by stable `autocomplete`
attributes. If something doesn't fill:
1. On the checkout page, right-click the field → **Inspect**.
2. Note a stable selector (an `id`, `name`, or `autocomplete`).
3. In Options → **Advanced — selector overrides**, add JSON, e.g.:
   ```json
   { "pincode": "input#postalCode", "phone": "input#phoneNumber" }
   ```
Keys: `firstName, lastName, email, phone, line1, line2, landmark, pincode, city,
state, cardNumber, cardName, ccExp, cvv, upiId`.

## Security
Card number / CVV entered in Options are stored **unencrypted** in
`chrome.storage.local`. Prefer **UPI** and leave card fields blank (type them by
hand). Don't sync this profile to a shared machine.

## Notes / limits
- Apple IN store **pickup** is "available today" at a store — there's usually no
  granular time-slot to pick; the extension selects fields that exist and leaves
  store selection to you if the store picker is a search widget.
- It stops before "Place Order" by design; it never auto-submits payment.
