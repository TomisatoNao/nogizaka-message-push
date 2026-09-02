// archive.js
"use strict";
const $ = (id) => document.getElementById(id);
// 清理旧版本遗留的可读 Token；新的 Token 模式使用 HttpOnly 会话 Cookie。
try { localStorage.removeItem("webAdminToken"); } catch (_) {}
const TYPES = [["", "全部"], ["text", "文字"], ["picture", "图片"], ["video", "视频"], ["voice", "语音"]];
const BLOG_GROUP_KEYS = ["nogizaka", "sakurazaka", "hinatazaka"];

let members = [];        // [{name, display, total, months}]
let blogGroups = [];     // [{key, total, first_date, last_date}]
let curMember = "";
let curBlogGroup = "";   // 非空 = 博客模式
let months = [];         // [{year, month, count}] 新的在前
let curYM = null;        // {year, month}
let curType = "";
const MESSAGE_ORDER_DEFAULT = "desc";
let messageOrder = (() => {
  try {
    return localStorage.getItem("archive_message_order") === "asc" ? "asc" : MESSAGE_ORDER_DEFAULT;
  } catch (_) {
    return MESSAGE_ORDER_DEFAULT;
  }
})();
let page = 1, totalPages = 1;
let images = [];         // 当月已渲染图片 [{url, caption}]，供灯箱翻页
let lastDay = "";
let searchQuery = "";    // 非空 = 搜索模式
let dayCounts = {};      // "YYYY-MM-DD" -> 条数（日历用）
let calYM = null;        // 日历当前显示的 {year, month}（可独立于时间线翻页）
let blogCalendarError = "";
let blogGroupsError = "";
let contentVersion = 0;  // 成员 / 月份 / 筛选变化后，旧响应不应覆盖新页面
let contentAbort = null;
let pageLoading = false;
let calendarVersion = 0;
let calendarAbort = null;
let blogPageVersion = 0;      // 博客列表请求版本，避免旧响应覆盖当前筛选
let blogPageAbort = null;
let blogSelectionVersion = 0; // 博客分组/作者切换版本
let lightboxOpener = null;
let memberVersion = 0;
let targetMsgId = "";    // 首页跳转目标消息 ID（避免被 syncHash 冲掉）
let curMode = "msg";     // "msg" 或 "blog"
let curBlogAuthor = "";  // 当前选中的博客作者
let curBlogDate = "";    // 当前选中的博客日期 (YYYY-MM-DD)
function esc(s) { const d = document.createElement("div"); d.textContent = String(s); return d.innerHTML; }
function sanitizeHtml(htmlStr) {
  if (!htmlStr) return "";
  try {
    const parser = new DOMParser();
    const doc = parser.parseFromString(htmlStr, "text/html");
    const dangerousTags = doc.querySelectorAll("script, iframe, object, embed, base, link, form, meta");
    dangerousTags.forEach(el => el.remove());
    const allElements = doc.querySelectorAll("*");
    allElements.forEach(el => {
      for (let i = el.attributes.length - 1; i >= 0; i--) {
        const attr = el.attributes[i];
        const attrName = attr.name.toLowerCase();
        if (attrName.startsWith("on") || (attr.value && attr.value.trim().toLowerCase().startsWith("javascript:"))) {
          el.removeAttribute(attr.name);
        }
      }
    });
    return doc.body.innerHTML;
  } catch (e) {
    return esc(htmlStr);
  }
}
function mediaUrl(u) {
  return u || "";
}

window.handleImgError = function(img) {
  const retryCount = parseInt(img.dataset.retry || "0", 10);
  if (retryCount < 3) {
    img.dataset.retry = String(retryCount + 1);
    const rawUrl = img.dataset.src || img.src;
    setTimeout(() => {
      const base = rawUrl.split("?")[0];
      img.src = base + "?_retry=" + Date.now();
    }, 250 * (retryCount + 1));
  } else {
    // 若本地媒体重试失败，尝试回退到官方 CDN 原始链接
    const origUrl = img.dataset.origSrc;
    if (origUrl && img.src !== origUrl) {
      img.dataset.retry = "99";
      img.src = origUrl;
      return;
    }
    img.classList.add("img-broken");
    if (!img.nextElementSibling || !img.nextElementSibling.classList.contains("pc-broken-fallback")) {
      const fb = document.createElement("div");
      fb.className = "pc-broken-fallback";
      fb.innerHTML = '<span style="font-size:26px;opacity:0.5;">🖼️</span>';
      img.parentNode.insertBefore(fb, img.nextSibling);
    }
  }
};

let _refreshingPromise = null;

async function silentRefreshToken() {
  if (!_refreshingPromise) {
    _refreshingPromise = (async () => {
      try {
        const resp = await fetch("/api/auth/refresh", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          cache: "no-store",
        });
        const data = await resp.json();
        return !!(data && data.ok);
      } catch (_) {
        return false;
      } finally {
        _refreshingPromise = null;
      }
    })();
  }
  return _refreshingPromise;
}

async function establishApiTokenSession(rawToken) {
  try {
    const resp = await fetch("/api/auth/token-session", {
      method: "POST",
      headers: { "X-Auth-Token": rawToken },
      cache: "no-store",
    });
    const data = await resp.json();
    return !!(resp.ok && data && data.ok);
  } catch (_) {
    return false;
  }
}

async function api(path, options = {}) {
  const headers = Object.assign({}, options.headers || {});
  const resp = await fetch(path, {
    method: options.method || "GET",
    headers: headers,
    body: options.body,
    cache: "no-store",
    signal: options.signal,
  });

  if (resp.status === 401 && !options._retried && path !== "/api/auth/refresh" && path !== "/api/auth/login") {
    const refreshed = await silentRefreshToken();
    if (refreshed) {
      return api(path, Object.assign({}, options, { _retried: true }));
    }
  }

  let data = null;
  try { data = await resp.json(); } catch (_) {}
  if (!resp.ok) {
    const err = (data && data.errors && data.errors.join("；")) || ("HTTP " + resp.status);
    if (resp.status === 401 && err.includes("未授权")) {
      const supplied = prompt("需要访问令牌（.env 的 WEB_ADMIN_TOKEN）：");
      if (supplied && await establishApiTokenSession(supplied.trim())) {
        return api(path, Object.assign({}, options, { _retried: true }));
      }
    }
    if (resp.status === 401 && err.includes("未登录")) {
      window.location.href = "/login?next=" + encodeURIComponent(window.location.pathname + window.location.search);
    }
    throw new Error(err);
  }
  return data;
}

function normalizedQuery(value) { return String(value || "").trim().slice(0, 100); }
function syncSearchInput() { $("searchBox").value = searchQuery; $("searchClear").hidden = !searchQuery; }
function messageOrderLabel() {
  return messageOrder === "asc" ? "从早到晚" : "最新优先";
}
function loadMoreLabel() {
  return messageOrder === "asc" ? "加载后续消息 ↓" : "加载更早消息 ↓";
}
function syncMessageOrderControls() {
  document.querySelectorAll("#messageOrderToggle [data-order]").forEach((button) => {
    const active = button.dataset.order === messageOrder;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}
function setMessageOrder(order, { persist = true, reload = true } = {}) {
  const next = order === "asc" ? "asc" : MESSAGE_ORDER_DEFAULT;
  const changed = next !== messageOrder;
  messageOrder = next;
  if (persist) {
    try { localStorage.setItem("archive_message_order", messageOrder); } catch (_) {}
  }
  syncMessageOrderControls();
  if (!changed || !reload || curMode !== "msg") {
    if (reload && curMode === "msg" && !curBlogGroup) syncHash();
    return;
  }
  if (searchQuery) {
    startSearch(searchQuery);
  } else if (curYM) {
    selectMonth(curYM.year, curYM.month);
  }
}

function resetContent() {
  contentVersion++;
  if (contentAbort) contentAbort.abort();
  contentAbort = null;
  pageLoading = false;
  page = 1; totalPages = 1; images = []; lastDay = "";
  $("timeline").innerHTML = "";
  $("emptyHint").hidden = true;
  $("loadMore").hidden = true;
  $("loadMore").disabled = false;
  $("loadMore").textContent = loadMoreLabel();
  return contentVersion;
}
function highlightQuery(str, query) {
  if (!str) return "";
  if (!query) return esc(str);
  const terms = query.split(/\s+/).filter(Boolean);
  if (!terms.length) return esc(str);
  let safe = esc(str);
  terms.forEach(t => {
    try {
      const re = new RegExp("(" + escRegex(t) + ")", "gi");
      safe = safe.replace(re, '<mark class="search-highlight">$1</mark>');
    } catch(e){}
  });
  return safe;
}

function formatMessageText(str, query) {
  if (!str) return "";
  // 直接移除订阅者占位符 %%%，恢复自然流畅的原文排版
  const cleaned = str.replace(/%%%/g, "");
  return highlightQuery(cleaned, query);
}

function formatCardText(str, maxLen = 160) {
  if (!str) return "";
  let s = str.replace(/%%%/g, "")
             .replace(/\[(?:opt|img|image|video|voice|media|emoji)[^\]]*\]/gi, "")
             .replace(/<[^>]+>/g, "")
             .replace(/[ \t]+/g, " ");
  // 压缩连续换行与多余空行
  s = s.replace(/\n\s*\n+/g, "\n").trim();
  if (s.length > maxLen) s = s.slice(0, maxLen).trim() + "...";
  return esc(s);
}

function escRegex(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function initInfiniteScroll() {
  window.addEventListener("scroll", () => {
    if (pageLoading || page >= totalPages) return;
    const scrollBottom = window.innerHeight + window.scrollY;
    const docHeight = document.documentElement.scrollHeight;
    if (docHeight - scrollBottom < 350) {
      if ($("blogGrid").style.display !== "none") return;
      page++;
      loadPage();
    }
  }, { passive: true });
}


function setPageLoading(loading) {
  pageLoading = loading;
  $("loadMore").disabled = loading;
  $("loadMore").textContent = loading ? "加载中…" : loadMoreLabel();
}


// ── JST 时间 ─────────────────────────────────────
function toJst(utc) {
  if (!utc) return new Date();
  if (utc instanceof Date) return new Date(utc.getTime() + 9 * 3600 * 1000);
  const s = String(utc).trim();
  let iso = s.replace(" ", "T");
  if (!iso.endsWith("Z") && !iso.includes("+") && !iso.includes("-", 10)) {
    iso += "Z";
  }
  const d = new Date(iso);
  if (isNaN(d.getTime())) return new Date();
  return new Date(d.getTime() + 9 * 3600 * 1000);
}
function fmtDay(utc) {
  const d = toJst(utc);
  const w = "日一二三四五六"[d.getUTCDay()];
  return d.getUTCFullYear() + "/" + (d.getUTCMonth() + 1) + "/" + d.getUTCDate() + "（" + w + "）";
}
function fmtTime(utc) {
  const d = toJst(utc);
  return String(d.getUTCHours()).padStart(2, "0") + ":" +
         String(d.getUTCMinutes()).padStart(2, "0") + ":" +
         String(d.getUTCSeconds()).padStart(2, "0");
}
function fmtUploadTime(uploadUtc, pubUtc) {
  const uD = toJst(uploadUtc);
  const pD = toJst(pubUtc);
  const isSameDay = uD.getUTCFullYear() === pD.getUTCFullYear() &&
                    uD.getUTCMonth() === pD.getUTCMonth() &&
                    uD.getUTCDate() === pD.getUTCDate();
  const timeStr = String(uD.getUTCHours()).padStart(2, "0") + ":" +
                  String(uD.getUTCMinutes()).padStart(2, "0") + ":" +
                  String(uD.getUTCSeconds()).padStart(2, "0");
  if (isSameDay) {
    return timeStr;
  }
  const dateStr = (uD.getUTCMonth() + 1) + "/" + uD.getUTCDate();
  return dateStr + " " + timeStr;
}
function fmtDelayDuration(sec) {
  if (sec < 0) return "0秒";
  if (sec < 60) return sec + "秒";
  const mins = Math.floor(sec / 60);
  if (mins < 60) {
    const s = sec % 60;
    return mins + "分" + (s > 0 ? s + "秒" : "");
  }
  const hrs = Math.floor(mins / 60);
  const remM = mins % 60;
  if (hrs < 24) {
    return hrs + "小时" + (remM > 0 ? remM + "分" : "");
  }
  const days = Math.floor(hrs / 24);
  const remH = hrs % 24;
  return days + "天" + (remH > 0 ? remH + "小时" : "");
}
function fmtCopyTime(utc) {
  const d = toJst(utc);
  const y = d.getUTCFullYear();
  const m = String(d.getUTCMonth() + 1).padStart(2, "0");
  const day = String(d.getUTCDate()).padStart(2, "0");
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  const ss = String(d.getUTCSeconds()).padStart(2, "0");
  return y + "/" + m + "/" + day + " " + hh + ":" + mm + ":" + ss;
}

function fmtDateKey(utc) {
  const d = toJst(utc);
  return d.getUTCFullYear() + "-" + String(d.getUTCMonth() + 1).padStart(2, "0") +
         "-" + String(d.getUTCDate()).padStart(2, "0");
}

function inferMemberGroup(m) {
  if (!m) return "";
  const grp = ((typeof m === "object" ? m.group : "") || "").toLowerCase();
  if (grp.includes("nogi")) return "nogizaka";
  if (grp.includes("sakura")) return "sakurazaka";
  if (grp.includes("yodel")) return "yodel";
  if (grp.includes("hinata")) return "hinatazaka";

  const nm = ((typeof m === "object" ? (m.name || m.display || "") : m) || "").replace(/[\s_　]/g, "");
  if (/^(マネダコ|松田好|丹生|yodel)/i.test(nm)) return "yodel";
  if (/^(冨里|賀喜|一ノ瀬|井上和|川崎|川﨑|五百城|中西|池田|奥田|菅原|小川|秋元|生田|生驹|伊藤|岩本|梅澤|遠藤さ|久保|齋藤飛|阪口|佐藤楓|柴田|白石|新内|鈴木|高山|田村真|筒井|西野|桥本|橋本|樋口|星野|松村|向井葉|山下美|弓木|与田|川端|小津|松尾美|黒見)/.test(nm)) return "nogizaka";
  if (/^(石森|小池|小林|田村保|森田|藤吉|山崎|山﨑|谷口|中川|山田|浅井|的野|上村莉|齋藤冬|菅井|土生|守屋|渡邉理|渡辺梨|井上梨|遠藤光|遠藤理|大園|大沼|幸阪|武元|増本|松田里|村井|村山|山下瞳|小島|向井|櫻坂|桜坂)/.test(nm)) return "sakurazaka";
  if (/^(金村|大野|佐藤|片山|坂井|下田|山下葉|大田|正源司|藤嶌|渡辺|小坂|加藤|齐藤|齊藤|佐佐木|佐々木|東村|河田|濱岸|富田|高本|高瀬|上村ひ|高橋|髙橋|森本|山口|平尾|平岡|竹内|岸|小西|清水|宮地|石塚|海邉|森平|矢田|松尾桜|新参者)/.test(nm)) return "hinatazaka";
  return "";
}

function getCurGroup() {
  const mObj = members.find(x => x.name === curMember);
  return inferMemberGroup(mObj || curMember);
}

// ── 日历 ─────────────────────────────────────────
function ensureCalendarMonth() {
  if (calYM) return;
  const now = toJst(new Date());
  calYM = { year: now.getUTCFullYear(), month: now.getUTCMonth() + 1 };
}

async function loadCalendar() {
  if (curMode === "blog") {
    return loadBlogCalendar();
  }
  const version = ++calendarVersion;
  if (calendarAbort) calendarAbort.abort();
  calendarAbort = new AbortController();
  try {
    const data = await api("/api/archive/calendar?member=" + encodeURIComponent(curMember) +
                           "&type=" + curType, { signal: calendarAbort.signal });
    if (version !== calendarVersion) return;
    dayCounts = data.ok ? data.days : {};
  } catch (e) {
    if (e.name === "AbortError" || version !== calendarVersion) return;
    dayCounts = {};
  }
  if (version !== calendarVersion) return;
  renderCalendar();
}

async function loadBlogCalendar() {
  if (curMode !== "blog" || !curBlogGroup) return;
  const version = ++calendarVersion;
  if (calendarAbort) calendarAbort.abort();
  calendarAbort = new AbortController();
  try {
    const data = await api("/api/archive/blog_calendar?group=" + encodeURIComponent(curBlogGroup) +
                           "&author=" + encodeURIComponent(curBlogAuthor || ""), { signal: calendarAbort.signal });
    if (version !== calendarVersion) return;
    if (!data.ok || typeof data.days !== "object") throw new Error("博客日历接口返回无效数据");
    blogCalendarError = "";
    dayCounts = data.days || {};
  } catch (e) {
    if (e.name === "AbortError" || version !== calendarVersion) return;
    dayCounts = {};
    blogCalendarError = "日历暂时不可用，请稍后重试";
  }
  if (version !== calendarVersion) return;
  
  if (!curBlogDate && (!calYM || Object.keys(dayCounts).length > 0)) {
    const keys = Object.keys(dayCounts).sort();
    if (keys.length > 0) {
      const latest = keys[keys.length - 1];
      const [y, m] = latest.split("-").map(Number);
      calYM = { year: y, month: m };
    }
  }
  renderCalendar();
}

function renderCalendar() {
  // 即使接口暂时失败或分组暂无数据，也显示一个可用的空日历，避免只剩标题占位符。
  ensureCalendarMonth();
  const { year, month } = calYM;
  $("calTitle").textContent = year + " 年 " + month + " 月";
  const grid = $("calGrid");
  grid.innerHTML = "";
  const entryNoun = curMode === "blog" ? "篇博客" : "条消息";
  for (const w of ["日", "一", "二", "三", "四", "五", "六"]) {
    const h = document.createElement("div");
    h.className = "cal-dow";
    h.textContent = w;
    grid.appendChild(h);
  }
  const firstDow = new Date(Date.UTC(year, month - 1, 1)).getUTCDay();
  const daysInMonth = new Date(Date.UTC(year, month, 0)).getUTCDate();
  for (let i = 0; i < firstDow; i++) grid.appendChild(document.createElement("div"));
  let monthTotal = 0;
  for (let d = 1; d <= daysInMonth; d++) {
    const key = year + "-" + String(month).padStart(2, "0") + "-" + String(d).padStart(2, "0");
    const n = dayCounts[key] || 0;
    monthTotal += n;
    const cell = document.createElement(n > 0 ? "button" : "div");
    cell.setAttribute("role", "gridcell");
    let cls = "cal-day" + (n > 0 ? " has" : "") +
      (n >= 6 ? " h3" : n >= 3 ? " h2" : n >= 1 ? " h1" : "");
    if (curMode === "blog" && curBlogDate === key) {
      cls += " active-day";
    }
    cell.className = cls;
    cell.textContent = d;
    if (n > 0) {
      cell.type = "button";
      cell.title = key + " · " + n + " " + entryNoun;
      cell.setAttribute("aria-label", key + "，共 " + n + " " + entryNoun + "，跳转到当天");
      cell.setAttribute("aria-pressed", String(curMode === "blog" && curBlogDate === key));
      const count = document.createElement("span");
      count.className = "n";
      count.textContent = n;
      cell.appendChild(count);
      cell.addEventListener("click", () => jumpToDay(key));
    }
    grid.appendChild(cell);
  }
  $("calFoot").textContent = (curMode === "blog" && blogCalendarError)
    ? blogCalendarError
    : (monthTotal > 0
      ? "本月 " + monthTotal + " " + entryNoun + " · 点日期跳转"
      : (curMode === "blog" ? "本月无博客" : "本月无消息"));
}

$("calPrev").addEventListener("click", () => {
  ensureCalendarMonth();
  calYM = calYM.month === 1 ? { year: calYM.year - 1, month: 12 }
                            : { year: calYM.year, month: calYM.month - 1 };
  renderCalendar();
});
$("calNext").addEventListener("click", () => {
  ensureCalendarMonth();
  calYM = calYM.month === 12 ? { year: calYM.year + 1, month: 1 }
                             : { year: calYM.year, month: calYM.month + 1 };
  renderCalendar();
});

async function jumpToDay(dateKey) {
  const [y, m] = dateKey.split("-").map(Number);
  searchQuery = "";
  syncSearchInput();

  if (curMode === "blog") {
    // 博客模式下的日期跳转：重置页码为 1，清空关键词，切换/锁定指定日期
    page = 1;
    curBlogDate = (curBlogDate === dateKey) ? "" : dateKey;
    renderCalendar();
    await loadBlogPage(1);

    // 平滑滚动至博客列表顶部
    const targetSection = $("blogCards").parentElement || $("blogCards");
    targetSection.scrollIntoView({ block: "start", behavior: "smooth" });
    return;
  }

  await selectMonth(y, m);
  let jumpVersion = contentVersion;
  // 加载全月（最多几页），保证目标日期的分隔条已渲染
  while (jumpVersion === contentVersion && page < totalPages) { page++; await loadPage(); }
  if (jumpVersion !== contentVersion) return;
  $("loadMore").hidden = true;
  // 兜底：当前类型筛选下该日期没有消息 → 自动切回「全部」重载
  if (!document.querySelector('.day-sep[data-date="' + dateKey + '"]') && curType) {
    curType = "";
    $("typeChips").querySelectorAll(".chip").forEach((c, i) =>
      c.classList.toggle("active", TYPES[i][0] === ""));
    loadCalendar();
    await selectMonth(y, m);
    jumpVersion = contentVersion;
    while (jumpVersion === contentVersion && page < totalPages) { page++; await loadPage(); }
    if (jumpVersion !== contentVersion) return;
    $("loadMore").hidden = true;
  }

  // ── 滚动定位 + 校正 ──
  // 全月消息加载完成后，图片是 lazy 的，加载时会撑高 DOM 把目标位置
  // 往下推，所以需要定时校正几次。但一旦用户主动操作（滚轮 / 触摸 /
  // 键盘），说明不需要这个位置了，立即停掉所有后续校正。
  let cancelled = false;
  const cancel = () => { cancelled = true; };
  window.addEventListener("wheel", cancel, { once: true, passive: true });
  window.addEventListener("touchstart", cancel, { once: true, passive: true });
  window.addEventListener("keydown", cancel, { once: true, passive: true });

  const pin = () => {
    if (cancelled) return false;
    const sep = document.querySelector('.day-sep[data-date="' + dateKey + '"]');
    if (!sep) return false;
    sep.scrollIntoView({ block: "start", behavior: "instant" });
    return true;
  };

  pin();
  setTimeout(pin, 350);
  setTimeout(pin, 900);
  setTimeout(() => {
    if (pin()) {
      const sep = document.querySelector('.day-sep[data-date="' + dateKey + '"]');
      if (sep) {
        sep.classList.add("flash");
        setTimeout(() => sep.classList.remove("flash"), 2500);
      }
    }
  }, 2200);

  // 清理监听器（最多保留 5 秒）
  setTimeout(() => {
    window.removeEventListener("wheel", cancel);
    window.removeEventListener("touchstart", cancel);
    window.removeEventListener("keydown", cancel);
  }, 5000);
}

// ── 模式切换与记忆 ─────────────────────────────────────
function getDefaultNogiMember() {
  if (!members || !members.length) return "冨里奈央";
  const nogi = members.find(m => inferMemberGroup(m) === "nogizaka");
  return nogi ? nogi.name : members[0].name;
}

function setHtmlViewClass(mode) {
  const root = document.documentElement;
  root.classList.remove("view-home", "view-msg", "view-blog", "view-letter");
  if (mode) root.classList.add("view-" + mode);
}

function switchMainTab(mode, keepHash) {
  curMode = mode;
  setHtmlViewClass(mode);
  try { localStorage.setItem("archive_last_main_tab", mode); } catch (_) {}
  const tabHome = $("tabHome");
  if (tabHome) tabHome.classList.toggle("active", mode === "home");
  if ($("tabMsg")) $("tabMsg").classList.toggle("active", mode === "msg");
  if ($("tabBlog")) $("tabBlog").classList.toggle("active", mode === "blog");
  if ($("tabLetter")) $("tabLetter").classList.toggle("active", mode === "letter");

  if (mode === "home") {
    if (!keepHash) goHome();
  } else if (mode === "msg") {
    _enterMemberMode();
    if (!keepHash) {
      let saved = null;
      try { saved = localStorage.getItem("archive_last_msg_member"); } catch (_) {}
      const wanted = (saved && members.some(m => m.name === saved))
        ? saved
        : (curMember && members.some(m => m.name === curMember))
          ? curMember
          : getDefaultNogiMember();
      selectMember(wanted);
    }
  } else if (mode === "blog") {
    let savedGroup = null;
    let savedAuthor = "";
    try {
      savedGroup = localStorage.getItem("archive_last_blog_group");
      savedAuthor = localStorage.getItem("archive_last_blog_author") || "";
    } catch (_) {}
    const gKey = (savedGroup && blogGroups.some(g => g.key === savedGroup)) ? savedGroup : (curBlogGroup || "nogizaka");
    selectBlogGroup(gKey, savedAuthor);
  } else if (mode === "letter") {
    if (!window._isArchiveAdmin) {
      switchMainTab("msg", keepHash);
      return;
    }
    let saved = null;
    try { saved = localStorage.getItem("archive_last_letter_member"); } catch (_) {}
    const wanted = (saved && members.some(m => m.name === saved))
      ? saved
      : (curLetterMember && members.some(m => m.name === curLetterMember))
        ? curLetterMember
        : getDefaultNogiMember();
    selectLetterMember(wanted);
  }
}

if ($("tabHome")) $("tabHome").addEventListener("click", () => goHome());
if ($("tabMsg")) $("tabMsg").addEventListener("click", () => switchMainTab("msg"));
if ($("tabBlog")) $("tabBlog").addEventListener("click", () => switchMainTab("blog"));
if ($("tabLetter")) $("tabLetter").addEventListener("click", () => switchMainTab("letter"));

// ── 数据加载 ─────────────────────────────────────
async function loadMembers(skipSelect = false) {
  const data = await api("/api/archive/members");
  if (!data.ok) { showEmpty("加载失败：" + (data.errors || []).join("；")); return; }
  members = data.members;
  if (!members.length) {
    showEmpty("还没有任何归档。确认 config.json 的 archive.enabled 已开启，" +
              "新消息会自动归档；历史消息用 python tools/backfill_archive.py 回填。");
    return;
  }
  // 渲染成员 chips & popover
  renderMemberChips();
  renderMemberPopover("");
  
  loadBlogGroupChips();

  // skipSelect=true 或非消息模式时只渲染 chips，不自动跳转
  if (skipSelect || curMode !== "msg") return;
  let saved = null;
  try { saved = localStorage.getItem("archive_last_msg_member"); } catch (_) {}
  const wanted = (curMember && members.some(m => m.name === curMember))
    ? curMember
    : (saved && members.some(m => m.name === saved))
      ? saved
      : getDefaultNogiMember();
  await selectMember(wanted, true);
}

function renderMemberChips() {
  const box = $("memberChips");
  if (!box) return;
  box.innerHTML = "";
  for (const m of members) {
    const b = document.createElement("button");
    b.className = "chip";
    if (m.name === curMember && curMode === "msg") b.classList.add("active");
    b.dataset.key = m.name;
    const numStr = m.total >= 1000 ? (m.total / 1000).toFixed(1).replace(/\.0$/, '') + "k" : m.total;
    b.innerHTML = '<span class="chip-name">' + esc(m.display) + '</span>' +
                  '<span class="chip-num" title="' + (m.total || 0).toLocaleString() + ' 条消息">' + numStr + '</span>';
    b.addEventListener("click", () => {
      hideHome();
      selectMember(m.name);
    });
    box.appendChild(b);
  }
}

function renderMemberPopover(filterKeyword = "") {
  const list = $("memberPopoverList");
  if (!list) return;
  list.setAttribute("role", "listbox");
  const searchInput = $("memberSearchInput");
  if (searchInput) searchInput.setAttribute("aria-label", "搜索成员");
  list.innerHTML = "";
  const kw = filterKeyword.toLowerCase().trim();
  const filtered = members.filter(m => !kw || m.display.toLowerCase().includes(kw) || m.name.toLowerCase().includes(kw));
  
  if ($("memberTotalBadge")) {
    $("memberTotalBadge").textContent = "共 " + members.length + " 人" + (kw ? " · 匹配 " + filtered.length + " 人" : "");
  }

  if (!filtered.length) {
    const empty = document.createElement("div");
    empty.style.cssText = "text-align:center; padding:20px 0; color:var(--muted); font-size:12.5px;";
    empty.textContent = "未找到匹配成员";
    list.appendChild(empty);
    return;
  }

  // 按坂道与 yodel 分组（全量遍历，绝不遗漏任何成员）
  const groups = [
    { key: "nogizaka", name: "乃木坂46", icon: "💜", cls: "nogi" },
    { key: "sakurazaka", name: "樱坂46", icon: "🌸", cls: "sakura" },
    { key: "hinatazaka", name: "日向坂46", icon: "🩵", cls: "hinata" },
    { key: "yodel", name: "yodel", icon: "🐙", cls: "yodel" },
    { key: "other", name: "其他成员", icon: "👤", cls: "other" },
  ];

  groups.forEach(g => {
    const grpMems = filtered.filter(m => {
      const gK = inferMemberGroup(m);
      return g.key === "other" ? (!gK || !["nogizaka", "sakurazaka", "hinatazaka", "yodel"].includes(gK)) : gK === g.key;
    });

    if (!grpMems.length) return;

    const gHead = document.createElement("div");
    gHead.className = "popover-group-header " + g.cls;
    gHead.innerHTML = '<span>' + g.icon + ' ' + g.name + '</span><span class="pgh-cnt">' + grpMems.length + ' 人</span>';
    list.appendChild(gHead);

    grpMems.forEach(m => {
      let avatarText = (m.display || "").replace(/[\s_　]/g, "");
      if (avatarText.length > 2) avatarText = avatarText.slice(-2);
      if (!avatarText) avatarText = "💬";
      if (m.name.includes("マネダコ")) avatarText = "🐙";

      let avatarHTML = '';
      if (m.avatar) {
        avatarHTML = '<img class="mpi-avatar-img" src="' + esc(m.avatar) + '" loading="lazy" decoding="async" alt="" onerror="this.style.display=\'none\';if(this.nextElementSibling)this.nextElementSibling.style.display=\'inline-flex\';" /><span class="mpi-avatar ' + g.cls + '" style="display:none;">' + esc(avatarText) + '</span>';
      } else {
        avatarHTML = '<span class="mpi-avatar ' + g.cls + '">' + esc(avatarText) + '</span>';
      }

      const item = document.createElement("div");
      item.className = "member-popover-item " + g.cls + (m.name === curMember && curMode === "msg" ? " active" : "");
      item.setAttribute("role", "option");
      item.tabIndex = 0;
      item.setAttribute("aria-selected", String(m.name === curMember && curMode === "msg"));
      item.setAttribute("aria-label", (m.display || m.name) + "，" + (m.total || 0).toLocaleString() + " 条消息");
      item.innerHTML = '<div class="m-name-txt">' +
                       avatarHTML +
                       '<span class="mpi-name">' + esc(m.display) + '</span>' +
                       '</div>' +
                       '<span class="m-cnt">' + (m.total || 0).toLocaleString() + ' 条</span>';
      item.addEventListener("click", () => {
        closeMemberPopover();
        hideHome();
        selectMember(m.name);
      });
      item.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          item.click();
        } else if (event.key === "Escape") {
          closeMemberPopover();
        }
      });
      list.appendChild(item);
    });
  });
}

