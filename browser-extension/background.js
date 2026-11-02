// SwiftGet browser integration for Chrome / Edge / Brave (Manifest V3) and Firefox.
// Catches downloads of the file types chosen in SwiftGet > Settings > Browser
// and hands them to the desktop app, which listens on 127.0.0.1 only.

// Firefox offers the promise-based `browser` namespace; Chrome's `chrome` (MV3) returns promises too.
const ext = globalThis.browser || globalThis.chrome;
const toolbar = ext.action || ext.browserAction;
const DEFAULT_PORT = 9614;
const bypass = new Set(); // URLs the browser should download itself (fallback)

async function settings() {
  return ext.storage.local.get({ port: DEFAULT_PORT, intercept: true });
}

async function callApp(path, options = {}, timeoutMs = 2000) {
  const { port } = await settings();
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(`http://127.0.0.1:${port}${path}`, { ...options, signal: ctrl.signal });
    if (!res.ok) return null;
    return await res.json();
  } catch (e) {
    return null; // app not running
  } finally {
    clearTimeout(timer);
  }
}

async function cookiesFor(url) {
  try {
    const list = await ext.cookies.getAll({ url });
    return list.map((c) => `${c.name}=${c.value}`).join("; ");
  } catch (e) {
    return "";
  }
}

async function sendToApp({ url, filename, referrer, fileSize }) {
  const body = {
    url,
    filename: filename || "",
    referrer: referrer || "",
    cookies: await cookiesFor(url),
    userAgent: navigator.userAgent,
    fileSize: fileSize || -1,
  };
  const res = await callApp("/download", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-SwiftGet": "1" },
    body: JSON.stringify(body),
  });
  return !!(res && res.ok);
}

function baseName(path) {
  return (path || "").split(/[\\/]/).pop();
}

function extensionOf(name) {
  const clean = (name || "").split("?")[0].split("#")[0];
  const i = clean.lastIndexOf(".");
  return i >= 0 ? clean.slice(i + 1).toLowerCase() : "";
}

async function handleDownload(item) {
  const url = item.finalUrl || item.url;
  if (!/^(https?|ftp):/i.test(url)) return; // blob:, data:, file: stay in the browser
  if (bypass.has(url) || bypass.has(item.url)) {
    bypass.delete(url);
    bypass.delete(item.url);
    return;
  }
  const { intercept } = await settings();
  if (!intercept) return;
  const cfg = await callApp("/config");
  if (!cfg || !cfg.enabled) return;

  let name = baseName(item.filename);
  if (!name) {
    try { name = baseName(decodeURIComponent(new URL(url).pathname)); } catch (e) { name = ""; }
  }
  const extName = extensionOf(name) || extensionOf(url);
  if (!cfg.extensions.includes(extName)) return;
  const size = item.totalBytes > 0 ? item.totalBytes : item.fileSize;
  if (cfg.minSizeKb > 0 && size > 0 && size < cfg.minSizeKb * 1024) return;

  try {
    await ext.downloads.cancel(item.id);
  } catch (e) {
    // Already finished (tiny file): remove what the browser saved.
    try { await ext.downloads.removeFile(item.id); } catch (e2) { /* ignore */ }
  }
  try { await ext.downloads.erase({ id: item.id }); } catch (e) { /* ignore */ }

  const ok = await sendToApp({ url, filename: name, referrer: item.referrer, fileSize: size });
  if (!ok) {
    // SwiftGet refused or stopped - let the browser download it instead.
    bypass.add(url);
    ext.downloads.download({ url });
  }
}

if (ext.downloads.onDeterminingFilename) {
  // Chrome / Edge: decide once the browser knows the real file name (Content-Disposition etc.)
  ext.downloads.onDeterminingFilename.addListener((item, suggest) => {
    handleDownload(item).finally(() => {
      try { suggest(); } catch (e) { /* already cancelled */ }
    });
    return true; // suggest() is called asynchronously
  });
} else {
  // Firefox: the file name is already known when the download is created.
  ext.downloads.onCreated.addListener((item) => { handleDownload(item); });
}

// Right-click menu (re-created whenever the background script starts)
async function setupMenu() {
  try { await ext.contextMenus.removeAll(); } catch (e) { /* ignore */ }
  ext.contextMenus.create({
    id: "swiftget-link",
    title: "Download with SwiftGet",
    contexts: ["link", "image", "video", "audio"],
  });
}
setupMenu();

ext.contextMenus.onClicked.addListener(async (info) => {
  if (info.menuItemId !== "swiftget-link") return;
  const url = info.linkUrl || info.srcUrl;
  if (!url) return;
  const ok = await sendToApp({ url, referrer: info.pageUrl });
  if (!ok && toolbar) {
    toolbar.setBadgeBackgroundColor({ color: "#c62828" });
    toolbar.setBadgeText({ text: "off" });
    setTimeout(() => toolbar.setBadgeText({ text: "" }), 4000);
  }
});

// Messages from the "Download this video" button (content.js)
function postJson(path, body, timeoutMs) {
  return callApp(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-SwiftGet": "1" },
    body: JSON.stringify(body),
  }, timeoutMs);
}

ext.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || typeof msg !== "object") return false;
  let job = null;
  if (msg.type === "config") {
    job = callApp("/config").then((cfg) => cfg || { videoButton: false });
  } else if (msg.type === "videoInfo") {
    job = postJson("/video/info", { url: msg.url }, 150000).then((r) => r || { offline: true });
  } else if (msg.type === "videoDownload") {
    job = postJson("/video/download", { url: msg.url, quality: msg.quality, title: msg.title, size: msg.size })
      .then((r) => r || { offline: true });
  }
  if (!job) return false;
  job.then(sendResponse, () => sendResponse(null));
  return true; // answer asynchronously
});
