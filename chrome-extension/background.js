/* Service worker: routes the keyboard command + popup actions to the content
 * script, and implements the "Reset & open fresh" flow.
 *
 * Fresh-client-per-attempt model: Apple links checkout attempts primarily via
 * cookies + per-origin storage, and secondarily via device fingerprint. "Reset"
 * rotates a persona *seed*, wipes both, then navigates to the buy URL -- i.e. it
 * resets BEFORE the session, because clearing/spoofing after autofill (mid-
 * checkout) would nuke the live cart/session and change nothing already sent.
 *
 * Honest caveat: the canvas/WebGL spoof injects a per-persona fingerprint into an
 * otherwise-real browser. A mismatch (spoofed GPU/canvas + real everything else)
 * can read as MORE anomalous to anti-fraud, not less -- randomization noise is
 * itself a signal. It's a consistent seeded persona (not per-call random) to
 * minimise that, it's inert until you use Reset, and it's one toggle to disable.
 */

const APPLE_RE = /^https:\/\/([^/]*\.)?apple\.com\//i;

/* --- autofill (unchanged) --------------------------------------------------- */
chrome.commands.onCommand.addListener(async (command) => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || tab.id == null) return;
  if (command === 'autofill') return sendAutofill(tab.id);
  if (command === 'reset-open') return resetAndOpen(tab).catch(() => {});
});

async function sendAutofill(tabId) {
  try {
    await chrome.tabs.sendMessage(tabId, { type: 'autofill' });
  } catch (e) {
    // Inject on demand if the content script isn't there yet, then retry.
    try {
      await chrome.scripting.executeScript({ target: { tabId }, files: ['content.js'] });
      setTimeout(() => chrome.tabs.sendMessage(tabId, { type: 'autofill' }).catch(() => {}), 350);
    } catch (_) {}
  }
}

chrome.runtime.onMessage.addListener((msg, _s, sendResponse) => {
  if (msg && msg.type === 'reset-open') {
    chrome.tabs.query({ active: true, currentWindow: true }).then(([tab]) => {
      resetAndOpen(tab).then(sendResponse, (e) => sendResponse({ error: String(e) }));
    });
    return true; // async
  }
});

/* --- reset + open fresh ------------------------------------------------------ */
async function resetAndOpen(tab) {
  if (!tab || tab.id == null) throw new Error('no active tab');
  const { appleAutofillProfile: p } = await chrome.storage.local.get('appleAutofillProfile');
  const buyUrl = p && p.buyUrl;

  // New persona seed = "reset the spoof". Consumed by applyFpSpoof on next load.
  const seed = crypto.getRandomValues(new Uint32Array(1))[0] >>> 0;
  // fpPendingClear: wipe sessionStorage once, on the reset navigation only (not on
  // later reloads, which would kill an in-progress cart).
  await chrome.storage.local.set({ fpSeed: seed, fpPendingClear: true });

  const cleared = await clearAppleData(tab);

  console.log(`[AppleReset] seed=${seed} clearedCookies=${cleared} opened=${buyUrl || '(reload)'}`);
  if (buyUrl) await chrome.tabs.update(tab.id, { url: buyUrl });
  else await chrome.tabs.reload(tab.id); // no buy URL configured -> just reopen clean
  return { ok: true, seed, clearedCookies: cleared, opened: !!buyUrl };
}