function toggleMemberPopover() {
  const pop = $("memberPopover");
  const btn = $("btnMemberDropdown");
  if (!pop) return;
  const isOpen = pop.style.display !== "none";
  if (isOpen) {
    closeMemberPopover();
  } else {
    pop.style.display = "flex";
    if (btn) btn.classList.add("active");
    if ($("memberSearchInput")) {
      $("memberSearchInput").value = "";
      if ($("btnMemberSearchClear")) $("btnMemberSearchClear").style.display = "none";
      renderMemberPopover("");
      setTimeout(() => $("memberSearchInput").focus(), 50);
    }
  }
}

function closeMemberPopover() {
  const pop = $("memberPopover");
  const btn = $("btnMemberDropdown");
  if (pop) pop.style.display = "none";
  if (btn) btn.classList.remove("active");
}

async function loadBlogGroupChips() {
  try {
    const bg = await api("/api/archive/blog_groups");
    if (!bg.ok || !Array.isArray(bg.groups)) throw new Error("博客分组接口返回无效数据");
    blogGroupsError = "";
    if ($("blogGroupSegment")) $("blogGroupSegment").removeAttribute("title");
    blogGroups = bg.groups;
    blogGroups.forEach(g => {
      const numStr = g.total >= 1000 ? (g.total / 1000).toFixed(1).replace(/\.0$/, '') + "k" : g.total;
      if (g.key === "nogizaka" && $("bgNogiBadge")) $("bgNogiBadge").textContent = "(" + numStr + ")";
      if (g.key === "sakurazaka" && $("bgSakuraBadge")) $("bgSakuraBadge").textContent = "(" + numStr + ")";
      if (g.key === "hinatazaka" && $("bgHinataBadge")) $("bgHinataBadge").textContent = "(" + numStr + ")";
    });
    syncChipHighlight();
  } catch(e) {
    // 分组接口失败不能让导航失去作用；保留固定分组并给出可见提示。
    blogGroupsError = "博客分组统计暂不可用";
    if ($("blogGroupSegment")) $("blogGroupSegment").setAttribute("title", blogGroupsError);
    if (!blogGroups.length) blogGroups = BLOG_GROUP_KEYS.map(key => ({ key, total: 0 }));
    const badgeMap = { nogizaka: "bgNogiBadge", sakurazaka: "bgSakuraBadge", hinatazaka: "bgHinataBadge" };
    BLOG_GROUP_KEYS.forEach(key => { if ($(badgeMap[key])) $(badgeMap[key]).textContent = "(暂不可用)"; });
    syncChipHighlight();
  }
}

function _enterMemberMode() {
  curMode = "msg";
  setHtmlViewClass("msg");
  curBlogGroup = "";
  if ($("tabHome")) $("tabHome").classList.remove("active");
  if ($("tabMsg")) $("tabMsg").classList.add("active");
  if ($("tabBlog")) $("tabBlog").classList.remove("active");
  if ($("tabLetter")) $("tabLetter").classList.remove("active");

  $('archiveHome').classList.remove('active');
  $('backTop').style.display = ''; $('backTop').classList.remove('force-hide');
  document.querySelector('.layout').style.display = '';
  $("archiveSide").style.display = "";
  $("blogGrid").style.display = "none";
  if ($("letterGrid")) $("letterGrid").style.display = "none";
  $("timeline").style.display = "";
  const msgTb = document.querySelector(".msg-toolbar");
  if (msgTb) msgTb.style.display = "";
  const searchTb = $("searchBox") ? $("searchBox").closest(".toolbar") : null;
  if (searchTb) searchTb.style.display = "";
  $("tagToggle").parentElement.style.display = "";
  $("searchBox").style.display = $("searchSubmit").style.display = $("searchClear").style.display = "";
}

// 根据 curMember / curBlogGroup 同步状态与主选择器显示
function syncChipHighlight() {
  if (curMode === "msg" && curMember) {
    const curObj = members.find(m => m.name === curMember);
    if (curObj) {
      if ($("curMemberDisplay")) $("curMemberDisplay").textContent = curObj.display;
      if ($("curMemberCount")) $("curMemberCount").textContent = "（" + (curObj.total || 0).toLocaleString() + "）";
    }
  }
  // 同步博客分组 Segmented Control
  document.querySelectorAll("#blogGroupSegment .seg-btn").forEach(btn => {
    const active = curMode === "blog" && btn.dataset.key === curBlogGroup;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", String(active));
  });
}

// ── 博客相关逻辑 ─────────────────────────────────────
let curGroupAuthors = [];

function writeArchiveHash(hash) {
  selfHashUpdate = true;
  location.hash = hash || "";
  setTimeout(() => { selfHashUpdate = false; }, 0);
}

function buildBlogHash({ group = curBlogGroup, author = curBlogAuthor, date = curBlogDate,
                        query = searchQuery, pageNum = page, id = "" } = {}) {
  const p = new URLSearchParams();
  if (group) p.set("blog", group);
  if (author) p.set("author", author);
  if (date) p.set("date", date);
  if (query) p.set("q", normalizedQuery(query));
  if (pageNum && pageNum > 1) p.set("page", String(pageNum));
  if (id !== "" && id !== null && id !== undefined) p.set("id", String(id));
  return p.toString();
}

function syncBlogHash(pageNum = page) {
  writeArchiveHash(buildBlogHash({ pageNum }));
}

async function selectBlogGroup(key, author = "", updateHash = true, routeState = {}) {
  const selectionVersion = ++blogSelectionVersion;
  curMode = "blog";
  setHtmlViewClass("blog");
  curMember = "";
  curBlogGroup = key;
  curBlogAuthor = author || "";
  try {
    localStorage.setItem("archive_last_blog_group", key);
    localStorage.setItem("archive_last_blog_author", curBlogAuthor);
  } catch (_) {}
  curBlogDate = Object.prototype.hasOwnProperty.call(routeState, "date")
    ? String(routeState.date || "").slice(0, 10) : "";
  calYM = null;
  if (/^\d{4}-\d{2}-\d{2}$/.test(curBlogDate)) {
    const dateYear = Number(curBlogDate.slice(0, 4));
    const dateMonth = Number(curBlogDate.slice(5, 7));
    const dateDay = Number(curBlogDate.slice(8, 10));
    const maxDay = new Date(Date.UTC(dateYear, dateMonth, 0)).getUTCDate();
    if (dateMonth >= 1 && dateMonth <= 12 && dateDay >= 1 && dateDay <= maxDay) {
      calYM = { year: dateYear, month: dateMonth };
    } else {
      curBlogDate = "";
    }
  }
  dayCounts = {};
  blogCalendarError = "";
  renderCalendar();
  searchQuery = Object.prototype.hasOwnProperty.call(routeState, "q")
    ? normalizedQuery(routeState.q) : "";
  syncSearchInput();
  syncChipHighlight();

  if ($("tabHome")) $("tabHome").classList.remove("active");
  if ($("tabMsg")) $("tabMsg").classList.remove("active");
  if ($("tabBlog")) $("tabBlog").classList.add("active");
  if ($("tabLetter")) $("tabLetter").classList.remove("active");

  const requestedPage = Math.max(1, parseInt(routeState.page, 10) || 1);
  if (updateHash) syncBlogHash(1);

  $('archiveHome').classList.remove('active');
  $('backTop').style.display = ''; $('backTop').classList.remove('force-hide');
  document.querySelector('.layout').style.display = '';
  $("timeline").style.display = "none";
  $("blogGrid").style.display = "";
  if ($("letterGrid")) $("letterGrid").style.display = "none";
  $("archiveSide").style.display = "";

  const msgTb = document.querySelector(".msg-toolbar");
  if (msgTb) msgTb.style.display = "none";
  const searchTb = $("searchBox") ? $("searchBox").closest(".toolbar") : null;
  if (searchTb) searchTb.style.display = "";
  $("tagToggle").parentElement.style.display = "none";
  
  await loadBlogAuthors(key, selectionVersion);
  if (selectionVersion !== blogSelectionVersion) return;
  await loadBlogCalendar();
  if (selectionVersion !== blogSelectionVersion) return;
  await loadBlogPage(requestedPage, updateHash);
}

async function loadBlogAuthors(key, selectionVersion = blogSelectionVersion) {
  try {
    const data = await api("/api/archive/blog_authors?group=" + encodeURIComponent(key));
    if (selectionVersion !== blogSelectionVersion || curMode !== "blog" || curBlogGroup !== key) return;
    if (!data.ok || !Array.isArray(data.authors)) throw new Error("博客作者接口返回无效数据");
    curGroupAuthors = data.authors.filter(a => a && a.name && a.name.trim());
    renderBlogAuthorChips();
    renderBlogAuthorPopover("");
    updateBlogAuthorDisplay();
  } catch (e) {
    if (e.name === "AbortError" || selectionVersion !== blogSelectionVersion) return;
    curGroupAuthors = [];
    renderBlogAuthorChips();
    renderBlogAuthorPopover("");
    updateBlogAuthorDisplay();
  }
}

function renderBlogAuthorChips() {
  const box = $("blogAuthorChips");
  if (!box) return;
  box.innerHTML = "";

  // 全部成员
  const allBtn = document.createElement("button");
  allBtn.className = "chip" + (!curBlogAuthor ? " active" : "");
  allBtn.innerHTML = '<span class="chip-name">全部成员</span><span class="chip-num">' + curGroupAuthors.length + '人</span>';
  allBtn.onclick = () => selectBlogAuthor("");
  box.appendChild(allBtn);

  curGroupAuthors.forEach(a => {
    const btn = document.createElement("button");
    const isMatch = curBlogAuthor && (a.name === curBlogAuthor || a.name.replace(/[\s　_]+/g, "") === curBlogAuthor.replace(/[\s　_]+/g, ""));
    btn.className = "chip" + (isMatch ? " active" : "");
    btn.dataset.author = a.name;
    const cntStr = a.count ? '<span class="chip-num" title="' + a.count.toLocaleString() + ' 篇博客">' + (a.count >= 1000 ? (a.count / 1000).toFixed(1).replace(/\.0$/, '') + 'k' : a.count) + '</span>' : '';
    btn.innerHTML = '<span class="chip-name">' + esc(a.name) + '</span>' + cntStr;
    btn.onclick = () => selectBlogAuthor(a.name);
    box.appendChild(btn);
  });
}

