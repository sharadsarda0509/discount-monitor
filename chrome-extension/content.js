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
  firstName: 'input[name="firstName"], input[data-autom="form-field-firstName"], input[autocomplete="given-name"]',
  lastName:  'input[name="lastName"], input[data-autom="form-field-lastName"], input[autocomplete="family-name"]',
  email:     'input[autocomplete="email"], input[type="email"], input[name*="email" i]',
  phone:     'input[autocomplete="tel"], input[type="tel"], input[name*="phone" i], input[id*="phone" i]',

  // Apple IN address (delivery AND the card's Billing Address) uses these exact names.
  // We do NOT set city/state -- entering the PIN triggers Apple's async lookup that
  // fills the City/State dropdown itself; setting it manually fights that.
  line1:     'input[name="street"], input[data-autom="form-field-street"], input[autocomplete="address-line1"]',
  line2:     'input[name="street2"], input[data-autom="form-field-street2"], input[autocomplete="address-line2"]',
  landmark:  'input[name="street3"], input[data-autom="form-field-street3"], input[autocomplete="address-line3"]',
  pincode:   'input[name="postalCode"], input[data-autom="form-field-postalCode"], input[autocomplete="postal-code"]',

  cardNumber: 'input[data-autom="card-number-input"], input[autocomplete="cc-number"]',
  cardName:   'input[autocomplete="cc-name"], input[name*="nameOnCard" i]',
  ccExp:      'input[data-autom="expiration-input"], input[autocomplete="cc-exp"]',
  cvv:        'input[data-autom="security-code-input"], input[autocomplete="cc-csc"]',
  upiId:      'input[name*="vpa" i], input[name*="upi" i]'
};

const STORAGE_KEYS = ['appleAutofillProfile', 'appleAutofillSelectors', 'appleAutofillOnLoad'];

function setReactValue(el, value) {
  const v = String(value);
  try { el.focus(); } catch (e) {}
  // Select any existing content so insertText replaces it, then type via
  // execCommand -- this fires a real InputEvent that stubborn formatted/masked
  // React inputs (Apple's card-number field) accept, unlike a bare value set.
  try { el.setSelectionRange(0, (el.value || '').length); } catch (e) {}
  let ok = false;
  try { ok = document.execCommand('insertText', false, v); } catch (e) {}
  if (!ok || !el.value) {
    // Fallback for plain fields / inputs where execCommand is a no-op (e.g. number).
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (setter) setter.call(el, v); else el.value = v;
    el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: v }));
  }
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
  const els = [...document.querySelectorAll(selector)].filter((el) =>
    ['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName) && !el.disabled && !el.readOnly && el.offsetParent !== null
  );  // fill ALL editable, visible matches (delivery + billing); skip view-mode spans
  if (!els.length) { results.missed.push(label); return; }
  let done = 0;
  for (const el of els) {
    if (el.tagName === 'SELECT') { if (setSelectValue(el, value)) done++; }
    else { setReactValue(el, value); done++; }
  }
  if (done) results.filled.push(els.length > 1 ? `${label}x${done}` : label);
  else results.missed.push(label);
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

  // Address block(s): the card's Billing Address is present even for PICKUP, so fill
  // address fields regardless of mode. fill() targets all matches (delivery + billing).
  const d = p.delivery || {};
  fill(sel.line1, d.line1, r, 'street');
  fill(sel.line2, d.line2, r, 'street2');
  fill(sel.landmark, d.landmark, r, 'landmark');
  // PIN last: entering it triggers Apple's async City/State auto-lookup -- we don't
  // touch the City/State dropdown ourselves.
  fill(sel.pincode, d.pincode, r, 'pincode');

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
