/* Shared helpers: REST calls, WebSocket with auto-reconnect, toasts, escaping. */

/* Theme: white background by default, dark available via the floating toggle.
   The robot face page is excluded - it is the robot's screen and stays dark. */
(function initTheme() {
  const theme = localStorage.getItem("cat_theme2") || "light";   // white by default
  document.documentElement.dataset.theme = theme;
  if (location.pathname === "/robot") return;
  const btn = document.createElement("button");
  btn.id = "themeBtn";
  btn.title = "Toggle light / dark theme";
  btn.textContent = theme === "light" ? "\u{1F319}" : "\u2600\uFE0F";
  btn.onclick = () => {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("cat_theme2", next);
    btn.textContent = next === "light" ? "\u{1F319}" : "\u2600\uFE0F";
  };
  if (document.body) document.body.appendChild(btn);
  else document.addEventListener("DOMContentLoaded", () => document.body.appendChild(btn));
})();

/* Shared top navigation for teacher-facing pages (consistent everywhere).
   Display screens (student / robot / projector) stay clean and nav-free. */
(function initNav() {
  const path = location.pathname;
  if (!["/", "/teacher", "/tracking"].includes(path)) return;
  const link = (href, label, title) => {
    const active = path === href ? " active" : "";
    const blank = title ? ` target="_blank" title="${title}"` : "";
    return `<a class="nav-link${active}" href="${href}"${blank}>${label}</a>`;
  };
  const nav = document.createElement("nav");
  nav.id = "catNav";
  nav.innerHTML = `
    <a class="nav-brand" href="/">\u{1F916} CAT</a>
    ${link("/teacher", "\u{1F4CA} Dashboard")}
    ${link("/tracking", "\u{1F50D} Tracking")}
    <span class="nav-sep"></span>
    <span class="nav-group">Displays</span>
    ${link("/robot", "\u{1F60C} Robot \u2197", "Open on the robot's screen")}
    ${link("/projector", "\u{1F4FA} Projector \u2197", "Open on the classroom projector")}
    ${link("/student", "\u{1F4F1} Student \u2197", "Preview the student app")}
    <span class="spacer"></span>
    <button class="nav-link" id="navLogout" style="display:none">\u{1F6AA} Logout</button>`;
  const mount = () => document.body.prepend(nav);
  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount);
  fetch("/api/auth/me").then((r) => r.json()).then((d) => {
    if (d.authenticated) {
      const b = nav.querySelector("#navLogout");
      b.style.display = "";
      b.onclick = logoutTeacher;
    }
  }).catch(() => {});
})();

/* Back-to-home button on the display pages (robot / projector / student). */
(function initBackButton() {
  const path = location.pathname;
  if (!["/robot", "/projector", "/student"].includes(path)) return;
  const btn = document.createElement("a");
  btn.id = "backBtn";
  btn.href = "/";
  btn.title = "Back to home";
  btn.setAttribute("aria-label", "Back to home");
  btn.innerHTML = "&#8592;";
  const mount = () => {
    if (path === "/student") {
      const top = document.querySelector(".top");
      if (top) { btn.classList.add("inline"); top.prepend(btn); return; }
    }
    document.body.appendChild(btn);
  };
  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount);
})();

async function api(path, body, method) {
  method = method || (body === undefined ? "GET" : "POST");
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && data.auth_required) {
    location.href = "/login?next=" + encodeURIComponent(location.pathname);
    throw new Error("Login required");
  }
  if (!res.ok) throw Object.assign(new Error(data.error || "Request failed"), { data });
  return data;
}

async function logoutTeacher() {
  await fetch("/api/auth/logout", { method: "POST" });
  location.href = "/login";
}

