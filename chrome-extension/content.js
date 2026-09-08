/* Apple IN Guest Checkout Autofill -- content script.
 *
 * Runs in your NORMAL browser (no webdriver flag). It only *fills* fields from
 * your saved profile; it never clicks "Place Order" and never submits payment --
 * you review and complete the bank OTP yourself. That human-in-the-loop is
 * deliberate: it's what keeps the session looking like a real buyer.
 *
 * Apple's store is React, so setting input.value directly is ignored -- we set
 * via the native setter and dispatch input/change/blur so React state updates.
 */

const DEFAULT_SELECTORS = {
  firstName: 'input[autocomplete="given-name"], input[name*="firstName" i], input[id*="firstName" i]',
  lastName:  'input[autocomplete="family-name"], input[name*="lastName" i], input[id*="lastName" i]',
  email:     'input[autocomplete="email"], input[type="email"], input[name*="email" i]',
  phone:     'input[autocomplete="tel"], input[type="tel"], input[name*="phone" i], input[id*="phone" i]',

  line1:     'input[autocomplete="address-line1"], input[name*="street" i], input[name*="addressLine1" i]',
  line2:     'input[autocomplete="address-line2"], input[name*="addressLine2" i]',
  landmark:  'input[name*="landmark" i], input[name*="hintLine" i]',
  pincode:   'input[autocomplete="postal-code"], input[name*="postal" i], input[name*="zip" i], input[name*="pin" i]',
  city:      'input[autocomplete="address-level2"], input[name*="city" i], input[name*="locality" i]',
  state:     'select[autocomplete="address-level1"], select[name*="state" i], input[name*="state" i]',

  cardNumber: 'input[autocomplete="cc-number"], input[name*="cardNumber" i]',
  cardName:   'input[autocomplete="cc-name"], input[name*="nameOnCard" i]',
  ccExp:      'input[autocomplete="cc-exp"], input[name*="expiration" i]',
  cvv:        'input[autocomplete="cc-csc"], input[name*="cvv" i], input[name*="securityCode" i], input[name*="cvc" i]',
  upiId:      'input[name*="vpa" i], input[name*="upi" i]'
};

const STORAGE_KEYS = ['appleAutofillProfile', 'appleAutofillSelectors', 'appleAutofillOnLoad'];

function setReactValue(el, value) {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
  if (setter) setter.call(el, value); else el.value = value;
  el.dispatchEvent(new Event('input',  { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  el.dispatchEvent(new Event('blur',   { bubbles: true }));
}

function setSelectValue(el, value) {
  const opt = [...el.options].find(o =>
    o.value === value ||
    o.text.trim().toLowerCase() === String(value).trim().toLowerCase()
  );
  if (opt) { el.value = opt.value; el.dispatchEvent(new Event('change', { bubbles: true })); return true; }
  return false;
}

function fill(selector, value, results, label) {
  if (value == null || value === '') return;
  const el = document.querySelector(selector);
  if (!el) { results.missed.push(label); return; }
  if (el.tagName === 'SELECT') { setSelectValue(el, value) ? results.filled.push(label) : results.missed.push(label); }
  else { setReactValue(el, value); results.filled.push(label); }
}

async function runAutofill() {
  const store = await chrome.storage.local.get(STORAGE_KEYS);
  const p = store.appleAutofillProfile;
  if (!p) { console.warn('[AppleAutofill] no profile saved -- open the extension Options first'); return; }
  const sel = Object.assign({}, DEFAULT_SELECTORS, store.appleAutofillSelectors || {});
  const r = { filled: [], missed: [] };

  const c = p.contact || {};
  fill(sel.firstName, c.firstName, r, 'firstName');
  fill(sel.lastName,  c.lastName,  r, 'lastName');
  fill(sel.email,     c.email,     r, 'email');
  fill(sel.phone,     c.phone,     r, 'phone');

  const f = p.fulfillment || {};
  if (f.pickupPerson === 'other') {
    const o = f.other || {};
    fill(sel.firstName, o.firstName, r, 'pickup.firstName');
    fill(sel.lastName,  o.lastName,  r, 'pickup.lastName');
    fill(sel.email,     o.email,     r, 'pickup.email');
    fill(sel.phone,     o.phone,     r, 'pickup.phone');
  }

  if ((f.mode || 'pickup') === 'delivery') {
    const d = p.delivery || {};
    fill(sel.line1, d.line1, r, 'line1');
    fill(sel.line2, d.line2, r, 'line2');
    fill(sel.landmark, d.landmark, r, 'landmark');
    fill(sel.pincode, d.pincode, r, 'pincode');
    fill(sel.city, d.city, r, 'city');
    fill(sel.state, d.state, r, 'state');
  }

  const pay = p.payment || {};
  if (pay.method === 'card') {
    fill(sel.cardNumber, pay.cardNumber, r, 'cardNumber');
    fill(sel.cardName,   pay.cardName,   r, 'cardName');
    fill(sel.ccExp,      pay.exp,        r, 'ccExp');
    fill(sel.cvv,        pay.cvv,        r, 'cvv');
  } else if (pay.method === 'upi') {
    fill(sel.upiId, pay.upiId, r, 'upiId');
  }

  console.log(`[AppleAutofill] filled: ${r.filled.join(', ') || 'none'} | missed: ${r.missed.join(', ') || 'none'}`);
  chrome.runtime.sendMessage({ type: 'autofill-result', filled: r.filled.length, missed: r.missed }).catch(() => {});
  return r;
}

chrome.runtime.onMessage.addListener((msg, _s, sendResponse) => {
  if (msg && msg.type === 'autofill') { runAutofill().then(r => sendResponse(r)); return true; }
});

// Optional: auto-fill when landing on a checkout-ish page (toggle in Options).
chrome.storage.local.get(['appleAutofillOnLoad'], (d) => {
  if (d.appleAutofillOnLoad && /\/shop\/(checkout|bag|fulfillment)|checkout|payment/i.test(location.href)) {
    setTimeout(runAutofill, 1500);
  }
});