function renderBlogAuthorPopover(filterKeyword = "") {
  const list = $("blogAuthorPopoverList");
  if (!list) return;
  list.setAttribute("role", "listbox");
  const searchInput = $("blogAuthorSearchInput");
  if (searchInput) searchInput.setAttribute("aria-label", "搜索博客作者");
  list.innerHTML = "";
  const kw = filterKeyword.toLowerCase().trim();
  const filtered = curGroupAuthors.filter(a => !kw || a.name.toLowerCase().includes(kw));

  if ($("blogAuthorTotalBadge")) {
    $("blogAuthorTotalBadge").textContent = "共 " + curGroupAuthors.length + " 位" + (kw ? " · 匹配 " + filtered.length + " 位" : "");
  }

  let grpClass = "hinata";
  if (curBlogGroup.includes("nogi")) grpClass = "nogi";
  else if (curBlogGroup.includes("sakura")) grpClass = "sakura";

  // 全部成员选项
  if (!kw) {
    const allItem = document.createElement("div");
    allItem.className = "author-popover-item" + (!curBlogAuthor ? " active" : "");
    allItem.setAttribute("role", "option");
    allItem.tabIndex = 0;
    allItem.setAttribute("aria-selected", String(!curBlogAuthor));
    allItem.innerHTML = '<div class="a-name-txt"><span class="mpi-avatar ' + grpClass + '">👥</span><span class="mpi-name">全部作者</span></div><span class="a-cnt">' + curGroupAuthors.length + ' 人</span>';
    allItem.addEventListener("click", () => {
      closeBlogAuthorPopover();
      selectBlogAuthor("");
    });
    allItem.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        allItem.click();
      } else if (event.key === "Escape") {
        closeBlogAuthorPopover();
      }
    });
    list.appendChild(allItem);
  }

  if (!filtered.length) {
    const empty = document.createElement("div");
    empty.style.cssText = "text-align:center; padding:16px 0; color:var(--muted); font-size:12px;";
    empty.textContent = "未找到匹配作者";
    list.appendChild(empty);
    return;
  }

  for (const a of filtered) {
    const isMatch = curBlogAuthor && (a.name === curBlogAuthor || a.name.replace(/[\s　_]+/g, "") === curBlogAuthor.replace(/[\s　_]+/g, ""));
    const cntTxt = a.count ? a.count.toLocaleString() + ' 篇' : '作者';
    const item = document.createElement("div");
    item.className = "author-popover-item" + (isMatch ? " active" : "");
    item.setAttribute("role", "option");
    item.tabIndex = 0;
    item.setAttribute("aria-selected", String(!!isMatch));
    item.setAttribute("aria-label", (a.name || "") + "，" + cntTxt);
    let avText = (a.name || "").replace(/[\s_　]/g, "");
    if (avText.length > 2) avText = avText.slice(-2);
    if (!avText) avText = "✍️";

    let avHTML = '';
    if (a.avatar) {
      avHTML = '<img class="mpi-avatar-img" src="' + esc(a.avatar) + '" loading="lazy" decoding="async" alt="" onerror="this.style.display=\'none\';if(this.nextElementSibling)this.nextElementSibling.style.display=\'inline-flex\';" /><span class="mpi-avatar ' + grpClass + '" style="display:none;">' + esc(avText) + '</span>';
    } else {
      avHTML = '<span class="mpi-avatar ' + grpClass + '">' + esc(avText) + '</span>';
    }

    item.innerHTML = '<div class="a-name-txt">' +
                     avHTML +
                     '<span class="mpi-name">' + esc(a.name) + '</span>' +
                     '</div>' +
                     '<span class="a-cnt">' + cntTxt + '</span>';
    item.addEventListener("click", () => {
      closeBlogAuthorPopover();
      selectBlogAuthor(a.name);
    });
    item.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        item.click();
      } else if (event.key === "Escape") {
        closeBlogAuthorPopover();
      }
    });
    list.appendChild(item);
  }
}

function updateBlogAuthorDisplay() {
  if ($("curBlogAuthorDisplay")) {
    $("curBlogAuthorDisplay").textContent = curBlogAuthor || "全部作者";
  }
}

function toggleBlogAuthorPopover() {
  const pop = $("blogAuthorPopover");
  const btn = $("btnBlogAuthorDropdown");
  if (!pop) return;
  const isOpen = pop.style.display !== "none";
  if (isOpen) {
    closeBlogAuthorPopover();
  } else {
    pop.style.display = "flex";
    if (btn) btn.classList.add("active");
    if ($("blogAuthorSearchInput")) {
      $("blogAuthorSearchInput").value = "";
      if ($("btnBlogAuthorSearchClear")) $("btnBlogAuthorSearchClear").style.display = "none";
      renderBlogAuthorPopover("");
      setTimeout(() => $("blogAuthorSearchInput").focus(), 50);
    }
  }
}

function closeBlogAuthorPopover() {
  const pop = $("blogAuthorPopover");
  const btn = $("btnBlogAuthorDropdown");
  if (pop) pop.style.display = "none";
  if (btn) btn.classList.remove("active");
}

function selectBlogAuthor(author) {
  ++blogSelectionVersion;
  curBlogAuthor = author;
  try { localStorage.setItem("archive_last_blog_author", author || ""); } catch (_) {}
  curBlogDate = "";
  dayCounts = {};
  blogCalendarError = "";
  calYM = null;
  updateBlogAuthorDisplay();
  renderCalendar();
  
  const chips = $("blogAuthorChips") ? $("blogAuthorChips").querySelectorAll(".chip") : [];
  chips.forEach(b => {
    const isAll = (!author && b.textContent.includes("全部成员"));
    const isMatch = author && b.dataset.author && (b.dataset.author === author || b.dataset.author.replace(/[\s　_]+/g, "") === author.replace(/[\s　_]+/g, ""));
    const act = !!(isAll || isMatch);
    b.classList.toggle("active", act);
    if (act) {
      b.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "center" });
    }
  });
  
  syncBlogHash(1);
  
  loadBlogCalendar();
  loadBlogPage(1);
}

// ── 渲染博客网格 ─────────────────────────────────────
async function loadBlogPage(pageNum, updateHash = true) {
  const requestedPage = Math.max(1, parseInt(pageNum, 10) || 1);
  const version = ++blogPageVersion;
  if (blogPageAbort) blogPageAbort.abort();
  blogPageAbort = new AbortController();
  page = requestedPage;
  if (updateHash && curMode === "blog") syncBlogHash(requestedPage);
  $("blogCards").innerHTML = "";
  $("blogHero").style.display = "none";
  $("blogHero").innerHTML = "";
  $("emptyHint").hidden = true;
  $("blogPagination").style.display = "none";
  $("blogPagination").innerHTML = "";
  $("loadMore").hidden = true;
  
  setPageLoading(true);
  try {
    let perPage = 24;
    let url = "/api/archive/blogs?group=" + encodeURIComponent(curBlogGroup) + "&page=" + requestedPage + "&per_page=" + perPage;
    if (curBlogAuthor) url += "&author=" + encodeURIComponent(curBlogAuthor);
    if (curBlogDate) url += "&date=" + encodeURIComponent(curBlogDate);
    if (searchQuery) url += "&q=" + encodeURIComponent(searchQuery);
    
    const data = await api(url, { signal: blogPageAbort.signal });
    if (version !== blogPageVersion || curMode !== "blog") return;
    if (!data.ok) throw new Error("加载失败");
    
    totalPages = Math.max(1, Number(data.total_pages) || 1);
    const postsData = Array.isArray(data.posts) ? data.posts : [];
    const blogStats = $("blogStats");
    if (blogStats) {
      blogStats.textContent = (blogGroupsError ? blogGroupsError + " · " : "") +
        (data.total || 0).toLocaleString() + " 篇" +
        (curBlogAuthor ? " · " + curBlogAuthor : "");
    }
    if (postsData.length === 0) {
      $("emptyHint").textContent = curBlogDate ? (curBlogDate + " 暂无符合条件的博客") : "没有找到博客";
      $("emptyHint").hidden = false;
    } else {
      let posts = postsData;
      if (requestedPage === 1 && posts.length > 0 && !searchQuery && !curBlogDate) {
        renderBlogHero(posts[0]);
        posts = posts.slice(1);
      }
      posts.forEach(p => {
        renderBlogMiniCard(p, $("blogCards"));
      });
      renderBlogPagination(requestedPage, totalPages);
      
      if (requestedPage > 1) {
        window.scrollTo({ top: 0, behavior: 'smooth' });
      }
    }
  } catch (e) {
    if (e.name !== "AbortError" && version === blogPageVersion && curMode === "blog") {
      $("emptyHint").textContent = "加载错误: " + e.message;
      $("emptyHint").hidden = false;
    }
  } finally {
    if (version === blogPageVersion) setPageLoading(false);
  }
}

function renderBlogPagination(curPage, total) {
  const container = $("blogPagination");
  if (total <= 1) return;
  container.style.display = "flex";
  
  let html = '';
  
  if (curPage > 1) {
    html += '<button class="bp-btn" onclick="loadBlogPage(' + (curPage - 1) + ')">‹</button>';
  } else {
    html += '<button class="bp-btn" disabled>‹</button>';
  }
  
  const pages = [];
  if (total <= 7) {
    for (let i = 1; i <= total; i++) pages.push(i);
  } else {
    if (curPage <= 4) {
      pages.push(1, 2, 3, 4, 5, '...', total);
    } else if (curPage >= total - 3) {
      pages.push(1, '...', total - 4, total - 3, total - 2, total - 1, total);
    } else {
      pages.push(1, '...', curPage - 1, curPage, curPage + 1, '...', total);
    }
  }
  
  for (const p of pages) {
    if (p === '...') {
      html += '<span class="bp-ellipsis">...</span>';
    } else {
      if (p === curPage) {
        html += '<button class="bp-btn active">' + p + '</button>';
      } else {
        html += '<button class="bp-btn" onclick="loadBlogPage(' + p + ')">' + p + '</button>';
      }
    }
  }
  
  if (curPage < total) {
    html += '<button class="bp-btn" onclick="loadBlogPage(' + (curPage + 1) + ')">›</button>';
  } else {
    html += '<button class="bp-btn" disabled>›</button>';
  }
  
  container.innerHTML = html;
}

function renderBlogHero(post) {
  const hero = $("blogHero");
  hero.style.display = "block";
  hero.dataset.date = (post.date || "").substring(0, 10);
  const dateStr = (post.date || "").substring(0, 16);

  // 列表接口只返回摘要和封面；正文在打开详情时按需加载。
  let bodyHtml = post.body_html || "";
  let coverUrl = post.cover || _getCoverUrl(bodyHtml);
  const excerptText = post.excerpt || bodyHtml.replace(/<[^>]+>/g, "");

  let coverHtml = '';
  if (coverUrl) {
    coverHtml = '<div class="bh-cover" style="background-image: url(\'' + esc(coverUrl) + '\')"><img src="' + esc(coverUrl) + '" data-orig-src="' + esc(post.cover_original || "") + '" loading="lazy" decoding="async" alt=""></div>';
  } else {
    // 无封面链接：保留原有无封面样式（📝 占位）
    coverHtml = '<div class="bh-cover no-pic" style="font-size:48px; color:var(--muted)">📝</div>';
  }

  hero.innerHTML =
    coverHtml +
    '<div class="bh-info">' +
      '<div class="bh-meta"><span class="bh-author">' + esc(post.author) + '</span><span class="bh-date">' + esc(dateStr) + '</span></div>' +
      '<h2 class="bh-title">' + highlightQuery(post.title || '无题', searchQuery) + '</h2>' +
      '<div class="bh-excerpt">' + esc(excerptText.substring(0, 150)) + (excerptText.length > 150 ? '...' : '') + '</div>' +
    '</div>';

  // 封面图加载失败（404/防盗链/资源不存在）→ 降级为 📝 占位
  const heroCoverImg = hero.querySelector('.bh-cover img');
  if (heroCoverImg) {
    heroCoverImg.addEventListener('error', () => {
      if (post.cover_original && heroCoverImg.dataset.fallback !== "1") {
        heroCoverImg.dataset.fallback = "1";
        heroCoverImg.src = post.cover_original;
        return;
      }
      const cover = heroCoverImg.parentElement;
      if (cover) {
        cover.outerHTML = '<div class="bh-cover no-pic" style="font-size:48px; color:var(--muted)">📝</div>';
      }
    });
  }

  hero.onclick = function(e) {
    if (e.target.tagName === 'A') return;
    openBlogReaderById(post.id);
  };
}

function renderBlogMiniCard(post, container) {
  const grid = container || $("blogCards");
  const dateStr = (post.date || "").substring(0, 16);

  const coverUrl = post.cover || _getCoverUrl(post.body_html || "");

  const card = document.createElement("div");
  card.className = "bmc-card blog-card-mini";
  card.dataset.date = (post.date || "").substring(0, 10);

  let html = '';
  if (coverUrl) {
    html += '<div class="bc-cover"><img src="' + esc(coverUrl) + '" data-orig-src="' + esc(post.cover_original || "") + '" alt="" loading="lazy"></div>';
  } else {
    // 无封面链接：保留原有无封面样式（📝 占位）
    html += '<div class="bc-cover no-pic">📝</div>';
  }

  let excerpt = "";
  if (searchQuery) {
    const fullText = post.excerpt || "";
    const lowerText = fullText.toLowerCase();
    const lowerQuery = searchQuery.toLowerCase();
    let idx = lowerText.indexOf(lowerQuery);
    if (idx !== -1) {
      let snippets = [];
      let lastEnd = 0;
      while (idx !== -1 && snippets.length < 3) {
        let start = Math.max(lastEnd, idx - 25);
        let end = Math.min(fullText.length, idx + lowerQuery.length + 35);
        let snippet = fullText.substring(start, end);
        if (start > lastEnd) snippet = "..." + snippet;
        snippets.push(snippet);
        lastEnd = end;
        idx = lowerText.indexOf(lowerQuery, lastEnd);
      }
      let finalSnippet = snippets.join("");
      if (lastEnd < fullText.length) finalSnippet += "...";
      excerpt = '<div class="bc-excerpt">' + highlightQuery(finalSnippet, searchQuery) + '</div>';
    } else {
      excerpt = '<div class="bc-excerpt"><span style="color:var(--muted)">原文/译文包含关键词</span></div>';
    }
  }

  html += '<div class="bc-info">' +
            '<div class="bc-meta">' + esc(post.author) + ' · ' + esc(dateStr) + '</div>' +
            '<div class="bc-title">' + highlightQuery(post.title || '无题', searchQuery) + '</div>' +
            excerpt +
          '</div>';
    
  card.innerHTML = html;

  // 缩略图加载失败（404/防盗链/资源不存在）→ 降级为 📝 占位
  const coverImg = card.querySelector('.bc-cover img');
  if (coverImg) {
    coverImg.addEventListener('error', () => {
      if (post.cover_original && coverImg.dataset.fallback !== "1") {
        coverImg.dataset.fallback = "1";
        coverImg.src = post.cover_original;
        return;
      }
      const cover = coverImg.parentElement;
      if (cover) {
        cover.outerHTML = '<div class="bc-cover no-pic">📝</div>';
      }
    });
  }

  card.onclick = function(e) {
    if (e.target.tagName === 'A') return;
    openBlogReaderById(post.id);
  };

  grid.appendChild(card);
}

let currentBlogReaderPost = null;
let blogReaderReturnHash = null;
let currentTransMode = "ja-zh";

function getStructuredBlocks(post) {
  if (!post) return null;
  const raw = post.content_json;
  if (!raw || raw === "[]") return null;
  try {
    const blocks = JSON.parse(raw);
    return (Array.isArray(blocks) && blocks.length) ? blocks : null;
  } catch (e) {
    return null;
  }
}

function hasTranslation(post) {
  return getStructuredBlocks(post) !== null;
}

let isFuriganaActive = localStorage.getItem("archive_furigana") === "true";

function renderBlocks(blocks, mode) {
  const parts = [];
  for (const b of blocks) {
    if (b.type === "img") {
      parts.push('<img src="' + esc(b.src || "") + '" referrerpolicy="no-referrer" loading="lazy">');
      continue;
    }
    let jp = b.jp || "";
    if (jp.includes("<ruby>")) {
      jp = sanitizeHtml(jp).replace(/\n/g, "<br>");
    } else {
      jp = esc(jp).replace(/\n/g, "<br>");
    }
    const zh = (b.zh || "").trim();
    const zhHtml = esc(zh).replace(/\n/g, "<br>");
    if (mode === "zh-only") {
      // 中文：有译文显示译文，无译文降级显示原文（严禁丢弃该段）
      parts.push(zh ? '<span>' + zhHtml + '</span>' : '<em>' + jp + '</em>');
    } else {
      // 日中对照：日文斜体 + 中文常规体（zh 空则仅日文）
      parts.push(zh ? '<em>' + jp + '</em><br><span>' + zhHtml + '</span>' : '<em>' + jp + '</em>');
    }
  }
  return parts.join("<br><br>");
}

function updateModeSelectorUI() {
  const selector = $("brModeSelector");
  const delBtn = $("brDeleteTranslate");
  const hasTrans = hasTranslation(currentBlogReaderPost);
  
  if (selector) {
    if (hasTrans) {
      selector.style.display = "inline-flex";
      const btns = selector.querySelectorAll(".brm-btn");
      btns.forEach(btn => {
        btn.classList.toggle("active", btn.dataset.mode === currentTransMode);
      });
    } else {
      selector.style.display = "none";
    }
  }

  if (delBtn) {
    if (window._isArchiveAdmin && hasTrans) {
      delBtn.style.display = "inline-flex";
    } else {
      delBtn.style.display = "none";
    }
  }
  updateFuriganaUI();
}

function updateFuriganaUI() {
  const btn = $("brFuriganaBtn");
  if (!btn) return;
  btn.classList.toggle("active", isFuriganaActive);
  btn.innerHTML = `<span class="btn-icon" style="font-weight:750; font-size:13.5px;">ふ</span><span>${isFuriganaActive ? "已注音" : "注音"}</span>`;
}

async function ensureFuriganaLoaded(post) {
  if (!post || post._furigana_html || post._loading_furigana) return;
  post._loading_furigana = true;
  const btn = $("brFuriganaBtn");
  if (btn && isFuriganaActive) {
    btn.innerHTML = '<span class="btn-icon">⏳</span><span>注音中…</span>';
  }
  try {
    const res = await api("/api/archive/blogs/furigana", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: post.id,
        html: post.body_html,
        title: post.title,
      }),
    });
    if (res && res.ok) {
      post._furigana_html = res.furigana_html;
      post._furigana_title = res.title;
      if (res.furigana_content_json) {
        try {
          post._furigana_blocks = JSON.parse(res.furigana_content_json);
        } catch (e) {}
      }
      if (currentBlogReaderPost && currentBlogReaderPost.id === post.id) {
        renderCurrentBlogContent();
      }
    }
  } catch (e) {
    console.warn("Furigana loading failed:", e);
  } finally {
    post._loading_furigana = false;
    updateFuriganaUI();
  }
}

async function toggleFurigana() {
  if (!currentBlogReaderPost) return;
  isFuriganaActive = !isFuriganaActive;
  localStorage.setItem("archive_furigana", isFuriganaActive ? "true" : "false");
  updateFuriganaUI();
  
  if (isFuriganaActive && !currentBlogReaderPost._furigana_html) {
    await ensureFuriganaLoaded(currentBlogReaderPost);
  } else {
    renderCurrentBlogContent();
  }
}

function renderCurrentBlogContent() {
  if (!currentBlogReaderPost) return;
  const blocks = (isFuriganaActive && currentBlogReaderPost._furigana_blocks)
    ? currentBlogReaderPost._furigana_blocks
    : getStructuredBlocks(currentBlogReaderPost);
  const images = JSON.parse(currentBlogReaderPost.images_json || "[]");
  const paths = JSON.parse(currentBlogReaderPost.image_paths_json || "[]");

  let bodyHtml = "";
  if (blocks && currentTransMode !== "ja-only") {
    // 中文 / 日中对照：从解耦的结构化数据渲染（日中对照按 jp/zh 插值）
    bodyHtml = _replaceImgUrls(renderBlocks(blocks, currentTransMode), images, paths);
  } else {
    // 日文（或暂无结构化译文）：直接渲染原始日文 body_html，经过 DOM 净化确保安全
    const rawJa = (isFuriganaActive && currentBlogReaderPost._furigana_html)
      ? currentBlogReaderPost._furigana_html
      : (currentBlogReaderPost.body_html || "");
    bodyHtml = sanitizeHtml(_replaceImgUrls(rawJa, images, paths));
  }

  // 翻译模型标记：仅在「日中对照/中文」视图且存在译文时展示，右对齐次级灰字
  const modelName = currentBlogReaderPost.translation_model || "";
  const showModel = blocks && currentTransMode !== "ja-only" && modelName;
  const modelTag = showModel
    ? '<div class="br-model-tag">翻译模型：' + esc(modelName) + '</div>'
    : '';

  const displayTitle = (isFuriganaActive && currentBlogReaderPost._furigana_title)
    ? currentBlogReaderPost._furigana_title
    : esc(currentBlogReaderPost.title || "无题");

  $("brContent").innerHTML =
    '<div class="br-meta">' +
      '<div><span class="br-author">' + esc(currentBlogReaderPost.author) + '</span><span style="margin-left:12px">' + esc((currentBlogReaderPost.date || "").substring(0, 16)) + '</span></div>' +
      '<a class="br-link" href="' + esc(currentBlogReaderPost.url) + '" target="_blank">阅读原文 ↗</a>' +
    '</div>' +
    modelTag +
    '<h1 style="margin-top:0; font-size:24px;">' + displayTitle + '</h1>' +
    bodyHtml;

  // 博客正文图片支持点击灯箱放大预览、加载失败自动重试与兜底
  const brImgs = $("brContent").querySelectorAll("img");
  const blogImages = Array.from(brImgs).map(img => ({ url: img.src, caption: currentBlogReaderPost.title || "" }));
  brImgs.forEach((img, idx) => {
    img.style.cursor = "zoom-in";
    img.onerror = () => handleImgError(img);
    if (img.complete && img.naturalWidth === 0) {
      handleImgError(img);
    }
    img.onclick = () => {
      images = blogImages;
      openLightbox(idx, img);
    };
  });

  updateModeSelectorUI();
}