async function clearAppleData(tab) {
  // Cookies: the main cross-attempt linker. Enumerate by registrable domain so we
  // catch every apple subdomain (www, store, secureN.store, ...), then remove each.
  let removed = 0;
  try {
    const cookies = await chrome.cookies.getAll({ domain: 'apple.com' });
    await Promise.all(cookies.map((c) => {
      const host = c.domain.replace(/^\./, '');
      const url = `http${c.secure ? 's' : ''}://${host}${c.path}`;
      return chrome.cookies.remove({ url, name: c.name, storeId: c.storeId })
        .then(() => { removed++; }).catch(() => {});
    }));
  } catch (e) { /* cookies perm missing / other -- non-fatal */ }

  // Per-origin storage (localStorage/IndexedDB/CacheStorage/SW/cache). origins
  // filtering only applies to cookies+cache+storage, which is exactly this set.
  const origins = new Set([
    'https://www.apple.com', 'https://apple.com', 'https://store.apple.com',
    'https://secure.store.apple.com', 'https://secure2.store.apple.com',
    'https://secure5.store.apple.com', 'https://secure6.store.apple.com',
    'https://secure7.store.apple.com', 'https://secure8.store.apple.com'
  ]);
  try { if (tab.url) origins.add(new URL(tab.url).origin); } catch (e) {}
  try {
    await chrome.browsingData.remove(
      { origins: [...origins] },
      { cookies: true, localStorage: true, indexedDB: true, cacheStorage: true,
        serviceWorkers: true, cache: true, fileSystems: true, webSQL: true }
    );
  } catch (e) { /* browsingData perm missing / other -- non-fatal */ }
  return removed;
}

/* --- spoof injection: MAIN world, as early as possible ---------------------- */
chrome.webNavigation.onCommitted.addListener(async (d) => {
  if (!APPLE_RE.test(d.url)) return;
  const { fpSeed, fpSpoofEnabled, fpPendingClear } =
    await chrome.storage.local.get(['fpSeed', 'fpSpoofEnabled', 'fpPendingClear']);
  const target = { tabId: d.tabId, frameIds: [d.frameId] };

  // One-shot sessionStorage/localStorage wipe on the reset navigation (top frame).
  if (fpPendingClear && d.frameId === 0) {
    try {
      await chrome.scripting.executeScript({
        target, injectImmediately: true,
        func: () => { try { sessionStorage.clear(); } catch (e) {} try { localStorage.clear(); } catch (e) {} }
      });
    } catch (e) {}
    await chrome.storage.local.set({ fpPendingClear: false });
  }

  // Spoof only after a Reset has produced a seed, AND only if explicitly enabled
  // (default OFF -- see README "Honest caveat": a spoofed canvas/WebGL persona on
  // an otherwise-real browser is itself a mismatch signal to Akamai/anti-fraud and
  // can cause Add to Bag to be blocked more often, not less).
  if (fpSeed != null && fpSpoofEnabled === true) {
    try {
      await chrome.scripting.executeScript({
        target, world: 'MAIN', injectImmediately: true, func: applyFpSpoof, args: [fpSeed]
      });
    } catch (e) {}
  }
});

/* Runs in the page's MAIN world at document_start. Self-contained (no closures):
 * executeScript serialises the function body and injects it with `seed` as arg. */