function connectWS(role, handler) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  let ws = null;
  function open() {
    ws = new WebSocket(`${proto}://${location.host}/ws?role=${role}`);
    ws.onmessage = (e) => {
      try { handler(JSON.parse(e.data)); } catch (err) { console.error(err); }
    };
    ws.onclose = () => setTimeout(open, 1500);
    ws.onerror = () => ws.close();
  }
  open();
  setInterval(() => { if (ws && ws.readyState === 1) ws.send("ping"); }, 20000);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function toast(text, level = "info") {
  let host = document.getElementById("toasts");
  if (!host) {
    host = document.createElement("div");
    host.id = "toasts";
    document.body.appendChild(host);
  }
  const t = document.createElement("div");
  t.className = `toast toast-${level}`;
  t.textContent = text;
  host.appendChild(t);
  setTimeout(() => t.classList.add("show"), 10);
  setTimeout(() => { t.classList.remove("show"); setTimeout(() => t.remove(), 400); }, 3800);
}

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/* Tiny sparkline for per-student live tracking cards. */
function drawSparkline(canvas, points, key = "score") {
  const ctx = canvas.getContext("2d");
  const W = (canvas.width = canvas.clientWidth * 2);
  const H = (canvas.height = canvas.clientHeight * 2);
  ctx.clearRect(0, 0, W, H);
  if (!points || points.length < 2) {
    ctx.fillStyle = "#5b6b8c";
    ctx.font = "20px system-ui";
    ctx.fillText("collecting...", 8, H / 2 + 6);
    return;
  }
  const xs = (i) => (i / (points.length - 1)) * (W - 8) + 4;
  const ys = (v) => H - 4 - (v / 100) * (H - 8);
  const last = points[points.length - 1][key];
  const color = last >= 70 ? "#34d399" : last >= 45 ? "#fbbf24" : "#f87171";
  const grad = ctx.createLinearGradient(0, 0, 0, H);
  grad.addColorStop(0, color + "44");
  grad.addColorStop(1, color + "00");
  ctx.beginPath();
  points.forEach((p, i) => (i ? ctx.lineTo(xs(i), ys(p[key])) : ctx.moveTo(xs(i), ys(p[key]))));
  ctx.strokeStyle = color;
  ctx.lineWidth = 3;
  ctx.stroke();
  ctx.lineTo(xs(points.length - 1), H); ctx.lineTo(xs(0), H); ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();
}

/* Simple canvas line chart (no external libraries - offline by design). */
function drawTimeline(canvas, points, key = "class_score") {
  const ctx = canvas.getContext("2d");
  const W = (canvas.width = canvas.clientWidth * 2);
  const H = (canvas.height = canvas.clientHeight * 2);
  ctx.clearRect(0, 0, W, H);
  if (!points || points.length < 2) {
    ctx.fillStyle = "#5b6b8c";
    ctx.font = "24px system-ui";
    ctx.fillText("Waiting for engagement data...", 20, H / 2);
    return;
  }
  ctx.strokeStyle = "rgba(148,163,184,.15)";
  ctx.lineWidth = 2;
  [25, 50, 75].forEach((g) => {
    const y = H - (g / 100) * (H - 30) - 15;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
  });
  const xs = (i) => (i / (points.length - 1)) * (W - 20) + 10;
  const ys = (v) => H - (v / 100) * (H - 30) - 15;
  const grad = ctx.createLinearGradient(0, 0, 0, H);
  grad.addColorStop(0, "rgba(34,211,238,.35)");
  grad.addColorStop(1, "rgba(34,211,238,0)");
  ctx.beginPath();
  points.forEach((p, i) => (i ? ctx.lineTo(xs(i), ys(p[key])) : ctx.moveTo(xs(i), ys(p[key]))));
  ctx.strokeStyle = "#22d3ee";
  ctx.lineWidth = 4;
  ctx.stroke();
  ctx.lineTo(xs(points.length - 1), H); ctx.lineTo(xs(0), H); ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();
}

/* PWA setup: inject manifest and iOS tags; register the service worker. */
(function pwaInit() {
  const head = document.head;
  if (!head.querySelector('link[rel="manifest"]')) {
    const m = document.createElement('link');
    m.rel = 'manifest';
    m.href = '/manifest.json';
    head.appendChild(m);
  }
  if (!head.querySelector('meta[name="theme-color"]')) {
    const t = document.createElement('meta');
    t.name = 'theme-color';
    t.content = document.documentElement.dataset.theme === 'dark' ? '#0b1120' : '#ffffff';
    head.appendChild(t);
  }
  if (!head.querySelector('meta[name="apple-mobile-web-app-capable"]')) {
    const a = document.createElement('meta');
    a.name = 'apple-mobile-web-app-capable';
    a.content = 'yes';
    head.appendChild(a);
  }
  if (!head.querySelector('link[rel="apple-touch-icon"]')) {
    const i = document.createElement('link');
    i.rel = 'apple-touch-icon';
    i.href = '/static/icons/apple-touch-icon.png';
    head.appendChild(i);
  }
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }
})();
