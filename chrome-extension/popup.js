const $ = (id) => document.getElementById(id);
const status = (t) => { $('status').textContent = t; };

$('open').addEventListener('click', async () => {
  const { appleAutofillProfile: p } = await chrome.storage.local.get('appleAutofillProfile');
  const url = p && p.buyUrl;
  if (!url) { status('Set a buy URL in config first.'); return; }
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  chrome.tabs.update(tab.id, { url });
  status('Opening buy page…');
});

$('fill').addEventListener('click', async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  try {
    const r = await chrome.tabs.sendMessage(tab.id, { type: 'autofill' });
    if (r) status(`Filled ${r.filled.length}${r.missed.length ? `, missed: ${r.missed.join(', ')}` : ''}`);
    else status('No response — are you on apple.com/in?');
  } catch (e) {
    status('Open an apple.com/in checkout page first.');
  }
});

$('opts').addEventListener('click', (e) => { e.preventDefault(); chrome.runtime.openOptionsPage(); });
