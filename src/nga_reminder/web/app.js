/* NGA Reminder — 单页前端，无框架。Discord 式双栏：左订阅，右通知流。 */
"use strict";

const $ = (id) => document.getElementById(id);
const SVG_NS = "http://www.w3.org/2000/svg";

/* ------------------------------------------------------------------ 工具 */

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  let body = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  if (!res.ok) {
    const detail = (body && body.detail) || `${res.status} ${res.statusText}`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return body;
}

const post = (path, data) =>
  api(path, { method: "POST", body: JSON.stringify(data ?? {}) });

/** 完整时间，给 title 提示用 */
function fullTime(value) {
  const d = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

/** 相对时间（"3 分钟前"），鼠标悬停显示完整时间 */
function relTime(value) {
  if (!value) return "从未";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "从未";
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 45) return "刚刚";
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  if (diff < 86400 * 7) return `${Math.floor(diff / 86400)} 天前`;
  return d.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}

/** 带悬停完整时间的 <span> */
function timeEl(value, extraClass = "") {
  const span = document.createElement("span");
  span.className = extraClass;
  span.textContent = relTime(value);
  const full = fullTime(value);
  if (full) {
    span.title = full;
    span.setAttribute("aria-label", full);
  }
  return span;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/* ---------------------------------------------------------- 正文里的图片 */

/** NGA 的图片标签在服务端被转成了「[图片]URL」，这里把它认回来。 */
const IMG_MARK =
  /\[图片\]\s*((?:https?:\/\/|\.\/)[^\s<>"']+?\.(?:jpe?g|png|gif|webp|bmp)(?:\?[^\s<>"']*)?)/gi;

/** NGA 有时给的是相对路径（./mon_202611/20/xxx.jpeg），补成完整地址。 */
function imageUrl(value) {
  if (value.startsWith("./")) return "https://img.nga.cn/attachments/" + value.slice(2);
  return value;
}

/**
 * 把正文填进容器：图片还原成真的图片，其余照旧当文本。
 *
 * 服务端把 BBCode 的 ``[img]URL[/img]`` 转成了「[图片]URL」——图片在
 * img.nga.cn 上是公开的（不需要 Cookie），所以浏览器直接取就行，不用服务端
 * 代理。图片点一下能打开原图。
 *
 * 用 DOM 节点拼，不拼 HTML 字符串：正文是别人写的，拼字符串就是给自己挖坑。
 */
function appendContent(container, text) {
  container.textContent = "";
  if (!text) return;
  let last = 0;
  let match;
  IMG_MARK.lastIndex = 0;
  while ((match = IMG_MARK.exec(text)) !== null) {
    if (match.index > last) {
      container.appendChild(document.createTextNode(text.slice(last, match.index)));
    }
    const src = imageUrl(match[1]);
    const link = el("a", "post-img-link");
    link.href = src;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.title = "打开原图";
    const img = el("img", "post-img");
    img.src = src;
    img.alt = "帖子里的图片";
    img.loading = "lazy";
    // 附件可能已被 NGA 删掉。它回的是一张 1x1 的占位 GIF——那是「加载成功」，
    // error 事件根本不触发，所以还得看 naturalWidth。两种情况都别留破图。
    img.addEventListener("load", () => {
      if (img.naturalWidth > 1) return;
      markMissingImage(link, src);
    });
    img.addEventListener("error", () => markMissingImage(link, src));
    link.appendChild(img);
    container.appendChild(link);
    last = match.index + match[0].length;
  }
  if (last < text.length) {
    container.appendChild(document.createTextNode(text.slice(last)));
  }
}

/** 图片取不到了：换成一行说明，地址照样能点开，不留破图占位。 */
function markMissingImage(link, src) {
  if (link.dataset.missing) return;
  link.dataset.missing = "1";
  const note = el("span", "post-img-missing");
  note.textContent = "图片加载失败（附件可能已被删除）";
  link.replaceChildren(note);
  link.title = src;
}

function icon(pathData, cls = "") {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  if (cls) svg.setAttribute("class", cls);
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", pathData);
  svg.appendChild(path);
  return svg;
}

const ICON_OK = "M20 6 9 17l-5-5";
const ICON_BAD = "M12 8v5m0 3.2v.2M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z";
const ICON_MORE = "M12 6.2v.1M12 12v.1M12 17.8v.1";

/* -------------------------------------------------------------- 轻提示 */

function toast(message, kind = "") {
  const host = $("toast-host");
  const node = el("div", `toast ${kind}`.trim());
  node.appendChild(icon(kind === "bad" ? ICON_BAD : ICON_OK));
  node.appendChild(el("span", "", message));
  host.appendChild(node);
  setTimeout(() => node.remove(), kind === "bad" ? 6500 : 3800);
}

function setMsg(id, text, kind = "") {
  const node = $(id);
  node.textContent = text;
  node.className = `inline-msg ${kind}`.trim();
}

/**
 * 新加订阅后，服务端会按当前设定的订阅时间立刻取一次数——把结果说清楚。
 *
 * 服务端回的 fetch 小结：{ new_posts, notified, skipped, reason }。
 */
function describeFetch(res) {
  const f = (res && res.fetch) || null;
  if (!f) return "订阅已添加";
  if (f.skipped) {
    return f.reason ? `订阅已添加（${f.reason}）` : "订阅已添加";
  }
  if (f.new_posts > 0) {
    return `订阅已添加，订阅时间之后有 ${f.new_posts} 条回复`;
  }
  return "订阅已添加，订阅时间之后没有新回复";
}

/* -------------------------------------------------------------- 弹窗 */

let lastFocus = null;

function openModal(id) {
  lastFocus = document.activeElement;
  const root = $(id);
  root.hidden = false;
  document.body.style.overflow = "hidden";
  const focusable = root.querySelector("input, textarea, button:not([data-close])");
  if (focusable) focusable.focus();
}

function closeModal(id) {
  $(id).hidden = true;
  if (!document.querySelector(".modal-root:not([hidden])")) {
    document.body.style.overflow = "";
  }
  if (lastFocus && lastFocus.focus) lastFocus.focus();
}

function trapFocus(event, id) {
  if (event.key !== "Tab") return;
  const root = $(id);
  const items = [...root.querySelectorAll("button, input, textarea, [href]")].filter(
    (n) => !n.disabled && n.offsetParent !== null
  );
  if (!items.length) return;
  const first = items[0];
  const last = items[items.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

/* -------------------------------------------------------------- 主题 */

const THEME_KEY = "nga-theme";

function currentTheme() {
  return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
}

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    /* 隐私模式下写不进去，无所谓 */
  }
  const tip = theme === "light" ? "当前浅色主题，点击切换到深色" : "当前深色主题，点击切换到浅色";
  // 引导页是全屏覆盖层，它自己也有一个主题按钮——两处都要更新提示。
  $("btn-theme").title = tip;
  $("g-theme").title = tip;
}

/* -------------------------------------------------------------- 状态 */

const state = {
  configured: false,
  subs: [],
  feed: [],
  activeSubId: null, // null = 全部通知
  viewSince: "",
  // 通知流的滚动分页：游标指着「最旧的那条」，往下翻时用它接着取。
  feedTailCursor: "",
  feedHasMore: true,
  feedLoading: false,
  feedRevision: 0,
  // 通知流已经完整加载过至少一次吗？
  // 第一次加载时列表是空的，那批历史内容看起来全都像「刚来的」——
  // 没有这个标志，一打开页面就是一串浏览器通知轰炸。
  feedPrimed: false,
  // 服务端报告的通知通道状态（/api/status 的 notify 段）。
  notify: null,
};

/**
 * 首轮是否拉成功了。
 *
 * 成功了就说明 state.configured / state.subs 是可信的，引导页直接用，
 * 不用再打一遍同样的请求；没成功就别信 state（那可能只是空壳）。
 */
let bootReady = false;

async function refreshStatus() {
  try {
    const s = await api("/api/status");
    state.configured = s.credentials_configured;
    state.viewSince = s.view_since || "";
    const w = s.watcher || {};
    const dot = $("status-dot");
    const text = $("status-text");
    const meta = $("status-meta");

    if (!s.credentials_configured) {
      dot.className = "dot warn";
      text.textContent = "尚未配置 NGA 账号";
      meta.textContent = "";
    } else if (w.auth_error) {
      dot.className = "dot bad";
      text.textContent = "账号已失效";
      meta.textContent = w.auth_error;
    } else if (w.paused) {
      // 所有订阅都暂停了，或者订阅时间在将来：抓取停摆，但计时器还在走。
      dot.className = "dot warn";
      text.textContent = "抓取已暂停";
      meta.textContent = w.resume_at
        ? `暂停至 ${toLocalInput(w.resume_at)} · 到点自动恢复`
        : `${w.paused_reason || "所有订阅均已暂停"} · 计时器继续走`;
      meta.title = "";
    } else if (s.poll_interval === 0 && !w.running) {
      dot.className = "dot warn";
      text.textContent = "定时检查已关闭";
      meta.textContent = "";
    } else if (w.last_error) {
      dot.className = "dot warn";
      text.textContent = "上次检查有问题";
      meta.textContent = w.last_error;
    } else if (w.running) {
      dot.className = "dot ok";
      text.textContent = "正在检查…";
      meta.textContent = "";
    } else {
      dot.className = "dot ok";
      text.textContent = "运行中";
      const last = w.last_run_at ? relTime(w.last_run_at) : "尚未检查";
      meta.textContent = `上次检查 ${last}`;
      meta.title = w.last_run_at ? fullTime(w.last_run_at) : "";
    }

    const report = w.last_report || {};
    if (report.new_posts) {
      meta.textContent += ` · 新增 ${report.new_posts} 条`;
    }
    // 通知通道坏了要在状态栏也露一眼——横幅会被滚动区域盖住。
    const nt = s.notify || {};
    state.notify = nt;
    if (nt.reachable === false) {
      meta.textContent += " · 通知桥接不可达";
    }

    syncIntervalSelect(s.poll_interval);
    syncTimeInput(s.view_since);
    updateBanner();
    updateNotifyBanner(s);
    return true;
  } catch (err) {
    $("status-dot").className = "dot bad";
    $("status-text").textContent = "无法连接本地服务";
    $("status-meta").textContent = err.message;
    return false;
  }
}

/**
 * 通知通道的横幅：桥接不可达时把「坏了」和「怎么修」摆出来。
 *
 * 容器里的通知走宿主上的桥接进程（scripts/notify_bridge.py），而那个进程
 * 不是开机自启的——重启或关掉窗口就没了。症状很隐蔽：通知只是落进日志，
 * 用户要过几天才发现「怎么没有弹窗」。
 */
function updateNotifyBanner(s) {
  const banner = $("notify-banner");
  const n = (s && s.notify) || {};
  if (n.reachable !== false) {
    banner.hidden = true;
    return;
  }
  banner.hidden = false;
  $("notify-banner-text").textContent =
    n.error || "桌面通知不会弹出：通知桥接不可达。";
  // 兜底通道接管了就把「启用」按钮收起来，换成一句「已经接管」。
  const fallback = browserNotifyReady();
  $("notify-browser-enable").hidden = fallback;
  $("notify-banner-fallback").hidden = !fallback;
}

/** 手动发一条测试通知，当场确认通道通不通。 */
async function testNotification() {
  const btn = $("notify-test");
  btn.disabled = true;
  setNotifyResult("", "正在发送测试通知…");
  try {
    const r = await api("/api/notify/test", { method: "POST" });
    if (r.delivered) {
      setNotifyResult(
        "ok",
        `已送达（${r.backend}）——桌面上应该已经弹出通知。`
      );
    } else {
      setNotifyResult("bad", `没发出去：${r.error || "未知原因"}`);
    }
  } catch (err) {
    setNotifyResult("bad", `没发出去：${err.message}`);
  } finally {
    btn.disabled = false;
    refreshStatus();
  }
}

/**
 * 写一行测试结果（显示在「添加订阅」弹窗里，按钮旁边）。
 *
 * 成功 / 失败用一个小圆点表示，颜色是图形元素、不受文字对比度约束；
 * 文字本身一律用 --text——浅色主题的 --ok 压在浅底上只有 3.6:1，读不清。
 */
function setNotifyResult(kind, text) {
  const result = $("notify-test-result");
  result.hidden = false;
  result.className = "notify-result";
  result.textContent = "";
  const dot = document.createElement("span");
  dot.className = kind ? `dot ${kind}` : "dot";
  result.appendChild(dot);
  result.appendChild(document.createTextNode(" " + text));
}

/* --------------------------------------------- 浏览器通知（兜底通道） */

/**
 * 桌面通知不通时的兜底：浏览器通知。
 *
 * 容器里的通知走宿主上的桥接进程（scripts/notify_bridge.py）。那个进程不是
 * 开机自启的——重启或关掉窗口就没了。症状很隐蔽：通知只是落进日志，用户要过
 * 几天才发现「怎么没有弹窗」。
 *
 * 所以这里开一条浏览器通道兜底：桌面通道不可达时，由网页弹系统通知。
 *
 * 它是**兜底**不是并联——桌面通道正常时只走桌面 toast，否则一件事响两次。
 *
 * 浏览器的 Notification 权限只能由用户手势触发（requestPermission() 必须在
 * 点击处理函数里调用），所以这是个开关按钮，不会自动打开。
 */
const BROWSER_NOTIFY_KEY = "nga-browser-notify";

/** 这个浏览器有没有 Notification API。 */
function browserNotifySupported() {
  return typeof window !== "undefined" && "Notification" in window;
}

/** 权限状态："granted" / "denied" / "default" / "unsupported"。 */
function browserNotifyPermission() {
  if (!browserNotifySupported()) return "unsupported";
  try {
    return Notification.permission;
  } catch {
    return "unsupported";
  }
}

/** 用户是不是把这条通道打开着。 */
function browserNotifyOn() {
  try {
    return localStorage.getItem(BROWSER_NOTIFY_KEY) === "1";
  } catch {
    return false; // 隐私模式读不到
  }
}

function setBrowserNotify(on) {
  try {
    localStorage.setItem(BROWSER_NOTIFY_KEY, on ? "1" : "0");
  } catch {
    /* 隐私模式下写不进去，无所谓 */
  }
}

/** 兜底通道现在能不能用（开着 + 浏览器给了权限）。 */
function browserNotifyReady() {
  return browserNotifyOn() && browserNotifyPermission() === "granted";
}

/** 桌面通道现在通不通？拿不到状态时先按「通」处理，别乱弹。 */
function desktopChannelOk() {
  const n = state.notify;
  if (!n) return true;
  return n.reachable !== false;
}

/** 把一段正文压成一行摘要。 */
function excerpt(text, max = 80) {
  const s = String(text || "").replace(/\s+/g, " ").trim();
  return s.length > max ? `${s.slice(0, max)}…` : s;
}

/** 弹一条浏览器通知；点它会打开对应的主题页。 */
function showBrowserNotification(title, body, url) {
  try {
    const note = new Notification(title, { body, tag: "nga-reminder" });
    note.onclick = () => {
      window.focus();
      if (url) window.open(url, "_blank", "noopener");
      note.close();
    };
  } catch (err) {
    // 弹不出来不是致命错误——通知流还在页面上摆着。
    console.warn("浏览器通知没弹出来:", err);
  }
}

/**
 * 新条目到了，而桌面通道不通——用浏览器通知兜底。
 *
 * 只在桌面通道不可达时才弹：两条通道都响，一件事响两次。
 */
function maybeBrowserNotify(items) {
  if (!browserNotifyReady()) return;
  items = items.filter((item) => !(item.delivered ?? desktopChannelOk()));
  if (!items.length) return;

  const first = items[0];
  if (items.length === 1) {
    showBrowserNotification(
      `${first.alias || first.author_name || "有人"} 在《${first.title || "主题"}》有新回复`,
      excerpt(first.content),
      first.url || ""
    );
    return;
  }
  const lines = items
    .slice(0, 3)
    .map((n) => `${n.alias || n.author_name || "有人"}：${excerpt(n.content, 40)}`)
    .join("\n");
  showBrowserNotification(
    `NGA 提醒助手：${items.length} 条新回复`,
    items.length > 3 ? `${lines}\n……还有 ${items.length - 3} 条` : lines,
    ""
  );
}

/** 开关按钮。权限申请必须由用户手势触发，所以它是个按钮。 */
async function toggleBrowserNotify() {
  if (!browserNotifySupported()) {
    toast("这个浏览器不支持系统通知", "bad");
    return;
  }
  if (browserNotifyOn()) {
    setBrowserNotify(false);
    toast("浏览器通知已关闭", "ok");
    syncBrowserNotifyUi();
    return;
  }
  let permission = browserNotifyPermission();
  if (permission === "default") {
    try {
      permission = await Notification.requestPermission();
    } catch {
      permission = "denied";
    }
  }
  if (permission !== "granted") {
    toast("浏览器没给通知权限，兜底通道用不了", "bad");
    syncBrowserNotifyUi();
    return;
  }
  setBrowserNotify(true);
  toast("浏览器通知已开启：桌面通道不通时由它兜底", "ok");
  syncBrowserNotifyUi();
  showBrowserNotification(
    "NGA 提醒助手",
    "浏览器通知已就绪——桌面通道不通时，会由这里提醒你。",
    ""
  );
}

/** 把开关的状态同步到界面上的两处按钮（横幅 + 添加订阅弹窗）。 */
function syncBrowserNotifyUi() {
  const on = browserNotifyOn();
  const permission = browserNotifyPermission();
  const ready = on && permission === "granted";

  const toggle = $("browser-notify-toggle");
  if (toggle) {
    toggle.textContent = on ? "浏览器通知：开" : "浏览器通知：关";
    toggle.setAttribute("aria-pressed", on ? "true" : "false");
    toggle.title =
      permission === "denied"
        ? "浏览器已经拒绝了通知权限——要去站点设置里改回来"
        : ready
          ? "桌面通道不通时，用浏览器弹窗提醒你"
          : "打开后，桌面通道不通时用浏览器弹窗提醒你";
    toggle.classList.toggle("on", ready);
  }

  const enable = $("notify-browser-enable");
  if (enable) {
    enable.hidden = ready;
    enable.textContent =
      permission === "denied" ? "浏览器通知已被拒绝" : "启用浏览器通知";
    enable.disabled = permission === "denied";
  }

  const fallback = $("notify-banner-fallback");
  if (fallback) fallback.hidden = !ready;
}

function updateBanner() {
  const banner = $("banner");
  if (state.configured) {
    banner.hidden = true;
    return;
  }
  banner.hidden = false;
  $("banner-text").textContent =
    "本工具需要你的 NGA Cookie 才能读取主题内容。Cookie 只保存在本机，全程只读。";
}

/* ------------------------------------------------------ 自定义下拉框 */

/**
 * 原生 <select> 的替代品。
 *
 * 原生下拉的弹出层是操作系统画的，配色 / 圆角 / 阴影都不受页面控制，和玻璃界面
 * 不是一套语言，所以这里自己画一个。但 listbox 语义和键盘操作要保住：
 * ↑↓ 移动、Home/End 跳头尾、Enter/空格选中、Esc 关闭。
 */
function createSelect({ trigger, menu, valueEl, options, value, onChange }) {
  const root = trigger.closest(".select") || trigger.parentElement;
  let items = options.map((o) => ({ value: String(o.value), label: o.label }));
  let current = String(value ?? "");
  let active = current;
  let open = false;
  let dirty = false;

  const labelOf = (v) => {
    const hit = items.find((i) => i.value === String(v));
    return hit ? hit.label : String(v);
  };

  function renderMenu() {
    menu.innerHTML = "";
    items.forEach((item) => {
      const opt = el("div", "select-option", item.label);
      opt.id = `${menu.id}-opt-${item.value}`;
      opt.setAttribute("role", "option");
      opt.setAttribute("aria-selected", item.value === current ? "true" : "false");
      if (item.value === current) opt.classList.add("is-current");
      opt.onclick = () => choose(item.value);
      opt.onmousemove = () => setActive(item.value);
      menu.appendChild(opt);
    });
  }

  function setActive(v) {
    const value = String(v);
    if (!items.some((i) => i.value === value)) return;
    active = value;
    for (const node of menu.children) {
      const on = node.id === `${menu.id}-opt-${active}`;
      node.classList.toggle("is-active", on);
      if (on) node.scrollIntoView({ block: "nearest" });
    }
    trigger.setAttribute("aria-activedescendant", `${menu.id}-opt-${active}`);
  }

  function openMenu() {
    if (open) return;
    open = true;
    menu.hidden = false;
    trigger.classList.add("open");
    trigger.setAttribute("aria-expanded", "true");
    renderMenu();
    setActive(current || (items[0] && items[0].value));
  }

  function closeMenu() {
    if (!open) return;
    open = false;
    menu.hidden = true;
    trigger.classList.remove("open");
    trigger.setAttribute("aria-expanded", "false");
    trigger.removeAttribute("aria-activedescendant");
  }

  function choose(v) {
    const value = String(v);
    const changed = value !== current;
    current = value;
    dirty = true;
    valueEl.textContent = labelOf(value);
    closeMenu();
    renderMenu();
    if (changed && onChange) onChange(value);
  }

  function move(step) {
    if (!items.length) return;
    const idx = items.findIndex((i) => i.value === active);
    const next = Math.min(items.length - 1, Math.max(0, (idx < 0 ? 0 : idx) + step));
    setActive(items[next].value);
  }

  trigger.onclick = () => (open ? closeMenu() : openMenu());

  trigger.onkeydown = (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!open) {
        openMenu();
        setActive(current || (items[0] && items[0].value));
      } else {
        move(e.key === "ArrowDown" ? 1 : -1);
      }
      return;
    }
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      if (open) choose(active);
      else openMenu();
      return;
    }
    if (e.key === "Home" && open) {
      e.preventDefault();
      setActive(items[0].value);
      return;
    }
    if (e.key === "End" && open) {
      e.preventDefault();
      setActive(items[items.length - 1].value);
      return;
    }
    if (e.key === "Escape") closeMenu();
  };

  // 点到别处就收起
  document.addEventListener("click", (e) => {
    if (open && !root.contains(e.target)) closeMenu();
  });
  window.addEventListener("resize", closeMenu);

  valueEl.textContent = labelOf(current);

  return {
    get options() { return items.slice(); },
    get value() { return current; },
    get dirty() { return dirty; },
    set dirty(v) { dirty = !!v; },
    has: (v) => items.some((i) => i.value === String(v)),
    setOptions(next) {
      items = next.map((o) => ({ value: String(o.value), label: o.label }));
      valueEl.textContent = labelOf(current);
      if (open) renderMenu();
    },
    setValue(v) {
      current = String(v);
      valueEl.textContent = labelOf(current);
      if (open) renderMenu();
    },
    close: closeMenu,
  };
}