function openBlogReader(post, bodyHtml, returnHash) {
  const readerWasHidden = $("blogReader").style.display === "none";
  if (returnHash !== undefined) {
    blogReaderReturnHash = returnHash || "";
  } else if (readerWasHidden) {
    // 记录打开来源：首页博客卡片应回到首页，列表卡片应回到原筛选/分页。
    blogReaderReturnHash = location.hash ? location.hash.slice(1) : "";
  }
  currentBlogReaderPost = post;
  // 进入博客时，若已有译文则默认选中「日中对照」
  if (hasTranslation(post)) {
    currentTransMode = "ja-zh";
  }
  $("brTitle").textContent = post.title || "无题";
  const authorBadge = $("brAuthorBadge");
  if (authorBadge) {
    const gKey = post.group_key || curBlogGroup || "nogizaka";
    const gIcon = gKey === "sakurazaka" ? "🌸" : gKey === "hinatazaka" ? "🩵" : "💜";
    const gClass = gKey === "sakurazaka" ? "sakura" : gKey === "hinatazaka" ? "hinata" : "nogi";
    authorBadge.className = "portal-pill-brand " + gClass;
    authorBadge.textContent = gIcon + " " + (post.author || "成员博客");
    authorBadge.style.display = "";
  }
  
  const transBtn = $("brTranslate");
  if (transBtn) {
    if (!window._isArchiveAdmin) {
      transBtn.style.display = "none";
    } else {
      transBtn.style.display = "inline-flex";
      if (hasTranslation(post) && post.translation_status !== "partial") {
        transBtn.innerHTML = '<span class="btn-icon">✓</span><span>已翻译</span>';
        transBtn.disabled = true;
      } else if (hasTranslation(post)) {
        transBtn.innerHTML = '<span class="btn-icon">↻</span><span>部分翻译，重试</span>';
        transBtn.disabled = false;
      } else {
        transBtn.innerHTML = '<span class="btn-icon">🌐</span><span>翻译</span>';
        transBtn.disabled = false;
      }
    }
  }

  renderCurrentBlogContent();
  if (isFuriganaActive && !post._furigana_html) {
    ensureFuriganaLoaded(post);
  }
  $("blogReader").style.display = "";
  $("blogReader").scrollTop = 0;
  
  document.documentElement.classList.add("modal-open");
  document.body.classList.add("modal-open");
  document.body.style.overflow = "hidden";
  if (typeof handleBackTopScroll === "function") handleBackTopScroll();

  // 同步 URL Hash 路由，便于直接分享定位单篇博客
  const p = new URLSearchParams();
  p.set("blog", post.group_key || curBlogGroup || "nogizaka");
  if (post.author) p.set("author", post.author);
  p.set("id", post.id);
  writeArchiveHash(p.toString());

  if (searchQuery) {
    setTimeout(() => {
      const contentDiv = $("brContent");
      if (!contentDiv) return;
      const walker = document.createTreeWalker(contentDiv, NodeFilter.SHOW_TEXT, null, false);
      let node;
      let found = false;
      const lowerQuery = searchQuery.toLowerCase();
      while ((node = walker.nextNode())) {
        if (node.nodeValue.toLowerCase().includes(lowerQuery)) {
          const span = document.createElement("mark");
          span.className = "br-search-target";
          span.style.background = "var(--accent-soft)";
          span.style.color = "var(--accent)";
          
          const idx = node.nodeValue.toLowerCase().indexOf(lowerQuery);
          const before = node.nodeValue.substring(0, idx);
          const match = node.nodeValue.substring(idx, idx + searchQuery.length);
          const after = node.nodeValue.substring(idx + searchQuery.length);
          
          span.textContent = match;
          const parent = node.parentNode;
          
          const beforeNode = document.createTextNode(before);
          const afterNode = document.createTextNode(after);
          
          parent.insertBefore(beforeNode, node);
          parent.insertBefore(span, node);
          parent.insertBefore(afterNode, node);
          parent.removeChild(node);
          
          if (!found) {
            setTimeout(() => {
              const reader = $("blogReader");
              const topPos = span.getBoundingClientRect().top + reader.scrollTop - (window.innerHeight / 2);
              reader.scrollTo({ top: Math.max(0, topPos), behavior: "smooth" });
            }, 100);
            found = true;
          }
          
          walker.currentNode = afterNode;
        }
      }
    }, 100);
  }
}

function closeBlogReader() {
  $("blogReader").style.display = "none";
  document.documentElement.classList.remove("modal-open");
  document.body.classList.remove("modal-open");
  document.body.style.overflow = "";
  $("brContent").innerHTML = "";
  currentBlogReaderPost = null;
  if (typeof handleBackTopScroll === "function") handleBackTopScroll();

  // 恢复打开前的路由：首页卡片关闭后必须回到首页，而不是博客列表。
  const returnHash = blogReaderReturnHash !== null
    ? blogReaderReturnHash
    : buildBlogHash({ pageNum: page });
  blogReaderReturnHash = null;
  writeArchiveHash(returnHash);
  // writeArchiveHash 会抑制同一轮 hashchange；主动分发一次，确保视觉状态与 URL 一致。
  setTimeout(() => handleRoute(false), 0);
}

const brCloseBtn = $("brClose");
if (brCloseBtn) {
  brCloseBtn.addEventListener("click", closeBlogReader);
}

const brShareBtn = $("brShare");
if (brShareBtn) {
  brShareBtn.addEventListener("click", () => {
    if (!currentBlogReaderPost) return;
    const p = new URLSearchParams();
    p.set("blog", currentBlogReaderPost.group_key || curBlogGroup || "nogizaka");
    if (currentBlogReaderPost.author) p.set("author", currentBlogReaderPost.author);
    p.set("id", currentBlogReaderPost.id);
    const url = location.origin + location.pathname + "#" + p.toString();
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(() => {
        showToast("已复制博客分享链接！", "success");
      }).catch(() => {
        customPrompt({ title: "博客分享链接", message: "请复制下方链接直接分享：", defaultValue: url, confirmText: "完成", icon: "🔗" });
      });
    } else {
      customPrompt({ title: "博客分享链接", message: "请复制下方链接直接分享：", defaultValue: url, confirmText: "完成", icon: "🔗" });
    }
  });
}

const brFuriganaBtn = $("brFuriganaBtn");
if (brFuriganaBtn) {
  brFuriganaBtn.addEventListener("click", toggleFurigana);
}

// 绑定全局 Esc 键退出博客阅读器
window.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("blogReader") && $("blogReader").style.display !== "none") {
    const lb = $("lightbox");
    if (!lb || lb.getAttribute("aria-hidden") === "true") {
      closeBlogReader();
    }
  }
});

const brModeSelector = $("brModeSelector");
if (brModeSelector) {
  brModeSelector.addEventListener("click", (e) => {
    const btn = e.target.closest(".brm-btn");
    if (!btn || !btn.dataset.mode) return;
    currentTransMode = btn.dataset.mode;
    renderCurrentBlogContent();
  });
}

function customConfirm({ title = "确认操作", message = "确定继续吗？", confirmText = "确认删除", icon = "🗑️" } = {}) {
  return new Promise((resolve) => {
    const modal = $("customConfirmModal");
    if (!modal) {
      resolve(confirm(message));
      return;
    }
    $("cmTitle").textContent = title;
    $("cmMessage").textContent = message;
    $("cmConfirm").textContent = confirmText;
    modal.querySelector(".cm-icon").textContent = icon;
    
    modal.style.display = "flex";

    const onConfirm = () => {
      cleanup();
      resolve(true);
    };
    const onCancel = () => {
      cleanup();
      resolve(false);
    };
    const cleanup = () => {
      modal.style.display = "none";
      $("cmConfirm").removeEventListener("click", onConfirm);
      $("cmCancel").removeEventListener("click", onCancel);
    };

    $("cmConfirm").addEventListener("click", onConfirm);
    $("cmCancel").addEventListener("click", onCancel);
  });
}

function showToast(msg, type = "info") {
  let container = $("toastContainer");
  if (!container) return;
  const toast = document.createElement("div");
  const icon = type === "success" ? "✅" : type === "error" ? "❌" : "ℹ️";
  toast.className = `custom-toast ${type}`;
  toast.innerHTML = `<span>${icon}</span><span>${esc(msg)}</span>`;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateY(-10px)";
    setTimeout(() => toast.remove(), 300);
  }, 2500);
}

const brDeleteTranslateBtn = $("brDeleteTranslate");
if (brDeleteTranslateBtn) {
  brDeleteTranslateBtn.addEventListener("click", async () => {
    if (!currentBlogReaderPost || !hasTranslation(currentBlogReaderPost)) return;

    const ok = await customConfirm({
      title: "清除翻译确认",
      message: "确认要删除该博客的 Gemini 翻译结果并恢复为原始状态吗？",
      confirmText: "确认删除",
      icon: "🗑️"
    });
    if (!ok) return;

    try {
      const res = await fetch("/api/archive/blogs/delete_translation", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: currentBlogReaderPost.id })
      });
      const data = await res.json();
      if (data.ok) {
        currentBlogReaderPost.translation = null;
        currentBlogReaderPost.content_json = null;
        const transBtn = $("brTranslate");
        if (transBtn) {
          transBtn.innerHTML = '<span class="btn-icon">🌐</span><span>翻译</span>';
          transBtn.disabled = false;
        }
        renderCurrentBlogContent();
        showToast("已成功删除翻译", "success");
      } else {
        showToast(data.msg || "删除失败", "error");
      }
    } catch(err) {
      showToast("网络异常: " + err, "error");
    }
  });
}

const brTranslateBtn = $("brTranslate");
if (brTranslateBtn) {
  brTranslateBtn.addEventListener("click", async () => {
    if (!currentBlogReaderPost || brTranslateBtn.disabled) return;
    
    const targetPost = currentBlogReaderPost;
    const reqBlogId = targetPost.id;
    
    brTranslateBtn.innerHTML = '<span class="btn-icon">⏳</span><span>翻译中（可能需要几分钟）...</span>';
    brTranslateBtn.disabled = true;
    
    try {
      const res = await fetch("/api/archive/blogs/translate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: reqBlogId })
      });
      const data = await res.json();
      if (data.ok && data.html) {
        targetPost.translation = data.html;
        if (data.content_json) targetPost.content_json = data.content_json;
        if (data.translation_model) targetPost.translation_model = data.translation_model;
        if (data.translation_status) targetPost.translation_status = data.translation_status;
        
        // 若当前仍在该博客阅读器界面，立即渲染并切换为日中对照
        if (currentBlogReaderPost && currentBlogReaderPost.id === reqBlogId && $("blogReader").style.display !== "none") {
          currentTransMode = "ja-zh";
          renderCurrentBlogContent();
          if (data.translation_status === "partial" || data.translation_complete === false) {
            brTranslateBtn.innerHTML = '<span class="btn-icon">↻</span><span>部分翻译，重试</span>';
            brTranslateBtn.disabled = false;
            showToast("部分段落翻译成功，可再次点击补齐", "info");
          } else {
            brTranslateBtn.innerHTML = '<span class="btn-icon">✓</span><span>已翻译</span>';
            brTranslateBtn.disabled = true;
          }
        }
      } else {
        const traceHint = data.request_id ? `（请求 ${data.request_id}）` : "";
        showToast((data.msg || "翻译失败，请检查 API Key 配置与网络连接") + traceHint, "error");
        if (currentBlogReaderPost && currentBlogReaderPost.id === reqBlogId) {
          brTranslateBtn.innerHTML = '<span class="btn-icon">🌐</span><span>重试翻译</span>';
          brTranslateBtn.disabled = false;
        }
      }
    } catch(err) {
      showToast("网络异常: " + err, "error");
      if (currentBlogReaderPost && currentBlogReaderPost.id === reqBlogId) {
        brTranslateBtn.innerHTML = '<span class="btn-icon">🌐</span><span>重试翻译</span>';
        brTranslateBtn.disabled = false;
      }
    }
  });
}

function _getCoverUrl(html) {
  if (!html) return "";
  const match = html.match(/<img[^>]+src=(?:"([^"]+)"|'([^']+)'|([^\s>]+))/i);
  if (match) return match[1] || match[2] || match[3] || "";
  return "";
}

function _replaceImgUrls(html, images, paths) {
  if (!html || !images || !images.length) return html || "";
  let result = html;
  for (let i = 0; i < images.length; i++) {
    const orig = images[i];
    if (!orig) continue;
    let localPath = (paths && paths[i]) ? paths[i] : "";
    if (localPath) {
      localPath = localPath.replace(/\\/g, '/');
    }
    const encodedPath = localPath ? localPath.split('/').map(encodeURIComponent).join('/') : "";
    const local = encodedPath ? "/api/archive/blog_media/" + encodedPath : orig;
    
    // 1. 若存在本地缓存路径，将完整的原图绝对 URL 替换为本地 API 路径
    if (local !== orig) {
      result = result.split(orig).join(local);
      try { result = result.split(esc(orig)).join(local); } catch(e) {}
    }

    // 2. 乃木坂/樱坂的原始 body_html 含相对路径（如 src="/files/46/..."），安全替换
    try {
      const u = new URL(orig, "https://dummy.com");
      const relPath = u.pathname + u.search;
      if (relPath && relPath !== orig && relPath !== '/') {
        const target = (local !== orig) ? local : orig;
        const origAttr = ' data-orig-src="' + esc(orig) + '"';
        // 使用正则限定在 src="..." 或 src='...' 中精准替换相对路径，避免匹配到已带有域名的完整 URL
        const safeRel = relPath.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const relRe = new RegExp('((?:src|href)=["\'])' + safeRel + '(["\'])', 'gi');
        result = result.replace(relRe, '$1' + target + '$2' + origAttr);
        const safeEscRel = esc(relPath).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const relEscRe = new RegExp('((?:src|href)=["\'])' + safeEscRel + '(["\'])', 'gi');
        result = result.replace(relEscRe, '$1' + target + '$2' + origAttr);
      }
    } catch(e) {}
  }
  return result;
}

// Removed old renderBlogBubble

async function selectMember(name, keepHash) {
  const version = ++memberVersion;
  curMode = "msg";
  ++blogSelectionVersion;
  if (blogPageAbort) blogPageAbort.abort();
  _enterMemberMode();
  curMember = name;
  try { localStorage.setItem("archive_last_msg_member", name); } catch (_) {}
  curBlogGroup = "";     // 切换到成员模式，清空博客分组
  syncChipHighlight();  // 同步 chip 高亮
  if (!keepHash) searchQuery = "";
  syncSearchInput();
  let data;
  try {
    data = await api("/api/archive/months?member=" + encodeURIComponent(name));
  } catch (e) {
    if (e.name === "AbortError" || version !== memberVersion) return;
    months = [];
    $("monthSelect").innerHTML = "";
    $("stats").textContent = "";
    resetContent();
    $("emptyHint").textContent = "成员「" + name + "」不可用：" + e.message + "。请重新选择成员。";
    $("emptyHint").hidden = false;
    return;
  }
  if (version !== memberVersion) return;
  months = data.ok ? data.months : [];
  const sel = $("monthSelect");
  sel.innerHTML = "";
  for (const m of months) {
    const opt = document.createElement("option");
    opt.value = m.year + "-" + m.month;
    opt.textContent = m.year + " 年 " + m.month + " 月（" + m.count + "）";
    sel.appendChild(opt);
  }
  if (!months.length) {
    resetContent();
    $("stats").textContent = "";
    $("emptyHint").textContent = "成员「" + name + "」还没有归档内容。请从成员列表重新选择。";
    $("emptyHint").hidden = false;
    return;
  }
  loadCalendar();   // 后台拉全档按天计数，不阻塞时间线
  const wanted = keepHash ? readHashYM() : null;
  const pick = (wanted && months.find((m) => m.year === wanted.year && m.month === wanted.month)) || months[0];
  await selectMonth(pick.year, pick.month);
  if (searchQuery) startSearch(searchQuery, false);
}

function readHashYM() {
  const p = new URLSearchParams(location.hash.slice(1));
  const y = parseInt(p.get("y"), 10), m = parseInt(p.get("m"), 10);
  return (y && m) ? { year: y, month: m } : null;
}

let selfHashUpdate = false;   // 区分"自己写的 hash"和"用户粘贴/前进后退"

function syncHash() {
  if (!curYM) return;
  const p = new URLSearchParams({ member: curMember, y: curYM.year, m: curYM.month });
  if (curType) p.set("t", curType);
  if (searchQuery) p.set("q", searchQuery);
  // asc 不是默认值时写入路由，分享链接能恢复用户的阅读顺序；desc 保持旧链接简洁。
  if (messageOrder !== MESSAGE_ORDER_DEFAULT) p.set("order", messageOrder);
  writeArchiveHash(p.toString());
}


async function selectMonth(year, month) {
  curYM = { year, month };
  calYM = { year, month };
  renderCalendar();
  $("monthSelect").value = year + "-" + month;
  const idx = months.findIndex((m) => m.year === year && m.month === month);
  $("prevMonth").disabled = idx >= months.length - 1;
  $("nextMonth").disabled = idx <= 0;
  resetContent();
  if (!curBlogGroup) syncHash();
  await loadPage();
}

async function loadPage() {
  if (curMode === "blog") return;
  const version = contentVersion;
  contentAbort = new AbortController();
  setPageLoading(true);
  const url = searchQuery
    ? "/api/archive/search?member=" + encodeURIComponent(curMember) +
      "&q=" + encodeURIComponent(searchQuery) +
      "&type=" + curType + "&order=" + messageOrder + "&page=" + page + "&per_page=50"
    : "/api/archive/messages?member=" + encodeURIComponent(curMember) +
      "&year=" + curYM.year + "&month=" + curYM.month +
      "&type=" + curType + "&order=" + messageOrder + "&page=" + page + "&per_page=50";
  let data;
  try {
    data = await api(url, { signal: contentAbort.signal });
  } catch (e) {
    if (e.name !== "AbortError" && version === contentVersion) showEmpty("加载失败：" + e.message);
    return;
  } finally {
    if (version === contentVersion) setPageLoading(false);
  }
  if (version !== contentVersion) return;
  if (!data.ok) { showEmpty("加载失败：" + (data.errors || []).join("；")); return; }
  totalPages = data.total_pages;
  if (searchQuery) {
    $("stats").textContent = "搜索「" + searchQuery + "」· " + data.total + " 条" +
      " · 全历史 · " + messageOrderLabel() +
      (data.capped ? "（已达上限，仅显示" + (messageOrder === "asc" ? "最早" : "最新") + " 500 条）" : "");
    if (!data.messages.length && page === 1) showEmpty("没有匹配「" + searchQuery + "」的消息");
  } else {
    $("stats").textContent = curYM.year + "/" + curYM.month + " · " + data.total + " 条 · " + messageOrderLabel();
    if (!data.messages.length && page === 1) {
      showEmpty("本月没有" + (curType ? "该类型的" : "") + "消息");
    }
  }
  for (const msg of data.messages) renderBubble(msg);
  $("loadMore").hidden = page >= totalPages;

  // 首页跳转：滚动到目标消息（跨页查找）
  if (targetMsgId) {
    const tid = targetMsgId;
    const findAndScroll = () => {
      const target = document.querySelector('.bubble[data-msg-id="' + tid + '"]');
      if (target) {
        // content-visibility:auto 阻止了屏外元素的布局计算，先强制渲染
        target.style.contentVisibility = "visible";
        void target.offsetHeight;
        // 手动计算居中位置：元素顶部 - 视口一半 + 元素一半 = 居中
        const rect = target.getBoundingClientRect();
        const top = rect.top + window.scrollY - (window.innerHeight / 2) + (rect.height / 2);
        window.scrollTo({ top: Math.max(0, top), behavior: "instant" });
        // 高亮动画
        target.style.boxShadow = "0 0 0 4px var(--accent), 0 0 20px var(--accent-ring)";
        target.style.borderRadius = "16px";
        target.style.transition = "box-shadow 0.3s ease-out";
        setTimeout(() => { target.style.boxShadow = ""; }, 2500);
        return true;
      }
      return false;
    };
    // 渲染完成后稍等一下再查找（content-visibility 延迟渲染）
    setTimeout(async () => {
      if (findAndScroll()) { targetMsgId = ""; return; }
      // 没找到，继续加载后续页
      while (page < totalPages) {
        page++;
        await loadPage();
        if (findAndScroll()) { targetMsgId = ""; return; }
      }
      targetMsgId = "";
    }, 400);
  }
}

function startSearch(q, updateHash = true) {
  searchQuery = normalizedQuery(q);
  syncSearchInput();
  resetContent();
  if (curMode === "blog") {
    loadBlogPage(1, updateHash);
    return;
  }
  if (updateHash) syncHash();
  if (!searchQuery) {
    if (curYM) { selectMonth(curYM.year, curYM.month); return; }
    return;
  }
  loadPage();
}
let searchDebounceTimer = null;
$("searchBox").addEventListener("input", () => {
  const val = $("searchBox").value.trim();
  $("searchClear").hidden = !val;
  clearTimeout(searchDebounceTimer);
  searchDebounceTimer = setTimeout(() => {
    const q = normalizedQuery($("searchBox").value);
    if (q !== searchQuery) {
      if (q) startSearch(q);
      else if (searchQuery) clearSearch();
    }
  }, 350);
});
$("searchBox").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && $("searchBox").value.trim()) {
    clearTimeout(searchDebounceTimer);
    startSearch($("searchBox").value.trim());
  }
  if (e.key === "Escape") { 
    clearTimeout(searchDebounceTimer);
    $("searchBox").value = ""; 
    if (searchQuery) clearSearch(); 
  }
});
$("searchSubmit").addEventListener("click", () => {
  clearTimeout(searchDebounceTimer);
  const q = normalizedQuery($("searchBox").value);
  if (q) startSearch(q);
  else $("searchBox").focus();
});
function clearSearch() {
  searchQuery = "";
  syncSearchInput();
  if (curMode === "blog") { loadBlogPage(1, true); return; }
  if (curYM) selectMonth(curYM.year, curYM.month);
}
$("searchClear").addEventListener("click", clearSearch);

// ── 标签开关 ─────────────────────────────────────
let showTags = localStorage.getItem("archiveShowTags") !== "false";
$("tagToggle").checked = showTags;
if (!showTags) document.body.classList.add("hide-tags");
$("tagToggle").addEventListener("change", () => {
  showTags = $("tagToggle").checked;
  localStorage.setItem("archiveShowTags", showTags ? "true" : "false");
  document.body.classList.toggle("hide-tags", !showTags);
});

function showEmpty(text) {
  $("emptyHint").textContent = text;
  $("emptyHint").hidden = false;
  $("loadMore").hidden = true;
  $("stats").textContent = "";
}

