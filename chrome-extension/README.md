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