/* ------------------------------------------------------ 分段选择（tabs） */

/**
 * 一段平铺的选项，点哪个选哪个——不展开任何弹层。
 *
 * 顶栏那个下拉框自带一层弹出菜单，把它搬进表单区会跟旁边的输入框对不齐
 * （触发器缩在左上角，菜单却按整块壳右对齐）。引导页的「更新间隔」改用这个。
 *
 * 语义是一组 radio：容器 role="radiogroup"，每个选项 role="radio" + aria-checked。
 * 键盘按 ARIA 的 radio 组约定走：方向键移动并选中、Home/End 跳头尾、
 * Tab 键只进出整组一次（组内用漫游 tabindex）。
 */
function createTabs({ root, options, value, onChange }) {
  if (!root) return null;
  let items = (options || []).map((o) => ({ value: String(o.value), label: o.label }));
  let current = String(value ?? "");
  if (!items.some((i) => i.value === current) && items.length) current = items[0].value;

  /** 把选中态刷到 DOM 上；漫游 tabindex 也在这里管。 */
  function paint() {
    root.querySelectorAll(".tab").forEach((btn) => {
      const on = btn.dataset.value === current;
      btn.setAttribute("aria-checked", on ? "true" : "false");
      btn.tabIndex = on ? 0 : -1;
    });
  }

  function tabOf(v) {
    let hit = null;
    root.querySelectorAll(".tab").forEach((btn) => {
      if (btn.dataset.value === String(v)) hit = btn;
    });
    return hit;
  }

  function choose(v, focus) {
    const next = String(v);
    if (!items.some((i) => i.value === next)) return;
    const changed = next !== current;
    current = next;
    paint();
    if (focus) {
      const btn = tabOf(next);
      if (btn) btn.focus();
    }
    if (changed && onChange) onChange(next);
  }

  function onKey(e) {
    const idx = items.findIndex((i) => i.value === e.currentTarget.dataset.value);
    if (idx < 0) return;
    let next = -1;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") next = (idx + 1) % items.length;
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = (idx - 1 + items.length) % items.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = items.length - 1;
    else if (e.key === " " || e.key === "Enter") {
      e.preventDefault();
      choose(items[idx].value, true);
      return;
    }
    if (next < 0) return;
    e.preventDefault();
    choose(items[next].value, true);
  }

  root.textContent = "";
  items.forEach((item) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "tab";
    btn.setAttribute("role", "radio");
    btn.dataset.value = item.value;
    btn.textContent = item.label;
    btn.addEventListener("click", () => choose(item.value));
    btn.addEventListener("keydown", onKey);
    root.appendChild(btn);
  });
  paint();

  return {
    get options() { return items.slice(); },
    get value() { return current; },
    has: (v) => items.some((i) => i.value === String(v)),
    setValue(v) {
      const next = String(v);
      if (!items.some((i) => i.value === next)) return;
      current = next;
      paint();
    },
  };
}

