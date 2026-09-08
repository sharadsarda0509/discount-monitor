const $ = (id) => document.getElementById(id);
const val = (id) => $(id).value.trim();

function toggleConditional() {
  $('otherWrap').style.display = $('pickupPerson').value === 'other' ? 'block' : 'none';
  const m = $('method').value;
  $('cardWrap').style.display = m === 'card' ? 'block' : 'none';
  $('upiWrap').style.display  = m === 'upi'  ? 'block' : 'none';
}
$('pickupPerson').addEventListener('change', toggleConditional);
$('method').addEventListener('change', toggleConditional);

async function load() {
  const { appleAutofillProfile: p, appleAutofillSelectors: s, appleAutofillOnLoad: on, fpSpoofEnabled: spoof } =
    await chrome.storage.local.get(['appleAutofillProfile', 'appleAutofillSelectors', 'appleAutofillOnLoad', 'fpSpoofEnabled']);
  if (p) {
    $('buyUrl').value = p.buyUrl || '';
    const c = p.contact || {}; $('firstName').value = c.firstName || ''; $('lastName').value = c.lastName || '';
    $('email').value = c.email || ''; $('phone').value = c.phone || '';
    const f = p.fulfillment || {}; $('mode').value = f.mode || 'pickup'; $('store').value = f.store || '';
    $('pickupPerson').value = f.pickupPerson || 'self';
    const o = f.other || {}; $('oFirst').value = o.firstName || ''; $('oLast').value = o.lastName || '';
    $('oEmail').value = o.email || ''; $('oPhone').value = o.phone || '';
    const d = p.delivery || {}; $('line1').value = d.line1 || ''; $('line2').value = d.line2 || '';
    $('landmark').value = d.landmark || ''; $('pincode').value = d.pincode || ''; $('cityState').value = d.cityState || '';
    const pay = p.payment || {}; $('method').value = pay.method || 'upi'; $('upiId').value = pay.upiId || '';
    $('cardNumber').value = pay.cardNumber || ''; $('cardName').value = pay.cardName || ''; $('exp').value = pay.exp || ''; $('cvv').value = pay.cvv || '';
  }
  if (s) $('selectors').value = JSON.stringify(s, null, 2);
  $('onload').checked = !!on;
  $('spoof').checked = spoof !== false; // default on (inert until first Reset)
  toggleConditional();
}

$('save').addEventListener('click', async () => {
  const profile = {
    buyUrl: val('buyUrl'),
    contact: { firstName: val('firstName'), lastName: val('lastName'), email: val('email'), phone: val('phone') },
    fulfillment: {
      mode: val('mode'), store: val('store'), pickupPerson: val('pickupPerson'),
      other: { firstName: val('oFirst'), lastName: val('oLast'), email: val('oEmail'), phone: val('oPhone') }
    },
    delivery: { line1: val('line1'), line2: val('line2'), landmark: val('landmark'), pincode: val('pincode'), cityState: val('cityState') },
    payment: { method: val('method'), upiId: val('upiId'), cardNumber: val('cardNumber'), cardName: val('cardName'), exp: val('exp'), cvv: val('cvv') }
  };
  let selectors = {};
  const raw = val('selectors');
  if (raw) { try { selectors = JSON.parse(raw); } catch (e) { $('saved').textContent = 'Selector JSON invalid — not saved'; $('saved').style.color = '#c00'; return; } }

  await chrome.storage.local.set({
    appleAutofillProfile: profile,
    appleAutofillSelectors: selectors,
    appleAutofillOnLoad: $('onload').checked,
    fpSpoofEnabled: $('spoof').checked
  });
  $('saved').style.color = '#1a7f37';
  $('saved').textContent = 'Saved ✓';
  setTimeout(() => ($('saved').textContent = ''), 2000);
});

load();
