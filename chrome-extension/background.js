/* Service worker: routes the keyboard command + popup actions to the content script. */

chrome.commands.onCommand.addListener(async (command) => {
  if (command !== 'autofill') return;
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || tab.id == null) return;
  try {
    await chrome.tabs.sendMessage(tab.id, { type: 'autofill' });
  } catch (e) {
    // Inject on demand if the content script isn't there yet, then retry.
    try {
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ['content.js'] });
      setTimeout(() => chrome.tabs.sendMessage(tab.id, { type: 'autofill' }).catch(() => {}), 350);
    } catch (_) {}
  }
});