/* ------------------------------------------------------ 更新间隔热调 */

const INTERVAL_LABELS = {
  0: "关闭", 30: "30 秒", 60: "1 分钟", 120: "2 分钟",
  300: "5 分钟", 600: "10 分钟", 1800: "30 分钟", 3600: "1 小时",
};

function intervalLabel(seconds) {
  return INTERVAL_LABELS[seconds] || `${seconds} 秒`;
}

/** 当前的自定义下拉框实例；init 时创建 */
let intervalSelect = null;

/** 把服务端的间隔同步到下拉框；遇到自定义值就补一个选项 */
function syncIntervalSelect(seconds) {
  if (!intervalSelect || seconds === undefined || seconds === null) return;
  const value = String(seconds);
  if (!intervalSelect.has(value)) {
    intervalSelect.setOptions([
      ...intervalSelect.options,
      { value, label: intervalLabel(seconds) },
    ]);
  }
  if (!intervalSelect.dirty) intervalSelect.setValue(value);
}

/* -------------------------------------------------------- 订阅时间 */

/** Air Datepicker 实例：用现成的日期组件面板，不自己画日历 */
let datePicker = null;
/** 上一次从服务端同步下来的本地时间字符串——用来判断「值其实没变」 */
let lastSyncedLocal = "";
/** 程序同步期间的闸门：组件回灌输入框时派发的 change 不算数 */
let syncGate = 0;
/** 攒下的时间：等用户停手了再一次性存 */
let pendingViewSince = "";
/** 已经提交过的值：浏览器在失焦时还会补一个 change，别再存一次 */
let lastAppliedLocal = "";
/** 存值的防抖计时器：改动来得又密又快，等他停手再存 */
let saveTimer = 0;

