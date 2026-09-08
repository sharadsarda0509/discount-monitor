/* Service worker: routes the keyboard command + popup actions to the content script. */

chrome.commands.onCommand.addListener((command) => {
  if (command !== 'autofill') return;
  chrome.tabs.query({ active: true, currentWindow: true }, ([tab]) => {
    if (tab && tab.id != null) chrome.tabs.sendMessage(tab.id, { type: 'autofill' }).catch(() => {});
  });
});