// ── 渲染 ─────────────────────────────────────────
function renderBubble(msg) {
  const tl = $("timeline");
  const day = fmtDay(msg.published_at);
  if (day !== lastDay) {
    lastDay = day;
    const sep = document.createElement("div");
    sep.className = "day-sep";
    sep.dataset.date = fmtDateKey(msg.published_at);   // 日历跳转定位锚点
    sep.innerHTML = "<span>" + esc(day) + "</span>";
    tl.appendChild(sep);
  }
  const b = document.createElement("div");
  b.className = "bubble virtual-card";
  if (msg.id) b.dataset.msgId = String(msg.id);

  const pubTimeStr = fmtTime(msg.published_at);
  const curGroup = (msg.group || getCurGroup() || "").toLowerCase();

  let uploadBadgeHtml = "";
  if (msg.upload_at) {
    const uDt = new Date(msg.upload_at);
    const pDt = new Date(msg.published_at);
    const diffSec = Math.max(0, Math.round((pDt.getTime() - uDt.getTime()) / 1000));
    const uFormatted = fmtUploadTime(msg.upload_at, msg.published_at);
    const durStr = fmtDelayDuration(diffSec);

    const pubJst = toJst(msg.published_at);
    const pSec = pubJst.getUTCSeconds();
    const pMin = pubJst.getUTCMinutes();
    const isRoundTime = (pMin === 0 || pMin === 30);

    const isHinata = curGroup.includes("hinata") || (!curGroup && /^(金村|大野|佐藤|片山|坂井|下田|山下|大田|正源司|藤嶌|渡辺|小坂|加藤|齐藤|佐佐木|東村|松田好|河田|丹生|濱岸|富田|高本|高瀬|上村ひ|高橋|森本|山口|平尾|平岡|竹内|岸|小西|清水理|宮地|石塚)/.test((curMember||"").replace(/[\s_　]/g, "")));
    const isSakura = curGroup.includes("sakura") || (!curGroup && /^(石森|小池|小林|田村保|森田|藤吉|山崎|谷口|中川|山田|浅井|的野|上村莉|齋藤冬|菅井|土生|守屋|渡邉理|渡辺梨|井上梨|遠藤光|大園|大沼|幸阪|武元|増本|松田里|村井|村山|山下瞳|小島|向井)/.test((curMember||"").replace(/[\s_　]/g, "")));
    const isNogi = curGroup.includes("nogi") || (!curGroup && /^(冨里|賀喜|一ノ瀬|井上和|川崎|五百城|中西|池田|奥田|菅原|小川|秋元|生田|生驹|伊藤|岩本|梅澤|遠藤さ|久保|齋藤飛|阪口|佐藤楓|柴田|白石|新内|鈴木|高山|田村真|筒井|西野|桥本|樋口|星野|松村|向井葉|山下美|弓木|与田|川端|小津)/.test((curMember||"").replace(/[\s_　]/g, "")));

    let isCronSec = false;
    let cronName = "";
    if (isHinata) {
      isCronSec = (pSec === 37);
      cronName = "日向坂:37s";
    } else if (isSakura) {
      isCronSec = (pSec === 9 || pSec === 28);
      cronName = "樱坂:" + String(pSec).padStart(2, "0") + "s";
    } else if (isNogi) {
      isCronSec = (pSec === 45 || pSec === 7);
      cronName = "乃木坂:" + String(pSec).padStart(2, "0") + "s";
    } else {
      isCronSec = (pSec === 37 || pSec === 9 || pSec === 45 || pSec === 7 || pSec === 28);
      cronName = ":" + String(pSec).padStart(2, "0") + "s";
    }

    const isMultiDay = diffSec >= 86400; // 跨天超24小时绝对存货
    const isCronHit = isCronSec && diffSec >= 900; // 命中本团定时管道且等待超15分钟
    const isRoundHit = isRoundTime && (pSec === 0 || pSec === 1 || isCronSec) && diffSec >= 300; // 整点/半点投放

    const isConfirmedScheduled = isMultiDay || isCronHit || isRoundHit;
    const isDelayedReview = !isConfirmedScheduled && diffSec >= 3600; // 1小时~24小时非定时秒数放行 (STAFF审核较长)

    if (isConfirmedScheduled) {
      let reason = isMultiDay ? "跨天提前备货" : (isRoundHit ? "整点/半点 定时投放" : ("命中 " + cronName + " 定时管道"));
      const tooltip = "⏰ 预设定时消息 (" + reason + ")\n" +
        "📸 成员拍摄/上传 (JST): " + fmtCopyTime(msg.upload_at) + "\n" +
        "📢 官方定时发布 (JST): " + fmtCopyTime(msg.published_at) + "\n" +
        "⏱️ 预设等待时长: " + durStr;

      uploadBadgeHtml = '<span class="upload-badge is-scheduled" title="' + esc(tooltip) + '">' +
        '<span class="ub-icon">⏰ 预设定时</span> ' +
        '<span class="ub-time">' + esc(uFormatted) + '</span> ' +
        '<span class="ub-delay">(+' + esc(durStr) + ')</span>' +
        '</span>';
    } else if (isDelayedReview) {
      const tooltip = "⏳ 审核流转耗时较长 (非固定定时管道秒数)\n" +
        "📸 成员拍摄/上传 (JST): " + fmtCopyTime(msg.upload_at) + "\n" +
        "📢 STF审核放行 (JST): " + fmtCopyTime(msg.published_at) + "\n" +
        "⏱️ 审核流转耗时: " + durStr + "\n" +
        "💡 说明: 发布秒数未命中固定定时管道，可能为 STAFF 会议/集中审批或高峰排队放行";

      uploadBadgeHtml = '<span class="upload-badge is-delayed" title="' + esc(tooltip) + '">' +
        '<span class="ub-icon">⏳ 审核放行</span> ' +
        '<span class="ub-time">' + esc(uFormatted) + '</span> ' +
        '<span class="ub-delay">(+' + esc(durStr) + ')</span>' +
        '</span>';
    } else {
      const tooltip = "📤 正常即拍即发 (常规审核流转)\n" +
        "📸 成员真实上传/拍摄于 (JST): " + fmtCopyTime(msg.upload_at) + "\n" +
        "📢 STF审核发布 (JST): " + fmtCopyTime(msg.published_at) + "\n" +
        "⏱️ 审核流转耗时: " + durStr;

      uploadBadgeHtml = '<span class="upload-badge" title="' + esc(tooltip) + '">' +
        '<span class="ub-icon">📤 真实上传</span> ' +
        '<span class="ub-time">' + esc(uFormatted) + '</span> ' +
        '<span class="ub-delay">(+' + esc(durStr) + ')</span>' +
        '</span>';
    }
  } else {
    // 纯文本消息：严格根据【当前成员所属坂道】的专属定时管道与整点特征智能推断
    const pubJst = toJst(msg.published_at);
    const pSec = pubJst.getUTCSeconds();
    const pMin = pubJst.getUTCMinutes();
    const isRoundTime = (pMin === 0 || pMin === 30);

    const isHinata = curGroup.includes("hinata") || (!curGroup && /^(金村|大野|佐藤|片山|坂井|下田|山下|大田|正源司|藤嶌|渡辺|小坂|加藤|齐藤|佐佐木|東村|松田好|河田|丹生|濱岸|富田|高本|高瀬|上村ひ|高桥|森本|山口|平尾|平岡|竹内|岸|小西|清水理|宮地|石塚)/.test((curMember||"").replace(/[\s_　]/g, "")));
    const isSakura = curGroup.includes("sakura") || (!curGroup && /^(石森|小池|小林|田村保|森田|藤吉|山崎|谷口|中川|山田|浅井|的野|上村莉|齋藤冬|菅井|土生|守屋|渡邉理|渡辺梨|井上梨|遠藤光|大園|大沼|幸阪|武元|増本|松田里|村井|村山|山下瞳|小島|向井)/.test((curMember||"").replace(/[\s_　]/g, "")));
    const isNogi = curGroup.includes("nogi") || (!curGroup && /^(冨里|賀喜|一ノ瀬|井上和|川崎|五百城|中西|池田|奥田|菅原|小川|秋元|生田|生驹|伊藤|岩本|梅澤|遠藤さ|久保|齋藤飛|阪口|佐藤楓|柴田|白石|新内|鈴木|高山|田村真|筒井|西野|桥本|樋口|星野|松村|向井葉|山下美|弓木|与田|川端|小津)/.test((curMember||"").replace(/[\s_　]/g, "")));

    let isMatch = false;
    let pipeDesc = "";

    if (isHinata) {
      if (pSec === 37) {
        isMatch = true;
        pipeDesc = "日向坂:37s 管道";
      } else if (isRoundTime && (pSec === 0 || pSec === 1)) {
        isMatch = true;
        pipeDesc = "整点/半点 投放";
      }
    } else if (isSakura) {
      if (pSec === 9 || pSec === 28) {
        isMatch = true;
        pipeDesc = "樱坂:" + String(pSec).padStart(2, "0") + "s 管道";
      } else if (isRoundTime && (pSec === 0 || pSec === 1)) {
        isMatch = true;
        pipeDesc = "整点/半点 投放";
      }
    } else if (isNogi) {
      if (pSec === 45 || pSec === 7) {
        isMatch = true;
        pipeDesc = "乃木坂:" + String(pSec).padStart(2, "0") + "s 管道";
      } else if (isRoundTime && (pSec === 0 || pSec === 1)) {
        isMatch = true;
        pipeDesc = "整点/半点 投放";
      }
    } else {
      if (isRoundTime && (pSec === 0 || pSec === 1)) {
        isMatch = true;
        pipeDesc = "整点/半点 投放";
      }
    }

    if (isMatch) {
      const tooltip = "🤖 疑似预设定时消息\n特征：命中 " + pipeDesc + " (JST " + pubTimeStr + ")\n说明：纯文本消息无媒体上传时间戳，根据所属坂道官方分发管道特征推断";

      uploadBadgeHtml = '<span class="upload-badge is-inferred" title="' + esc(tooltip) + '">' +
        '<span class="ub-icon">⏰ 疑似定时</span> ' +
        '<span class="ub-delay">(' + esc(pipeDesc.split(" ")[0]) + ')</span>' +
        '</span>';
    }
  }

  const hasText = Boolean((msg.text && msg.text.trim()) || (msg.translation && msg.translation.trim()));
  let jumpHtml = "";
  if (searchQuery && msg.year) {
    const dateKey = fmtDateKey(msg.published_at);
    jumpHtml = '<a href="#" class="jump" data-date="' + dateKey + '" style="color:var(--accent); text-decoration:none; font-size:12px;">查看当日 →</a>';
  }
  let copyHtml = hasText ? '<button type="button" class="copy-btn" title="复制整条消息与译文">📋 复制</button>' : '';

  let html = '<div class="msg-header">' +
    '<div class="msg-meta-left">' +
      '<span class="pub-time" title="官方审核发布时间 (JST): ' + fmtCopyTime(msg.published_at) + '">' + pubTimeStr + '</span>' +
      '<span class="msg-type-pill type-' + esc(msg.type) + '">' + esc(msg.type) + '</span>' +
      uploadBadgeHtml +
    '</div>' +
    '<div class="msg-meta-right">' +
      jumpHtml +
      copyHtml +
    '</div>' +
  '</div>';



  if (msg.media_url) {
    const url = mediaUrl(msg.media_url);
    const dim = (msg.w && msg.h) ? ' width="' + msg.w + '" height="' + msg.h + '"' : "";
    if (msg.type === "video") {
      html += '<video controls preload="metadata" src="' + esc(url) + '"></video>';
    } else if (msg.type === "voice") {
      html += '<audio controls preload="metadata" src="' + esc(url) + '"></audio>';
    } else {
      images.push({ url: url, caption: (msg.text || "").slice(0, 80) });
      html += '<img loading="lazy"' + dim + ' data-lb="' + (images.length - 1) + '" src="' + esc(url) + '" alt="">';
    }
  } else if (msg.download_failed) {
    html += '<div class="miss">⚠️ 媒体文件下载失败 ' +
      (window._isArchiveAdmin ? '<button type="button" class="btn small retry-dl-btn" style="margin-left:8px; padding:2px 8px; font-size:12px; vertical-align:middle;">🔄 重试下载</button>' : '（可用回填工具重试）') +
      '</div>';
  }
  if (msg.text) html += '<div class="text">' + formatMessageText(msg.text, searchQuery) + "</div>";
  if (msg.translation) html += '<div class="trans">' + formatMessageText(msg.translation, searchQuery) + "</div>";
  b.innerHTML = html;

  const retryBtn = b.querySelector(".retry-dl-btn");
  if (retryBtn) {
    retryBtn.addEventListener("click", async () => {
      retryBtn.disabled = true;
      retryBtn.textContent = "⏳ 下载中…";
      const msgId = msg.id;
      const msgYear = msg.year || curYM.year;
      const msgMonth = msg.month || curYM.month;
      try {
        const resp = await fetch("/api/archive/retry_download?member=" + encodeURIComponent(curMember), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: msgId, year: msgYear, month: msgMonth }),
        });
        const data = await resp.json();
        if (data.ok && data.media_url) {
          showToast("🎉 媒体文件下载成功！", "ok");
          msg.download_failed = false;
          msg.media_url = data.media_url;
          const missDiv = b.querySelector(".miss");
          if (missDiv) {
            const url = mediaUrl(data.media_url);
            let mediaEl;
            if (msg.type === "video") {
              mediaEl = document.createElement("video");
              mediaEl.controls = true;
              mediaEl.preload = "metadata";
              mediaEl.src = url;
            } else if (msg.type === "voice") {
              mediaEl = document.createElement("audio");
              mediaEl.controls = true;
              mediaEl.preload = "metadata";
              mediaEl.src = url;
            } else {
              images.push({ url: url, caption: (msg.text || "").slice(0, 80) });
              mediaEl = document.createElement("img");
              mediaEl.loading = "lazy";
              mediaEl.dataset.lb = String(images.length - 1);
              mediaEl.src = url;
            }
            missDiv.replaceWith(mediaEl);
          }
        } else {
          showToast("重试失败：" + (data.errors || []).join("；"), "error");
          retryBtn.disabled = false;
          retryBtn.textContent = "🔄 重试下载";
        }
      } catch (e) {
        showToast("重试失败：" + e.message, "error");
        retryBtn.disabled = false;
        retryBtn.textContent = "🔄 重试下载";
      }
    });
  }

  // ── 标签 ──
  const allTags = [];
  if (msg.tags) for (const t of msg.tags.split(" ").filter(Boolean)) allTags.push({ text: t, type: "auto" });
  if (msg.custom_tags) for (const t of msg.custom_tags.split(" ").filter(Boolean)) allTags.push({ text: t, type: "custom" });

  if (allTags.length > 0 || !searchQuery) {
    const tagsDiv = document.createElement("div");
    tagsDiv.className = "tags";
    for (const { text, type } of allTags) {
      const chip = document.createElement(type === "auto" ? "button" : "span");
      chip.className = "tag-chip" + (type === "custom" ? " custom" : "");
      chip.textContent = (type === "auto" ? "🔍 " : "🏷 ") + text;
      chip.title = type === "auto" ? "搜索「" + text + "」" : "自定义标签「" + text + "」";
      if (type === "auto") {
        chip.type = "button";
        chip.setAttribute("aria-label", "搜索标签「" + text + "」");
        chip.addEventListener("click", () => { $("searchBox").value = text; startSearch(text); });
      }
      tagsDiv.appendChild(chip);
    }

    // 编辑自定义标签（仅管理员可见）
    if (window._isArchiveAdmin) {
    const editBtn = document.createElement("button");
    editBtn.type = "button";
    editBtn.className = "tag-edit";
    editBtn.textContent = msg.custom_tags ? "✎ 编辑" : "+ 加标签";
    editBtn.title = "添加或编辑自定义标签";
    const input = document.createElement("input");
    input.className = "tag-input";
    input.value = msg.custom_tags || "";
    input.placeholder = "自定义标签，空格分隔";
    input.hidden = true;

    const msgId = msg.id;
    const msgYear = msg.year || curYM.year;
    const msgMonth = msg.month || curYM.month;

    editBtn.addEventListener("click", () => {
      if (input.hidden) {
        input.hidden = false; editBtn.hidden = true; input.focus();
      }
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") saveTags();
      if (e.key === "Escape") { input.hidden = true; editBtn.hidden = false; input.value = msg.custom_tags || ""; }
    });
    input.addEventListener("blur", () => {
      setTimeout(() => { if (!input.matches(":focus")) { input.hidden = true; editBtn.hidden = false; } }, 150);
    });

    async function saveTags() {
      const val = input.value.trim();
      try {
        const data = await api("/api/archive/tags?member=" + encodeURIComponent(curMember), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: msgId, year: msgYear, month: msgMonth, custom_tags: val }),
        });
        if (data.ok) {
          msg.custom_tags = val;
          // 重建标签区
          while (tagsDiv.firstChild) tagsDiv.removeChild(tagsDiv.firstChild);
          const tags = [];
          if (msg.tags) for (const t of msg.tags.split(" ").filter(Boolean)) tags.push({ text: t, type: "auto" });
          if (msg.custom_tags) for (const t of msg.custom_tags.split(" ").filter(Boolean)) tags.push({ text: t, type: "custom" });
          for (const { text, type } of tags) {
            const chip = document.createElement(type === "auto" ? "button" : "span");
            chip.className = "tag-chip" + (type === "custom" ? " custom" : "");
            chip.textContent = (type === "auto" ? "🔍 " : "🏷 ") + text;
            chip.title = type === "auto" ? "搜索「" + text + "」" : "自定义标签「" + text + "」";
            if (type === "auto") {
              chip.type = "button";
              chip.setAttribute("aria-label", "搜索标签「" + text + "」");
              chip.addEventListener("click", () => { $("searchBox").value = text; startSearch(text); });
            }
            tagsDiv.appendChild(chip);
          }
          tagsDiv.appendChild(editBtn);
          tagsDiv.appendChild(input);
          editBtn.textContent = val ? "✎ 编辑" : "+ 加标签";
          editBtn.hidden = false; input.hidden = true;
        } else {
          showToast("保存失败：" + (data.errors || []).join("；"), "error");
        }
      } catch (e) { showToast("保存失败：" + e.message, "error"); }
    }

    tagsDiv.appendChild(editBtn);
    tagsDiv.appendChild(input);
    }
    b.appendChild(tagsDiv);
  }

  const copyBtn = b.querySelector(".copy-btn");
  if (copyBtn) {
    copyBtn.addEventListener("click", () => {
      let parts = [];
      const mObj = members.find(x => x.name === curMember);
      const mName = (mObj ? mObj.display : curMember) || "成员";
      const timeStr = fmtCopyTime(msg.published_at);
      let headerStr = mName + " " + timeStr;
      if (msg.upload_at) {
        headerStr += " [真实上传: " + fmtCopyTime(msg.upload_at) + "]";
      }
      parts.push(headerStr);

      if (msg.text && msg.text.trim()) {
        parts.push(msg.text.trim());
      }

      if (msg.translation && msg.translation.trim()) {
        parts.push("----------------------------------------");
        parts.push(msg.translation.trim());
      }

      const textToCopy = parts.join("\n\n");
      navigator.clipboard.writeText(textToCopy).then(() => {
        showToast("📋 已复制整条消息与译文");
      }).catch(err => {
        showToast("⚠️ 复制失败：" + err.message);
      });
    });
  }


  const img = b.querySelector("img[data-lb]");
  if (img) img.addEventListener("click", () => openLightbox(parseInt(img.dataset.lb, 10), img));
  const jump = b.querySelector("a.jump");
  if (jump) jump.addEventListener("click", (e) => {
    e.preventDefault();
    searchQuery = "";
    syncSearchInput();
    jumpToDay(jump.dataset.date);
  });
  tl.appendChild(b);
}

let toastTimer = null;
function showToast(text) {
  let el = $("toastMsg");
  if (!el) {
    el = document.createElement("div");
    el.id = "toastMsg";
    el.className = "toast-msg";
    document.body.appendChild(el);
  }
  el.textContent = text;
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.classList.remove("show"); }, 2400);
}


// ── 灯箱 ─────────────────────────────────────────
let lbIndex = 0;
function openLightbox(i, opener, caption) {
  if (typeof i === "string") {
    if (opener) lightboxOpener = opener;
    $("lbImg").src = i;
    $("lbImg").alt = caption || "图片预览";
    $("lbCounter").style.display = "none";
    $("lbPrev").style.display = "none";
    $("lbNext").style.display = "none";
    $("lightbox").classList.add("open");
    $("lightbox").setAttribute("aria-hidden", "false");
    $("lbClose").focus();
    return;
  }
  const idx = Number(i);
  if (isNaN(idx) || idx < 0 || idx >= images.length) return;
  if (opener) lightboxOpener = opener;
  lbIndex = idx;
  $("lbImg").src = images[idx].url;
  $("lbImg").alt = images[idx].caption || "归档图片";
  if (images.length > 1) {
    $("lbCounter").style.display = "";
    $("lbCounter").textContent = (idx + 1) + " / " + images.length;
    $("lbPrev").style.display = "";
    $("lbNext").style.display = "";
  } else {
    $("lbCounter").style.display = "none";
    $("lbPrev").style.display = "none";
    $("lbNext").style.display = "none";
  }
  $("lightbox").classList.add("open");
  $("lightbox").setAttribute("aria-hidden", "false");
  $("lbClose").focus();
}
function closeLightbox() {
  const box = $("lightbox");
  if (!box.classList.contains("open")) return;
  box.classList.remove("open");
  box.setAttribute("aria-hidden", "true");
  if (lightboxOpener && document.contains(lightboxOpener)) lightboxOpener.focus();
  lightboxOpener = null;
}
function lbMove(delta) {
  const next = lbIndex + delta;
  if (next < 0 || next >= images.length) return;
  openLightbox(next);
}
$("lightbox").addEventListener("click", (e) => {
  if (e.target === $("lightbox") || e.target === $("lbImg")) closeLightbox();
});
$("lbClose").addEventListener("click", closeLightbox);
$("lbPrev").addEventListener("click", (e) => { e.stopPropagation(); lbMove(-1); });
$("lbNext").addEventListener("click", (e) => { e.stopPropagation(); lbMove(1); });
document.addEventListener("keydown", (e) => {
  // 1. 灯箱模式优先处理
  if ($("lightbox").classList.contains("open")) {
    if (e.key === "Escape") closeLightbox();
    if (e.key === "ArrowLeft") lbMove(-1);
    if (e.key === "ArrowRight") lbMove(1);
    if (e.key === "Tab") {
      const focusable = [$("lbClose"), $("lbPrev"), $("lbNext")];
      const index = focusable.indexOf(document.activeElement);
      e.preventDefault();
      focusable[(index + (e.shiftKey ? focusable.length - 1 : 1)) % focusable.length].focus();
    }
    return;
  }

  // 2. 如果焦点在输入框/文本域，不触发全局热键（Esc 除外）
  const isInput = ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName);
  if (isInput) {
    if (e.key === "Escape") {
      document.activeElement.blur();
    }
    return;
  }

  // 3. 全局键盘热键
  if (e.key === "[") {
    e.preventDefault();
    $("prevMonth").click();
    showToast("📅 切换至上一月");
  } else if (e.key === "]") {
    e.preventDefault();
    $("nextMonth").click();
    showToast("📅 切换至下一月");
  } else if (e.key === "/") {
    e.preventDefault();
    $("searchBox").focus();
    $("searchBox").select();
    showToast("🔍 聚焦搜索框 (按 Esc 退出)");
  }
});