/** 攒一个改动，等用户停手（或者面板收起）再存一次。 */
function scheduleViewSinceSave(value) {
  if (!value) return;
  pendingViewSince = value;
  const input = $("view-since-input");
  // 先标成「有未保存的改动」：定时刷新别在这时候把用户选的值冲掉。
  if (input) input.dataset.dirty = "1";
  clearTimeout(saveTimer);
  saveTimer = setTimeout(flushViewSinceSave, 900);
}

/** 把攒下的时间存到服务端——先判断有没有变更，有才存，而且只存一次。 */
function flushViewSinceSave() {
  clearTimeout(saveTimer);
  saveTimer = 0;
  const value = pendingViewSince;
  pendingViewSince = "";
  const input = $("view-since-input");
  if (!value || !input) return;
  if (value === lastSyncedLocal) { input.dataset.dirty = ""; return; } // 跟服务端一致
  if (value === lastAppliedLocal) { input.dataset.dirty = ""; return; } // 刚刚存过
  lastAppliedLocal = value;
  applyViewSince(value);
}

/** 把服务端的订阅时间同步到日期选择器 */
function syncTimeInput(viewSince) {
  const input = $("view-since-input");
  if (!input || input.dataset.dirty) return;
  const local = toLocalInput(viewSince);
  if (!local) return;
  // 记账一定要更新：判断「值有没有真的变」靠的就是它。
  lastSyncedLocal = local;
  // 面板开着的时候别去动组件——否则定时刷新会把用户正在选的值冲掉。
  if (datePicker && datePicker.visible) return;
  const d = new Date(local.replace(" ", "T"));
  if (Number.isNaN(d.getTime())) return;
  if (!datePicker) {
    if (input.value !== local) input.value = local;
    return;
  }
  // ⚠ 这里有两件事不能搞错：
  //
  // 1) **不能把 updateTime 这个选项打开**。它会把时间换成「组件以为的现在」，
  //    而那个「现在」是 Air Datepicker 实例创建那一刻（opts.startDate）固化的。
  //    标签页开久了——或者干脆是昨天开的——回灌进来的就是个旧时间，15 秒一次的
  //    刷新于是静默把用户设的订阅时间改掉。我们要的是服务端存的**精确**那一刻。
  //
  // 2) 组件的 selectDate() 是在一个 setTimeout 里回写输入框并派发 change 的，
  //    而 change 在我们这儿意味着「用户改了时间，要写回服务端」。所以先把
  //    闸门关上，等它回灌完再打开。
  syncGate += 1;
  datePicker.selectDate(d, { silent: true });
  setTimeout(() => {
    syncGate = Math.max(0, syncGate - 1);
    // 组件回灌的值不作数，最终以服务端的为准。
    if (input.value !== local) input.value = local;
  }, 0);
}

/** ISO 时间 → 选择器要的本地时间字符串（"Y-m-d H:i"） */
function toLocalInput(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    ` ${pad(d.getHours())}:${pad(d.getMinutes())}`
  );
}

/** 「2026-11-16 20:13」→ 带时区的绝对时间（UTC ISO）
 *
 * 时间选择器给的是「墙上时间」，没有时区。浏览器知道自己处在哪个时区，
 * 所以在这里补上时区再发出去——否则服务端只能按它自己机器的时区去猜，
 * 容器（UTC）和浏览器（+08:00）之间就会差出几个小时。
 */
function toAbsoluteIso(localValue) {
  if (!localValue) return "";
  const d = new Date(localValue.replace(" ", "T"));
  if (Number.isNaN(d.getTime())) return localValue;
  return d.toISOString();
}

async function applyViewSince(value) {
  const input = $("view-since-input");
  try {
    const r = await post("/api/settings/view-since", {
      view_since: toAbsoluteIso(value),
    });
    state.viewSince = r.view_since;
    input.dataset.dirty = "";
    syncTimeInput(r.view_since);
    await loadFeedHead(true);
    await loadSubs();
    if (r.resume_at) {
      // 选的是将来的时间：抓取冻结到那一刻，到点自己开始。
      toast(`已暂停到 ${toLocalInput(r.resume_at)}，到点自动开始`, "ok");
    } else if (r.rescan) {
      // 往前挪了订阅时间：正在按新时间把这段回复找出来。
      toast("订阅时间已更新，正在按新时间补取回复", "ok");
    } else {
      toast(
        r.collected ? `订阅时间已更新，补收了 ${r.collected} 条` : "订阅时间已更新",
        "ok"
      );
    }
  } catch (err) {
    input.dataset.dirty = "";
    await refreshStatus();
    toast(err.message, "bad");
  }
}

/** 冻结状态（订阅时间在将来）
 *
 *  不再单挂一个徽标——日期框里已经写着那个时间，顶栏状态条也会说
 *  「抓取已暂停 · 暂停至 … · 到点自动恢复」，再冒一个粉色的胶囊只是重复三遍。
 *  冻结这件事由 refreshStatus() 走正常的状态显示，不额外占地方。
 */

/* -------------------------------------------------------------- 订阅 */

/** 订阅列表的指纹。内容没变就不重画列表。 */
let subsSignature = "";

async function loadSubs() {
  let subs;
  try {
    subs = await api("/api/subscriptions");
  } catch (err) {
    toast(err.message, "bad");
    return false;
  }
  state.subs = subs;
  // 每 15 秒的刷新如果每次都重画列表，会把正开着的「⋯ 菜单」和滚动位置一起冲掉。
  // 列表没变就别动它。
  const signature = JSON.stringify(subs);
  if (signature === subsSignature) return true;
  subsSignature = signature;
  renderSubs();
  return true;
}

function renderSubs() {
  const box = $("subs");
  // 保留「全部通知」这一行，重建其余订阅
  const all = $("sub-all");
  box.innerHTML = "";
  box.appendChild(all);

  $("subs-empty").hidden = state.subs.length > 0;

  for (const sub of state.subs) box.appendChild(subItem(sub));

  all.classList.toggle("active", state.activeSubId === null);
}

function subItem(sub) {
  const item = el("div", "sub-item");
  item.setAttribute("role", "button");
  item.setAttribute("tabindex", "0");
  if (state.activeSubId === sub.id) item.classList.add("active");
  if (!sub.enabled) item.classList.add("off");
  item.dataset.subId = String(sub.id);
  if (sub.color) item.style.setProperty("--sub-color", sub.color);

  item.appendChild(el("span", "sub-dot"));
  item.appendChild(el("span", "sub-label", sub.display_name || `主题 ${sub.tid}`));

  if (!sub.enabled) {
    // 暂停的订阅必须一眼看得出：行里挂一个醒目的标签。
    const paused = el("span", "sub-paused", "已暂停");
    paused.title = "这条订阅已暂停追踪，不会再向 NGA 发请求";
    item.appendChild(paused);
  }

  if (sub.unread) {
    const badge = el("span", "sub-badge", sub.unread > 99 ? "99+" : String(sub.unread));
    item.appendChild(badge);
  }

  const more = el("button", "sub-more");
  more.type = "button";
  more.title = "订阅菜单";
  more.setAttribute("aria-label", "订阅菜单");
  more.appendChild(icon(ICON_MORE));
  more.onclick = (e) => {
    e.stopPropagation();
    toggleMenu(item, more, sub);
  };
  item.appendChild(more);

  item.onclick = () => selectSub(sub.id);
  item.onkeydown = (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      selectSub(sub.id);
    }
  };
  // 右键也能唤起菜单
  item.oncontextmenu = (e) => {
    e.preventDefault();
    toggleMenu(item, more, sub);
  };

  return item;
}

function selectSub(subId) {
  state.activeSubId = subId;
  renderSubs();
  // 换订阅 = 换一份列表，服务端按订阅过滤，所以要重新从头取。
  loadFeedHead(true);
  closeAllMenus();
}

function closeAllMenus() {
  document.querySelectorAll(".sub-menu").forEach((m) => m.remove());
  document.querySelectorAll(".sub-more.open").forEach((b) => b.classList.remove("open"));
}

function toggleMenu(item, more, sub) {
  const open = item.querySelector(".sub-menu");
  if (open) {
    closeAllMenus();
    return;
  }
  closeAllMenus();
  more.classList.add("open");

  const menu = el("div", "sub-menu");
  const entries = sub
    ? [
        { label: "查看楼层", run: () => openDrawer(sub) },
        { label: "编辑订阅", run: () => openAddModal(sub) },
        {
          label: sub.enabled ? "暂停追踪" : "恢复追踪",
          run: () => toggleEnabled(sub),
        },
        { label: "删除订阅", danger: true, run: () => deleteSub(sub) },
      ]
    : [];

  for (const entry of entries) {
    menu.appendChild(menuButton(entry.label, entry.run, entry.danger));
  }
  item.appendChild(menu);
}

function menuButton(label, run, danger = false) {
  const btn = el("button", danger ? "danger" : "", label);
  btn.type = "button";
  btn.onclick = (e) => {
    // 菜单挂在订阅行里，不拦住冒泡的话，点「删除订阅」这种动作
    // 会顺带把右侧切到这条订阅——菜单动作不该改变当前视图。
    e.stopPropagation();
    closeAllMenus();
    run();
  };
  return btn;
}

