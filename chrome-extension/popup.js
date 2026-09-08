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

async function autofillTab(tabId) {
  try {
    return await chrome.tabs.sendMessage(tabId, { type: 'autofill' });
  } catch (e) {
    // Content script not present (e.g. page loaded before the extension, or a
    // host added later like secure8.store.apple.com). Inject it on demand via
    // activeTab, then retry.
    await chrome.scripting.executeScript({ target: { tabId }, files: ['content.js'] });
    await new Promise((r) => setTimeout(r, 350));
    return await chrome.tabs.sendMessage(tabId, { type: 'autofill' });
  }
}

$('fill').addEventListener('click', async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!/^https:\/\/[^/]*\.apple\.com\/in\//.test(tab.url || '')) {
    status('Open an apple.com/in (or store.apple.com/in) page first.');
    return;
  }
  try {
    const r = await autofillTab(tab.id);
    if (r) status(`Filled ${r.filled.length}${r.missed.length ? `, missed: ${r.missed.join(', ')}` : ''}`);
    else status('No response from the page.');
  } catch (e) {
    status('Could not inject on this page: ' + (e.message || e));
  }
});

$('reset').addEventListener('click', async () => {
  status('Rotating persona, clearing cookies/storage…');
  try {
    const r = await chrome.runtime.sendMessage({ type: 'reset-open' });
    if (r && r.error) status('Reset failed: ' + r.error);
    else if (r && r.opened) status('Fresh session — buy page opening.');
    else status('Cleared. Set a buy URL in config to auto-open.');
  } catch (e) {
    status('Reset failed: ' + (e.message || e));
  }
});

$('opts').addEventListener('click', (e) => { e.preventDefault(); chrome.runtime.openOptionsPage(); });