// ── 控件 ─────────────────────────────────────────
function initTypeChips() {
  const box = $("typeChips");
  for (const [val, label] of TYPES) {
    const b = document.createElement("button");
    b.className = "chip" + (val === curType ? " active" : "");
    b.textContent = label;
    b.addEventListener("click", () => {
      curType = val;
      box.querySelectorAll(".chip").forEach((c, i) => c.classList.toggle("active", TYPES[i][0] === val));
      loadCalendar();                                     // 日历计数跟随类型筛选
      if (searchQuery) startSearch(searchQuery);          // 搜索模式下重搜（带新类型）
      else if (curYM) selectMonth(curYM.year, curYM.month);
    });
    box.appendChild(b);
  }
}

function initMessageOrder() {
  document.querySelectorAll("#messageOrderToggle [data-order]").forEach((button) => {
    button.onclick = () => setMessageOrder(button.dataset.order);
  });
  syncMessageOrderControls();
}

$("monthSelect").addEventListener("change", () => {
  const [y, m] = $("monthSelect").value.split("-").map(Number);
  selectMonth(y, m);
});
$("prevMonth").addEventListener("click", () => {
  const idx = months.findIndex((m) => m.year === curYM.year && m.month === curYM.month);
  if (idx < months.length - 1) selectMonth(months[idx + 1].year, months[idx + 1].month);
});
$("nextMonth").addEventListener("click", () => {
  const idx = months.findIndex((m) => m.year === curYM.year && m.month === curYM.month);
  if (idx > 0) selectMonth(months[idx - 1].year, months[idx - 1].month);
});
$("loadMore").addEventListener("click", () => {
  if (pageLoading || page >= totalPages) return;
  if ($("blogGrid").style.display !== "none") return;
  page++;
  loadPage();
});
$("calendarToggle").addEventListener("click", () => {
  const side = $("archiveSide");
  const expanded = side.classList.toggle("expanded");
  $("calendarToggle").setAttribute("aria-expanded", String(expanded));
  $("calendarToggle").textContent = expanded ? "▾ 收起日期跳转" : "▦ 按日期跳转";
});

// ── 回到顶部 ─────────────────────────────────────
const backTop = $("backTop");

function handleBackTopScroll() {
  if (backTop.classList.contains('force-hide')) return;
  if ($("blogReader").style.display === "") {
    backTop.classList.toggle("show", $("blogReader").scrollTop > 400);
  } else {
    backTop.classList.toggle("show", window.scrollY > 400);
  }
}

window.addEventListener("scroll", handleBackTopScroll, { passive: true });
$("blogReader").addEventListener("scroll", handleBackTopScroll, { passive: true });

backTop.addEventListener("click", () => {
  if ($("blogReader").style.display === "") {
    $("blogReader").scrollTo({ top: 0, behavior: "smooth" });
  } else {
    window.scrollTo({ top: 0, behavior: "smooth" });
  }
});
initInfiniteScroll();


function _updateAdminUI(isAdmin) {
  window._isArchiveAdmin = !!isAdmin;
  const tabLetter = $("tabLetter");
  if (tabLetter) {
    tabLetter.hidden = !isAdmin;
    tabLetter.style.display = isAdmin ? "inline-flex" : "none";
  }
  const adminIds = ["archiveToolsDropdown", "btnArchiveMember", "btnArchiveMessage"];
  adminIds.forEach(id => {
    const el = $(id);
    if (el) {
      el.hidden = !isAdmin;
      el.style.display = isAdmin ? (el.classList.contains("header-dropdown") ? "inline-block" : "inline-flex") : "none";
    }
  });
}

// ── 成员工具下拉菜单（桌面端下拉 / 移动端底部 Action Sheet 抽屉） ────────────────
const archiveDropdownEl = $("archiveToolsDropdown");
const archiveBtnEl = $("btnArchiveMenu");
const archiveSheetEl = $("archiveToolsSheet");
const closeArchiveSheet = () => {
  if (archiveSheetEl) archiveSheetEl.style.display = "none";
};

if (archiveBtnEl) {
  archiveBtnEl.addEventListener("click", (e) => {
    e.stopPropagation();
    if (window.innerWidth <= 768) {
      if (archiveSheetEl) archiveSheetEl.style.display = "flex";
    } else if (archiveDropdownEl) {
      archiveDropdownEl.classList.toggle("open");
    }
  });
}

if (archiveDropdownEl) {
  document.addEventListener("click", (e) => {
    if (!archiveDropdownEl.contains(e.target)) {
      archiveDropdownEl.classList.remove("open");
    }
  });
}

if ($("archiveToolsBackdrop")) $("archiveToolsBackdrop").addEventListener("click", closeArchiveSheet);
if ($("sheetBtnCancel")) $("sheetBtnCancel").addEventListener("click", closeArchiveSheet);
if ($("sheetBtnArchiveMember")) {
  $("sheetBtnArchiveMember").addEventListener("click", () => {
    closeArchiveSheet();
    promptArchiveMember();
  });
}
if ($("sheetBtnArchiveMessage")) {
  $("sheetBtnArchiveMessage").addEventListener("click", () => {
    closeArchiveSheet();
    promptArchiveMessage();
  });
}

// ── 成员快捷下拉选择器与横向滚动控制 ───────────────
if ($("btnMemberDropdown")) {
  $("btnMemberDropdown").addEventListener("click", (e) => {
    e.stopPropagation();
    toggleMemberPopover();
  });
}
document.addEventListener("click", (e) => {
  const wrap = $("memberDropdownWrap");
  if (wrap && !wrap.contains(e.target)) {
    closeMemberPopover();
  }
});
if ($("memberSearchInput")) {
  $("memberSearchInput").addEventListener("input", (e) => {
    const val = e.target.value;
    if ($("btnMemberSearchClear")) $("btnMemberSearchClear").style.display = val ? "block" : "none";
    renderMemberPopover(val);
  });
}
if ($("btnMemberSearchClear")) {
  $("btnMemberSearchClear").addEventListener("click", () => {
    $("memberSearchInput").value = "";
    $("btnMemberSearchClear").style.display = "none";
    renderMemberPopover("");
    $("memberSearchInput").focus();
  });
}
// ── 博客三坂分组分段控制器点击事件 ───────────────
document.querySelectorAll("#blogGroupSegment .seg-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    const k = btn.dataset.key;
    if (k) selectBlogGroup(k);
  });
});

// ── 博客作者下拉选择器控制 ───────────────
if ($("btnBlogAuthorDropdown")) {
  $("btnBlogAuthorDropdown").addEventListener("click", (e) => {
    e.stopPropagation();
    toggleBlogAuthorPopover();
  });
}
document.addEventListener("click", (e) => {
  const wrap = $("blogAuthorDropdownWrap");
  if (wrap && !wrap.contains(e.target)) {
    closeBlogAuthorPopover();
  }
});
if ($("blogAuthorSearchInput")) {
  $("blogAuthorSearchInput").addEventListener("input", (e) => {
    const val = e.target.value;
    if ($("btnBlogAuthorSearchClear")) $("btnBlogAuthorSearchClear").style.display = val ? "block" : "none";
    renderBlogAuthorPopover(val);
  });
}
if ($("btnBlogAuthorSearchClear")) {
  $("btnBlogAuthorSearchClear").addEventListener("click", () => {
    $("blogAuthorSearchInput").value = "";
    $("btnBlogAuthorSearchClear").style.display = "none";
    renderBlogAuthorPopover("");
    $("blogAuthorSearchInput").focus();
  });
}

// ── 登录状态 ─────────────────────────────────────
window._isLoggedIn = false;
let _authInitPromise = null;

async function initAuth() {
  if (_authInitPromise) return _authInitPromise;
  _authInitPromise = (async () => {
    try {
      const me = await (await fetch("/api/auth/me", { cache: "no-store" })).json();
      const adminLink = $("adminLink");
      if (!me.auth_enabled) { 
        window._isLoggedIn = true; 
        _updateAdminUI(true);
        if (adminLink) {
          adminLink.hidden = false;
          adminLink.style.display = "inline-flex";
          adminLink.href = "/";
          adminLink.title = "进入系统管理后台";
          adminLink.innerHTML = "<span>⚙️</span><span>管理后台</span>";
        }
        $("logoutBtn").hidden = true;
        $("logoutBtn").style.display = "none";
        return; 
      }
      if (me.user) {
        window._isLoggedIn = true;
        $("whoami").textContent = "👤 " + me.user.username;
        $("logoutBtn").hidden = false;
        $("logoutBtn").style.display = "inline-flex";
        const isAdmin = me.user.role === "admin";
        _updateAdminUI(isAdmin);
        if (adminLink) {
          adminLink.hidden = !isAdmin;
          adminLink.style.display = isAdmin ? "inline-flex" : "none";
          adminLink.href = "/";
          adminLink.title = "进入系统管理后台";
          adminLink.innerHTML = "<span>⚙️</span><span>管理后台</span>";
        }
      } else {
        window._isLoggedIn = false;
        $("whoami").textContent = "";
        $("logoutBtn").hidden = true;
        $("logoutBtn").style.display = "none";
        _updateAdminUI(false);
        // 未登录 / 游客免登录模式下：展示「管理后台」按钮，点击前往登录页 /login
        if (adminLink) {
          adminLink.hidden = false;
          adminLink.style.display = "inline-flex";
          adminLink.href = "/login?next=/";
          adminLink.title = "登录管理员账号以进入后台";
          adminLink.innerHTML = "<span>⚙️</span><span>管理后台</span>";
        }
      }
    } catch (e) { /* 忽略 */ }
  })();
  return _authInitPromise;
}
initAuth();
const logoutBtn = $("logoutBtn");
if (logoutBtn) {
  logoutBtn.addEventListener("click", async (e) => {
    e.preventDefault();
    try { await fetch("/api/auth/logout", { method: "POST" }); } catch (err) { /* 忽略 */ }
    try { localStorage.removeItem("webAdminToken"); } catch (err) { /* 保留偏好 */ }
    location.href = "/login";
  });
}

// ── 首页 ─────────────────────────────────────────
function parseDateSafe(utc) {
  if (!utc) return null;
  const s = String(utc).trim();
  let d = new Date(s);
  if (!isNaN(d.getTime())) return d;
  let iso = s.replace(" ", "T");
  if (!iso.endsWith("Z") && !iso.includes("+") && !iso.includes("-", 10)) {
    iso += "Z";
  }
  d = new Date(iso);
  if (!isNaN(d.getTime())) return d;
  return null;
}

function fmtDate(utc) {
  const d = parseDateSafe(utc);
  if (!d) return String(utc || "").slice(0, 16);
  return (d.getMonth() + 1) + "月" + d.getDate() + "日 " +
    String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}

function fmtDateFull(utc) {
  const d = parseDateSafe(utc);
  if (!d) return String(utc || "").slice(0, 16);
  return d.getFullYear() + "年" + (d.getMonth() + 1) + "月" + d.getDate() + "日 " +
    String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
}

function fmtDateShort(utc) {
  const d = parseDateSafe(utc);
  if (!d) return String(utc || "").slice(0, 10);
  return (d.getMonth() + 1) + "月" + d.getDate() + "日";
}

async function openBlogReaderById(blogId) {
  try {
    const res = await api("/api/archive/blogs?id=" + encodeURIComponent(blogId));
    if (res.ok && res.post) {
      openBlogReader(res.post);
    } else {
      showToast("⚠️ 博客加载失败");
    }
  } catch (e) {
    showToast("⚠️ 加载博客异常: " + e.message);
  }
}

function jumpToMessage(member, year, month, msgId) {
  if (!member) return;
  curMode = "msg";
  switchMainTab("msg", true);
  hideHome();
  curMember = member;
  try { localStorage.setItem("archive_last_msg_member", member); } catch (_) {}
  curType = "";
  searchQuery = "";
  syncSearchInput();
  targetMsgId = msgId ? String(msgId) : "";
  selfHashUpdate = true;
  const y = parseInt(year, 10), m = parseInt(month, 10);
  if (y && m) {
    location.hash = "member=" + encodeURIComponent(member) + "&y=" + y + "&m=" + m;
  } else {
    location.hash = "member=" + encodeURIComponent(member);
  }
  setTimeout(() => { selfHashUpdate = false; }, 100);
  selectMember(member, true);
}

let _portalHomeCached = null;
let _homeRequestVersion = 0;
let _homeRenderVersion = 0;
let _homePhotoCleanup = null;

async function showHome() {
  const requestVersion = ++_homeRequestVersion;
  const routeAtStart = location.hash;
  curMode = "home";
  setHtmlViewClass("home");
  if ($("tabHome")) $("tabHome").classList.add("active");
  if ($("tabMsg")) $("tabMsg").classList.remove("active");
  if ($("tabBlog")) $("tabBlog").classList.remove("active");
  if ($("tabLetter")) $("tabLetter").classList.remove("active");

  document.querySelector('.layout').style.display = 'none';
  if ($("letterGrid")) $("letterGrid").style.display = "none";
  if ($("blogGrid")) $("blogGrid").style.display = "none";
  if ($("timeline")) $("timeline").style.display = "none";
  $('backTop').classList.remove('show'); $('backTop').classList.add('force-hide');
  $('archiveHome').classList.add('active');
  
  // 1. 如果已有内存或 sessionStorage 缓存，优先秒出（0ms 首屏响应）
  let hasRenderedCache = false;
  if (!_portalHomeCached) {
    try {
      const raw = sessionStorage.getItem("archive_portal_home_cache");
      if (raw) {
        const parsed = JSON.parse(raw);
        if (parsed && (Date.now() - parsed._ts < 60000)) {
          _portalHomeCached = parsed.data;
        }
      }
    } catch(e) {}
  }
  
  if (_portalHomeCached) {
    renderHome(_portalHomeCached);
    $('homeSkeleton').classList.remove('active');
    $('portalContent').style.display = '';
    hasRenderedCache = true;
  } else {
    // 骨架屏
    $('homeSkeleton').classList.add('active');
    $('portalContent').style.display = 'none';
  }
  
  try {
    const data = await api("/api/archive/home");
    // 首页请求可能在用户切换到其它路由后才返回，避免旧响应覆盖当前视图。
    if (requestVersion !== _homeRequestVersion || curMode !== "home" || location.hash !== routeAtStart) return;
    if (!data.ok || (!data.members.length && !data.blog_groups.length)) {
      $('homeSkeleton').classList.remove('active');
      $('archiveHome').innerHTML =
        '<div class="home-empty active"><div class="ee-icon">📭</div>' +
        '<div class="ee-title">还没有归档数据</div>' +
        '<div class="ee-desc">确认 config.json 的 archive.enabled 已开启。<br>新消息会自动归档；历史消息用 <code>python tools/backfill_archive.py</code> 回填。<br><br><a href="/">⚙️ 前往管理端</a></div></div>';
      return;
    }
    _portalHomeCached = data;
    try {
      sessionStorage.setItem("archive_portal_home_cache", JSON.stringify({ _ts: Date.now(), data }));
    } catch(e) {}
    renderHome(data);
    $('homeSkeleton').classList.remove('active');
    $('portalContent').style.display = '';
  } catch (e) {
    if (!hasRenderedCache) {
      $('homeSkeleton').classList.remove('active');
      $('archiveHome').innerHTML = '<div style="text-align:center;color:var(--err);padding:60px 20px">加载失败：' + esc(e.message) + '</div>';
    }
  }
}