async function toggleEnabled(sub) {
  try {
    await api(`/api/subscriptions/${sub.id}`, {
      method: "PATCH",
      body: JSON.stringify({ enabled: !sub.enabled }),
    });
    toast(sub.enabled ? "已暂停追踪" : "已恢复追踪", "ok");
    loadSubs();
  } catch (err) {
    toast(err.message, "bad");
  }
}

async function deleteSub(sub) {
  if (!confirm(`删除订阅《${sub.alias || sub.title || sub.tid}》？`)) return;
  try {
    await api(`/api/subscriptions/${sub.id}`, { method: "DELETE" });
    toast("订阅已删除", "ok");
    if (state.activeSubId === sub.id) state.activeSubId = null;
    await loadSubs();
    await loadFeedHead(true);
  } catch (err) {
    toast(err.message, "bad");
  }
}

/* -------------------------------------------------------------- 通知流 */

/** 一次取多少条。滚到底部再接下一页。 */
const FEED_PAGE = 30;

/** 条目的游标。服务端给的「收录时间|pid」，原样带回去就能接着翻。 */
function feedCursor(item) {
  return item.cursor || "";
}

/** 通知流接口的查询参数 */
function feedParams(extra) {
  const params = new URLSearchParams(extra);
  if (state.activeSubId !== null) {
    params.set("subscription_id", String(state.activeSubId));
  }
  return params;
}

/**
 * 取最新的一段通知。
 *
 * reset=true：从头再来（换订阅、改订阅时间、清空列表）。
 * reset=false：定时刷新——重新取服务端的首页，跟已有的内容合并。
 *
 * 合并而不是替换，是为了让两件事同时成立：新冒出来的通知排到最前面，
 * 而已经显示过的那几条能被更新过（比如后台刚补上的楼号）。
 */
async function loadFeedHead(reset = false) {
  if (state.feedLoading && !reset) return;
  const revision = ++state.feedRevision;
  state.feedLoading = true;

  // 列表还是空的（第一次打开）——按从头来过处理。
  const fresh = reset || state.feed.length === 0;
  const known = new Set(fresh ? [] : state.feed.map((n) => n.pid));
  const collected = [];
  let cursor = "";
  let hasMore = false;
  let sawKnown = false;

  try {
    for (let round = 0; round < 5; round += 1) {
      const params = feedParams({ limit: String(FEED_PAGE + 1) });
      if (cursor) params.set("before", cursor);
      const items = await api(`/api/notifications?${params.toString()}`);
      if (revision !== state.feedRevision) return;
      const more = items.length > FEED_PAGE;
      const page = more ? items.slice(0, FEED_PAGE) : items;
      if (!page.length) break;
      collected.push(...page);
      hasMore = more;
      if (page.some((n) => known.has(n.pid))) sawKnown = true;
      if (fresh) break;
      if (sawKnown) break;
      // 一页都没碰到旧内容：说明刚才来了一大波新的，接着往下翻。
      if (!more) break;
      cursor = page[page.length - 1].cursor;
    }
  } catch (err) {
    if (revision !== state.feedRevision) return;
    toast(err.message, "bad");
    state.feedLoading = false;
    return;
  }

  if (fresh || !sawKnown) {
    state.feed = collected;
    state.feedHasMore = hasMore;
  } else {
    // 服务端给的那几条放在前面（它们更新），其余按原顺序接在后面。
    const seen = new Set(collected.map((n) => n.pid));
    const merged = collected.slice();
    for (const old of state.feed) {
      if (seen.has(old.pid)) continue;
      merged.push(old);
      seen.add(old.pid);
    }
    state.feed = merged;
  }

  // 真正「新」冒出来的条目：不在已知列表里的那些。
  // 首次加载只显示历史，后续刷新即使原先列表为空也会提醒。
  const arrived =
    !reset && state.feedPrimed ? collected.filter((n) => !known.has(n.pid)) : [];
  state.feedPrimed = true;

  if (state.feed.length) {
    state.feedTailCursor = feedCursor(state.feed[state.feed.length - 1]);
  } else if (fresh) {
    state.feedTailCursor = "";
  }
  state.feedLoading = false;
  renderFeed();
  // 桌面通道不通时，这条新通知由浏览器弹窗兜底。
  maybeBrowserNotify(arrived);
}

/** 往下翻：接上更旧的一段。 */
async function loadFeedMore() {
  if (!state.feedHasMore || state.feedLoading || !state.feedTailCursor) return;
  const revision = ++state.feedRevision;
  state.feedLoading = true;
  paintFeedMeta();
  const params = feedParams({
    limit: String(FEED_PAGE + 1),
    before: state.feedTailCursor,
  });

  let items;
  try {
    items = await api(`/api/notifications?${params.toString()}`);
  } catch (err) {
    if (revision !== state.feedRevision) return;
    toast(err.message, "bad");
    state.feedLoading = false;
    return;
  }
  if (revision !== state.feedRevision) return;
  const hasMore = items.length > FEED_PAGE;
  const page = hasMore ? items.slice(0, FEED_PAGE) : items;
  state.feed = state.feed.concat(page);
  if (page.length) {
    state.feedTailCursor = feedCursor(page[page.length - 1]);
  }
  state.feedHasMore = hasMore;
  state.feedLoading = false;
  renderFeed(page);
}

/** 通知流头部：标题、条数、空状态、还有没有更多。 */
function paintFeedMeta() {
  $("feed-count").textContent = state.feed.length ? `${state.feed.length} 条` : "";
  $("feed-empty").hidden = state.feed.length > 0;
  const active = state.activeSubId === null
    ? null
    : state.subs.find((s) => s.id === state.activeSubId);
  $("feed-title").textContent = active ? (active.display_name || `主题 ${active.tid}`) : "全部通知";
  $("feed-sub").textContent = active ? `tid ${active.tid}` : "";

  const more = $("feed-more");
  if (!more) return;
  if (state.feedLoading) {
    more.hidden = false;
    more.textContent = "正在加载…";
  } else if (state.feed.length && state.feedHasMore) {
    more.hidden = false;
    more.textContent = "继续往下滑，加载更早的通知";
  } else {
    more.hidden = true;
  }
}

function renderFeed(appended = null) {
  const body = $("main-body");
  const box = $("feed");

  if (appended && appended.length) {
    // 往下翻：只在末尾接上新的卡片，滚动位置一点不动。
    for (const n of appended) box.appendChild(feedCard(n, false));
    paintFeedMeta();
    return;
  }

  // 最新的一条在顶上：读者停在顶部就跟着看新来的；
  // 已经往下翻了就不打断他——只把新插入的高度补回去，
  // 让正在看的那一条停在原处。
  const atTop = body.scrollTop < 40;
  const prevTop = body.scrollTop;
  const prevHeight = body.scrollHeight;

  box.innerHTML = "";
  state.feed.forEach((n, i) => box.appendChild(feedCard(n, i === 0)));
  paintFeedMeta();

  if (atTop) {
    body.scrollTop = 0;
  } else {
    body.scrollTop = prevTop + (body.scrollHeight - prevHeight);
  }
}

/** 引用块里存的是「名字：原文」，把名字抠出来做标签 */
function quoteSource(text) {
  const m = /^\s*([^：\n]{1,20})：/.exec(text || "");
  return m ? m[1].trim() : "";
}

function feedCard(n, isLatest = false) {
  const sub = state.subs.find((s) => s.id === n.subscription_id);
  const color = (sub && sub.color) || (n.color || "");

  const card = el("article", "msg");
  if (color) card.style.setProperty("--sub-color", color);
  if (isLatest) card.classList.add("msg-latest");

  const who = (sub && sub.display_name) || n.alias || n.author_name || `UID ${n.author_uid}`;
  const avatar = el("span", "msg-avatar", (who || "?").trim().slice(0, 1).toUpperCase());
  card.appendChild(avatar);

  const main = el("div", "msg-main");

  const head = el("div", "msg-head");
  head.appendChild(el("span", "msg-author", who));
  const alias = (sub && sub.alias) || n.alias;
  if (alias && alias !== who) {
    head.appendChild(el("span", "msg-alias", alias));
  }
  head.appendChild(el("span", "msg-thread", n.title || `主题 ${n.tid || ""}`));
  // 楼层号：NGA 页面上的那个 #1234。
  //
  // 定位不到就不显示——宁可空着，也不用「该用户的第几条回复」冒充楼号，
  // 那是另一个东西，显示出来会误导人。
  if (n.floor_no > 0) {
    head.appendChild(el("span", "msg-floor", `#${n.floor_no}`));
  }
  // 最新的一条挂个徽标，扫一眼就知道哪条是刚来的
  if (isLatest) {
    head.appendChild(el("span", "msg-latest-badge", "最新"));
  }
  // 显示的是**发帖时间**，不是收录时间。
  //
  // 收录时间是「我们什么时候记下这条的」——一批楼层挤在同一轮里抓回来时，
  // 它们会被写成同一个时刻，看上去就像所有回复都是同一时间发的。发帖时间
  // 才是用户关心的那个。
  const postedAt = n.ts > 0 ? new Date(n.ts * 1000).toISOString() : n.created_at;
  head.appendChild(timeEl(postedAt, "msg-time"));
  // 「在 NGA 打开」放在卡片右上角
  if (n.url) {
    const link = el("a", "msg-open", "在 NGA 打开 ↗");
    link.href = n.url;
    link.target = "_blank";
    link.rel = "noreferrer";
    head.appendChild(link);
  }
  main.appendChild(head);

  // 正文：完整显示，保留原始空格与换行；图片还原成图片
  const body = el("div", "msg-body");
  appendContent(body, n.content || "");
  main.appendChild(body);

  if (n.quote_text) {
    const quote = el("blockquote", "msg-quote");
    const source = quoteSource(n.quote_text);
    quote.appendChild(el("span", "msg-quote-label", source ? `引用 · ${source}` : "引用原文"));
    quote.appendChild(document.createTextNode(n.quote_text));
    main.appendChild(quote);
  }

  card.appendChild(main);
  return card;
}

