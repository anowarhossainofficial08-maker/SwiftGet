// SwiftGet "Download this video" button.
// Shows a small button at the top-right of any playing-size <video> (YouTube, Facebook, Vimeo, …)
// while the mouse is over it. Clicking lists the qualities (read by SwiftGet using yt-dlp);
// picking one sends the page to SwiftGet, which downloads it.
(() => {
  if (window.__swiftgetVideoButton) return;
  window.__swiftgetVideoButton = true;

  const ext = globalThis.browser || globalThis.chrome;
  const MIN_W = 200, MIN_H = 110;
  let enabled = null;           // null = not asked yet
  let host = null, root = null, btn = null, menu = null;
  let current = null;           // the <video> the button belongs to
  let hideTimer = null, menuOpen = false, lastMove = 0;

  function send(msg) {
    try {
      return Promise.resolve(ext.runtime.sendMessage(msg)).catch(() => null);
    } catch (e) {
      return Promise.resolve(null); // extension was reloaded; page needs a refresh
    }
  }

  async function isEnabled() {
    if (enabled === null) {
      const cfg = await send({ type: "config" });
      enabled = !!(cfg && cfg.videoButton);
      setTimeout(() => { enabled = null; }, 30000); // re-check now and then
    }
    return enabled;
  }

  function pageUrl() {
    return location.href;
  }

  function fmtSize(n) {
    if (!n || n <= 0) return "";
    const u = ["B", "KB", "MB", "GB"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : n.toFixed(0)) + " " + u[i];
  }

  function build() {
    host = document.createElement("div");
    host.style.cssText = "all:initial;position:fixed;z-index:2147483647;top:0;left:0;display:none;";
    root = host.attachShadow({ mode: "closed" });
    root.innerHTML = `
      <style>
        * { box-sizing: border-box; font-family: "Segoe UI", system-ui, -apple-system, sans-serif; }
        .btn { display:flex; align-items:center; gap:6px; border:0; cursor:pointer; padding:7px 12px 7px 9px;
               border-radius:8px; background:#0067c0; color:#fff; font-size:13px; font-weight:600;
               box-shadow:0 2px 10px rgba(0,0,0,.35); opacity:.95; }
        .btn:hover { background:#0a74d4; opacity:1; }
        .btn svg { width:16px; height:16px; }
        .menu { margin-top:6px; min-width:250px; max-width:320px; background:#202020; color:#f3f3f3;
                border-radius:10px; padding:6px; box-shadow:0 8px 24px rgba(0,0,0,.45); font-size:13px; }
        .title { padding:6px 8px 8px; color:#bdbdbd; font-size:12px; white-space:nowrap; overflow:hidden;
                 text-overflow:ellipsis; border-bottom:1px solid #333; margin-bottom:4px; }
        .item { display:flex; justify-content:space-between; gap:14px; padding:8px 10px; border-radius:6px;
                cursor:pointer; }
        .item:hover { background:#0067c0; }
        .item .d { color:#a8a8a8; }
        .item:hover .d { color:#e6f0fb; }
        .msg { padding:8px 10px; color:#d0d0d0; line-height:1.35; }
        .ok { color:#6ccb5f; }
        .err { color:#ff99a4; }
        .bar { height:4px; background:#3b3b3b; border-radius:2px; margin:6px 10px 4px; overflow:hidden; }
        .bar > div { height:100%; background:#4cc2ff; width:0; transition:width .3s; }
      </style>
      <button class="btn" title="Download with SwiftGet">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"
             stroke-linejoin="round"><path d="M12 3v12M6 10l6 6 6-6M5 20h14"/></svg>
        Download this video
      </button>
      <div class="menu" hidden></div>`;
    btn = root.querySelector(".btn");
    menu = root.querySelector(".menu");
    for (const type of ["mousedown", "mouseup", "click", "dblclick", "pointerdown", "pointerup"]) {
      host.addEventListener(type, (e) => e.stopPropagation());
    }
    host.addEventListener("mouseenter", () => clearTimeout(hideTimer));
    host.addEventListener("mouseleave", () => scheduleHide(1500));
    btn.addEventListener("click", (e) => { e.preventDefault(); toggleMenu(); });
    attach();
  }

  function attach() {
    const parent = document.fullscreenElement || document.documentElement;
    if (host && host.parentNode !== parent) parent.appendChild(host);
  }

  function place() {
    if (!current || !host) return;
    const r = current.getBoundingClientRect();
    if (r.width < MIN_W || r.height < MIN_H || r.bottom < 0 || r.top > innerHeight) { hide(); return; }
    const w = host.offsetWidth || 190;
    host.style.left = Math.max(4, Math.min(innerWidth - w - 4, r.right - w - 10)) + "px";
    host.style.top = Math.max(4, r.top + 10) + "px";
  }

  function show(video) {
    if (!host) build();
    attach();
    if (current !== video) { current = video; closeMenu(); }
    host.style.display = "block";
    place();
    clearTimeout(hideTimer);
  }

  function hide() {
    if (host) host.style.display = "none";
    closeMenu();
    current = null;
  }

  function scheduleHide(ms) {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => { if (!menuOpen) hide(); }, ms);
  }

  function closeMenu() {
    menuOpen = false;
    if (menu) { menu.hidden = true; menu.replaceChildren(); }
  }

  function el(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function setMenu(...nodes) {
    menu.replaceChildren(...nodes);
    menu.hidden = false;
    menuOpen = true;
    place();
  }

  function msg(text, cls) {
    return el("div", "msg" + (cls ? " " + cls : ""), text);
  }

  async function toggleMenu() {
    if (menuOpen) { closeMenu(); return; }
    setMenu(msg("Reading available qualities…"));
    loadInfo(pageUrl());
  }

  async function loadInfo(url) {
    const res = await send({ type: "videoInfo", url });
    if (!menuOpen) return;
    if (!res || res.offline) {
      setMenu(msg("SwiftGet is not running. Start SwiftGet and try again.", "err"));
      return;
    }
    if (res.preparing || (res.message && !res.qualities && !res.error)) {
      const bar = el("div", "bar");
      const fill = el("div");
      fill.style.width = Math.max(2, res.progress || 0) + "%";
      bar.appendChild(fill);
      setMenu(msg(res.message || "Setting up video support (first time only)…"), bar);
      setTimeout(() => { if (menuOpen) loadInfo(url); }, 1500);
      return;
    }
    if (res.error) {
      setMenu(msg(res.error, "err"));
      return;
    }
    const title = el("div", "title", res.title);
    title.title = res.title;
    const items = res.qualities.map((q) => {
      const item = el("div", "item");
      item.appendChild(el("span", "", q.label));
      item.appendChild(el("span", "d", q.detail + (q.size > 0 ? " · " + fmtSize(q.size) : "")));
      item.addEventListener("click", async () => {
        setMenu(msg("Sending to SwiftGet…"));
        const r = await send({ type: "videoDownload", url, quality: q.id, title: res.title, size: q.size });
        if (r && r.ok) {
          setMenu(msg("✓ Downloading in SwiftGet", "ok"));
          setTimeout(() => { closeMenu(); scheduleHide(800); }, 1800);
        } else {
          setMenu(msg("Could not reach SwiftGet.", "err"));
        }
      });
      return item;
    });
    setMenu(title, ...items);
  }

  function videoAt(x, y) {
    let best = null;
    for (const v of document.querySelectorAll("video")) {
      const r = v.getBoundingClientRect();
      if (r.width >= MIN_W && r.height >= MIN_H && x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) {
        if (!best || r.width * r.height > best.r.width * best.r.height) best = { v, r };
      }
    }
    return best && best.v;
  }

  document.addEventListener("mousemove", async (e) => {
    const now = Date.now();
    if (now - lastMove < 120) return;
    lastMove = now;
    if (host && e.composedPath().includes(host)) return;
    const v = videoAt(e.clientX, e.clientY);
    if (v) {
      if (!(await isEnabled())) return;
      show(v);
      scheduleHide(3500);
    } else if (current && !menuOpen) {
      scheduleHide(1200);
    }
  }, { passive: true, capture: true });

  document.addEventListener("scroll", place, { passive: true, capture: true });
  window.addEventListener("resize", place, { passive: true });
  document.addEventListener("fullscreenchange", () => { attach(); place(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && menuOpen) closeMenu(); }, true);
})();