function renderHome(data) {
  const renderVersion = ++_homeRenderVersion;
  if (_homePhotoCleanup) _homePhotoCleanup();
  const summary = data.summary || {};
  const members = data.members || [];
  const blogGroups = data.blog_groups || [];
  const recentPics = data.recent_pics || [];
  const recentFeed = data.recent_feed || [];
  const timeTunnel = data.time_tunnel || [];

  // 1. Portal Hero 顶级数字看板
  const heroDiv = $("portalHero");
  let heroHTML = '';
  heroHTML += '<div class="portal-hero-top">';
  heroHTML += '<div class="portal-hero-brand">';
  heroHTML += '<div class="portal-hero-icon"><img src="/static/archive_icon.svg" alt="坂道时光归档"></div>';
  heroHTML += '<div class="portal-hero-title-box">';
  heroHTML += '<div class="portal-hero-badge-row">';
  heroHTML += '<span class="portal-pill-brand nogi">乃木坂46</span>';
  heroHTML += '<span class="portal-pill-brand sakura">樱坂46</span>';
  heroHTML += '<span class="portal-pill-brand hinata">日向坂46</span>';
  heroHTML += '<span class="portal-status-live"><span class="pulse-dot"></span> 实时监控中</span>';
  heroHTML += '</div>';
  heroHTML += '<h1 class="portal-hero-title">坂道时光归档</h1>';
  heroHTML += '<div class="portal-hero-sub">乃木坂46 · 樱坂46 · 日向坂46 · 官方 Message 私信与博客全景收录</div>';
  heroHTML += '</div></div>';
  heroHTML += '</div>';

  // 4 个 Bento Metric 卡片
  heroHTML += '<div class="portal-metric-grid">';
  heroHTML += '<div class="portal-metric-card" id="heroCardMsg" title="点击直达 Message 时间线">';
  heroHTML += '<div class="pm-top"><span class="pm-icon msg">💬</span><span class="pm-tag">Message 消息</span></div>';
  heroHTML += '<div class="pm-val">' + (summary.total_messages || 0).toLocaleString() + ' <small>条</small></div>';
  heroHTML += '<div class="pm-sub">' + (summary.member_count || 0) + ' 位重点监控成员 ↗</div>';
  heroHTML += '</div>';

  heroHTML += '<div class="portal-metric-card" id="heroCardBlog" title="点击直达官方博客中心">';
  heroHTML += '<div class="pm-top"><span class="pm-icon blog">📝</span><span class="pm-tag">官方博客</span></div>';
  heroHTML += '<div class="pm-val">' + (summary.total_blogs || 0).toLocaleString() + ' <small>篇</small></div>';
  heroHTML += '<div class="pm-sub">3 团全量 · ' + (summary.blog_author_count || 0) + ' 位作者 ↗</div>';
  heroHTML += '</div>';

  const totalMedia = Number.isFinite(Number(summary.message_media_total))
    ? Number(summary.message_media_total)
    : (summary.total_pictures || 0) + (summary.total_videos || 0) + (summary.total_voices || 0);
  heroHTML += '<div class="portal-metric-card">';
  heroHTML += '<div class="pm-top"><span class="pm-icon media">📸</span><span class="pm-tag">消息媒体</span></div>';
  heroHTML += '<div class="pm-val">' + totalMedia.toLocaleString() + ' <small>项</small></div>';
  heroHTML += '<div class="pm-sub">照片 ' + (summary.total_pictures || 0).toLocaleString() + ' · 视频 ' +
    (summary.total_videos || 0).toLocaleString() + ' · 语音 ' + (summary.total_voices || 0).toLocaleString() + '</div>';
  heroHTML += '</div>';

  const lu = summary.last_updated ? fmtDate(summary.last_updated) : '—';
  heroHTML += '<div class="portal-metric-card">';
  heroHTML += '<div class="pm-top"><span class="pm-icon clock">⏳</span><span class="pm-tag">归档年谱</span></div>';
  heroHTML += '<div class="pm-val" style="font-size:16px; margin-top:2px;">' + (summary.first_date || '2012/02') + ' — ' + (summary.last_date || '2026/08') + '</div>';
  heroHTML += '<div class="pm-sub">最近更新: ' + lu + '</div>';
  heroHTML += '</div>';
  heroHTML += '</div>';

  const today = summary.today_stats || {};
  let actionHTML = '';
  if (today.total > 0) {
    actionHTML += '<button class="portal-today-btn" id="portalTodayBtn">🔥 今日收录 <b>' + today.total + '</b> 条动态（Message ' + (today.messages || 0) + ' · 博客 ' + (today.blogs || 0) + '）· 点击速览 →</button>';
  } else {
    actionHTML += '<span style="font-size:12.5px;color:var(--muted)">✨ 历史消息与官方博客已全部同步就绪</span>';
  }
  heroHTML += '<div class="portal-hero-banner">' + actionHTML + '<span style="font-size:12px;color:var(--muted)">📅 ' + (summary.first_date || '2012/02') + ' 至今</span></div>';
  heroDiv.innerHTML = heroHTML;

  // 快捷跳转
  $("heroCardMsg")?.addEventListener("click", () => switchMainTab("msg"));
  $("heroCardBlog")?.addEventListener("click", () => switchMainTab("blog"));
  $("portalTodayBtn")?.addEventListener("click", () => {
    const feedSec = $("homeFeedList");
    if (feedSec) {
      const topY = feedSec.getBoundingClientRect().top + window.scrollY - 80;
      window.scrollTo({ top: topY, behavior: "smooth" });
    }
  });

  // 2. 综合写真画廊
  const strip = $("photoStrip");
  if (recentPics.length) {
    strip.innerHTML = recentPics.map(p =>
      '<div class="photo-card" data-type="' + p.type + '" data-member="' + esc(p.member || '') + '" data-group="' + esc(p.group_key || '') + '" data-id="' + p.id + '" data-year="' + (p.year || '') + '" data-month="' + (p.month || '') + '">' +
        '<span class="pc-member">' + esc(p.member_display) + '</span>' +
        '<img src="' + mediaUrl(p.url) + '" loading="lazy" decoding="async" data-src="' + esc(p.url) + '" alt="" onerror="handleImgError(this)" onload="this.classList.add(\'loaded\')">' +
        (p.text ? '<div class="pc-overlay"><div class="pc-cap">' + formatMessageText(p.text) + '</div></div>' : '') +
      '</div>'
    ).join('');

    strip.querySelectorAll('.photo-card').forEach(el => {
      el.addEventListener('click', () => {
        if (dragMoved) return;
        const pType = el.dataset.type;
        if (pType === "blog") {
          openBlogReaderById(el.dataset.id);
        } else {
          jumpToMessage(el.dataset.member, el.dataset.year, el.dataset.month, el.dataset.id);
        }
      });
    });
  } else {
    strip.innerHTML = '<div style="color:var(--muted);padding:30px 10px;text-align:center">暂无图片</div>';
  }

  // 图片条自动滚动与拖拽交互
  let photoTimer = null;
  let photoScrolling = false;
  let photoDisposed = false;
  function photoAdvance() {
    if (photoScrolling) return;
    if (strip.scrollWidth <= strip.clientWidth) return;
    const step = (strip.querySelector('.photo-card')?.offsetWidth || 180) + 14;
    let target;
    if (strip.scrollLeft + strip.clientWidth >= strip.scrollWidth - 10) {
      target = 0;
    } else {
      target = strip.scrollLeft + step;
    }
    photoScrolling = true;
    strip.scrollTo({ left: target, behavior: 'smooth' });
    setTimeout(() => { photoScrolling = false; }, 700);
  }
  function startPhotoScroll() {
    if (photoDisposed || photoTimer) return;
    photoTimer = setInterval(photoAdvance, 2400);
  }
  function stopPhotoScroll() { clearInterval(photoTimer); photoTimer = null; photoScrolling = false; }
  startPhotoScroll();
  strip.addEventListener("touchstart", stopPhotoScroll, { once: true });
  strip.addEventListener("wheel", stopPhotoScroll, { once: true });
  strip.addEventListener("mouseenter", stopPhotoScroll);
  strip.addEventListener("mouseleave", startPhotoScroll);

  let dragOn = false, dragStartX = 0, dragStartScroll = 0;
  let dragTrail = [];
  let dragMoved = false;
  let inertiaRaf = null;
  function cancelInertia() { if (inertiaRaf) { cancelAnimationFrame(inertiaRaf); inertiaRaf = null; } }
  function startInertia(pxPerFrame) {
    cancelInertia();
    if (Math.abs(pxPerFrame) < 0.3) return;
    let v = pxPerFrame;
    const friction = 0.94;
    function step() {
      v *= friction;
      strip.scrollLeft -= v;
      if (Math.abs(v) > 0.25 && strip.scrollLeft > 0 &&
          strip.scrollLeft < strip.scrollWidth - strip.clientWidth) {
        inertiaRaf = requestAnimationFrame(step);
      } else { cancelInertia(); }
    }
    inertiaRaf = requestAnimationFrame(step);
  }
  strip.addEventListener("mousedown", (e) => {
    dragOn = true; dragStartX = e.clientX; dragStartScroll = strip.scrollLeft;
    dragTrail = []; dragMoved = false;
    cancelInertia(); stopPhotoScroll();
    strip.style.cursor = "grabbing";
  });
  window.addEventListener("mousemove", (e) => {
    if (!dragOn) return;
    if (Math.abs(e.clientX - dragStartX) > 5) {
      dragMoved = true;
      strip.scrollLeft = dragStartScroll + (dragStartX - e.clientX);
    }
    dragTrail.push({ t: performance.now(), x: e.clientX });
    const cutoff = performance.now() - 100;
    dragTrail = dragTrail.filter(p => p.t > cutoff);
  });
  window.addEventListener("mouseup", () => {
    if (!dragOn) return;
    dragOn = false;
    strip.style.cursor = "";
    if (dragTrail.length >= 2) {
      const a = dragTrail[0], b = dragTrail[dragTrail.length - 1];
      const dt = b.t - a.t;
      if (dt > 5) startInertia((b.x - a.x) / dt * 16);
    }
    dragTrail = [];
    setTimeout(() => { if (!dragOn) startPhotoScroll(); }, 3000);
    setTimeout(() => { dragMoved = false; }, 50);
  });
  strip.addEventListener("click", (e) => {
    if (dragMoved) { e.stopPropagation(); e.stopImmediatePropagation(); e.preventDefault(); }
  }, true);
  _homePhotoCleanup = () => {
    photoDisposed = true;
    stopPhotoScroll();
    cancelInertia();
  };

  // 3. 核心归档专区入口：三坂官方频道便当卡 + 成员快捷入口
  const renderSecondary = () => {
  const secDiv = $("portalSections");
  let secHTML = '';

  // 3.1 三坂官方频道卡片 (3-Group Channel Bento Cards)
  secHTML += '<div class="portal-channels-grid">';
  
  // 乃木坂46
  const nogiBlog = blogGroups.find(g => g.key === "nogizaka") || {};
  const nogiMsgCount = members.filter(m => (m.group || '').includes('nogi')).reduce((acc, x) => acc + (x.stats?.total || 0), 0);
  secHTML += '<div class="channel-bento-card nogi" data-group="nogizaka">';
  secHTML += '<div class="cbc-header"><div class="cbc-brand"><span class="cbc-icon">💜</span><div><div class="cbc-name">乃木坂46 频道</div><div class="cbc-meta">Message & 官方博客总库</div></div></div><span class="cbc-jump">进入频道 →</span></div>';
  secHTML += '<div class="cbc-stats"><div class="cbc-stat"><span class="cs-label">💬 Message</span><span class="cs-val">' + nogiMsgCount.toLocaleString() + ' <small>条</small></span></div><div class="cbc-stat"><span class="cs-label">📝 官方博客</span><span class="cs-val">' + (nogiBlog.total || 0).toLocaleString() + ' <small>篇</small></span></div></div>';
  secHTML += '<div class="cbc-actions"><button class="cbc-btn msg" data-action="msg" data-target="冨里奈央">进入消息</button><button class="cbc-btn blog" data-action="blog" data-group="nogizaka">浏览博客</button></div>';
  secHTML += '</div>';

  // 樱坂46
  const sakuraBlog = blogGroups.find(g => g.key === "sakurazaka") || {};
  const sakuraMsgCount = members.filter(m => (m.group || '').includes('sakura')).reduce((acc, x) => acc + (x.stats?.total || 0), 0);
  secHTML += '<div class="channel-bento-card sakura" data-group="sakurazaka">';
  secHTML += '<div class="cbc-header"><div class="cbc-brand"><span class="cbc-icon">🌸</span><div><div class="cbc-name">樱坂46 频道</div><div class="cbc-meta">Message & 官方博客总库</div></div></div><span class="cbc-jump">进入频道 →</span></div>';
  secHTML += '<div class="cbc-stats"><div class="cbc-stat"><span class="cs-label">💬 Message</span><span class="cs-val">' + sakuraMsgCount.toLocaleString() + ' <small>条</small></span></div><div class="cbc-stat"><span class="cs-label">📝 官方博客</span><span class="cs-val">' + (sakuraBlog.total || 0).toLocaleString() + ' <small>篇</small></span></div></div>';
  secHTML += '<div class="cbc-actions"><button class="cbc-btn msg" data-action="msg" data-target="石森_璃花">进入消息</button><button class="cbc-btn blog" data-action="blog" data-group="sakurazaka">浏览博客</button></div>';
  secHTML += '</div>';

  // 日向坂46
  const hinataBlog = blogGroups.find(g => g.key === "hinatazaka") || {};
  const hinataMsgCount = members.filter(m => inferMemberGroup(m) === "hinatazaka").reduce((acc, x) => acc + (x.stats?.total || 0), 0);
  secHTML += '<div class="channel-bento-card hinata" data-group="hinatazaka">';
  secHTML += '<div class="cbc-header"><div class="cbc-brand"><span class="cbc-icon">🩵</span><div><div class="cbc-name">日向坂46 频道</div><div class="cbc-meta">Message & 官方博客总库</div></div></div><span class="cbc-jump">进入频道 →</span></div>';
  secHTML += '<div class="cbc-stats"><div class="cbc-stat"><span class="cs-label">💬 Message</span><span class="cs-val">' + hinataMsgCount.toLocaleString() + ' <small>条</small></span></div><div class="cbc-stat"><span class="cs-label">📝 官方博客</span><span class="cs-val">' + (hinataBlog.total || 0).toLocaleString() + ' <small>篇</small></span></div></div>';
  secHTML += '<div class="cbc-actions"><button class="cbc-btn msg" data-action="msg" data-target="佐藤_優羽">进入消息</button><button class="cbc-btn blog" data-action="blog" data-group="hinatazaka">浏览博客</button></div>';
  secHTML += '</div>';

  secHTML += '</div>'; // End portal-channels-grid

  // 3.2 监控成员快捷入口网格 (Member Quick Bento Grid)
  secHTML += '<div class="portal-member-section">';
  secHTML += '<div class="pms-header"><span>👥 监控成员快捷通道</span><span class="pms-sub">点击直达对应成员消息时间线</span></div>';
  secHTML += '<div class="portal-members-grid">';
  members.forEach(m => {
    const gKey = inferMemberGroup(m);
    let grpClass = "other";
    let grpName = "其他";
    if (gKey === "nogizaka") { grpClass = "nogi"; grpName = "乃木坂"; }
    else if (gKey === "sakurazaka") { grpClass = "sakura"; grpName = "樱坂"; }
    else if (gKey === "hinatazaka") { grpClass = "hinata"; grpName = "日向坂"; }
    else if (gKey === "yodel") { grpClass = "yodel"; grpName = "yodel"; }
    
    let avatarText = (m.display || "").replace(/[\s_　]/g, "");
    if (avatarText.length > 2) avatarText = avatarText.slice(-2);
    if (!avatarText) avatarText = "💬";
    if (m.name.includes("マネダコ")) avatarText = "🐙";
    let avHTML = '';
    if (m.avatar) {
      avHTML = '<img class="mbc-avatar-img" src="' + esc(m.avatar) + '" loading="lazy" decoding="async" alt="" onerror="this.style.display=\'none\';if(this.nextElementSibling)this.nextElementSibling.style.display=\'flex\';" /><div class="mbc-avatar ' + grpClass + '" style="display:none;">' + esc(avatarText) + '</div>';
    } else {
      avHTML = '<div class="mbc-avatar ' + grpClass + '">' + esc(avatarText) + '</div>';
    }

    secHTML += '<div class="member-bento-card ' + grpClass + '" data-name="' + esc(m.name) + '">';
    secHTML += avHTML;
    secHTML += '<div class="mbc-info">';
    secHTML += '<div class="mbc-top"><span class="mbc-name">' + esc(m.display) + '</span><span class="mbc-pill ' + grpClass + '">' + grpName + '</span></div>';
    secHTML += '<div class="mbc-meta">' + (m.stats?.total || 0).toLocaleString() + ' 条归档 · ' + (m.stats?.months || 0) + ' 个月</div>';
    secHTML += '</div>';
    secHTML += '<span class="mbc-arrow">↗</span>';
    secHTML += '</div>';
  });
  secHTML += '</div></div>';

  secDiv.innerHTML = secHTML;

  // 专区卡片与按钮点击交互
  secDiv.querySelectorAll('.channel-bento-card .cbc-btn').forEach(btn => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const action = btn.dataset.action;
      if (action === "blog") {
        const gKey = btn.dataset.group;
        hideHome();
        selectBlogGroup(gKey);
      } else {
        const target = btn.dataset.target;
        curMode = "msg";
        switchMainTab("msg", true);
        hideHome();
        selectMember(target);
      }
    });
  });

  secDiv.querySelectorAll('.channel-bento-card').forEach(card => {
    card.addEventListener("click", () => {
      const gKey = card.dataset.group;
      hideHome();
      selectBlogGroup(gKey);
    });
  });

  secDiv.querySelectorAll('.member-bento-card').forEach(card => {
    card.addEventListener("click", () => {
      const mName = card.dataset.name;
      jumpToMessage(mName);
    });
  });

  };

  // 4. 最新动态聚合流 (Message + Blog 双列网格排版，严格对齐)
  const renderTertiary = () => {
  const feedDiv = $("homeFeedList");
  // 保证偶数个卡片，使双列底部完美平齐
  const evenRecentFeed = recentFeed.length % 2 === 0 ? recentFeed : recentFeed.slice(0, recentFeed.length - 1);
  if (evenRecentFeed.length) {
    let cardsHTML = '';
    evenRecentFeed.forEach((item, i) => {
      let cardHTML = '';
      const dateStr = fmtDate(item.published_at);
      if (item.type === "blog") {
        cardHTML += '<div class="home-msg-card" style="animation-delay:' + (i * .03) + 's" onclick="openBlogReaderById(\'' + item.id + '\')">';
        cardHTML += '<div class="hmc-header">';
        cardHTML += '<div class="hmc-meta-left">';
        cardHTML += '<span class="hmc-mem-badge" style="background:rgba(167,139,250,0.12);color:#a78bfa;">' + esc(item.member_display) + '</span>';
        cardHTML += '<span class="hmc-time">' + dateStr + '</span>';
        cardHTML += '</div>';
        cardHTML += '<span class="hmc-jump">阅读博客 ↗</span>';
        cardHTML += '</div>';
        cardHTML += '<div class="hmc-text" style="font-weight:600;">' + esc(item.text) + '</div>';
        cardHTML += '</div>';
      } else {
        cardHTML += '<div class="home-msg-card" style="animation-delay:' + (i * .03) + 's" data-member="' + esc(item.member) + '" data-year="' + item.year + '" data-month="' + item.month + '" data-id="' + item.id + '">';
        cardHTML += '<div class="hmc-header">';
        cardHTML += '<div class="hmc-meta-left">';
        cardHTML += '<span class="hmc-mem-badge">' + esc(item.member_display) + '</span>';
        cardHTML += '<span class="hmc-time">' + dateStr + '</span>';
        cardHTML += '</div>';
        cardHTML += '<span class="hmc-jump">查看消息 →</span>';
        cardHTML += '</div>';
        cardHTML += '<div class="hmc-text">' + formatCardText(item.text) + '</div>';
        if (item.translation) {
          cardHTML += '<div class="hmc-trans">' + formatCardText(item.translation) + '</div>';
        }
        cardHTML += '</div>';
      }
      cardsHTML += cardHTML;
    });
    feedDiv.innerHTML = cardsHTML;
    feedDiv.querySelectorAll('.home-msg-card[data-member]').forEach(el => {
      el.addEventListener('click', () => {
        jumpToMessage(el.dataset.member, el.dataset.year, el.dataset.month, el.dataset.id);
      });
    });
  } else {
    feedDiv.innerHTML = '<div style="text-align:center;color:var(--muted);padding:24px 10px;grid-column:1/-1;">暂无最新动态</div>';
  }

  // 5. 时光隧道 (Message + Blog 双列网格排版，严格对齐)
  const tunnelDiv = $("homeTimeTunnel");
  // 保证偶数个卡片，使双列底部完美平齐
  const evenTimeTunnel = timeTunnel && timeTunnel.length ? (timeTunnel.length % 2 === 0 ? timeTunnel : timeTunnel.slice(0, timeTunnel.length - 1)) : [];
  if (evenTimeTunnel.length) {
    let cardsHTML = '';
    evenTimeTunnel.forEach((item, i) => {
      let cardHTML = '';
      const dateStr = fmtDateFull(item.published_at);
      const d = parseDateSafe(item.published_at);
      const year = d ? d.getFullYear() : (item.year || '');
      const thisYear = new Date().getFullYear();
      const yearsAgo = thisYear - (year || thisYear);
      const agoTag = yearsAgo > 0 ? (yearsAgo + '年前 · ' + year + '年') : (year + '年');

      if (item.type === "blog") {
        cardHTML += '<div class="home-msg-card tunnel" style="animation-delay:' + (i * .04) + 's" onclick="openBlogReaderById(\'' + item.id + '\')">';
        cardHTML += '<div class="hmc-header">';
        cardHTML += '<div class="hmc-meta-left">';
        cardHTML += '<span class="hmc-tunnel-badge">⏳ ' + agoTag + '</span>';
        cardHTML += '<span class="hmc-mem-badge" style="background:rgba(167,139,250,0.12);color:#a78bfa;">' + esc(item.member_display) + '</span>';
        cardHTML += '<span class="hmc-time">' + dateStr + '</span>';
        cardHTML += '</div>';
        cardHTML += '<span class="hmc-jump">阅读博客 ↗</span>';
        cardHTML += '</div>';
        cardHTML += '<div class="hmc-text" style="font-weight:600;">' + esc(item.text) + '</div>';
        if (item.translation) {
          cardHTML += '<div class="hmc-trans" style="color:var(--text);">' + formatCardText(item.translation) + '</div>';
        }
        cardHTML += '</div>';
      } else {
        cardHTML += '<div class="home-msg-card tunnel" style="animation-delay:' + (i * .04) + 's" data-member="' + esc(item.member) + '" data-year="' + item.year + '" data-month="' + item.month + '" data-id="' + item.id + '">';
        cardHTML += '<div class="hmc-header">';
        cardHTML += '<div class="hmc-meta-left">';
        cardHTML += '<span class="hmc-tunnel-badge">⏳ ' + agoTag + '</span>';
        cardHTML += '<span class="hmc-mem-badge">' + esc(item.member_display) + '</span>';
        cardHTML += '<span class="hmc-time">' + dateStr + '</span>';
        cardHTML += '</div>';
        cardHTML += '<span class="hmc-jump">跳转当日 →</span>';
        cardHTML += '</div>';
        cardHTML += '<div class="hmc-text">' + formatCardText(item.text) + '</div>';
        if (item.translation) {
          cardHTML += '<div class="hmc-trans">' + formatCardText(item.translation) + '</div>';
        }
        cardHTML += '</div>';
      }
      cardsHTML += cardHTML;
    });
    tunnelDiv.innerHTML = cardsHTML;
    tunnelDiv.querySelectorAll('.home-msg-card[data-member]').forEach(el => {
      el.addEventListener('click', () => {
        jumpToMessage(el.dataset.member, el.dataset.year, el.dataset.month, el.dataset.id);
      });
    });
  } else {
    tunnelDiv.innerHTML = '<div style="text-align:center;color:var(--muted);padding:24px 10px;grid-column:1/-1;">暂无历史消息</div>';
  }
  };

  const renderIfCurrent = (callback) => {
    if (renderVersion !== _homeRenderVersion || curMode !== "home") return;
    callback();
  };
  const scheduleIdle = (callback) => {
    if (typeof window.requestIdleCallback === "function") {
      window.requestIdleCallback(callback, { timeout: 300 });
    } else {
      window.setTimeout(callback, 0);
    }
  };
  window.requestAnimationFrame(() => {
    renderIfCurrent(() => {
      renderSecondary();
      scheduleIdle(() => renderIfCurrent(renderTertiary));
    });
  });
}

function goHome() {
  curMember = ""; curBlogGroup = "";
  curType = ""; searchQuery = "";
  ++blogSelectionVersion;
  ++blogPageVersion;
  if (blogPageAbort) blogPageAbort.abort();
  syncSearchInput();
  writeArchiveHash("");
  switchMainTab("home", true);
  showHome();
}

function hideHome() {
  _homeRequestVersion++;
  $('archiveHome').classList.remove('active');
  $('backTop').classList.remove('force-hide');
  document.querySelector('.layout').style.display = '';
  _enterMemberMode();
}