/* -------------------------------------------------------------- 清空 */

/* -------------------------------------------------------------- 抽屉 */

async function openDrawer(sub) {
  $("drawer").hidden = false;
  document.body.style.overflow = "hidden";
  $("drawer-title").textContent = sub.title || `主题 ${sub.tid}`;
  $("drawer-sub").textContent =
    `${sub.display_name || `UID ${sub.author_uid}`} · tid=${sub.tid}`;
  const body = $("drawer-body");
  body.innerHTML = "";
  body.appendChild(el("p", "muted", "加载中…"));

  let posts = [];
  try {
    posts = await api(`/api/subscriptions/${sub.id}/posts`);
  } catch (err) {
    body.innerHTML = "";
    body.appendChild(el("p", "inline-msg bad", err.message));
    return;
  }

  body.innerHTML = "";
  if (!posts.length) {
    body.appendChild(el("p", "muted", "这个窗口里还没有抓到楼层。"));
  }
  for (const p of posts) {
    const floor = el("div", "floor");
    const head = el("div", "floor-head");
    head.appendChild(timeEl(new Date(p.ts * 1000).toISOString()));
    if (p.floor_no > 0) {
      head.appendChild(el("span", "msg-floor", `#${p.floor_no}`));
    }
    const link = el("a", "", "在 NGA 打开 ↗");
    // 服务端给的链接带着页码和楼层锚点：点开直接落在那一条上。
    link.href = p.url || `https://bbs.nga.cn/read.php?tid=${sub.tid}&pid=${p.pid}`;
    link.target = "_blank";
    link.rel = "noreferrer";
    head.appendChild(link);
    floor.appendChild(head);

    const content = el("div", "floor-content");
    appendContent(content, p.content || "（空）");
    floor.appendChild(content);

    if (p.quote_text) {
      const quote = el("div", "floor-quote");
      const source = quoteSource(p.quote_text);
      quote.appendChild(el("span", "floor-quote-label", source ? `引用 · ${source}` : "引用原文"));
      quote.appendChild(document.createTextNode(p.quote_text));
      floor.appendChild(quote);
    }
    body.appendChild(floor);
  }

  await post(`/api/subscriptions/${sub.id}/read`).catch(() => {});
  loadSubs();
}

function closeDrawer() {
  $("drawer").hidden = true;
  if (!document.querySelector(".modal-root:not([hidden])")) {
    document.body.style.overflow = "";
  }
}

/* -------------------------------------------------------- 添加订阅 */

const COLORS = [
  { v: "", name: "默认" },
  { v: "#A8442F", name: "砖红" },
  { v: "#C2740E", name: "橙" },
  { v: "#9C7C1E", name: "金" },
  { v: "#3A7D44", name: "松绿" },
  { v: "#2A7F8E", name: "青" },
  { v: "#3B6EA5", name: "靛蓝" },
  { v: "#7B5EA7", name: "紫" },
  { v: "#C2557A", name: "玫红" },
];

let editingSub = null;
let pickedColor = "";
/** 引导页自己的标识颜色——跟添加订阅弹窗各存各的，互不干扰。 */
let guidePickedColor = "";

/** 画一排颜色圆点。
 *
 *  添加订阅弹窗和引导页各有一份「当前选中的颜色」，所以这里不直接读全局变量，
 *  而是把「当前值」和「点一下之后怎么办」都交给调用方。
 */
function renderSwatches(boxId, current, onPick) {
  const box = $(boxId);
  box.innerHTML = "";
  for (const c of COLORS) {
    const b = el("button", `swatch${c.v ? "" : " none"}`);
    b.type = "button";
    b.setAttribute("role", "radio");
    b.setAttribute("aria-checked", c.v === current ? "true" : "false");
    b.setAttribute("aria-label", c.name);
    b.title = c.name;
    if (c.v) b.style.background = c.v;
    else b.appendChild(icon(ICON_BAD));
    b.onclick = () => onPick(c.v);
    box.appendChild(b);
  }
}

function buildSwatches() {
  renderSwatches("swatches", pickedColor, (v) => {
    pickedColor = v;
    buildSwatches();
  });
}

function buildGuideSwatches() {
  renderSwatches("g-swatches", guidePickedColor, (v) => {
    guidePickedColor = v;
    buildGuideSwatches();
  });
}

/** 从「只看楼主」链接里解析 tid 与 authorid */
function parseLink(text) {
  const trimmed = (text || "").trim();
  if (!trimmed) return null;
  const m = trimmed.match(/[?&](?:amp;)?tid=(\d+)/);
  const a = trimmed.match(/[?&](?:amp;)?authorid=(\d+)/);
  if (!m) return null;
  return { tid: m[1], uid: a ? a[1] : "" };
}

function updateParsePreview() {
  const parsed = parseLink($("in-url").value);
  const box = $("parse-preview");
  if (!parsed) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  $("pv-tid").textContent = parsed.tid;
  $("pv-uid").textContent = parsed.uid || "链接里没有，需要手填";
  $("pv-uid").style.color = parsed.uid ? "" : "var(--warn)";
}

/** 引导页第 2 步的解析预览——跟添加订阅弹窗一个样。 */
function updateGuideParsePreview() {
  const parsed = parseLink($("g-url").value);
  const box = $("g-parse");
  if (!parsed) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  $("gpv-tid").textContent = parsed.tid;
  $("gpv-uid").textContent = parsed.uid || "链接里没有，需要手填";
  $("gpv-uid").style.color = parsed.uid ? "" : "var(--warn)";
}

function openAddModal(sub = null) {
  editingSub = sub;
  pickedColor = sub ? sub.color || "" : "";
  $("add-title").textContent = sub ? "编辑订阅" : "添加订阅";
  $("btn-add-sub").textContent = sub ? "保存修改" : "添加订阅";
  $("in-url").value = sub ? `https://bbs.nga.cn/read.php?tid=${sub.tid}&authorid=${sub.author_uid}` : "";
  $("in-name").value = sub ? sub.alias || "" : "";
  setMsg("add-msg", "");
  // 上一次的测试通知结果不该留在这儿——每次打开弹窗都从干净状态开始。
  $("notify-test-result").hidden = true;
  $("notify-test-result").textContent = "";
  // 兜底通道的开关状态也顺手同步一下（权限可能被别人改过）。
  syncBrowserNotifyUi();
  buildSwatches();
  updateParsePreview();
  openModal("modal-add");
  if (!sub) $("in-url").focus();
}

async function submitSubscription() {
  setMsg("add-msg", "");
  const url = $("in-url").value.trim();
  const alias = $("in-name").value.trim();
  const parsed = parseLink(url);

  if (!parsed) {
    setMsg("add-msg", "没看懂这个链接。贴「只看楼主」的地址栏链接，或直接填 tid=…&authorid=…", "bad");
    return;
  }
  if (!parsed.uid) {
    setMsg("add-msg", "链接里没有 authorid（用户 ID），请补一个或换用「只看楼主」链接", "bad");
    return;
  }

  const body = {
    url,
    author_uid: Number(parsed.uid),
    alias,
    color: pickedColor,
  };

  try {
    if (editingSub) {
      await api(`/api/subscriptions/${editingSub.id}`, {
        method: "PATCH",
        body: JSON.stringify({ alias, color: pickedColor }),
      });
      toast("订阅已更新", "ok");
    } else {
      const res = await post("/api/subscriptions", body);
      toast(describeFetch(res), "ok");
    }
    closeModal("modal-add");
    await loadSubs();
    await loadFeedHead(true);
  } catch (err) {
    setMsg("add-msg", err.message, "bad");
  }
}

/* ---------------------------------------------------------- 账号设置 */

async function openSetupModal() {
  setMsg("setup-msg", "");
  const notice = $("setup-state");
  notice.hidden = true;
  try {
    const s = await api("/api/setup/cookie");
    if (s.configured) {
      notice.hidden = false;
      notice.className = "notice ok";
      // 显示完整 uid：用户要靠它分辨配的是哪个账号。
      notice.textContent =
        `已配置账号（uid ${s.uid || s.uid_hint}）。更新账号不会删除订阅数据。`;
    }
  } catch {
    /* 状态拿不到不影响使用 */
  }
  openModal("modal-setup");
  $("in-uid").focus();
}