function applyFpSpoof(seed) {
  try {
    if (window.__fpSpoofed) return;           // idempotent per document
    window.__fpSpoofed = seed;

    // mulberry32: seeded PRNG -> the SAME noise every call this session, a NEW
    // persona only after the next Reset. Consistency avoids the "random noise is
    // a signal" trap.
    let s = seed >>> 0;
    const rnd = () => {
      s = (s + 0x6D2B79F5) | 0;
      let t = Math.imul(s ^ (s >>> 15), 1 | s);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
    const clamp = (v) => (v < 0 ? 0 : v > 255 ? 255 : v);
    const shift = { r: (rnd() * 10 | 0) - 5, g: (rnd() * 10 | 0) - 5,
                    b: (rnd() * 10 | 0) - 5, a: (rnd() * 6 | 0) - 3 };
    const noisify = (data) => {
      for (let i = 0; i < data.length; i += 4) {
        data[i]     = clamp(data[i]     + shift.r);
        data[i + 1] = clamp(data[i + 1] + shift.g);
        data[i + 2] = clamp(data[i + 2] + shift.b);
        data[i + 3] = clamp(data[i + 3] + shift.a);
      }
    };

    // Capture the pristine getImageData before we wrap it, so canvas readbacks and
    // toDataURL both noise exactly once off the clean pixels.
    const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function () {
      const img = origGetImageData.apply(this, arguments);
      noisify(img.data);
      return img;
    };
    const wrapExport = (name) => {
      const orig = HTMLCanvasElement.prototype[name];
      if (!orig) return;
      HTMLCanvasElement.prototype[name] = function () {
        try {
          const ctx = this.getContext('2d');
          if (ctx && this.width && this.height) {
            const d = origGetImageData.call(ctx, 0, 0, this.width, this.height);
            noisify(d.data);
            ctx.putImageData(d, 0, 0);
          }
        } catch (e) {}
        return orig.apply(this, arguments);
      };
    };
    wrapExport('toDataURL');
    wrapExport('toBlob');

    // WebGL: only rotate the vendor/renderer STRING within your REAL GPU's
    // vendor family (Apple/Intel/NVIDIA/AMD), never across it. Swapping to a
    // different vendor family (e.g. reporting NVIDIA on a MacBook, which never
    // ships a discrete NVIDIA GPU with Chrome's ANGLE/Metal renderer) is a much
    // bigger red flag than no spoof at all -- it's a checkable, deterministic
    // contradiction. If we can't confidently detect the real family, we skip
    // the WebGL vendor/renderer spoof entirely and only keep the canvas noise
    // above (which is subtle and vendor-agnostic).
    let realRenderer = '';
    try {
      const probe = document.createElement('canvas');
      const gl = probe.getContext('webgl') || probe.getContext('experimental-webgl');
      const dbg = gl && gl.getExtension('WEBGL_debug_renderer_info');
      if (dbg) realRenderer = String(gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) || '');
    } catch (e) {}

    const personaGroups = {
      apple: [
        ['Google Inc. (Apple)', 'ANGLE (Apple, ANGLE Metal Renderer: Apple M1, Unspecified Version)'],
        ['Google Inc. (Apple)', 'ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified Version)'],
        ['Google Inc. (Apple)', 'ANGLE (Apple, ANGLE Metal Renderer: Apple M3, Unspecified Version)']
      ],
      intel: [
        ['Google Inc. (Intel)', 'ANGLE (Intel, Intel(R) Iris(TM) Plus Graphics 640, OpenGL 4.1)'],
        ['Google Inc. (Intel)', 'ANGLE (Intel, Intel(R) UHD Graphics 620, OpenGL 4.1)'],
        ['Google Inc. (Intel)', 'ANGLE (Intel, Intel(R) Iris(TM) Xe Graphics, OpenGL 4.1)']
      ],
      nvidia: [
        ['Google Inc. (NVIDIA)', 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)'],
        ['Google Inc. (NVIDIA)', 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1660 Direct3D11 vs_5_0 ps_5_0, D3D11)']
      ],
      amd: [
        ['Google Inc. (AMD)', 'ANGLE (AMD, AMD Radeon Pro 5500M Direct3D11 vs_5_0 ps_5_0, D3D11)'],
        ['Google Inc. (AMD)', 'ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0, D3D11)']
      ]
    };
    const family = /apple/i.test(realRenderer) ? 'apple'
      : /nvidia|geforce|gtx|rtx/i.test(realRenderer) ? 'nvidia'
      : /amd|radeon/i.test(realRenderer) ? 'amd'
      : /intel/i.test(realRenderer) ? 'intel'
      : null;
    const pool = family && personaGroups[family];

    if (pool && pool.length) {
      const persona = pool[(seed >>> 0) % pool.length];
      const patchGL = (proto) => {
        if (!proto || !proto.getParameter) return;
        const gp = proto.getParameter;
        proto.getParameter = function (p) {
          if (p === 37445) return persona[0]; // UNMASKED_VENDOR_WEBGL
          if (p === 37446) return persona[1]; // UNMASKED_RENDERER_WEBGL
          return gp.apply(this, arguments);
        };
      };
      if (window.WebGLRenderingContext)  patchGL(WebGLRenderingContext.prototype);
      if (window.WebGL2RenderingContext) patchGL(WebGL2RenderingContext.prototype);
      console.log('[AppleReset] fp spoof installed — seed', seed, '| family', family, '| webgl', persona[1]);
    } else {
      console.log('[AppleReset] fp spoof: canvas noise only — real GPU family unrecognised, skipping WebGL vendor swap to avoid mismatch');
    }
  } catch (e) { /* never break the page */ }
}