// ── 路由与视图分发 ─────────────────────────────────────
async function handleRoute(isInitial = false) {
  const rawHash = (location.hash || "").replace(/^#/, "");
  const p = new URLSearchParams(rawHash);

  // 1. 博客模式：#blog, #blog=nogizaka, #id=..., #blog_id=...
  const blogId = p.get("id") || p.get("blog_id") || p.get("post");
  if (p.has("blog") || rawHash === "blog" || blogId) {
    let savedGroup = null;
    let savedAuthor = "";
    try {
      savedGroup = localStorage.getItem("archive_last_blog_group");
      savedAuthor = localStorage.getItem("archive_last_blog_author") || "";
    } catch (_) {}
    const requestedGroup = p.get("blog") || "";
    const knownGroups = new Set(BLOG_GROUP_KEYS);
    let group = requestedGroup ||
      (savedGroup && knownGroups.has(savedGroup) ? savedGroup : curBlogGroup) || "nogizaka";
    if (group === "true" || group === "1" || group === "") group = "nogizaka";
    if (!knownGroups.has(group)) {
      showToast("未知博客分组，已切换到乃木坂46", "error");
      group = "nogizaka";
    }
    // 只要 URL 明确指定博客路由，就不能把上一次的作者筛选偷偷带入。
    const author = p.has("author") ? (p.get("author") || "") :
      (p.has("blog") || rawHash === "blog" || blogId ? "" : savedAuthor);
    const date = p.get("date") || "";
    const q = normalizedQuery(p.get("q"));
    const requestedPage = Math.max(1, parseInt(p.get("page"), 10) || 1);

    if (blogId) {
      // 若当前已经打开了同一篇博客且阅读器处于显示状态，无需重复拉取
      if (currentBlogReaderPost && String(currentBlogReaderPost.id) === String(blogId) && $("blogReader").style.display !== "none") {
        return;
      }
      try {
        const res = await api("/api/archive/blogs?id=" + encodeURIComponent(blogId));
        if (res.ok && res.post) {
          const post = res.post;
          const targetGroup = post.group_key || group || "nogizaka";
          const targetAuthor = post.author || author || "";
          const returnHash = buildBlogHash({
            group: targetGroup, author: targetAuthor, date, query: q, pageNum: requestedPage,
          });
          openBlogReader(post, undefined, returnHash);
          await selectBlogGroup(targetGroup, targetAuthor, false, {
            date, q, page: requestedPage,
          });
        } else {
          showToast("未找到该博客或已被移除", "error");
          await selectBlogGroup(group, author, false, { date, q, page: requestedPage });
        }
      } catch (err) {
        showToast("加载博客失败: " + err.message, "error");
        await selectBlogGroup(group, author, false, { date, q, page: requestedPage });
      }
      return;
    }

    // 未指定博客 ID：若阅读器正开着，关闭它并回到列表
    if ($("blogReader").style.display !== "none") {
      $("blogReader").style.display = "none";
      document.documentElement.classList.remove("modal-open");
      document.body.classList.remove("modal-open");
      document.body.style.overflow = "";
      $("brContent").innerHTML = "";
      currentBlogReaderPost = null;
      blogReaderReturnHash = null;
    }
    await selectBlogGroup(group, author, false, { date, q, page: requestedPage });
    return;
  }

  // 1.5 信件模式：#letter, #letter=...
  if (p.has("letter") || rawHash === "letter") {
    if (window._isArchiveAdmin === false) {
      switchMainTab("msg", true);
      return;
    }
    let saved = null;
    try { saved = localStorage.getItem("archive_last_letter_member"); } catch (_) {}
    const mem = p.get("letter") || (saved && members.some(m => m.name === saved) ? saved : curLetterMember) || getDefaultNogiMember();
    if (mem) {
      await selectLetterMember(mem);
    }
    return;
  }

  // 2. 消息模式：#msg, #member=..., #y=..., #msg_id=...
  if (p.has("member") || p.has("y") || p.has("m") || p.has("msg_id") || p.has("msg") || rawHash === "msg") {
    let saved = null;
    try { saved = localStorage.getItem("archive_last_msg_member"); } catch (_) {}
    const mem = p.get("member") || (saved && members.some(m => m.name === saved) ? saved : curMember) || getDefaultNogiMember();
    const t = p.get("t") || "";
    const q = normalizedQuery(p.get("q"));
    const msgId = p.get("msg_id") || p.get("msg") || "";
    const requestedOrder = p.get("order") || p.get("o");
    if (requestedOrder === "asc" || requestedOrder === "desc") {
      setMessageOrder(requestedOrder, { persist: false, reload: false });
    }
    if (msgId) targetMsgId = String(msgId);
    curType = t;
    searchQuery = q;
    syncSearchInput();
    if (mem) {
      await selectMember(mem, true);
    }
    return;
  }

  // 3. 首页模式（默认无 hash 或 #home）
  if (!rawHash || rawHash === "home") {
    showHome();
    return;
  }

  // 4. 未知非法 Hash 路由：严格跳转至 404 页面
  location.replace("/404?from=" + encodeURIComponent(location.pathname + location.search + location.hash));
}

// ── 启动入口 ─────────────────────────────────────
async function boot() {
  const searchParams = new URLSearchParams(location.search);
  if (searchParams.has("id") || searchParams.has("blog") || searchParams.has("member") || searchParams.has("letter")) {
    const targetHash = searchParams.toString();
    history.replaceState(null, "", location.pathname + "#" + targetHash);
  }

  const p = new URLSearchParams((location.hash || "").replace(/^#/, ""));
  // 极速预处理：如果 URL 包含博客 ID，0ms 同步打开阅读器容器与骨架，彻底消除任何闪烁
  const earlyBlogId = p.get("id") || p.get("blog_id") || p.get("post");
  if (earlyBlogId) {
    if ($("tabHome")) $("tabHome").classList.remove("active");
    if ($("tabBlog")) $("tabBlog").classList.add("active");
    if ($("archiveHome")) $("archiveHome").classList.remove("active");
    const layout = document.querySelector('.layout');
    if (layout) layout.style.display = '';
    if ($("blogGrid")) $("blogGrid").style.display = "";
    if ($("timeline")) $("timeline").style.display = "none";
    const msgTb = document.querySelector(".msg-toolbar");
    if (msgTb) msgTb.style.display = "none";
    const reader = $("blogReader");
    if (reader) {
      reader.style.display = "";
      $("brTitle").textContent = "正在打开博客...";
      $("brContent").innerHTML = '<div class="home-skeleton" style="padding:32px 16px;"><div class="sk-hero" style="height:40px;width:65%;margin-bottom:20px;"></div><div class="sk-strip"><div></div><div></div><div></div></div><div class="sk-msg" style="margin-top:20px;"><div></div><div></div><div></div></div></div>';
      document.documentElement.classList.add("modal-open");
      document.body.classList.add("modal-open");
      document.body.style.overflow = "hidden";
    }
  }

  // 极速预处理：如果 URL 包含信件路由 #letter，0ms 同步切换至信件视图骨架，杜绝页面抖动
  if (p.has("letter") || (location.hash || "").replace(/^#/, "") === "letter") {
    if ($("tabHome")) $("tabHome").classList.remove("active");
    if ($("tabMsg")) $("tabMsg").classList.remove("active");
    if ($("tabBlog")) $("tabBlog").classList.remove("active");
    if ($("tabLetter")) {
      $("tabLetter").classList.add("active");
      $("tabLetter").hidden = false;
      $("tabLetter").style.display = "inline-flex";
    }
    if ($("archiveHome")) $("archiveHome").classList.remove("active");
    const layout = document.querySelector('.layout');
    if (layout) layout.style.display = '';
    if ($("timeline")) $("timeline").style.display = "none";
    if ($("blogGrid")) $("blogGrid").style.display = "none";
    if ($("letterGrid")) $("letterGrid").style.display = "block";
    const msgTb = document.querySelector(".msg-toolbar");
    if (msgTb) msgTb.style.display = "none";
  }

  curType = p.get("t") || "";
  searchQuery = normalizedQuery(p.get("q"));
  syncSearchInput();
  initTypeChips();
  initMessageOrder();

  // 首页数据不依赖成员选择器，直接与认证/成员列表并行，避免首屏串行等待。
  const initialHome = !location.hash || location.hash === "#home";
  const authPromise = initAuth();
  const membersPromise = loadMembers(true);
  const homePromise = initialHome ? showHome() : null;
  await Promise.all([authPromise, membersPromise, homePromise].filter(Boolean));
  if (!initialHome) await handleRoute(true);
}

boot();
// 从浏览器 bfcache 恢复时重新加载内容
window.addEventListener("pageshow", (e) => { if (e.persisted) boot(); });

// hash 变化时统一由 handleRoute 分发视图
window.addEventListener("hashchange", () => {
  if (selfHashUpdate) return;
  handleRoute(false);
});

function customPrompt({ title = "请输入", message = "", placeholder = "", defaultValue = "", icon = "📥", confirmText = "提交", showCheckbox = false, checkText = "" } = {}) {
  return new Promise((resolve) => {
    const modal = $("customPromptModal");
    if (!modal) return resolve(null);
    
    $("pmIcon").textContent = icon;
    $("pmTitle").textContent = title;
    $("pmMessage").textContent = message;
    $("pmConfirm").textContent = confirmText;
    
    const input = $("pmInput");
    input.placeholder = placeholder;
    input.value = defaultValue;
    
    const checkLabel = $("pmCheckLabel");
    const checkbox = $("pmCheckbox");
    if (showCheckbox) {
      checkLabel.style.display = "flex";
      $("pmCheckText").textContent = checkText;
      checkbox.checked = false;
    } else {
      checkLabel.style.display = "none";
    }
    
    modal.style.display = "flex";
    setTimeout(() => { input.focus(); input.select(); }, 60);

    const onConfirm = () => {
      const val = input.value.trim();
      const checked = checkbox.checked;
      cleanup();
      resolve({ value: val, checked: checked });
    };
    const onCancel = () => {
      cleanup();
      resolve(null);
    };
    const onKeydown = (e) => {
      if (e.key === "Enter") onConfirm();
      if (e.key === "Escape") onCancel();
    };
    const cleanup = () => {
      modal.style.display = "none";
      $("pmConfirm").removeEventListener("click", onConfirm);
      $("pmCancel").removeEventListener("click", onCancel);
      input.removeEventListener("keydown", onKeydown);
    };

    $("pmConfirm").addEventListener("click", onConfirm);
    $("pmCancel").addEventListener("click", onCancel);
    input.addEventListener("keydown", onKeydown);
  });
}

async function promptArchiveMember() {
  const result = await customPrompt({
    title: "📥 归档成员博客",
    message: "请输入任意坂道成员博客列表页 URL（支持乃木坂46 / 樱坂46 / 日向坂46）：",
    placeholder: "例：https://sakurazaka46.com/s/s46/diary/blog/list?ima=0000&ct=59",
    icon: "📝",
    confirmText: "开始归档",
    showCheckbox: true,
    checkText: "开启 Gemini AI 中日双语翻译（勾选将较慢）"
  });

  if (!result || !result.value) return;

  try {
    const res = await fetch("/api/archive/blogs/archive_member", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: result.value, translate: result.checked })
    });
    const data = await res.json();
    showToast(data.msg || (data.ok ? "已成功启动后台博客归档任务！" : "操作失败"), data.ok ? "success" : "error");
  } catch(e) {
    showToast("请求异常: " + e, "error");
  }
}

async function promptArchiveMessage() {
  const result = await customPrompt({
    title: "💬 归档成员消息",
    message: "请输入要补全历史消息的成员姓名（留空代表处理全部监控成员）：\n注：系统将自动扫描补全全部历史，已归档的消息自动秒级跳过。",
    placeholder: "例：冨里 奈央（支持多姓名或留空）",
    icon: "💬",
    confirmText: "开始回填消息"
  });

  if (result === null) return;

  try {
    const res = await fetch("/api/archive/messages/backfill", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ member: result.value || "", reset: true })
    });
    const data = await res.json();
    showToast(data.msg || (data.ok ? "已成功启动消息归档回填任务！" : "操作失败"), data.ok ? "success" : "error");
  } catch(e) {
    showToast("请求异常: " + e, "error");
  }
}

// ── 粉丝信件 (Fan Letters) 交互逻辑 ───────────────────────
let curLetterMember = "";
let curLetterImages = [];

function openLetterLightbox(idx) {
  if (idx < 0 || idx >= curLetterImages.length) return;
  images = curLetterImages;
  openLightbox(idx);
}

async function selectLetterMember(mName) {
  curMode = "letter";
  setHtmlViewClass("letter");
  curLetterMember = mName;
  try { localStorage.setItem("archive_last_letter_member", mName); } catch (_) {}
  const mObj = members.find(m => m.name === mName) || { name: mName, display: mName };
  const disp = $("curLetterMemberDisplay");
  if (disp) disp.textContent = mObj.display || mName;

  if ($("tabHome")) $("tabHome").classList.remove("active");
  if ($("tabMsg")) $("tabMsg").classList.remove("active");
  if ($("tabBlog")) $("tabBlog").classList.remove("active");
  if ($("tabLetter")) $("tabLetter").classList.add("active");

  $('archiveHome').classList.remove('active');
  $('backTop').style.display = ''; $('backTop').classList.remove('force-hide');
  document.querySelector('.layout').style.display = '';
  $("timeline").style.display = "none";
  $("blogGrid").style.display = "none";
  if ($("letterGrid")) $("letterGrid").style.display = "block";
  $("archiveSide").style.display = "none";

  const msgTb = document.querySelector(".msg-toolbar");
  if (msgTb) msgTb.style.display = "none";
  const searchTb = $("searchBox") ? $("searchBox").closest(".toolbar") : null;
  if (searchTb) searchTb.style.display = "none";

  const p = new URLSearchParams({ letter: mName });
  selfHashUpdate = true;
  location.hash = p.toString();
  setTimeout(() => { selfHashUpdate = false; }, 0);

  renderLetterMemberPopover();
  await loadLetters(mName);
}

async function loadLetters(mName) {
  const cardsBox = $("letterCards");
  const statsBox = $("letterStats");
  if (!cardsBox) return;

  cardsBox.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:40px;color:var(--muted);"><span class="sync-icon" style="display:inline-block;animation:spin 1s linear infinite;">🔄</span> 正在加载信件...</div>';
  if (statsBox) statsBox.textContent = "";

  try {
    const data = await api("/api/archive/letters?member=" + encodeURIComponent(mName));
    if (!data.ok) {
      cardsBox.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:40px;color:var(--muted);">加载失败：' + esc((data.errors || []).join("; ")) + '</div>';
      return;
    }

    const list = data.letters || [];
    if (statsBox) {
      statsBox.textContent = "共 " + list.length + " 封信件";
    }

    if (!list.length) {
      cardsBox.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:60px 20px;color:var(--muted);background:var(--card);border:1px dashed var(--border);border-radius:16px;">' +
        '<div style="font-size:36px;margin-bottom:12px;">✉️</div>' +
        '<div style="font-size:15px;font-weight:600;color:var(--text-strong);">暂无已归档信件</div>' +
        '<div style="font-size:13px;margin-top:6px;">可点击上方「🔄 同步信件」从官方接口拉取，或在终端运行 <code>python tools/archive_letters.py ' + esc(mName) + '</code></div>' +
        '</div>';
      return;
    }

    curLetterImages = list.map(letter => {
      const u = letter.media_url || letter.file_url || letter.thumbnail_url || "";
      let dStr = letter.created_at || "";
      try {
        const dt = new Date(letter.created_at);
        if (!isNaN(dt.getTime())) {
          dStr = dt.toLocaleDateString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", timeZone: "Asia/Tokyo" });
        }
      } catch (_) {}
      return {
        url: u,
        caption: "【" + (letter.member_name || mName) + " 粉丝信件】" + dStr + " · " + (letter.text || "").slice(0, 50)
      };
    });

    cardsBox.innerHTML = "";
    list.forEach((letter, idx) => {
      const card = document.createElement("div");
      card.className = "letter-card";
      
      let dateStr = letter.created_at || "";
      try {
        const dt = new Date(letter.created_at);
        if (!isNaN(dt.getTime())) {
          dateStr = dt.toLocaleDateString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", timeZone: "Asia/Tokyo" });
        }
      } catch (_) {}

      const imgUrl = letter.media_url || letter.file_url || letter.thumbnail_url || "";
      const favHTML = letter.is_favorite ? '<span class="letter-fav-star" title="已收藏">★</span>' : '';
      
      const textContent = letter.text || "(无正文文字)";
      const textLen = (letter.text || "").length;

      let thumbHTML = '';
      if (imgUrl) {
        thumbHTML = '<div class="letter-thumb-wrap" role="button" tabindex="0" title="点击查看大图">' +
                    '<img src="' + esc(imgUrl) + '" loading="lazy" decoding="async" alt="信纸卡片" onerror="this.parentElement.style.display=\'none\';" />' +
                    '<span class="letter-zoom-hint">🔍 查看大图</span>' +
                    '</div>';
      }

      card.innerHTML = thumbHTML +
        '<div class="letter-body">' +
          '<div class="letter-header-row">' +
            '<span class="letter-date">📅 ' + esc(dateStr) + '</span>' +
            '<div style="display:flex;align-items:center;gap:6px;">' +
              favHTML +
              '<span class="letter-badge-id">#' + esc(letter.id) + ' · ' + textLen + '字</span>' +
            '</div>' +
          '</div>' +
          '<div class="letter-text">' + esc(textContent) + '</div>' +
        '</div>';

      const thumbEl = card.querySelector(".letter-thumb-wrap");
      if (thumbEl) {
        thumbEl.addEventListener("click", () => openLetterLightbox(idx));
        thumbEl.addEventListener("keydown", (e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            openLetterLightbox(idx);
          }
        });
      }

      cardsBox.appendChild(card);
    });
  } catch (err) {
    cardsBox.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:40px;color:var(--muted);">加载异常：' + esc(err) + '</div>';
  }
}

function renderLetterMemberPopover(filterKeyword = "") {
  const list = $("letterMemberPopoverList");
  if (!list) return;
  list.innerHTML = "";
  const kw = filterKeyword.toLowerCase().trim();
  const filtered = members.filter(m => !kw || m.display.toLowerCase().includes(kw) || m.name.toLowerCase().includes(kw));

  if ($("letterMemberTotalBadge")) {
    $("letterMemberTotalBadge").textContent = "共 " + members.length + " 人" + (kw ? " · 匹配 " + filtered.length + " 人" : "");
  }

  if (!filtered.length) {
    const empty = document.createElement("div");
    empty.style.cssText = "text-align:center; padding:20px 0; color:var(--muted); font-size:12.5px;";
    empty.textContent = "未找到匹配成员";
    list.appendChild(empty);
    return;
  }

  // 按坂道分组
  const groups = [
    { key: "nogizaka", name: "乃木坂46", icon: "💜", cls: "nogi" },
    { key: "sakurazaka", name: "樱坂46", icon: "🌸", cls: "sakura" },
    { key: "hinatazaka", name: "日向坂46", icon: "🩵", cls: "hinata" }
  ];

  groups.forEach(g => {
    const grpMems = filtered.filter(m => inferMemberGroup(m) === g.key);
    if (!grpMems.length) return;

    const gHead = document.createElement("div");
    gHead.className = "popover-group-header " + g.cls;
    gHead.innerHTML = '<span>' + g.icon + ' ' + g.name + '</span><span class="pgh-cnt">' + grpMems.length + ' 人</span>';
    list.appendChild(gHead);

    grpMems.forEach(m => {
      let avatarText = (m.display || "").replace(/[\s_　]/g, "");
      if (avatarText.length > 2) avatarText = avatarText.slice(-2);
      if (!avatarText) avatarText = "✉️";
      if (m.name.includes("マネダコ")) avatarText = "🐙";

      let avatarHTML = '';
      if (m.avatar) {
        avatarHTML = '<img class="mpi-avatar-img" src="' + esc(m.avatar) + '" loading="lazy" decoding="async" alt="" onerror="this.style.display=\'none\';if(this.nextElementSibling)this.nextElementSibling.style.display=\'inline-flex\';" /><span class="mpi-avatar ' + g.cls + '" style="display:none;">' + esc(avatarText) + '</span>';
      } else {
        avatarHTML = '<span class="mpi-avatar ' + g.cls + '">' + esc(avatarText) + '</span>';
      }

      const item = document.createElement("div");
      item.className = "member-popover-item " + g.cls + (m.name === curLetterMember ? " active" : "");
      item.innerHTML = '<div class="m-name-txt">' +
                       avatarHTML +
                       '<span class="mpi-name">' + esc(m.display) + '</span>' +
                       '</div>';
      item.addEventListener("click", () => {
        closeLetterMemberPopover();
        selectLetterMember(m.name);
      });
      list.appendChild(item);
    });
  });
}

function toggleLetterMemberPopover() {
  const pop = $("letterMemberPopover");
  const btn = $("btnLetterMemberDropdown");
  if (!pop) return;
  const isOpen = pop.style.display !== "none";
  if (isOpen) {
    pop.style.display = "none";
    if (btn) btn.classList.remove("active");
  } else {
    pop.style.display = "flex";
    if (btn) btn.classList.add("active");
    renderLetterMemberPopover();
  }
}

function closeLetterMemberPopover() {
  const pop = $("letterMemberPopover");
  const btn = $("btnLetterMemberDropdown");
  if (pop) pop.style.display = "none";
  if (btn) btn.classList.remove("active");
}

if ($("btnLetterMemberDropdown")) {
  $("btnLetterMemberDropdown").addEventListener("click", (e) => {
    e.stopPropagation();
    toggleLetterMemberPopover();
  });
}

if ($("letterMemberSearchInput")) {
  $("letterMemberSearchInput").addEventListener("input", (e) => {
    const kw = e.target.value;
    if ($("btnLetterMemberSearchClear")) {
      $("btnLetterMemberSearchClear").style.display = kw ? "block" : "none";
    }
    renderLetterMemberPopover(kw);
  });
}

if ($("btnLetterMemberSearchClear")) {
  $("btnLetterMemberSearchClear").addEventListener("click", () => {
    $("letterMemberSearchInput").value = "";
    $("btnLetterMemberSearchClear").style.display = "none";
    renderLetterMemberPopover("");
  });
}

document.addEventListener("click", (e) => {
  const wrap = $("letterMemberDropdownWrap");
  if (wrap && !wrap.contains(e.target)) {
    closeLetterMemberPopover();
  }
});

if ($("btnSyncLetters")) {
  $("btnSyncLetters").addEventListener("click", async () => {
    if (!curLetterMember) return;
    const btnSync = $("btnSyncLetters");
    btnSync.classList.add("loading");
    btnSync.disabled = true;
    try {
      const resp = await api("/api/archive/letters_sync", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ member: curLetterMember })
      });
      if (resp.ok) {
        if (resp.new > 0) {
          showToast(`信件同步完成！新增 ${resp.new} 封新信件（总计 ${resp.total || 0} 封）。`, "success");
        } else {
          showToast(`信件已是最新（共 ${resp.total || 0} 封），无新增信件。`, "info");
        }
        await loadLetters(curLetterMember);
      } else {
        showToast("同步信件失败: " + (resp.errors || []).join("; "), "error");
      }
    } catch (ex) {
      showToast("同步异常: " + ex, "error");
    } finally {
      btnSync.classList.remove("loading");
      btnSync.disabled = false;
    }
  });
}