async function verifyCookie() {
  setMsg("setup-msg", "验证中…");
  try {
    const r = await post("/api/setup/cookie/verify", {
      uid: $("in-uid").value.trim(),
      cid: $("in-cid").value.trim(),
    });
    setMsg(
      "setup-msg",
      `验证通过${r.username ? `，账号 ${r.username}${r.group ? "（" + r.group + "）" : ""}` : ""}`,
      "ok"
    );
  } catch (err) {
    setMsg("setup-msg", err.message, "bad");
  }
}

async function saveCookie() {
  setMsg("setup-msg", "保存中…");
  try {
    await post("/api/setup/cookie", {
      uid: $("in-uid").value.trim(),
      cid: $("in-cid").value.trim(),
    });
    setMsg("setup-msg", "已保存", "ok");
    toast("账号已保存", "ok");
    await refreshStatus();
    await loadSubs();
    setTimeout(() => closeModal("modal-setup"), 500);
  } catch (err) {
    setMsg("setup-msg", err.message, "bad");
  }
}

/* ---------------------------------------------------------- 引导页 */

/** 引导页是不是被 ?guide=init 强制唤起的。
 *
 *  这一页平时看不到，留这个开关只是为了方便测试；强制模式下 Esc 也能关，
 *  自动模式下（首启无账号无订阅）Esc 不关——按理走完流程。
 */
let guideForced = false;

/**
 * 引导页要不要自动出现：既没有账号，也没有订阅。
 *
 * 首轮 tick() 已经把状态和订阅拉过一遍了——这里直接复用，别再打一遍
 * 同样的请求（那会让每个页面加载都多两次往返）。
 */
async function shouldShowGuide() {
  try {
    guideForced = new URLSearchParams(location.search).get("guide") === "init";
  } catch {
    /* 老浏览器没有 URLSearchParams，忽略即可 */
    guideForced = false;
  }
  if (guideForced) return true;
  if (bootReady) return !state.configured && state.subs.length === 0;
  // 首轮没拉成功（服务刚起、连接被拒）——自己再试一次。
  try {
    const s = await api("/api/status");
    if (s.credentials_configured) return false;
    const subs = await api("/api/subscriptions");
    return !subs.length;
  } catch (err) {
    return false;
  }
}

function showGuide(step = 1) {
  $("guide").hidden = false;
  document.body.style.overflow = "hidden";
  // 颜色圆点得先画出来，不然第 2 步是空的。
  buildGuideSwatches();
  prepareGuideReminders();
  guideStep(step);
}

function hideGuide() {
  // 日历面板挂在 body 上，藏起引导页不会自动跟着走——得自己收掉，
  // 否则它会留在主页面中间不走。
  closeGuideDatePicker();
  $("guide").hidden = true;
  if (!document.querySelector(".modal-root:not([hidden])")) {
    document.body.style.overflow = "";
  }
}

/** 收起引导页自己的日历面板（如果开着）。 */
function closeGuideDatePicker() {
  if (guideDatePicker && guideDatePicker.visible) guideDatePicker.hide();
}

function guideStep(n) {
  // 离开第 3 步就把日历收起来——它挂在 body 上，不会跟着面板一起走。
  if (n !== 3) closeGuideDatePicker();
  ["1", "2", "3"].forEach((i) => {
    $("gpanel-" + i).classList.toggle("on", i === String(n));
  });
  // 步骤条：当前那步高亮，走过的那些标成「已完成」。
  ["1", "2", "3"].forEach((i, idx) => {
    const node = $("gstep-" + i);
    node.classList.toggle("on", n === idx + 1);
    node.classList.toggle("done", n > idx + 1);
  });
  setMsg(n === 1 ? "g-msg" : n === 2 ? "g-msg2" : "g-msg3", "");
}

/* ------------------------------------------------ 引导页第 3 步：提醒设置 */

/** 引导页自己的分段选择（更新间隔）与日期组件（订阅时间）。 */
let guideIntervalTabs = null;
let guideDatePicker = null;

/** 默认值：今天 0 点、每 5 分钟。 */
const GUIDE_INTERVAL_DEFAULT = "300";

/** 「今天 0 点」的本地时间字符串 */
function guideDefaultTime() {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const today =
    `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  return `${today} 00:00`;
}

/** 打开引导页时把第 3 步的默认值填好——用户不改也能直接用。 */
function prepareGuideReminders() {
  const input = $("g-time");
  if (input && !input.value) input.value = guideDefaultTime();
  if (guideIntervalTabs) guideIntervalTabs.setValue(GUIDE_INTERVAL_DEFAULT);
}

/**
 * 走完第 3 步：把订阅时间和更新间隔写回服务端。
 *
 * 订阅时间用本地墙上时间提交（跟顶栏那个日期框一样），浏览器知道自己在
 * 哪个时区，服务端就不用猜。
 */
async function guideFinishReminders() {
  const btn = $("g-finish");
  btn.disabled = true;
  setMsg("g-msg3", "正在保存…", "");
  try {
    const local = $("g-time").value.trim() || guideDefaultTime();
    const seconds = Number(
      guideIntervalTabs ? guideIntervalTabs.value : GUIDE_INTERVAL_DEFAULT
    );
    await post("/api/settings/poll-interval", { seconds });
    const r = await post("/api/settings/view-since", { view_since: toAbsoluteIso(local) });
    hideGuide();
    toast(
      r && r.rescan ? "提醒设置已保存，正在按这个时间补取回复" : "提醒设置已保存",
      "ok"
    );
    await tick();
  } catch (err) {
    setMsg("g-msg3", err.message, "bad");
  } finally {
    btn.disabled = false;
  }
}

async function guideSaveAccount() {
  const uid = $("g-uid").value.trim();
  const cid = $("g-cid").value.trim();
  if (!uid || !cid) {
    setMsg("g-msg", "两个值都要填", "bad");
    return;
  }
  const btn = $("g-save");
  btn.disabled = true;
  setMsg("g-msg", "正在验证…", "");
  try {
    const v = await post("/api/setup/cookie/verify", { uid, cid });
    await post("/api/setup/cookie", { uid, cid });
    setMsg("g-msg", `已保存：${v.username || uid}`, "ok");
    await refreshStatus();
    guideStep(2);
  } catch (err) {
    setMsg("g-msg", err.message, "bad");
  } finally {
    btn.disabled = false;
  }
}

async function guideAddSubscription() {
  const url = $("g-url").value.trim();
  const alias = $("g-name").value.trim();
  const parsed = parseLink(url);
  if (!parsed) {
    setMsg("g-msg2", "没看懂这个链接。贴「只看楼主」的地址栏链接，或直接填 tid=…&authorid=…", "bad");
    return;
  }
  if (!parsed.uid) {
    setMsg("g-msg2", "链接里没有 authorid（用户 ID），请补一个或换用「只看楼主」链接", "bad");
    return;
  }
  const btn = $("g-add");
  btn.disabled = true;
  setMsg("g-msg2", "正在添加…", "");
  try {
    const res = await post("/api/subscriptions", {
      url,
      author_uid: Number(parsed.uid),
      alias,
      color: guidePickedColor,
    });
    // 引导页里订阅时间还没定（第 3 步才选），这里先不报「有没有回复」——
    // 第 3 步定下时间后，服务端会按那个时间重新扫一遍。
    toast("订阅已添加", "ok");
    // 加完订阅别急着走——还有提醒设置这一步。
    guideStep(3);
  } catch (err) {
    setMsg("g-msg2", err.message, "bad");
  } finally {
    btn.disabled = false;
  }
}

/* -------------------------------------------------------------- 事件 */

function wire() {
  $("btn-account").onclick = openSetupModal;
  $("banner-action").onclick = openSetupModal;
  $("notify-test").onclick = testNotification;
  $("notify-browser-enable").onclick = toggleBrowserNotify;
  $("browser-notify-toggle").onclick = toggleBrowserNotify;
  $("btn-add-side").onclick = () => openAddModal();

  // 滚到接近底部就自动接上更早的一段通知。
  const mainBody = $("main-body");
  mainBody.addEventListener(
    "scroll",
    () => {
      if (!state.feedHasMore || state.feedLoading) return;
      const gap = mainBody.scrollHeight - mainBody.scrollTop - mainBody.clientHeight;
      if (gap > 240) return;
      loadFeedMore();
    },
    { passive: true }
  );

  $("sub-all").onclick = () => selectSub(null);
  $("sub-all").onkeydown = (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      selectSub(null);
    }
  };
  // 「全部通知」这行不再挂菜单——它就是一个视图入口，没有子动作。

  $("btn-theme").onclick = () => applyTheme(currentTheme() === "light" ? "dark" : "light");

  intervalSelect = createSelect({
    trigger: $("interval-trigger"),
    menu: $("interval-menu"),
    valueEl: $("interval-value"),
    options: Object.entries(INTERVAL_LABELS).map(([value, label]) => ({ value, label })),
    value: "300",
    onChange: async (value) => {
      const seconds = Number(value);
      try {
        await post("/api/settings/poll-interval", { seconds });
        toast(
          seconds > 0 ? `更新间隔已改为 ${intervalLabel(seconds)}` : "已关闭更新",
          "ok"
        );
      } catch (err) {
        toast(err.message, "bad");
      }
      intervalSelect.dirty = false;
      await refreshStatus();
    },
  });

  // 订阅时间：Air Datepicker 面板（现成组件，配色用 CSS 变量跟玻璃主题统一）。
  // 整个胶囊都是唤起入口——点标签文字、点空白、点日历图标都开面板。
  const timeField = $("time-field");
  const timeInput = $("view-since-input");
  const AirDatepicker = window.AirDatepicker;
  if (AirDatepicker) {
    datePicker = new AirDatepicker(timeInput, {
      // 值格式跟服务端对得上：日期 + 空格 + 24 小时制时间
      dateFormat: "yyyy-MM-dd",
      timeFormat: "HH:mm",
      dateTimeSeparator: " ",
      timepicker: true,
      hoursStep: 1,
      minutesStep: 1,
      locale: window.NGA_DATE_LOCALE,
      classes: "nga-datepicker",
      position: "bottom left",
      offset: 8,
      // 再点一次已经选中的日期，不要把它取消掉——那是误触，不是「反悔」。
      toggleSelected: false,
      // 组件被收起的时候判断一下有没有变更：有就存一次。
      //
      // 拖时间滑杆的时候组件一路派发 change——滑一格就是一次。每一次都存
      // 就是一场请求风暴：每存一次服务端都要重扫一遍订阅、把窗口外的楼层
      // 重新判一遍。
      //
      // 所以这里不直接存，而是「攒下来，等用户停手再存」：收起时读输入框
      // 此刻的值攒着，拖拽还没结束的话，接下来的 change 会把等待重新计时，
      // 最后只存一次、存的是拖完之后那个值。
      onHide: () => {
        scheduleViewSinceSave(timeInput.value.trim());
      },
    });
  }

  // 值变了就记下最新的值：点日期、调时间、手打输入都走这一条路。
  // 组件每次更新输入框都会派发一个 change 事件，所以这一处就够了。
  //
  // 这里只记值，不判断也不保存——「有没有真的变」留到保存的时候再判断。
  // 在这里判断会出错：拖滑杆时会「路过」服务端那个值（比如 09:00 拖到 19:00
  // 途中经过 09:00），一判断就把它当没变跳过去，最后存下去的是上一步的值。
  timeInput.addEventListener("change", () => {
    // 程序同步时组件会回灌输入框并派发 change——那不是用户改的，不算数。
    if (syncGate > 0) return;
    const value = timeInput.value.trim();
    if (!value) return;
    scheduleViewSinceSave(value);
  });

  timeField.addEventListener("click", (e) => {
    if (!datePicker) return;
    if (e.target === timeInput) return; // 点输入框本身，交给组件自己的行为
    e.preventDefault();
    if (datePicker.visible) datePicker.hide();
    else datePicker.show();
  });

  // 点日历面板和日期框以外的地方就收起日历。
  // 组件自己只在「输入框失焦」时收起，可从「订阅时间」文字唤起的面板并没有焦点，
  // 失焦那条路走不通——所以这里自己兜一道。
  document.addEventListener("click", (e) => {
    if (!datePicker || !datePicker.visible) return;
    const t = e.target;
    if (!(t instanceof Element)) return;
    if (t.closest(".air-datepicker") || t.closest("#time-field")) return;
    datePicker.hide();
  });

  $("btn-verify").onclick = verifyCookie;
  $("btn-save-cookie").onclick = saveCookie;
  // 「清除全部数据」是二级确认：先关掉账号弹窗，再弹确认框。
  $("btn-wipe-all").onclick = () => {
    closeModal("modal-setup");
    openModal("modal-wipe");
    $("btn-confirm-wipe").focus();
  };
  $("btn-confirm-wipe").onclick = async () => {
    const btn = $("btn-confirm-wipe");
    btn.disabled = true;
    try {
      await api("/api/setup/all", { method: "DELETE" });
      closeModal("modal-wipe");
      $("in-uid").value = "";
      $("in-cid").value = "";
      toast("已清除全部数据", "ok");
      await refreshStatus();
      await loadSubs();
      await loadFeedHead(true);
      // 回到最开始的态：没有账号、没有订阅——引导页的条件成立了。
      showGuide(1);
    } catch (err) {
      toast(err.message, "bad");
    } finally {
      btn.disabled = false;
    }
  };

  // 引导页
  $("g-theme").onclick = () => applyTheme(currentTheme() === "light" ? "dark" : "light");
  $("g-save").onclick = guideSaveAccount;
  $("g-skip").onclick = () => guideStep(2);
  $("g-to-sub").onclick = () => guideStep(2);
  $("g-add").onclick = guideAddSubscription;
  $("g-url").oninput = updateGuideParsePreview;
  $("g-back").onclick = () => guideStep(1);
  // 订阅以后再加也行，但提醒设置这一步还是值得顺手看一眼。
  $("g-later").onclick = () => guideStep(3);
  $("g-back2").onclick = () => guideStep(2);
  $("g-finish").onclick = guideFinishReminders;
  $("g-skip3").onclick = () => {
    hideGuide();
    tick();
  };

  // 第 3 步的更新间隔：一段平铺的 tab，不复用顶栏那个下拉框。
  guideIntervalTabs = createTabs({
    root: $("g-interval"),
    options: Object.entries(INTERVAL_LABELS).map(([value, label]) => ({ value, label })),
    value: GUIDE_INTERVAL_DEFAULT,
    onChange: () => {},
  });

  // 第 3 步的订阅时间：同样是现成的日期组件，不是自己画的日历。
  if (AirDatepicker) {
    guideDatePicker = new AirDatepicker($("g-time"), {
      dateFormat: "yyyy-MM-dd",
      timeFormat: "HH:mm",
      dateTimeSeparator: " ",
      timepicker: true,
      hoursStep: 1,
      minutesStep: 1,
      locale: window.NGA_DATE_LOCALE,
      classes: "nga-datepicker",
      position: "bottom left",
      offset: 8,
      toggleSelected: false,
    });
  }

  // 输入框旁边的「选择」按钮：显式地开 / 关日历面板。
  // stopPropagation 是为了别让下面那条「点外面就收起」的监听器接着把它关掉。
  $("g-time-pick").onclick = (e) => {
    e.stopPropagation();
    if (!guideDatePicker) return;
    if (guideDatePicker.visible) guideDatePicker.hide();
    else guideDatePicker.show();
  };

  // 点日历面板和日期框以外的地方就收起日历。
  // 组件自己只在「输入框失焦」时收起，而从「选择」按钮唤起的面板并没有焦点，
  // 失焦那条路走不通——所以这里自己兜一道。
  document.addEventListener("click", (e) => {
    if (!guideDatePicker || !guideDatePicker.visible) return;
    const t = e.target;
    if (!(t instanceof Element)) return;
    if (t.closest(".air-datepicker") || t.closest("#g-time") || t.closest("#g-time-pick")) {
      return;
    }
    guideDatePicker.hide();
  });

  $("btn-add-sub").onclick = submitSubscription;
  $("in-url").oninput = updateParsePreview;

  document.querySelectorAll("[data-close]").forEach((n) => {
    n.onclick = () => closeModal(n.closest(".modal-root").id);
  });
  document.querySelectorAll("[data-close-drawer]").forEach((n) => {
    n.onclick = closeDrawer;
  });

  document.addEventListener("click", (e) => {
    if (!e.target.closest(".sub-menu") && !e.target.closest(".sub-more")) {
      closeAllMenus();
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeAllMenus();
      const open = document.querySelector(".modal-root:not([hidden])");
      if (open) closeModal(open.id);
      else if (!$("drawer").hidden) closeDrawer();
      // 引导页只在「强制唤起」（?guide=init）时允许 Esc 关掉——
      // 正常的新用户流程里不该被随手关掉。
      else if (!$("guide").hidden && guideForced) {
        hideGuide();
        tick();
      }
    }
    const modal = document.querySelector(".modal-root:not([hidden])");
    if (modal) trapFocus(e, modal.id);
  });
}

/* -------------------------------------------------------------- 启动 */

/**
 * 状态 + 订阅：顶栏、左栏，以及引导页的判断都靠它。
 *
 * 单独拎出来，是因为引导页要**立刻**知道「有没有账号 / 有没有订阅」——
 * 要是让它等整个通知流加载完才决定，用户得多等一次全量拉取。
 */
async function loadCore() {
  const [statusOk, subsOk] = await Promise.all([refreshStatus(), loadSubs()]);
  if (statusOk && subsOk) bootReady = true;
  return statusOk && subsOk;
}

async function tick() {
  await loadCore();
  await loadFeedHead(false);
}

applyTheme(currentTheme());
wire();
syncBrowserNotifyUi();

// 首轮：状态和订阅先走（引导页马上要答案），通知流同时拉——不必排在后面等。
const boot = loadCore();
loadFeedHead(false).catch(() => {});
setInterval(tick, 15000);

// 新用户（没账号也没订阅）直接落到引导页；?guide=init 强制唤起。
// 首轮万一抛了错也不能把引导页吞掉——bootReady 仍是 false，
// shouldShowGuide() 会自己再拉一次。
boot
  .catch(() => {})
  .then(async () => {
    if (await shouldShowGuide()) showGuide(1);
  });
