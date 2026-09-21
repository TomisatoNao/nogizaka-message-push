// archive.js
"use strict";
const $ = (id) => document.getElementById(id);
// 清理旧版本遗留的可读 Token；新的 Token 模式使用 HttpOnly 会话 Cookie。
try { localStorage.removeItem("webAdminToken"); } catch (_) {}
const TYPES = [["", "全部"], ["text", "文字"], ["picture", "图片"], ["video", "视频"], ["voice", "语音"]];
const BLOG_GROUP_KEYS = ["nogizaka", "sakurazaka", "hinatazaka"];

let members = [];        // 归档浏览列表：包含所有已有历史数据的成员
let monitorMembers = []; // 回填目标列表：仅来自当前 config.MONITOR_LIST
let blogGroups = [];     // [{key, total, first_date, last_date}]
let curMember = "";
let curBlogGroup = "";   // 非空 = 博客模式
let months = [];         // [{year, month, count}] 新的在前
let curYM = null;        // {year, month}
let curType = "";
let isFavFilter = false; // 是否开启收藏筛选
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

// 前端毫秒级 SWR 瞬时缓存
const memberMonthsCache = new Map();   // `${member}:${type}:${fav}` -> months array
const msgCalendarCache = new Map();    // `${member}:${type}:${fav}:${q}` -> dayCounts
const blogAuthorsCache = new Map();    // groupKey -> authors array
const blogCalendarCache = new Map();   // `${group}:${author}:${q}` -> dayCounts

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

// ── Message 媒体可见性控制 ─────────────────────────
// 媒体只在用户明确点击后播放；滚动离开视野或切到后台时暂停，返回后
// 保持暂停，避免突然出声、持续耗流量。25% 是“仍在阅读区域内”的最低
// 可见比例，IntersectionObserver 不支持时才启用节流后的几何计算兜底。
const ARCHIVE_MEDIA_VISIBILITY_THRESHOLD = 0.25;
let archiveMediaObserver = null;
const observedArchiveMedia = new Set();
let archiveMediaFallbackBound = false;
let archiveMediaFallbackFrame = 0;

function requestArchiveMediaFrame(callback) {
  if (typeof window.requestAnimationFrame === "function") {
    return window.requestAnimationFrame(callback);
  }
  return window.setTimeout(callback, 0);
}

function archiveMediaElements() {
  const timeline = $("timeline");
  return timeline ? timeline.querySelectorAll("video, audio") : [];
}

function pauseArchiveMedia(media) {
  if (!media || typeof media.pause !== "function" || media.paused) return;
  try {
    media.pause();
  } catch (_) {
    // 某些浏览器在页面切换瞬间可能拒绝 pause；不能阻塞其它媒体处理。
  }
}

function pauseAllArchiveMedia() {
  archiveMediaElements().forEach(pauseArchiveMedia);
}

function isArchiveMediaVisible(media) {
  if (!media || typeof media.getBoundingClientRect !== "function") return false;
  const rect = media.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return false;
  const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 0;
  const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 0;
  const visibleWidth = Math.max(0, Math.min(rect.right, viewportWidth) - Math.max(rect.left, 0));
  const visibleHeight = Math.max(0, Math.min(rect.bottom, viewportHeight) - Math.max(rect.top, 0));
  const visibleRatio = (visibleWidth * visibleHeight) / (rect.width * rect.height);
  return visibleRatio >= ARCHIVE_MEDIA_VISIBILITY_THRESHOLD;
}

function checkArchiveMediaVisibility() {
  archiveMediaElements().forEach((media) => {
    if (!isArchiveMediaVisible(media)) pauseArchiveMedia(media);
  });
}

function scheduleArchiveMediaVisibilityCheck() {
  if (archiveMediaFallbackFrame) return;
  archiveMediaFallbackFrame = requestArchiveMediaFrame(() => {
    archiveMediaFallbackFrame = 0;
    checkArchiveMediaVisibility();
  });
}

function bindArchiveMediaFallback() {
  if (archiveMediaFallbackBound) return;
  archiveMediaFallbackBound = true;
  window.addEventListener("scroll", scheduleArchiveMediaVisibilityCheck, { passive: true });
  window.addEventListener("resize", scheduleArchiveMediaVisibilityCheck, { passive: true });
}

if (typeof IntersectionObserver === "function") {
  archiveMediaObserver = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting || entry.intersectionRatio < ARCHIVE_MEDIA_VISIBILITY_THRESHOLD) {
        pauseArchiveMedia(entry.target);
      }
    }
  }, { threshold: [0, ARCHIVE_MEDIA_VISIBILITY_THRESHOLD] });
} else {
  bindArchiveMediaFallback();
}

function observeArchiveMedia(media) {
  if (!media || (media.tagName !== "VIDEO" && media.tagName !== "AUDIO")) return;
  if (observedArchiveMedia.has(media)) return;
  observedArchiveMedia.add(media);
  if (archiveMediaObserver) {
    archiveMediaObserver.observe(media);
  } else {
    scheduleArchiveMediaVisibilityCheck();
  }
}

function clearArchiveMediaObservers(container) {
  for (const media of observedArchiveMedia) {
    if (!container || container.contains(media)) {
      if (archiveMediaObserver) archiveMediaObserver.unobserve(media);
      observedArchiveMedia.delete(media);
    }
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden || document.visibilityState !== "visible") pauseAllArchiveMedia();
});
window.addEventListener("pagehide", pauseAllArchiveMedia);

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
function syncMessageMonthNavigation() {
  const monthNav = $("monthNavGroup");
  if (!monthNav) return;
  const locked = curMode === "msg" && Boolean(searchQuery);
  // 保持顶部月份控件占位，搜索态只禁用交互，避免页面跳动。
  monthNav.hidden = false;
  monthNav.setAttribute("aria-disabled", String(locked));
  const prev = $("prevMonth");
  const next = $("nextMonth");
  const select = $("monthSelect");
  if (select) select.disabled = locked;
  if (locked) {
    if (prev) prev.disabled = true;
    if (next) next.disabled = true;
    return;
  }
  const monthIndex = currentMessageMonthIndex();
  if (prev) prev.disabled = monthIndex < 0 || monthIndex >= months.length - 1;
  if (next) next.disabled = monthIndex < 0 || monthIndex <= 0;
}
function syncSearchInput() {
  $("searchBox").value = searchQuery;
  $("searchClear").hidden = !searchQuery;
  // 搜索结果跨越全部月份，时间线月份控件保留布局但不允许切换。
  // 日历仍然独立显示搜索命中的日期，并允许用户跨月浏览。
  syncMessageMonthNavigation();
}
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
  clearArchiveMediaObservers($("timeline"));
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
    if (curMode !== "msg") return;
    if (pageLoading || page >= totalPages) return;
    const scrollBottom = window.innerHeight + window.scrollY;
    const docHeight = document.documentElement.scrollHeight;
    if (docHeight - scrollBottom < 350) {
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
  if (curMode !== "msg" || !curMember) return;
  const version = ++calendarVersion;
  if (calendarAbort) calendarAbort.abort();
  calendarAbort = new AbortController();

  const calCacheKey = `${curMember}:${curType}:${isFavFilter ? 1 : 0}:${searchQuery || ""}`;
  if (msgCalendarCache.has(calCacheKey)) {
    dayCounts = msgCalendarCache.get(calCacheKey) || {};
    if (searchQuery) syncSearchCalendarMonth();
    renderCalendar();
  }

  try {
    let calUrl = "/api/archive/calendar?member=" + encodeURIComponent(curMember) + "&type=" + curType;
    if (isFavFilter) calUrl += "&favorite=1";
    if (searchQuery) calUrl += "&q=" + encodeURIComponent(searchQuery);
    const data = await api(calUrl, { signal: calendarAbort.signal });
    if (version !== calendarVersion) return;
    dayCounts = data.ok ? data.days : {};
    msgCalendarCache.set(calCacheKey, dayCounts);
  } catch (e) {
    if (e.name === "AbortError" || version !== calendarVersion) return;
    if (!msgCalendarCache.has(calCacheKey)) {
      dayCounts = {};
    }
  }
  if (version !== calendarVersion) return;
  if (searchQuery) syncSearchCalendarMonth();
  renderCalendar();
}

function hasCalendarMatchesInMonth(year, month) {
  const prefix = year + "-" + String(month).padStart(2, "0") + "-";
  return Object.keys(dayCounts).some((key) => key.startsWith(prefix) && Number(dayCounts[key]) > 0);
}

function syncSearchCalendarMonth() {
  if ((curMode !== "msg" && curMode !== "blog") || !searchQuery || !dayCounts || !Object.keys(dayCounts).length) return;
  ensureCalendarMonth();
  if (hasCalendarMatchesInMonth(calYM.year, calYM.month)) return;
  const latest = Object.keys(dayCounts)
    .filter((key) => Number(dayCounts[key]) > 0)
    .sort()
    .pop();
  if (!latest) return;
  const [year, month] = latest.split("-").map(Number);
  calYM = { year, month };
}

async function loadBlogCalendar() {
  if (curMode !== "blog" || !curBlogGroup) return;
  const version = ++calendarVersion;
  if (calendarAbort) calendarAbort.abort();
  calendarAbort = new AbortController();

  const cacheKey = `${curBlogGroup}:${curBlogAuthor || ""}:${searchQuery || ""}`;
  if (blogCalendarCache.has(cacheKey)) {
    dayCounts = blogCalendarCache.get(cacheKey) || {};
    blogCalendarError = "";
    if (searchQuery) {
      syncSearchCalendarMonth();
    } else if (!curBlogDate && (!calYM || Object.keys(dayCounts).length > 0)) {
      const keys = Object.keys(dayCounts).sort();
      if (keys.length > 0) {
        const latest = keys[keys.length - 1];
        const [y, m] = latest.split("-").map(Number);
        calYM = { year: y, month: m };
      }
    }
    renderCalendar();
  }

  try {
    let url = "/api/archive/blog_calendar?group=" + encodeURIComponent(curBlogGroup) +
              "&author=" + encodeURIComponent(curBlogAuthor || "");
    if (searchQuery) url += "&q=" + encodeURIComponent(searchQuery);
    const data = await api(url, { signal: calendarAbort.signal });
    if (version !== calendarVersion) return;
    if (!data.ok || typeof data.days !== "object") throw new Error("博客日历接口返回无效数据");
    blogCalendarError = "";
    dayCounts = data.days || {};
    blogCalendarCache.set(cacheKey, dayCounts);
  } catch (e) {
    if (e.name === "AbortError" || version !== calendarVersion) return;
    if (!blogCalendarCache.has(cacheKey)) {
      dayCounts = {};
      blogCalendarError = "日历暂时不可用，请稍后重试";
    }
  }
  if (version !== calendarVersion) return;
  
  if (searchQuery) {
    syncSearchCalendarMonth();
  } else if (!curBlogDate && (!calYM || Object.keys(dayCounts).length > 0)) {
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
  const entryNoun = curMode === "blog" ? "篇博客" : (searchQuery ? "条匹配消息" : "条消息");
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
      : (curMode === "blog" ? (searchQuery ? "本月无匹配博客" : "本月无博客") : (searchQuery ? "本月无匹配消息" : "本月无消息")));
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
  const keepMessageSearch = curMode === "msg" && Boolean(searchQuery);
  const keepBlogSearch = curMode === "blog" && Boolean(searchQuery);
  if (!keepMessageSearch && !keepBlogSearch) searchQuery = "";
  syncSearchInput();

  if (curMode === "blog") {
    // 博客模式下的日期跳转：重置页码为 1，切换/锁定指定日期（搜索模式下保留关键词）
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
  if (!document.querySelector('.day-sep[data-date="' + dateKey + '"]') && curType && !keepMessageSearch) {
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
  root.classList.remove("view-home", "view-msg", "view-blog", "view-letter", "view-gallery");
  if (mode) root.classList.add("view-" + mode);
}

function syncNavTabs(activeTabName) {
  const tabs = {
    home: $("tabHome"),
    msg: $("tabMsg"),
    blog: $("tabBlog"),
    gallery: $("tabGallery"),
    letter: $("tabLetter")
  };
  Object.entries(tabs).forEach(([k, el]) => {
    if (el) el.classList.toggle("active", k === activeTabName);
  });
}

let _membersLoadPromise = null;
function ensureMembersLoaded(skipSelect = true) {
  if (members && members.length) return Promise.resolve();
  if (!_membersLoadPromise) {
    _membersLoadPromise = loadMembers(skipSelect).finally(() => {
      _membersLoadPromise = null;
    });
  }
  return _membersLoadPromise;
}

let _auxiliaryPreloadStarted = false;
function scheduleAuxiliaryPreload() {
  if (_auxiliaryPreloadStarted) return;
  _auxiliaryPreloadStarted = true;
  const run = () => {
    loadBlogGroupChips();
    loadGalleryMembers();
  };
  if (typeof requestIdleCallback === "function") {
    requestIdleCallback(run, { timeout: 3500 });
  } else {
    setTimeout(run, 1200);
  }
}

async function switchMainTab(mode, keepHash) {
  curMode = mode;
  setHtmlViewClass(mode);
  try { localStorage.setItem("archive_last_main_tab", mode); } catch (_) {}
  syncNavTabs(mode);
  if (mode !== "msg") {
    if ($("emptyHint")) $("emptyHint").hidden = true;
    if ($("loadMore")) $("loadMore").hidden = true;
  }

  if (mode === "home") {
    if (!keepHash) goHome();
  } else if (mode === "msg") {
    _enterMemberMode();
    if (!members.length) {
      await ensureMembersLoaded(true);
    }
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
  } else if (mode === "gallery") {
    if (!galleryMembers.length) {
      loadGalleryMembers();
    }
    let saved = null;
    try { saved = localStorage.getItem("archive_last_gallery_member"); } catch (_) {}
    const activeList = galleryMembers.length ? galleryMembers : members;
    const wanted = (saved !== null && (saved === "" || activeList.some(m => m.name === saved || m.display === saved)))
      ? saved
      : (curGalleryMember || "");
    selectGalleryMember(wanted);
  } else if (mode === "blog") {
    if (!blogGroups.length) {
      loadBlogGroupChips();
    }
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
    if (!members.length) {
      await ensureMembersLoaded(true);
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
if ($("tabMsg")) {
  $("tabMsg").addEventListener("mouseenter", () => ensureMembersLoaded(true), { once: true });
  $("tabMsg").addEventListener("click", () => switchMainTab("msg"));
}
if ($("tabBlog")) {
  $("tabBlog").addEventListener("mouseenter", () => loadBlogGroupChips(), { once: true });
  $("tabBlog").addEventListener("click", () => switchMainTab("blog"));
}
if ($("tabGallery")) {
  $("tabGallery").addEventListener("mouseenter", () => loadGalleryMembers(), { once: true });
  $("tabGallery").addEventListener("click", () => switchMainTab("gallery"));
}
if ($("tabLetter")) $("tabLetter").addEventListener("click", () => switchMainTab("letter"));

// ── 数据加载 ─────────────────────────────────────
async function loadMembers(skipSelect = false) {
  const data = await api("/api/archive/members");
  if (!data.ok) { showEmpty("加载失败：" + (data.errors || []).join("；")); return; }
  members = data.members;
  monitorMembers = Array.isArray(data.monitor_members)
    ? data.monitor_members.filter((member) => member && member.name)
    : [];
  if (!members.length) {
    showEmpty("还没有任何归档。确认 config.json 的 archive.enabled 已开启，" +
              "新消息会自动归档；历史消息用 python tools/backfill_archive.py 回填。");
    return;
  }
  // 渲染成员 chips & popover
  renderMemberChips();
  renderMemberPopover("");
  
  // 闲时或按需异步预热相册成员与博客分组，避免开屏堵塞 HTTP 管道
  scheduleAuxiliaryPreload();

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

// 成员选择器、信件和相册共用坂道分组折叠状态。
// 默认全部展开；仅在当前标签页内记忆，避免一次折叠永久影响后续访问。
const POPOVER_GROUP_STATE_KEY = "archive_popover_collapsed_groups_v1";
const POPOVER_GROUP_KEYS = new Set(["nogizaka", "sakurazaka", "hinatazaka", "yodel", "other"]);

function loadCollapsedPopoverGroups() {
  try {
    const raw = sessionStorage.getItem(POPOVER_GROUP_STATE_KEY);
    if (!raw) return new Set();
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return new Set();
    return new Set(parsed.filter(key => POPOVER_GROUP_KEYS.has(key)));
  } catch (_) {
    // 隐私模式、禁用存储或旧数据异常时回退到默认全部展开。
    return new Set();
  }
}

function persistCollapsedPopoverGroups() {
  try {
    sessionStorage.setItem(POPOVER_GROUP_STATE_KEY, JSON.stringify([...collapsedPopoverGroups]));
  } catch (_) {
    // 存储不可用不影响当前页面内的折叠操作。
  }
}

const collapsedPopoverGroups = loadCollapsedPopoverGroups();

function updatePopoverExpandAllControl(scope, groupKeys) {
  const control = document.querySelector('.popover-expand-all[data-popover-scope="' + scope + '"]');
  if (!control) return;
  const canExpand = (groupKeys || []).some(key => collapsedPopoverGroups.has(key));
  control.hidden = !canExpand;
  control.setAttribute("aria-hidden", String(!canExpand));
}

function expandPopoverGroups(scope) {
  const groupKeys = {
    msg: ["nogizaka", "sakurazaka", "hinatazaka", "yodel", "other"],
    letter: ["nogizaka", "sakurazaka", "hinatazaka"],
    gallery: ["nogizaka", "sakurazaka", "hinatazaka"],
  }[scope] || [];
  let changed = false;
  groupKeys.forEach(key => {
    if (collapsedPopoverGroups.delete(key)) changed = true;
  });
  if (!changed) return;
  persistCollapsedPopoverGroups();

  if (scope === "msg") {
    renderMemberPopover($("memberSearchInput")?.value || "");
  } else if (scope === "letter") {
    renderLetterMemberPopover($("letterMemberSearchInput")?.value || "");
  } else if (scope === "gallery") {
    renderGalleryMemberPopover($("galleryMemberSearchInput")?.value || "");
  }
}
function createPopoverGroupHeader(group, count, onToggle) {
  const collapsed = collapsedPopoverGroups.has(group.key);
  const head = document.createElement("button");
  head.type = "button";
  head.className = "popover-group-header " + group.cls;
  head.setAttribute("aria-expanded", String(!collapsed));
  head.setAttribute("aria-label", group.name + (collapsed ? "，展开成员" : "，收起成员"));
  head.innerHTML = '<span class="pgh-title">' + group.icon + ' ' + group.name + '</span>' +
                   '<span class="pgh-meta"><span class="pgh-cnt">' + count + ' 人</span>' +
                   '<span class="pgh-chevron" aria-hidden="true">▾</span></span>';
  head.addEventListener("click", (event) => {
    // 重绘会替换当前按钮节点；阻止旧节点继续冒泡到文档级“点击外部关闭”监听。
    event.stopPropagation();
    if (collapsedPopoverGroups.has(group.key)) collapsedPopoverGroups.delete(group.key);
    else collapsedPopoverGroups.add(group.key);
    persistCollapsedPopoverGroups();
    onToggle();
  });
  return { head, collapsed };
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
    updatePopoverExpandAllControl("msg", []);
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

  const visibleGroupKeys = [];
  groups.forEach(g => {
    const grpMems = filtered.filter(m => {
      const gK = inferMemberGroup(m);
      return g.key === "other" ? (!gK || !["nogizaka", "sakurazaka", "hinatazaka", "yodel"].includes(gK)) : gK === g.key;
    });

    if (!grpMems.length) return;
    visibleGroupKeys.push(g.key);

    const groupHeader = createPopoverGroupHeader(g, grpMems.length, () => renderMemberPopover(filterKeyword));
    list.appendChild(groupHeader.head);
    if (groupHeader.collapsed) return;

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
  updatePopoverExpandAllControl("msg", visibleGroupKeys);
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
  hideMessageMonthFooter();
  syncNavTabs("msg");

  $('archiveHome').classList.remove('active');
  $('backTop').style.display = ''; $('backTop').classList.remove('force-hide');
  document.querySelector('.layout').style.display = '';
  $("archiveSide").style.display = "";
  $("blogGrid").style.display = "none";
  if ($("letterGrid")) $("letterGrid").style.display = "none";
  if ($("galleryGrid")) $("galleryGrid").style.display = "none";
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
    const normalizeMemberKey = (value) => String(value || "").replace(/[\s_　]/g, "");
    const curKey = normalizeMemberKey(curMember);
    const curObj = members.find(m =>
      m.name === curMember ||
      normalizeMemberKey(m.name) === curKey ||
      normalizeMemberKey(m.display) === curKey
    );
    if ($("curMemberDisplay")) $("curMemberDisplay").textContent = curObj ? curObj.display : curMember;
    if ($("curMemberCount")) {
      $("curMemberCount").textContent = curObj ? "（" + (curObj.total || 0).toLocaleString() + "）" : "";
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
  hideMessageMonthFooter();
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
  syncNavTabs("blog");

  const requestedPage = Math.max(1, parseInt(routeState.page, 10) || 1);
  if (updateHash) syncBlogHash(1);

  $('archiveHome').classList.remove('active');
  $('backTop').style.display = ''; $('backTop').classList.remove('force-hide');
  document.querySelector('.layout').style.display = '';
  $("timeline").style.display = "none";
  $("blogGrid").style.display = "";
  if ($("letterGrid")) $("letterGrid").style.display = "none";
  if ($("galleryGrid")) $("galleryGrid").style.display = "none";
  $("archiveSide").style.display = "";

  const msgTb = document.querySelector(".msg-toolbar");
  if (msgTb) msgTb.style.display = "none";
  const searchTb = $("searchBox") ? $("searchBox").closest(".toolbar") : null;
  if (searchTb) searchTb.style.display = "";
  $("tagToggle").parentElement.style.display = "none";
  
  // 三路完全并行并发：作者列表、日历分布、文章卡片首屏并发，彻底消除瀑布流白屏
  await Promise.all([
    loadBlogAuthors(key, selectionVersion),
    loadBlogCalendar(),
    loadBlogPage(requestedPage, updateHash),
  ]);
}

async function loadBlogAuthors(key, selectionVersion = blogSelectionVersion) {
  // SWR: 命中内存缓存时立即同步渲染，消除切换等待
  if (blogAuthorsCache.has(key)) {
    curGroupAuthors = blogAuthorsCache.get(key) || [];
    renderBlogAuthorChips();
    renderBlogAuthorPopover("");
    updateBlogAuthorDisplay();
  }
  try {
    const data = await api("/api/archive/blog_authors?group=" + encodeURIComponent(key));
    if (selectionVersion !== blogSelectionVersion || curMode !== "blog" || curBlogGroup !== key) return;
    if (!data.ok || !Array.isArray(data.authors)) throw new Error("博客作者接口返回无效数据");
    curGroupAuthors = data.authors.filter(a => a && a.name && a.name.trim());
    blogAuthorsCache.set(key, curGroupAuthors);
    renderBlogAuthorChips();
    renderBlogAuthorPopover("");
    updateBlogAuthorDisplay();
  } catch (e) {
    if (e.name === "AbortError" || selectionVersion !== blogSelectionVersion) return;
    if (!blogAuthorsCache.has(key)) {
      curGroupAuthors = [];
      renderBlogAuthorChips();
      renderBlogAuthorPopover("");
      updateBlogAuthorDisplay();
    }
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
    const authorCount = Number(a.total ?? a.count ?? 0);
    const cntStr = '<span class="chip-num" title="' + authorCount.toLocaleString() + ' 篇博客">' + (authorCount >= 1000 ? (authorCount / 1000).toFixed(1).replace(/\.0$/, '') + 'k' : authorCount) + '</span>';
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
    const authorCount = Number(a.total ?? a.count ?? 0);
    const cntTxt = authorCount.toLocaleString() + ' 篇';
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
  const normalizedAuthor = (curBlogAuthor || "").replace(/[\s　_]+/g, "");
  const count = curBlogAuthor
    ? Number((curGroupAuthors.find(a => a.name === curBlogAuthor || (a.name || "").replace(/[\s　_]+/g, "") === normalizedAuthor) || {}).total || 0)
    : Number((blogGroups.find(g => g.key === curBlogGroup) || {}).total || 0);
  if ($("curBlogAuthorCount")) $("curBlogAuthorCount").textContent = "（" + count.toLocaleString() + "）";
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

function _getBlogThumbUrl(url) {
  if (!url) return "";
  if (url.startsWith("/api/archive/blog_media/") || url.startsWith("/api/archive/media/")) {
    return url + (url.includes("?") ? "&thumb=1" : "?thumb=1");
  }
  return url;
}

function renderBlogHero(post) {
  const hero = $("blogHero");
  hero.style.display = "block";
  hero.dataset.date = (post.date || "").substring(0, 10);
  const dateStr = (post.date || "").substring(0, 16);

  // 列表接口只返回摘要和封面；正文在打开详情时按需加载。
  let bodyHtml = post.body_html || "";
  let coverUrl = post.cover || _getCoverUrl(bodyHtml);
  let coverThumbUrl = _getBlogThumbUrl(coverUrl);
  const excerptText = post.excerpt || bodyHtml.replace(/<[^>]+>/g, "");

  let coverHtml = '';
  if (coverUrl) {
    coverHtml = '<div class="bh-cover" style="background-image: url(\'' + esc(coverThumbUrl) + '\')"><img src="' + esc(coverThumbUrl) + '" data-full-src="' + esc(coverUrl) + '" data-orig-src="' + esc(post.cover_original || "") + '" loading="lazy" decoding="async" alt=""></div>';
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

  // 封面图加载失败：先回退到本地原图，若仍失败回退到官方远程，最后降级为 📝 占位
  const heroCoverImg = hero.querySelector('.bh-cover img');
  if (heroCoverImg) {
    heroCoverImg.addEventListener('error', () => {
      const fullSrc = heroCoverImg.dataset.fullSrc;
      if (fullSrc && heroCoverImg.src !== fullSrc && heroCoverImg.dataset.triedFull !== "1") {
        heroCoverImg.dataset.triedFull = "1";
        heroCoverImg.src = fullSrc;
        const coverBox = hero.querySelector('.bh-cover');
        if (coverBox) coverBox.style.backgroundImage = 'url(\'' + esc(fullSrc) + '\')';
        return;
      }
      if (post.cover_original && heroCoverImg.dataset.fallback !== "1") {
        heroCoverImg.dataset.fallback = "1";
        heroCoverImg.src = post.cover_original;
        const coverBox = hero.querySelector('.bh-cover');
        if (coverBox) coverBox.style.backgroundImage = 'url(\'' + esc(post.cover_original) + '\')';
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
  const coverThumbUrl = _getBlogThumbUrl(coverUrl);

  const card = document.createElement("div");
  card.className = "bmc-card blog-card-mini";
  card.dataset.date = (post.date || "").substring(0, 10);

  let html = '';
  if (coverUrl) {
    html += '<div class="bc-cover"><img src="' + esc(coverThumbUrl) + '" data-full-src="' + esc(coverUrl) + '" data-orig-src="' + esc(post.cover_original || "") + '" alt="" loading="lazy"></div>';
  } else {
    // 无封面链接：保留原有无封面样式（📝 占位）
    html += '<div class="bc-cover no-pic">📝</div>';
  }

  let excerpt = "";
  if (searchQuery) {
    const fullText = post.excerpt || "";
    const lowerText = fullText.toLowerCase();
    const terms = searchQuery.split(/\s+/).filter(Boolean);
    const hasMatch = terms.some(t => lowerText.includes(t.toLowerCase()));
    if (hasMatch) {
      excerpt = '<div class="bc-excerpt">' + highlightQuery(fullText, searchQuery) + '</div>';
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

  // 缩略图加载失败：先回退到本地原图，若仍失败回退到官方远程，最后降级为 📝 占位
  const coverImg = card.querySelector('.bc-cover img');
  if (coverImg) {
    coverImg.addEventListener('error', () => {
      const fullSrc = coverImg.dataset.fullSrc;
      if (fullSrc && coverImg.src !== fullSrc && coverImg.dataset.triedFull !== "1") {
        coverImg.dataset.triedFull = "1";
        coverImg.src = fullSrc;
        return;
      }
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
let blogReaderSavedScroll = 0;

function restoreWindowScroll(pos) {
  if (typeof pos === "number" && pos > 0) {
    window.scrollTo({ top: pos, behavior: "instant" });
    requestAnimationFrame(() => {
      window.scrollTo({ top: pos, behavior: "instant" });
      setTimeout(() => {
        const cur = window.scrollY || document.documentElement.scrollTop || 0;
        if (Math.abs(cur - pos) > 10) {
          window.scrollTo({ top: pos, behavior: "instant" });
        }
      }, 50);
    });
  }
}

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
  const blogImages = Array.from(brImgs).map(img => ({
    url: img.src,
    caption: currentBlogReaderPost.title || "",
    source: "blog",
    blogId: currentBlogReaderPost.id,
    groupKey: currentBlogReaderPost.group_key || curBlogGroup || "nogizaka",
  }));
  brImgs.forEach((img, idx) => {
    img.style.cursor = "zoom-in";
    img.onerror = () => handleImgError(img);
    if (img.complete && img.naturalWidth === 0) {
      handleImgError(img);
    }
    img.onclick = () => {
      images = blogImages;
      openLightbox(idx, img, null, img.src);
    };
  });

  updateModeSelectorUI();
  if (searchQuery && $("blogReader") && $("blogReader").style.display !== "none") {
    highlightBlogReaderSearch(false);
  }
}

function highlightBlogReaderSearch(scrollIntoView = true) {

  if (!searchQuery) return;

  const contentDiv = $("brContent");

  if (!contentDiv) return;

  const terms = searchQuery.split(/\s+/).map(t => t.trim()).filter(Boolean);

  if (!terms.length) return;

  const re = new RegExp("(" + terms.map(escRegex).join("|") + ")", "gi");

  const walker = document.createTreeWalker(contentDiv, NodeFilter.SHOW_TEXT, {

    acceptNode: (n) => {

      if (!n.nodeValue || !n.nodeValue.trim()) return NodeFilter.FILTER_REJECT;

      const parent = n.parentNode;

      if (parent && (parent.nodeName === "SCRIPT" || parent.nodeName === "STYLE" || parent.classList?.contains("br-search-target"))) {

        return NodeFilter.FILTER_REJECT;

      }

      return NodeFilter.FILTER_ACCEPT;

    }

  }, false);



  const textNodes = [];

  let curr;

  while ((curr = walker.nextNode())) {

    re.lastIndex = 0;

    if (re.test(curr.nodeValue)) {

      textNodes.push(curr);

    }

  }



  let firstMark = null;

  textNodes.forEach(node => {

    const parent = node.parentNode;

    if (!parent) return;

    const parts = node.nodeValue.split(re);

    if (parts.length <= 1) return;

    const frag = document.createDocumentFragment();

    parts.forEach(part => {

      if (!part) return;

      re.lastIndex = 0;

      if (re.test(part)) {

        const mark = document.createElement("mark");

        mark.className = "br-search-target";

        mark.style.background = "var(--accent-soft)";

        mark.style.color = "var(--accent)";

        mark.textContent = part;

        frag.appendChild(mark);

        if (!firstMark) firstMark = mark;

      } else {

        frag.appendChild(document.createTextNode(part));

      }

    });

    parent.replaceChild(frag, node);

  });



  if (scrollIntoView && firstMark) {

    setTimeout(() => {

      const reader = $("blogReader");

      if (reader && firstMark) {

        const topPos = firstMark.getBoundingClientRect().top + reader.scrollTop - (window.innerHeight / 2);

        reader.scrollTo({ top: Math.max(0, topPos), behavior: "smooth" });

      }

    }, 100);

  }

}



function openBlogReader(post, bodyHtml, returnHash) {
  const readerWasHidden = $("blogReader").style.display === "none";
  if (readerWasHidden && !blogReaderSavedScroll) {
    blogReaderSavedScroll = window.scrollY || document.documentElement.scrollTop || document.body.scrollTop || 0;
  }
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
      highlightBlogReaderSearch(true);
    }, 100);
  }
}

function closeBlogReader() {
  const savedScroll = blogReaderSavedScroll;
  $("blogReader").style.display = "none";
  document.documentElement.classList.remove("modal-open");
  document.body.classList.remove("modal-open");
  document.body.style.overflow = "";
  $("brContent").innerHTML = "";
  currentBlogReaderPost = null;
  if (typeof handleBackTopScroll === "function") handleBackTopScroll();

  restoreWindowScroll(savedScroll);

  // 恢复打开前的路由：首页卡片关闭后必须回到首页，而不是博客列表。
  const returnHash = blogReaderReturnHash !== null
    ? blogReaderReturnHash
    : buildBlogHash({ pageNum: page });
  blogReaderReturnHash = null;
  writeArchiveHash(returnHash);
  // writeArchiveHash 会抑制同一轮 hashchange；主动分发一次，确保视觉状态与 URL 一致。
  setTimeout(() => handleRoute(false, savedScroll), 0);
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
    const opener = document.activeElement;

    modal.style.display = "flex";

    const onConfirm = () => {
      cleanup();
      resolve(true);
    };
    const onCancel = () => {
      cleanup();
      resolve(false);
    };
    const onKeydown = (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCancel();
      }
    };
    const cleanup = () => {
      modal.style.display = "none";
      $("cmConfirm").removeEventListener("click", onConfirm);
      $("cmCancel").removeEventListener("click", onCancel);
      document.removeEventListener("keydown", onKeydown);
      if (opener && opener !== document.body && document.contains(opener)) opener.focus();
    };

    $("cmConfirm").addEventListener("click", onConfirm);
    $("cmCancel").addEventListener("click", onCancel);
    document.addEventListener("keydown", onKeydown);
    setTimeout(() => $("cmConfirm")?.focus(), 0);
  });
}

function showToast(msg, type = "info") {
  let container = $("toastContainer");
  if (!container) {
    container = document.createElement("div");
    container.id = "toastContainer";
    container.className = "toast-container";
    document.body.appendChild(container);
  }
  const normType = (type === "ok" || type === "success") ? "success" : (type === "error" ? "error" : "info");
  let icon = normType === "success" ? "✅" : (normType === "error" ? "❌" : "");
  if (!icon) {
    const hasEmoji = /^\p{Emoji}/u.test(String(msg).trim());
    if (!hasEmoji) {
      icon = "ℹ️";
    }
  }
  const toast = document.createElement("div");
  toast.className = `custom-toast ${normType}`;
  toast.innerHTML = (icon ? `<span>${icon}</span>` : '') + `<span>${esc(msg)}</span>`;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateY(-10px)";
    setTimeout(() => toast.remove(), 300);
  }, 2500);
}

function legacyCopyText(text) {
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.top = "0";
  textarea.style.left = "-9999px";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);

  const selection = document.getSelection ? document.getSelection() : null;
  const ranges = [];
  if (selection) {
    for (let i = 0; i < selection.rangeCount; i += 1) {
      ranges.push(selection.getRangeAt(i));
    }
    selection.removeAllRanges();
  }

  textarea.focus({ preventScroll: true });
  textarea.select();
  textarea.setSelectionRange(0, textarea.value.length);

  let copied = false;
  try {
    copied = typeof document.execCommand === "function" && document.execCommand("copy");
  } catch (_) {
    copied = false;
  } finally {
    textarea.remove();
    if (selection) {
      selection.removeAllRanges();
      ranges.forEach((range) => selection.addRange(range));
    }
  }
  return copied;
}

async function copyTextToClipboard(text) {
  if (typeof navigator !== "undefined" && navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) {
      // Permission-denied or unavailable Clipboard API: try the user-gesture fallback below.
    }
  }
  return legacyCopyText(text);
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

async function loadMonths(preserveSelected = true) {
  if (!curMember) return [];
  const version = memberVersion;
  const cacheKey = `${curMember}:${curType}:${isFavFilter ? 1 : 0}`;

  if (memberMonthsCache.has(cacheKey)) {
    months = memberMonthsCache.get(cacheKey) || [];
    syncMessageMonthFooter();
    const sel = $("monthSelect");
    const prevVal = sel ? sel.value : "";
    if (sel) {
      sel.innerHTML = "";
      for (const m of months) {
        const opt = document.createElement("option");
        opt.value = m.year + "-" + m.month;
        opt.textContent = m.year + " 年 " + m.month + " 月（" + m.count + "）";
        sel.appendChild(opt);
      }
      if (preserveSelected && prevVal && months.some(m => (m.year + "-" + m.month) === prevVal)) {
        sel.value = prevVal;
      } else if (curYM && months.some(m => (m.year + "-" + m.month) === (curYM.year + "-" + curYM.month))) {
        sel.value = curYM.year + "-" + curYM.month;
      }
    }
    syncMessageMonthNavigation();
  }

  try {
    let mUrl = "/api/archive/months?member=" + encodeURIComponent(curMember);
    if (curType) mUrl += "&type=" + encodeURIComponent(curType);
    if (isFavFilter) mUrl += "&favorite=1";
    const data = await api(mUrl);
    if (version !== memberVersion) return [];
    months = data.ok ? data.months : [];
    memberMonthsCache.set(cacheKey, months);
    syncMessageMonthFooter();
    const sel = $("monthSelect");
    const prevVal = sel ? sel.value : "";
    if (sel) {
      sel.innerHTML = "";
      for (const m of months) {
        const opt = document.createElement("option");
        opt.value = m.year + "-" + m.month;
        opt.textContent = m.year + " 年 " + m.month + " 月（" + m.count + "）";
        sel.appendChild(opt);
      }
      if (preserveSelected && prevVal && months.some(m => (m.year + "-" + m.month) === prevVal)) {
        sel.value = prevVal;
      } else if (curYM && months.some(m => (m.year + "-" + m.month) === (curYM.year + "-" + curYM.month))) {
        sel.value = curYM.year + "-" + curYM.month;
      }
    }
    syncMessageMonthNavigation();
    return months;
  } catch (e) {
    if (e.name === "AbortError" || version !== memberVersion) return [];
    syncMessageMonthNavigation();
    return memberMonthsCache.get(cacheKey) || [];
  }
}

async function selectMember(name, keepHash) {
  const version = ++memberVersion;
  curMode = "msg";
  ++blogSelectionVersion;
  if (blogPageAbort) blogPageAbort.abort();
  _enterMemberMode();
  curMember = name;
  try { localStorage.setItem("archive_last_msg_member", name); } catch (_) {}
  curBlogGroup = "";     // 切换到成员模式，清空博客分组
  hideMessageMonthFooter();
  if (!members.length) {
    await ensureMembersLoaded(true);
  }
  syncChipHighlight();  // 同步 chip 高亮
  if (!keepHash) {
    searchQuery = "";
  }
  syncSearchInput();

  const cacheKey = `${name}:${curType}:${isFavFilter ? 1 : 0}`;
  const cachedMonths = memberMonthsCache.get(cacheKey);
  const wanted = keepHash ? readHashYM() : null;
  const initialPick = (wanted && cachedMonths ? cachedMonths.find((m) => m.year === wanted.year && m.month === wanted.month) : null) || wanted || (cachedMonths && cachedMonths.length ? cachedMonths[0] : null);

  loadCalendar();   // 后台拉全档按天计数（SQLite 极速原生聚合返回，不阻塞时间线）

  if (initialPick) {
    // 命中预判/缓存/URL Hash：并行发起月份列表校验与第一页消息加载，节省一次网络 RTT 串行等待
    const monthsPromise = loadMonths(false);
    await selectMonth(initialPick.year, initialPick.month);
    if (version !== memberVersion) return;
    const loadedMonths = await monthsPromise;
    if (version !== memberVersion) return;
    if (!loadedMonths.length) {
      hideMessageMonthFooter();
      resetContent();
      $("stats").textContent = "";
      $("emptyHint").textContent = "成员「" + name + "」还没有归档内容。请从成员列表重新选择。";
      $("emptyHint").hidden = false;
      return;
    }
  } else {
    // 首次冷访问该成员且无 Hash：串行加载月份列表后再选定最新月
    const loadedMonths = await loadMonths(false);
    if (version !== memberVersion) return;
    if (!loadedMonths.length) {
      hideMessageMonthFooter();
      resetContent();
      $("stats").textContent = "";
      $("emptyHint").textContent = "成员「" + name + "」还没有归档内容。请从成员列表重新选择。";
      $("emptyHint").hidden = false;
      return;
    }
    const pick = loadedMonths[0];
    await selectMonth(pick.year, pick.month);
  }
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
  // 深链接打开时保留目标消息 ID，确保新标签页加载和刷新后仍可精确定位。
  if (targetMsgId) p.set("msg_id", targetMsgId);
  if (curType) p.set("t", curType);
  if (isFavFilter) p.set("fav", "1");
  if (searchQuery) p.set("q", searchQuery);
  // asc 不是默认值时写入路由，分享链接能恢复用户的阅读顺序；desc 保持旧链接简洁。
  if (messageOrder !== MESSAGE_ORDER_DEFAULT) p.set("order", messageOrder);
  writeArchiveHash(p.toString());
}

function currentMessageMonthIndex() {
  if (!curYM) return -1;
  return months.findIndex((m) => m.year === curYM.year && m.month === curYM.month);
}

function hideMessageMonthFooter() {
  const footer = $("messageMonthFooter");
  if (footer) footer.hidden = true;
}

function syncMessageMonthFooter() {
  const footer = $("messageMonthFooter");
  if (!footer) return;

  const monthIndex = currentMessageMonthIndex();
  const visible = curMode === "msg" && !!curMember && !searchQuery && monthIndex >= 0;
  footer.hidden = !visible;
  if (!visible) return;

  $("messageMonthFooterCurrent").textContent = curYM.year + " 年 " + curYM.month + " 月";
  $("prevMonthBottom").disabled = monthIndex >= months.length - 1;
  $("nextMonthBottom").disabled = monthIndex <= 0;
}

function navigateAdjacentMonth(offset, { scrollToTop = false } = {}) {
  if (curMode === "msg" && searchQuery) return;
  const monthIndex = currentMessageMonthIndex();
  const target = monthIndex >= 0 ? months[monthIndex + offset] : null;
  if (!target) return;

  selectMonth(target.year, target.month);
  if (scrollToTop) {
    try {
      window.scrollTo({ top: 0, behavior: "instant" });
    } catch (_) {
      window.scrollTo(0, 0);
    }
  }
}


async function selectMonth(year, month) {
  curYM = { year, month };
  calYM = { year, month };
  renderCalendar();
  $("monthSelect").value = year + "-" + month;
  syncMessageMonthFooter();
  const idx = currentMessageMonthIndex();
  $("prevMonth").disabled = idx >= months.length - 1;
  $("nextMonth").disabled = idx <= 0;
  syncMessageMonthNavigation();
  resetContent();
  if (!curBlogGroup) syncHash();
  await loadPage();
}

async function loadPage() {
  if (curMode !== "msg" || !curMember) return;
  const version = contentVersion;
  contentAbort = new AbortController();
  setPageLoading(true);
  const favParam = isFavFilter ? "&favorite=1" : "";
  const url = searchQuery
    ? "/api/archive/search?member=" + encodeURIComponent(curMember) +
      "&q=" + encodeURIComponent(searchQuery) +
      "&type=" + curType +
      favParam +
      "&order=" + messageOrder + "&page=" + page + "&per_page=50"
    : "/api/archive/messages?member=" + encodeURIComponent(curMember) +
      "&year=" + curYM.year + "&month=" + curYM.month +
      "&type=" + curType +
      favParam +
      "&order=" + messageOrder + "&page=" + page + "&per_page=50";
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
    const typeMap = { text: "文字", picture: "图片", video: "视频", voice: "语音" };
    const filterParts = [];
    if (curType && typeMap[curType]) filterParts.push(typeMap[curType]);
    if (isFavFilter) filterParts.push("已收藏");
    const filterDesc = filterParts.join(" · ");
    const filterSuffix = filterDesc ? "（" + filterDesc + "）" : "";
    $("stats").textContent = curYM.year + "/" + curYM.month + filterSuffix + " · " + data.total + " 条 · " + messageOrderLabel();
    if (!data.messages.length && page === 1) {
      showEmpty("本月没有" + (filterDesc ? filterDesc + "的" : "") + "消息");
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
  syncMessageMonthFooter();
  resetContent();
  if (curMode === "blog") {
    loadBlogCalendar();
    loadBlogPage(1, updateHash);
    return;
  }
  if (curMode !== "msg") return;
  if (updateHash) syncHash();
  if (!searchQuery) {
    loadCalendar();
    if (curYM) { selectMonth(curYM.year, curYM.month); return; }
    return;
  }
  loadCalendar();
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
  syncMessageMonthFooter();
  if (curMode === "blog") {
    loadBlogCalendar();
    loadBlogPage(1, true);
    return;
  }
  if (curMode !== "msg") return;
  loadCalendar();
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

  let favHtml = "";
  if (window._isLoggedIn) {
    const isFav = Boolean(msg.is_favorite);
    favHtml =
      '<button type="button" class="msg-action-btn fav-btn' + (isFav ? ' active' : '') + '" title="' + (isFav ? '取消收藏' : '收藏') + '" aria-label="收藏">' +
        '<span class="btn-icon">' + (isFav ? '⭐' : '☆') + '</span>' +
      '</button>';
  }

  let html = '<div class="msg-header">' +
    '<div class="msg-meta-left">' +
      '<span class="pub-time" title="官方审核发布时间 (JST): ' + fmtCopyTime(msg.published_at) + '">' + pubTimeStr + '</span>' +
      '<span class="msg-type-pill type-' + esc(msg.type) + '">' + esc(msg.type) + '</span>' +
      uploadBadgeHtml +
    '</div>' +
    '<div class="msg-meta-right">' +
      jumpHtml +
      copyHtml +
      favHtml +
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
      images.push({
        url: url,
        caption: (msg.text || "").slice(0, 80),
        source: "message",
        messageId: msg.id,
        memberName: msg.member_name || curMember,
        memberDir: msg.member_dir || curMember,
        year: msg.year || (curYM && curYM.year),
        month: msg.month || (curYM && curYM.month),
      });
      html += '<img loading="lazy"' + dim + ' data-lb="' + (images.length - 1) + '" src="' + esc(url) + '" alt="">';
    }
  } else if (msg.download_failed) {
    html += '<div class="miss">⚠️ 媒体文件下载失败 ' +
      (window._isArchiveAdmin ? '<button type="button" class="btn small retry-dl-btn" style="margin-left:8px; padding:2px 8px; font-size:12px; vertical-align:middle;">🔄 重试下载</button>' : '（可用回填工具重试）') +
      '</div>';
  }
  const isCanceled = msg.state === "canceled" || (!msg.text && !msg.translation && !msg.media_url && !msg.download_failed);
  if (isCanceled) {
    b.classList.add("is-canceled");
    html += '<div class="canceled-msg-hint" style="padding:10px 14px; font-size:13px; color:var(--muted); display:flex; align-items:center; gap:8px; font-style:italic;">' +
      '<span style="font-size:15px; opacity:0.8;">🚫</span><span>该消息已被发送者撤回</span>' +
      '</div>';
  } else {
    if (msg.text) html += '<div class="text">' + formatMessageText(msg.text, searchQuery) + "</div>";
    if (msg.translation) html += '<div class="trans">' + formatMessageText(msg.translation, searchQuery) + "</div>";
  }
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
              images.push({
                url: url,
                caption: (msg.text || "").slice(0, 80),
                source: "message",
                messageId: msg.id,
                memberName: msg.member_name || curMember,
                memberDir: msg.member_dir || curMember,
                year: msg.year || (curYM && curYM.year),
                month: msg.month || (curYM && curYM.month),
              });
              mediaEl = document.createElement("img");
              mediaEl.loading = "lazy";
              mediaEl.dataset.lb = String(images.length - 1);
              mediaEl.src = url;
            }
            missDiv.replaceWith(mediaEl);
            if (mediaEl.tagName === "VIDEO" || mediaEl.tagName === "AUDIO") {
              observeArchiveMedia(mediaEl);
            }
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

  if (window._isLoggedIn) {
    const favBtn = b.querySelector(".fav-btn");
    if (favBtn) {
      favBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        toggleMessageFavorite(msg, favBtn);
      });
    }
  }

  const copyBtn = b.querySelector(".copy-btn");
  if (copyBtn) {
    copyBtn.addEventListener("click", async () => {
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
      try {
        const copied = await copyTextToClipboard(textToCopy);
        if (copied) {
          showToast("📋 已复制整条消息与译文", "success");
        } else {
          showToast("⚠️ 当前浏览器禁止复制，请手动选择文本复制", "error");
        }
      } catch (err) {
        showToast("⚠️ 复制失败：" + (err && err.message ? err.message : "未知错误"), "error");
      }
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
  b.querySelectorAll("video, audio").forEach(observeArchiveMedia);
}

// toast notifications unified in showToast(msg, type)


// ── 灯箱手势与缩放交互控制 ─────────────────────────
let lbIndex = 0;
let lbImageLoadVersion = 0;
let lbScale = 1;
let lbPanX = 0;
let lbPanY = 0;
let lbIsPinching = false;
let lbIsDragging = false;
let lbStartDist = 0;
let lbStartScale = 1;
let lbStartPanX = 0;
let lbStartPanY = 0;
let lbStartX = 0;
let lbStartY = 0;
let lbTouchStartTime = 0;
let lbMoved = false;
let lbLastTapTime = 0;
let lbSingleTapTimer = null;
let lbLastTouchEndTime = 0;
let lbSuppressClickUntil = 0;
let lbTouchOnImage = false;
// 导航只允许发生在当前图片已完成加载后；加载中的预览/空白区域不能触发切图。
let lbImageReady = false;
let lbTouchCanNavigate = false;

function setLightboxNavigationReady(ready) {
  lbImageReady = Boolean(ready);
  const canShowNav = lbImageReady && images.length > 1;
  ["lbPrev", "lbNext"].forEach((id) => {
    const btn = $(id);
    if (!btn) return;
    btn.style.display = canShowNav ? "" : "none";
    btn.disabled = !canShowNav;
    btn.setAttribute("aria-disabled", String(!canShowNav));
  });
}

function applyLightboxTransform(animate = false) {
  const img = $("lbImg");
  if (!img) return;
  if (animate) {
    img.style.transition = "transform 0.24s cubic-bezier(0.2, 0, 0.2, 1)";
  } else {
    img.style.transition = "none";
  }
  if (lbScale === 1 && lbPanX === 0 && lbPanY === 0) {
    img.style.transform = "";
  } else {
    img.style.transform = `translate3d(${lbPanX}px, ${lbPanY}px, 0) scale(${lbScale})`;
  }
}

function resetLightboxTransform(animate = false) {
  lbScale = 1;
  lbPanX = 0;
  lbPanY = 0;
  lbIsPinching = false;
  lbIsDragging = false;
  lbMoved = false;
  if (lbSingleTapTimer) {
    clearTimeout(lbSingleTapTimer);
    lbSingleTapTimer = null;
  }
  applyLightboxTransform(animate);
}

function clampLightboxPan(animate = true) {
  const img = $("lbImg");
  if (!img) return;
  if (lbScale <= 1.02) {
    lbScale = 1;
    lbPanX = 0;
    lbPanY = 0;
    applyLightboxTransform(animate);
    return;
  }
  const rect = img.getBoundingClientRect();
  const unscaledW = img.offsetWidth || (rect.width / lbScale);
  const unscaledH = img.offsetHeight || (rect.height / lbScale);
  const scaledW = unscaledW * lbScale;
  const scaledH = unscaledH * lbScale;
  const vw = window.innerWidth;
  const vh = window.innerHeight;

  const maxPanX = Math.max(0, (scaledW - vw) / 2 + 30);
  const maxPanY = Math.max(0, (scaledH - vh) / 2 + 30);

  lbPanX = Math.min(Math.max(lbPanX, -maxPanX), maxPanX);
  lbPanY = Math.min(Math.max(lbPanY, -maxPanY), maxPanY);
  applyLightboxTransform(animate);
}

function zoomLightboxAtPoint(clientX, clientY, targetScale) {
  const img = $("lbImg");
  if (!img) return;
  if (targetScale <= 1.02) {
    resetLightboxTransform(true);
    return;
  }
  const cx = window.innerWidth / 2;
  const cy = window.innerHeight / 2;
  const x = (typeof clientX === "number") ? clientX : cx;
  const y = (typeof clientY === "number") ? clientY : cy;
  const dx = cx - x;
  const dy = cy - y;

  lbScale = targetScale;
  lbPanX = dx * (targetScale - 1) * 0.5;
  lbPanY = dy * (targetScale - 1) * 0.5;
  clampLightboxPan(true);
}

function resetMobileViewport() {
  const meta = document.querySelector('meta[name="viewport"]');
  if (!meta) return;
  const original = meta.getAttribute("content") || "width=device-width, initial-scale=1";
  // 临时注入 maximum-scale=1 强制移动端 Safari / Chrome 视口平滑复位至 1.0
  meta.setAttribute("content", "width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no");
  if (window.visualViewport && window.visualViewport.scale > 1.01) {
    if (window.scrollX !== 0) window.scrollTo(0, window.scrollY);
  }
  setTimeout(() => {
    meta.setAttribute("content", original);
  }, 250);
}

function buildLightboxSourceAction(item) {
  if (!item || !item.source) return null;
  const params = new URLSearchParams();
  let label = "";
  let title = "";

  if (item.source === "blog" && item.blogId) {
    params.set("blog", item.groupKey || "nogizaka");
    params.set("id", String(item.blogId));
    label = "📄 前往对应博客";
    title = "在新标签页打开对应博客";
  } else if (
    item.source === "message" && item.messageId &&
    (item.memberDir || item.memberName) &&
    Number(item.year) > 0 && Number(item.month) > 0
  ) {
    // message_name 可能是带空格/展示用的名称，成员路由必须优先使用
    // messages.member_dir，才能让新标签页稳定命中同一个成员。
    params.set("member", item.memberDir || item.memberName);
    params.set("y", String(item.year));
    params.set("m", String(item.month));
    params.set("msg_id", String(item.messageId));
    label = "💬 前往对应消息";
    title = "在新标签页打开并定位到对应消息";
  } else {
    return null;
  }

  const url = new URL(window.location.href);
  url.hash = params.toString();
  return { href: url.toString(), label, title };
}

function syncLightboxSourceAction(item) {
  const btn = $("lbSourceBtn");
  if (!btn) return;
  const action = buildLightboxSourceAction(item);
  if (!action) {
    btn.removeAttribute("href");
    btn.removeAttribute("title");
    btn.textContent = "";
    btn.style.display = "none";
    return;
  }
  btn.href = action.href;
  btn.textContent = action.label;
  btn.title = action.title;
  btn.style.display = "inline-flex";
}

function openLightbox(i, opener, caption, placeholderUrl) {
  const currentVersion = ++lbImageLoadVersion;
  if (typeof i === "string") {
    if (opener) lightboxOpener = opener;
    resetLightboxTransform(false);
    setLightboxNavigationReady(false);
    lbTouchOnImage = false;
    lbTouchCanNavigate = false;
    document.body.style.overflow = "hidden";
    document.documentElement.classList.add("lightbox-open");
    $("lbImg").src = i;
    $("lbImg").alt = caption || "图片预览";
    $("lbImg").classList.remove("lb-preview");
    $("lbImg").classList.add("lb-full");
    $("lbCounter").style.display = "none";
    $("lbPrev").style.display = "none";
    $("lbNext").style.display = "none";
    syncLightboxSourceAction(null);
    if ($("lbDownloadBtn")) $("lbDownloadBtn").style.display = "inline-flex";
    if ($("lbStatus")) $("lbStatus").style.display = "none";
    $("lightbox").classList.add("open");
    $("lightbox").setAttribute("aria-hidden", "false");
    $("lbClose").focus();
    return;
  }
  const idx = Number(i);
  if (isNaN(idx) || idx < 0 || idx >= images.length) return;
  if (opener) lightboxOpener = opener;
  resetLightboxTransform(false);
  setLightboxNavigationReady(false);
  lbTouchOnImage = false;
  lbTouchCanNavigate = false;
  document.body.style.overflow = "hidden";
  document.documentElement.classList.add("lightbox-open");
  lbIndex = idx;
  const item = images[idx];
  const targetUrl = item.url;
  const targetPlaceholder = placeholderUrl || item.thumbUrl || "";

  // 1. 设置来源跳转与原图下载入口。来源在新标签页打开，当前相册浏览状态保持不变。
  syncLightboxSourceAction(item);
  if ($("lbDownloadBtn")) {
    $("lbDownloadBtn").style.display = targetUrl ? "inline-flex" : "none";
  }

  $("lbImg").alt = item.caption || "归档图片";

  // 2. 优先使用当前已加载的缩略图立即占位展示（0ms秒开，彻底杜绝上一张旧图残留与黑屏等待）
  if (targetPlaceholder) {
    $("lbImg").src = targetPlaceholder;
    $("lbImg").classList.add("lb-preview");
    $("lbImg").classList.remove("lb-full");
  } else {
    $("lbImg").removeAttribute("src");
    $("lbImg").classList.remove("lb-preview", "lb-full");
  }

  // 3. 后台加载原图并平滑替换，确保 100% 触发且无事件竞态遗漏
  if (targetUrl) {
    if (targetUrl !== targetPlaceholder && $("lbStatus")) {
      $("lbStatus").innerHTML = '<span class="sync-icon" style="display:inline-block;animation:spin 1s linear infinite;">🔄</span> 正在加载高清原图...';
      $("lbStatus").style.display = "inline-flex";
    }

    const preloader = new Image();
    const onDone = () => {
      if (currentVersion !== lbImageLoadVersion) return;
      $("lbImg").src = targetUrl;
      $("lbImg").classList.remove("lb-preview");
      $("lbImg").classList.add("lb-full");
      setLightboxNavigationReady(true);
      if ($("lbStatus") && targetUrl !== targetPlaceholder) {
        $("lbStatus").innerHTML = '✓ 已加载高清原图';
        setTimeout(() => {
          if (currentVersion === lbImageLoadVersion && $("lbStatus")) {
            $("lbStatus").style.display = "none";
          }
        }, 1200);
      } else if ($("lbStatus")) {
        $("lbStatus").style.display = "none";
      }
    };

    preloader.onload = onDone;
    preloader.onerror = () => {
      if (currentVersion !== lbImageLoadVersion) return;
      // 原图失败时仍保留当前缩略图，但不再把失败的加载状态当成切图竞态。
      setLightboxNavigationReady(true);
      if ($("lbStatus")) {
        $("lbStatus").innerHTML = '⚠️ 原图加载受阻，当前显示预览图';
        setTimeout(() => {
          if (currentVersion === lbImageLoadVersion && $("lbStatus")) {
            $("lbStatus").style.display = "none";
          }
        }, 2500);
      }
    };

    // 先绑定事件回调再赋值 src，杜绝内存缓存同步完成导致事件丢失。
    preloader.src = targetUrl;
    if (preloader.complete && preloader.naturalWidth > 0) {
      onDone();
    }
  } else {
    // 没有独立原图地址时，当前缩略图就是唯一可用图片；仍允许正常切图。
    setLightboxNavigationReady(true);
  }

  if (images.length > 1) {
    $("lbCounter").style.display = "";
    $("lbCounter").textContent = (idx + 1) + " / " + images.length;
  } else {
    $("lbCounter").style.display = "none";
  }
  $("lightbox").classList.add("open");
  $("lightbox").setAttribute("aria-hidden", "false");
  $("lbClose").focus();
}
function closeLightbox() {
  const box = $("lightbox");
  if (!box.classList.contains("open")) return;
  // Android Chrome 会在 touchend 后补发一次 click；灯箱若已关闭，该 click
  // 会落到下面的相册卡片，造成“点击背景却打开另一张图”。仅在触摸关闭窗口
  // 的短时间内拦截下一次合成 click，不影响键盘或正常鼠标操作。
  if (Date.now() - lbLastTouchEndTime < 700) {
    lbSuppressClickUntil = Date.now() + 700;
  }
  box.classList.remove("open");
  box.setAttribute("aria-hidden", "true");
  if ($("blogReader") && $("blogReader").style.display !== "none") {
    document.body.style.overflow = "hidden";
  } else {
    document.body.style.overflow = "";
  }
  document.documentElement.classList.remove("lightbox-open");
  resetLightboxTransform(false);
  setLightboxNavigationReady(false);
  lbTouchOnImage = false;
  lbTouchCanNavigate = false;
  resetMobileViewport();
  ++lbImageLoadVersion;
  if ($("lbImg")) {
    $("lbImg").removeAttribute("src");
    $("lbImg").classList.remove("lb-preview", "lb-full");
  }
  if ($("lbStatus")) $("lbStatus").style.display = "none";
  if (lightboxOpener && document.contains(lightboxOpener)) lightboxOpener.focus();
  lightboxOpener = null;
}
function lbMove(delta) {
  if (!lbImageReady) return;
  const next = lbIndex + delta;
  if (next < 0 || next >= images.length) return;
  resetLightboxTransform(false);
  openLightbox(next);
}

const lbBox = $("lightbox");
if (lbBox) {
  lbBox.addEventListener("touchstart", (e) => {
    if (!lbBox.classList.contains("open")) return;
    if (e.target.closest("#lbActions, #lbPrev, #lbNext")) {
      lbTouchOnImage = false;
      lbTouchCanNavigate = false;
      return;
    }
    // 只有命中真实 <img> 元素且当前图片已加载完成，才允许缩放、拖拽或左右切图；
    // 灯箱其它空白区域始终是“返回相册”的安全点击区。lbStage 本身不接收指针事件，
    // 这样 Chrome Android 桌面站点在图片加载期间也不会把空白误判为图片。
    const image = $("lbImg");
    const firstTouch = e.touches[0];
    const pointTarget = firstTouch ? document.elementFromPoint(firstTouch.clientX, firstTouch.clientY) : null;
    lbTouchOnImage = e.target === image && pointTarget === image;
    lbTouchCanNavigate = lbTouchOnImage && lbImageReady;

    if (e.touches.length === 2) {
      lbIsPinching = lbTouchCanNavigate;
      lbIsDragging = false;
      lbMoved = lbTouchCanNavigate;
      lbStartDist = Math.hypot(
        e.touches[0].clientX - e.touches[1].clientX,
        e.touches[0].clientY - e.touches[1].clientY
      );
      lbStartScale = lbScale;
      lbStartPanX = lbPanX;
      lbStartPanY = lbPanY;
      if (lbSingleTapTimer) {
        clearTimeout(lbSingleTapTimer);
        lbSingleTapTimer = null;
      }
    } else if (e.touches.length === 1) {
      lbIsPinching = false;
      lbStartX = e.touches[0].clientX;
      lbStartY = e.touches[0].clientY;
      lbStartPanX = lbPanX;
      lbStartPanY = lbPanY;
      lbTouchStartTime = Date.now();
      lbMoved = false;
      lbIsDragging = lbTouchCanNavigate && (lbScale > 1.05);
    }
  }, { passive: false });

  lbBox.addEventListener("touchmove", (e) => {
    if (!lbBox.classList.contains("open")) return;
    if (e.target.closest("#lbActions, #lbPrev, #lbNext")) return;

    // 关键：杜绝移动端浏览器对底层页面的全局视口缩放与滚动
    e.preventDefault();

    if (!lbTouchCanNavigate) return;

    if (lbIsPinching && e.touches.length === 2) {
      const curDist = Math.hypot(
        e.touches[0].clientX - e.touches[1].clientX,
        e.touches[0].clientY - e.touches[1].clientY
      );
      if (lbStartDist > 0) {
        const factor = curDist / lbStartDist;
        lbScale = Math.min(Math.max(lbStartScale * factor, 0.85), 4.5);
        applyLightboxTransform(false);
      }
    } else if (e.touches.length === 1) {
      const dx = e.touches[0].clientX - lbStartX;
      const dy = e.touches[0].clientY - lbStartY;
      if (Math.hypot(dx, dy) > 8) {
        lbMoved = true;
      }
      if (lbIsDragging) {
        lbPanX = lbStartPanX + dx;
        lbPanY = lbStartPanY + dy;
        applyLightboxTransform(false);
      }
    }
  }, { passive: false });

  lbBox.addEventListener("touchend", (e) => {
    if (!lbBox.classList.contains("open")) return;
    if (e.target.closest("#lbActions, #lbPrev, #lbNext")) return;
    lbLastTouchEndTime = Date.now();

    if (lbIsPinching) {
      if (e.touches.length === 0) {
        lbIsPinching = false;
        if (lbScale < 1.05) {
          resetLightboxTransform(true);
        } else if (lbScale > 4.0) {
          lbScale = 4.0;
          clampLightboxPan(true);
        } else {
          clampLightboxPan(true);
        }
      }
      return;
    }

    if (e.touches.length === 0) {
      if (!lbTouchOnImage) {
        // 空白区域的轻微横向位移不能被误判为上一张/下一张。
        lbIsDragging = false;
        closeLightbox();
        lbTouchOnImage = false;
        return;
      }
      if (!lbTouchCanNavigate) {
        // 图片仍在加载时，点击图片本身不关闭、不切换，等待当前图完成即可。
        lbIsDragging = false;
        lbTouchOnImage = false;
        lbTouchCanNavigate = false;
        return;
      }
      if (!lbMoved) {
        // 轻触点击识别
        const touch = e.changedTouches[0];
        const isBackdrop = (e.target === lbBox);
        if (isBackdrop) {
          if (lbSingleTapTimer) {
            clearTimeout(lbSingleTapTimer);
            lbSingleTapTimer = null;
          }
          closeLightbox();
          return;
        }

        const now = Date.now();
        if (now - lbLastTapTime < 300) {
          // 双击快速缩放 / 复位
          if (lbSingleTapTimer) {
            clearTimeout(lbSingleTapTimer);
            lbSingleTapTimer = null;
          }
          lbLastTapTime = 0;
          if (lbScale > 1.2) {
            resetLightboxTransform(true);
          } else {
            zoomLightboxAtPoint(touch ? touch.clientX : 0, touch ? touch.clientY : 0, 2.5);
          }
        } else {
          // 单击图片：延迟 240ms，若无后续点击则关闭灯箱回到相册
          lbLastTapTime = now;
          lbSingleTapTimer = setTimeout(() => {
            lbSingleTapTimer = null;
            closeLightbox();
          }, 240);
        }
      } else if (lbIsDragging) {
        lbIsDragging = false;
        clampLightboxPan(true);
      } else if (lbScale <= 1.05) {
        // 1x 状态下单指滑动切图与下拉关闭
        const dx = e.changedTouches[0].clientX - lbStartX;
        const dy = e.changedTouches[0].clientY - lbStartY;
        const dt = Date.now() - lbTouchStartTime;
        if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5 && dt < 400) {
          if (dx < 0) lbMove(1);
          else lbMove(-1);
        } else if (dy > 120 && Math.abs(dy) > Math.abs(dx) * 1.5 && dt < 400) {
          closeLightbox();
        }
      }
      lbTouchOnImage = false;
      lbTouchCanNavigate = false;
    }
  }, { passive: false });

  lbBox.addEventListener("touchcancel", () => {
    lbTouchOnImage = false;
    lbTouchCanNavigate = false;
    if (lbScale < 1.05) resetLightboxTransform(true);
    else clampLightboxPan(true);
  });

  lbBox.addEventListener("gesturestart", (e) => e.preventDefault());
  lbBox.addEventListener("gesturechange", (e) => e.preventDefault());
  lbBox.addEventListener("gestureend", (e) => e.preventDefault());
}

// 必须使用捕获阶段：合成 click 的目标已经是灯箱下面的相册卡片，事件不会再经过
// #lightbox 自身，所以仅在灯箱上的 click 监听器里无法阻止穿透。
document.addEventListener("click", (e) => {
  if (Date.now() > lbSuppressClickUntil) return;
  lbSuppressClickUntil = 0;
  e.preventDefault();
  e.stopPropagation();
}, true);

window.addEventListener("resize", () => {
  if ($("lightbox") && $("lightbox").classList.contains("open")) {
    clampLightboxPan(false);
  }
});

$("lightbox").addEventListener("click", (e) => {
  if (Date.now() - lbLastTouchEndTime < 500) return;
  // 操作栏与左右箭头是交互控件；仅灯箱背景或真实图片本身可以关闭。
  // stage 可能在部分移动浏览器中成为事件目标，不能把它误当成背景，
  // 否则加载中的原图点击会把事件继续交给底层相册卡片。
  const interactive = e.target.closest("#lbActions, #lbPrev, #lbNext");
  const onImage = e.target === $("lbImg");
  const isBackdrop = e.target === e.currentTarget;
  if (!interactive && (isBackdrop || onImage)) closeLightbox();
});

if ($("lbImg")) {
  $("lbImg").addEventListener("dblclick", (e) => {
    e.stopPropagation();
    if (lbScale > 1.2) {
      resetLightboxTransform(true);
    } else {
      zoomLightboxAtPoint(e.clientX, e.clientY, 2.5);
    }
  });
}

$("lightbox").addEventListener("wheel", (e) => {
  if (!$("lightbox").classList.contains("open")) return;
  if (e.target.closest("#lbActions")) return;
  e.preventDefault();
  const delta = e.deltaY < 0 ? 0.25 : -0.25;
  const nextScale = Math.min(Math.max(lbScale + delta, 1), 4.5);
  if (nextScale <= 1.02) {
    resetLightboxTransform(true);
  } else {
    lbScale = nextScale;
    clampLightboxPan(true);
  }
}, { passive: false });
if ($("lbActions")) {
  $("lbActions").addEventListener("click", (e) => e.stopPropagation());
}
if ($("lbDownloadBtn")) {
  $("lbDownloadBtn").addEventListener("click", (e) => {
    e.stopPropagation();
    const item = images[lbIndex];
    const url = item ? item.url : ($("lbImg") ? $("lbImg").src : "");
    if (!url) return;
    const a = document.createElement("a");
    a.href = url;
    let filename = url.split("/").pop() || "photo.jpg";
    if (filename.includes("?")) filename = filename.split("?")[0];
    if (item && item.caption) {
      const safeCaption = item.caption.replace(/[\\/:*?"<>|\r\n\t]/g, "_").slice(0, 30);
      if (safeCaption) filename = safeCaption + "_" + filename;
    }
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    showToast("💾 正在下载原图: " + filename);
  });
}
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
      const focusable = [$("lbSourceBtn"), $("lbDownloadBtn"), $("lbClose"), $("lbPrev"), $("lbNext")].filter(el => el && el.style.display !== "none" && !el.disabled);
      if (focusable.length) {
        const index = focusable.indexOf(document.activeElement);
        e.preventDefault();
        focusable[(index + (e.shiftKey ? focusable.length - 1 : 1)) % focusable.length].focus();
      }
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
  if (curMode === "msg" && searchQuery && (e.key === "[" || e.key === "]")) return;
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
// ── 复合筛选协同更新（月份选择器、日历与消息列表全同步） ─────────────
async function refreshFilteredView() {
  page = 1;
  resetContent();
  syncHash();
  // 并行更新月份下拉框计数值与日历标记
  await Promise.all([
    loadMonths(true),
    loadCalendar(),
  ]);
  if (searchQuery) {
    startSearch(searchQuery, false);
  } else {
    await loadPage();
  }
}

// ── 消息收藏互动控制（仅登录用户可见与操作） ─────────────────
async function toggleMessageFavorite(msg, btn) {
  if (!window._isLoggedIn) return;
  const currentVal = Boolean(msg.is_favorite);
  const newVal = !currentVal;

  btn.classList.add("anim-pop");
  setTimeout(() => btn.classList.remove("anim-pop"), 400);

  msg.is_favorite = newVal;
  btn.classList.toggle("active", newVal);
  btn.title = newVal ? "取消收藏" : "收藏";
  const iconEl = btn.querySelector(".btn-icon");
  if (iconEl) iconEl.textContent = newVal ? "⭐" : "☆";

  try {
    const res = await api("/api/archive/message/interaction", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message_id: msg.id,
        member: curMember || msg.member || "",
        action: "favorite",
        value: newVal,
      }),
    });
    if (res && res.ok) {
      msg.is_favorite = Boolean(res.is_favorite);
      showToast(newVal ? "⭐ 已加入收藏" : "已取消收藏", "ok");
      // 实时同步更新月份选择器条数与日历标记，清空旧缓存
      msgCalendarCache.clear();
      memberMonthsCache.clear();
      loadMonths(true);
      loadCalendar();
    } else {
      msg.is_favorite = currentVal;
      btn.classList.toggle("active", currentVal);
      btn.title = currentVal ? "取消收藏" : "收藏";
      if (iconEl) iconEl.textContent = currentVal ? "⭐" : "☆";
      showToast("操作失败：" + ((res && res.errors) ? res.errors.join("；") : "网络异常"), "error");
    }
  } catch (e) {
    msg.is_favorite = currentVal;
    btn.classList.toggle("active", currentVal);
    if (iconEl) iconEl.textContent = currentVal ? "⭐" : "☆";
    showToast("操作失败：" + e.message, "error");
  }
}

function initInteractionChips() {
  const chipFav = $("chipFav");
  if (chipFav) {
    chipFav.addEventListener("click", () => {
      isFavFilter = !isFavFilter;
      chipFav.classList.toggle("active", isFavFilter);
      refreshFilteredView();
    });
  }
}

function initTypeChips() {
  const box = $("typeChips");
  for (const [val, label] of TYPES) {
    const b = document.createElement("button");
    b.className = "chip" + (val === curType ? " active" : "");
    b.textContent = label;
    b.addEventListener("click", () => {
      curType = val;
      box.querySelectorAll(".chip").forEach((c, i) => c.classList.toggle("active", TYPES[i][0] === val));
      refreshFilteredView();
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
  if (curMode === "msg" && searchQuery) return;
  const [y, m] = $("monthSelect").value.split("-").map(Number);
  selectMonth(y, m);
});
$("prevMonth").addEventListener("click", () => {
  navigateAdjacentMonth(1);
});
$("nextMonth").addEventListener("click", () => {
  navigateAdjacentMonth(-1);
});
$("prevMonthBottom").addEventListener("click", () => {
  navigateAdjacentMonth(1, { scrollToTop: true });
});
$("nextMonthBottom").addEventListener("click", () => {
  navigateAdjacentMonth(-1, { scrollToTop: true });
});
$("loadMore").addEventListener("click", () => {
  if (curMode !== "msg") return;
  if (pageLoading || page >= totalPages) return;
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
    promptArchiveGroups();
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
document.querySelectorAll(".popover-expand-all").forEach((control) => {
  control.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    expandPopoverGroups(control.dataset.popoverScope || "");
  });
});
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
        const ichips = $("interactionChips");
        if (ichips) { ichips.hidden = false; ichips.style.display = "inline-flex"; }
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
        const ichips = $("interactionChips");
        if (ichips) { ichips.hidden = false; ichips.style.display = "inline-flex"; }
        $("whoami").textContent = me.user.username;
        if ($("userMenuName")) $("userMenuName").textContent = "👤 " + me.user.username;
        if ($("userMenuRole")) $("userMenuRole").textContent = me.user.role === "admin" ? "系统管理员" : "普通用户";
        if ($("userDropdown")) { $("userDropdown").hidden = false; $("userDropdown").style.display = "inline-block"; }
        $("logoutBtn").hidden = false;
        $("logoutBtn").style.display = "flex";
        if ($("changePwBtn")) { $("changePwBtn").hidden = false; $("changePwBtn").style.display = "flex"; }
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
        const ichips = $("interactionChips");
        if (ichips) { ichips.hidden = true; ichips.style.display = "none"; }
        $("whoami").textContent = "";
        if ($("userDropdown")) { $("userDropdown").hidden = true; $("userDropdown").style.display = "none"; }
        $("logoutBtn").hidden = true;
        $("logoutBtn").style.display = "none";
        if ($("changePwBtn")) { $("changePwBtn").hidden = true; $("changePwBtn").style.display = "none"; }
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
// ── 修改个人密码 ──────────────────────────────────
function bindChangePwDialog() {
  const dlg = $("changePwDialog");
  const btn = $("changePwBtn");
  if (!btn) return;

  const toggleEye = (input, eye) => {
    if (!input || !eye) return;
    if (input.classList.contains("masked")) {
      input.classList.remove("masked");
      eye.textContent = "🙈";
    } else {
      input.classList.add("masked");
      eye.textContent = "👁";
    }
  };

  if ($("cpOldPwEye")) $("cpOldPwEye").onclick = () => toggleEye($("cpOldPw"), $("cpOldPwEye"));
  if ($("cpNewPwEye")) $("cpNewPwEye").onclick = () => toggleEye($("cpNewPw"), $("cpNewPwEye"));
  if ($("cpConfirmPwEye")) $("cpConfirmPwEye").onclick = () => toggleEye($("cpConfirmPw"), $("cpConfirmPwEye"));

  btn.onclick = (e) => {
    if (e) e.preventDefault();
    if ($("userDropdown")) $("userDropdown").classList.remove("open");
    const d = $("changePwDialog");
    if (!d) return;
    const oldPw = $("cpOldPw"), newPw = $("cpNewPw"), confirmPw = $("cpConfirmPw"), err = $("cpError");
    if (oldPw) { oldPw.value = ""; oldPw.classList.add("masked"); }
    if (newPw) { newPw.value = ""; newPw.classList.add("masked"); }
    if (confirmPw) { confirmPw.value = ""; confirmPw.classList.add("masked"); }
    if ($("cpOldPwEye")) $("cpOldPwEye").textContent = "👁";
    if ($("cpNewPwEye")) $("cpNewPwEye").textContent = "👁";
    if ($("cpConfirmPwEye")) $("cpConfirmPwEye").textContent = "👁";
    if (err) err.style.display = "none";
    try {
      if (typeof d.showModal === "function") d.showModal();
      else d.setAttribute("open", "");
    } catch (_) {
      d.setAttribute("open", "");
    }
    if (oldPw) oldPw.focus();
  };

  if ($("cpCancel")) $("cpCancel").onclick = () => {
    if ($("userDropdown")) $("userDropdown").classList.remove("open");
    const d = $("changePwDialog");
    if (d && typeof d.close === "function") d.close();
    else if (d) d.removeAttribute("open");
  };

  if ($("cpSave")) $("cpSave").onclick = async () => {
    const oldPw = $("cpOldPw"), newPw = $("cpNewPw"), confirmPw = $("cpConfirmPw"), err = $("cpError");
    const d = $("changePwDialog");
    const o = (oldPw ? oldPw.value : "").trim();
    const n = (newPw ? newPw.value : "").trim();
    const c = (confirmPw ? confirmPw.value : "").trim();
    if (err) err.style.display = "none";
    if (!o) { if (err) { err.textContent = "请输入当前原密码"; err.style.display = "block"; } return; }
    if (!n || n.length < 8) { if (err) { err.textContent = "新密码至少需要 8 位"; err.style.display = "block"; } return; }
    if (n === o) { if (err) { err.textContent = "新密码不能与当前原密码相同"; err.style.display = "block"; } return; }
    if (c && c !== n) { if (err) { err.textContent = "两次输入的新密码不一致"; err.style.display = "block"; } return; }

    $("cpSave").disabled = true;
    try {
      const resp = await fetch("/api/auth/change_password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ old_password: o, new_password: n, confirm_password: c }),
      });
      const data = await resp.json();
      if (!data.ok) {
        if (err) {
          err.textContent = (data.errors || []).join("；") || "修改失败";
          err.style.display = "block";
        }
        return;
      }
      if (d && typeof d.close === "function") d.close();
      else if (d) d.removeAttribute("open");
      if (typeof showToast === "function") {
        showToast("✅ 密码修改成功！当前会话已自动续期", "success");
      } else {
        alert("✅ 密码修改成功！当前会话已自动续期");
      }
    } catch (e) {
      if (err) {
        err.textContent = "网络请求失败: " + e.message;
        err.style.display = "block";
      }
    } finally {
      $("cpSave").disabled = false;
    }
  };
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", bindChangePwDialog);
} else {
  bindChangePwDialog();
}

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

function fmtDateShort(utc) {
  const d = parseDateSafe(utc);
  if (!d) return String(utc || "").slice(0, 10);
  return (d.getMonth() + 1) + "月" + d.getDate() + "日";
}

async function openBlogReaderById(blogId) {
  if ($("blogReader") && $("blogReader").style.display === "none") {
    blogReaderSavedScroll = window.scrollY || document.documentElement.scrollTop || document.body.scrollTop || 0;
  }
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
  hideMessageMonthFooter();
  setHtmlViewClass("home");
  syncNavTabs("home");

  document.querySelector('.layout').style.display = 'none';
  if ($("letterGrid")) $("letterGrid").style.display = "none";
  if ($("galleryGrid")) $("galleryGrid").style.display = "none";
  if ($("blogGrid")) $("blogGrid").style.display = "none";
  if ($("timeline")) $("timeline").style.display = "none";
  $('backTop').classList.remove('show'); $('backTop').classList.add('force-hide');
  $('archiveHome').classList.add('active');
  
  // 1. 如果已有内存或本地持久缓存，优先秒出（0ms 首屏响应，SWR 策略）
  let hasRenderedCache = false;
  if (!_portalHomeCached) {
    try {
      const raw = localStorage.getItem("archive_portal_home_cache") || sessionStorage.getItem("archive_portal_home_cache");
      if (raw) {
        const parsed = JSON.parse(raw);
        // SWR 策略：只要本地有缓存（7 天内），立即秒出静态看板与最近动态，再静默刷新
        if (parsed && parsed.data && (Date.now() - (parsed._ts || 0) < 7 * 86400000)) {
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
      if (!hasRenderedCache) {
        $('homeSkeleton').classList.remove('active');
        $('archiveHome').innerHTML =
          '<div class="home-empty active"><div class="ee-icon">📭</div>' +
          '<div class="ee-title">还没有归档数据</div>' +
          '<div class="ee-desc">确认 config.json 的 archive.enabled 已开启。<br>新消息会自动归档；历史消息用 <code>python tools/backfill_archive.py</code> 回填。<br><br><a href="/">⚙️ 前往管理端</a></div></div>';
      }
      return;
    }
    _portalHomeCached = data;
    try {
      const serialized = JSON.stringify({ _ts: Date.now(), data });
      localStorage.setItem("archive_portal_home_cache", serialized);
      sessionStorage.setItem("archive_portal_home_cache", serialized);
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
        '<img src="' + mediaUrl(p.thumb_url || p.url) + '" loading="lazy" decoding="async" data-src="' + esc(p.url) + '" alt="" onerror="handleImgError(this)" onload="this.classList.add(\'loaded\')">' +
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
async function handleRoute(isInitial = false, restoreScrollPos = null) {
  const rawHash = (location.hash || "").replace(/^#/, "");
  const p = new URLSearchParams(rawHash);

  // 0. 相册画廊模式：#gallery, #gallery=..., #source=..., #year=..., #order=...
  if (p.has("gallery") || rawHash === "gallery") {
    let saved = null;
    try { saved = localStorage.getItem("archive_last_gallery_member"); } catch (_) {}
    const mem = p.get("gallery") || (saved !== null ? saved : curGalleryMember) || "";
    const source = p.get("source") || curGallerySource || "all";
    const year = p.get("year") || "";
    const order = p.get("order") || "desc";
    curGallerySource = source;
    curGalleryYear = year;
    curGalleryOrder = order === "asc" ? "asc" : "desc";
    await selectGalleryMember(mem, false);
    return;
  }

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

    const targetScroll = restoreScrollPos !== null ? restoreScrollPos : blogReaderSavedScroll;

    // 未指定博客 ID：若阅读器正开着，关闭它并回到列表
    const readerWasOpen = $("blogReader").style.display !== "none";
    if (readerWasOpen) {
      $("blogReader").style.display = "none";
      document.documentElement.classList.remove("modal-open");
      document.body.classList.remove("modal-open");
      document.body.style.overflow = "";
      $("brContent").innerHTML = "";
      currentBlogReaderPost = null;
      blogReaderReturnHash = null;
    }

    const isAlreadyMatchingBlogList = (
      curMode === "blog" &&
      curBlogGroup === group &&
      curBlogAuthor === author &&
      curBlogDate === date &&
      searchQuery === q &&
      page === requestedPage &&
      $("blogGrid") && $("blogGrid").style.display !== "none" &&
      $("blogCards") && $("blogCards").children.length > 0
    );

    if (isAlreadyMatchingBlogList) {
      restoreWindowScroll(targetScroll);
      if (typeof handleBackTopScroll === "function") handleBackTopScroll();
      return;
    }

    await selectBlogGroup(group, author, false, { date, q, page: requestedPage });
    restoreWindowScroll(targetScroll);
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
    const readerWasOpen = $("blogReader").style.display !== "none";
    if (readerWasOpen) {
      $("blogReader").style.display = "none";
      document.documentElement.classList.remove("modal-open");
      document.body.classList.remove("modal-open");
      document.body.style.overflow = "";
      $("brContent").innerHTML = "";
      currentBlogReaderPost = null;
      blogReaderReturnHash = null;
    }

    const targetScroll = restoreScrollPos !== null ? restoreScrollPos : blogReaderSavedScroll;
    const isAlreadyMatchingHome = (
      curMode === "home" &&
      $('archiveHome') && $('archiveHome').classList.contains('active') &&
      $('portalContent') && $('portalContent').style.display !== 'none' &&
      $('portalContent').children.length > 0
    );

    if (isAlreadyMatchingHome) {
      restoreWindowScroll(targetScroll);
      if (typeof handleBackTopScroll === "function") handleBackTopScroll();
      return;
    }

    await showHome();
    restoreWindowScroll(targetScroll);
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
  // 极速预处理：如果 URL 包含博客 ID 或博客路由，0ms 同步打开视图骨架，彻底消除任何闪烁
  const earlyBlogId = p.get("id") || p.get("blog_id") || p.get("post");
  if (earlyBlogId || p.has("blog") || (location.hash || "").replace(/^#/, "") === "blog") {
    setHtmlViewClass("blog");
    syncNavTabs("blog");
    if ($("archiveHome")) $("archiveHome").classList.remove("active");
    const layout = document.querySelector('.layout');
    if (layout) layout.style.display = '';
    if ($("blogGrid")) $("blogGrid").style.display = "";
    if ($("timeline")) $("timeline").style.display = "none";
    if ($("galleryGrid")) $("galleryGrid").style.display = "none";
    if ($("letterGrid")) $("letterGrid").style.display = "none";
    const msgTb = document.querySelector(".msg-toolbar");
    if (msgTb) msgTb.style.display = "none";
    if ($("tagToggleWrap")) $("tagToggleWrap").style.display = "none";
    if (earlyBlogId) {
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
  }

  // 极速预处理：如果 URL 包含相册路由 #gallery，0ms 同步隔离隐藏消息专属控件
  if (p.has("gallery") || (location.hash || "").replace(/^#/, "") === "gallery") {
    setHtmlViewClass("gallery");
    syncNavTabs("gallery");
    if ($("archiveHome")) $("archiveHome").classList.remove("active");
    const layout = document.querySelector('.layout');
    if (layout) layout.style.display = '';
    if ($("timeline")) $("timeline").style.display = "none";
    if ($("blogGrid")) $("blogGrid").style.display = "none";
    if ($("letterGrid")) $("letterGrid").style.display = "none";
    if ($("galleryGrid")) $("galleryGrid").style.display = "block";
    if ($("archiveSide")) $("archiveSide").style.display = "none";
    const msgTb = document.querySelector(".msg-toolbar");
    if (msgTb) msgTb.style.display = "none";
    const searchTb = $("searchBox") ? $("searchBox").closest(".toolbar") : null;
    if (searchTb) searchTb.style.display = "none";
  }

  // 极速预处理：如果 URL 包含信件路由 #letter，0ms 同步切换至信件视图骨架，杜绝页面抖动
  if (p.has("letter") || (location.hash || "").replace(/^#/, "") === "letter") {
    setHtmlViewClass("letter");
    syncNavTabs("letter");
    if ($("tabLetter")) {
      $("tabLetter").hidden = false;
      $("tabLetter").style.display = "inline-flex";
    }
    if ($("archiveHome")) $("archiveHome").classList.remove("active");
    const layout = document.querySelector('.layout');
    if (layout) layout.style.display = '';
    if ($("timeline")) $("timeline").style.display = "none";
    if ($("blogGrid")) $("blogGrid").style.display = "none";
    if ($("galleryGrid")) $("galleryGrid").style.display = "none";
    if ($("letterGrid")) $("letterGrid").style.display = "block";
    if ($("archiveSide")) $("archiveSide").style.display = "none";
    const msgTb = document.querySelector(".msg-toolbar");
    if (msgTb) msgTb.style.display = "none";
    const searchTb = $("searchBox") ? $("searchBox").closest(".toolbar") : null;
    if (searchTb) searchTb.style.display = "none";
  }

  curType = p.get("t") || "";
  isFavFilter = p.get("fav") === "1";
  const initialChipFav = $("chipFav");
  if (initialChipFav) initialChipFav.classList.toggle("active", isFavFilter);
  searchQuery = normalizedQuery(p.get("q"));
  syncSearchInput();
  initTypeChips();
  initInteractionChips();
  initMessageOrder();

  // 首页数据不依赖成员选择器，避免首屏多接口并发抢占 HTTP 连接。
  const initialHome = !location.hash || location.hash === "#home";
  const authPromise = initAuth();
  const membersPromise = initialHome ? null : ensureMembersLoaded(true);
  const homePromise = initialHome ? showHome() : null;
  await Promise.all([authPromise, membersPromise, homePromise].filter(Boolean));
  if (initialHome) {
    // 首页首屏极速呈现后，在空闲时段调度成员名册与周边预加载
    if (typeof requestIdleCallback === "function") {
      requestIdleCallback(() => ensureMembersLoaded(true), { timeout: 2000 });
    } else {
      setTimeout(() => ensureMembersLoaded(true), 400);
    }
  } else {
    await handleRoute(true);
  }
}

boot();
// 从浏览器 bfcache 恢复时重新加载内容
window.addEventListener("pageshow", (e) => { if (e.persisted) boot(); });

// hash 变化时统一由 handleRoute 分发视图
window.addEventListener("hashchange", () => {
  if (selfHashUpdate) return;
  handleRoute(false);
});

function customPrompt({ title = "请输入", message = "", placeholder = "", defaultValue = "", icon = "📥", confirmText = "提交", showCheckbox = false, checkText = "", inputType = "text", validate = null } = {}) {
  return new Promise((resolve) => {
    const modal = $("customPromptModal");
    if (!modal) return resolve(null);
    
    $("pmIcon").textContent = icon;
    $("pmTitle").textContent = title;
    $("pmMessage").textContent = message;
    $("pmConfirm").textContent = confirmText;

    const input = $("pmInput");
    input.type = inputType;
    input.placeholder = placeholder;
    input.value = defaultValue;
    input.removeAttribute("aria-invalid");
    const errorEl = $("pmError");
    if (errorEl) {
      errorEl.hidden = true;
      errorEl.textContent = "";
    }
    
    const checkLabel = $("pmCheckLabel");
    const checkbox = $("pmCheckbox");
    if (showCheckbox) {
      checkLabel.style.display = "flex";
      $("pmCheckText").textContent = checkText;
      checkbox.checked = false;
    } else {
      checkLabel.style.display = "none";
    }
    
    const opener = document.activeElement;
    modal.style.display = "flex";
    setTimeout(() => { input.focus(); input.select(); }, 60);

    const clearValidationError = () => {
      input.removeAttribute("aria-invalid");
      if (errorEl) {
        errorEl.hidden = true;
        errorEl.textContent = "";
      }
    };
    const onConfirm = () => {
      const val = input.value.trim();
      const validationMessage = typeof validate === "function" ? validate(val) : "";
      if (validationMessage) {
        input.setAttribute("aria-invalid", "true");
        if (errorEl) {
          errorEl.hidden = false;
          errorEl.textContent = validationMessage;
        }
        input.focus();
        return;
      }
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
      if (e.key === "Escape") {
        e.preventDefault();
        onCancel();
      }
    };
    const onDocumentKeydown = (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onCancel();
      }
    };
    const cleanup = () => {
      modal.style.display = "none";
      $("pmConfirm").removeEventListener("click", onConfirm);
      $("pmCancel").removeEventListener("click", onCancel);
      input.removeEventListener("keydown", onKeydown);
      input.removeEventListener("input", clearValidationError);
      document.removeEventListener("keydown", onDocumentKeydown);
      if (opener && opener !== document.body && document.contains(opener)) opener.focus();
    };

    $("pmConfirm").addEventListener("click", onConfirm);
    $("pmCancel").addEventListener("click", onCancel);
    input.addEventListener("keydown", onKeydown);
    input.addEventListener("input", clearValidationError);
    document.addEventListener("keydown", onDocumentKeydown);
  });
}

const BLOG_BACKFILL_GROUPS = [
  { id: "bgbNogizaka", key: "nogizaka" },
  { id: "bgbSakurazaka", key: "sakurazaka" },
  { id: "bgbHinatazaka", key: "hinatazaka" },
];

function selectedBlogBackfillGroups() {
  return BLOG_BACKFILL_GROUPS
    .filter(({ id }) => $(id)?.checked)
    .map(({ key }) => key);
}

function syncBlogBackfillGroupUI() {
  const selected = selectedBlogBackfillGroups();
  const confirm = $("bgbConfirm");
  const selectAll = $("bgbSelectAll");
  if (confirm) confirm.disabled = selected.length === 0;
  if (selectAll) selectAll.textContent = selected.length === BLOG_BACKFILL_GROUPS.length ? "清空" : "全选";
  BLOG_BACKFILL_GROUPS.forEach(({ id }) => {
    const input = $(id);
    const option = input?.closest(".blog-group-option");
    if (option) option.classList.toggle("selected", !!input.checked);
  });
}

function closeBlogBackfillGroupModal() {
  const modal = $("blogGroupBackfillModal");
  if (modal) modal.style.display = "none";
}

function promptArchiveGroups() {
  const modal = $("blogGroupBackfillModal");
  if (!modal) return;
  BLOG_BACKFILL_GROUPS.forEach(({ id }) => {
    const input = $(id);
    if (input) input.checked = false;
  });
  syncBlogBackfillGroupUI();
  modal.style.display = "flex";
  const first = $(BLOG_BACKFILL_GROUPS[0].id);
  setTimeout(() => first?.focus(), 60);
}

if ($("blogGroupBackfillModal")) {
  BLOG_BACKFILL_GROUPS.forEach(({ id }) => {
    $(id)?.addEventListener("change", syncBlogBackfillGroupUI);
  });
  $("bgbSelectAll")?.addEventListener("click", () => {
    const selectAll = selectedBlogBackfillGroups().length !== BLOG_BACKFILL_GROUPS.length;
    BLOG_BACKFILL_GROUPS.forEach(({ id }) => {
      const input = $(id);
      if (input) input.checked = selectAll;
    });
    syncBlogBackfillGroupUI();
  });
  $("bgbCancel")?.addEventListener("click", closeBlogBackfillGroupModal);
  $("bgbAdvanced")?.addEventListener("click", () => {
    closeBlogBackfillGroupModal();
    promptArchiveMemberUrl();
  });
  $("blogGroupBackfillModal").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) closeBlogBackfillGroupModal();
  });
  $("blogGroupBackfillModal").addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeBlogBackfillGroupModal();
  });
  $("bgbConfirm")?.addEventListener("click", async () => {
    const groups = selectedBlogBackfillGroups();
    if (!groups.length) return;
    const confirm = $("bgbConfirm");
    if (confirm) confirm.disabled = true;
    closeBlogBackfillGroupModal();
    try {
      const res = await fetch("/api/archive/blogs/archive_groups", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ groups }),
      });
      const data = await res.json();
      const ok = res.ok && data.ok;
      const error = Array.isArray(data.errors) ? data.errors.join("；") : "";
      showToast(
        data.msg || error || (ok ? "已成功启动后台博客归档任务！" : `归档请求失败（HTTP ${res.status}）`),
        ok ? "success" : "error",
      );
    } catch (error) {
      showToast("请求异常: " + error, "error");
    } finally {
      syncBlogBackfillGroupUI();
    }
  });
}

async function promptArchiveMemberUrl() {
  const result = await customPrompt({
    title: "📥 归档成员博客",
    message: "请输入任意坂道成员博客列表页 URL（支持乃木坂46 / 樱坂46 / 日向坂46）：",
    placeholder: "例：https://sakurazaka46.com/s/s46/diary/blog/list?ima=0000&ct=59",
    icon: "📝",
    confirmText: "开始归档",
    showCheckbox: true,
    checkText: "开启 Gemini AI 中日双语翻译（勾选将较慢）",
    inputType: "url",
    validate: (value) => isValidArchiveMemberBlogUrl(value)
      ? ""
      : "请输入三坂官方成员博客列表页链接，并确认包含数字 ct 成员编号。"
  });

  if (!result || !result.value) return;

  try {
    const res = await fetch("/api/archive/blogs/archive_member", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: result.value, translate: result.checked })
    });
    const data = await res.json();
    const ok = res.ok && data.ok;
    const error = Array.isArray(data.errors) ? data.errors.join("；") : "";
    showToast(data.msg || error || (ok ? "已成功启动后台博客归档任务！" : `归档请求失败（HTTP ${res.status}）`), ok ? "success" : "error");
  } catch(e) {
    showToast("请求异常: " + e, "error");
  }
}

// 兼容旧版书签、脚本及外部页面调用；新入口使用 promptArchiveGroups()。
async function promptArchiveMember() {
  return promptArchiveMemberUrl();
}

const BACKFILL_GROUP_LABELS = {
  nogizaka46: "乃木坂",
  hinatazaka46: "日向坂",
  sakurazaka46: "樱坂",
  yodel: "yodel",
};

function backfillMemberOptions() {
  return Array.from($('bmMemberOptions')?.querySelectorAll('input[type="checkbox"]') || []);
}

function selectedBackfillMembers() {
  return backfillMemberOptions()
    .filter((input) => input.checked)
    .map((input) => input.value)
    .filter(Boolean);
}

function syncBackfillMemberSelection() {
  const options = backfillMemberOptions();
  const selected = selectedBackfillMembers();
  const hidden = $("bmMemberInput");
  const summary = $("bmMemberSelectionSummary");
  const toggle = $("bmMemberSelectAll");
  if (hidden) hidden.value = selected.join(", ");
  if (summary) {
    summary.textContent = selected.length
      ? `已选择 ${selected.length} 位成员`
      : "未选择：全部监控成员";
  }
  if (toggle) {
    toggle.disabled = options.length === 0;
    toggle.textContent = options.length && selected.length === options.length ? "清空选择" : "全选";
  }
}

function renderBackfillMemberOptions(selectedNames = []) {
  const box = $("bmMemberOptions");
  if (!box) return;
  const available = (Array.isArray(monitorMembers) ? monitorMembers : []).filter((member) => member && member.name);
  const selected = new Set(selectedNames);
  box.innerHTML = "";
  if (!available.length) {
    box.innerHTML = '<div class="bm-member-empty">暂无当前监控成员；请先在管理端配置 Message 监控成员。</div>';
    syncBackfillMemberSelection();
    return;
  }
  available.forEach((member) => {
    const label = document.createElement("label");
    label.className = "bm-member-option";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = member.name;
    input.checked = selected.has(member.name);
    input.setAttribute("aria-label", `选择 ${member.display || member.name}`);
    const name = document.createElement("span");
    name.className = "bm-member-option-name";
    name.textContent = member.display || member.name;
    const group = document.createElement("span");
    group.className = "bm-member-option-group";
    group.textContent = BACKFILL_GROUP_LABELS[member.group] || member.group || "成员";
    label.append(input, name, group);
    box.appendChild(label);
  });
  syncBackfillMemberSelection();
}

function backfillMemberSelectionForOpen() {
  const normalize = (value) => String(value || "").replace(/[ _　]/g, "");
  const currentMember = curMember && curMember !== "__all__"
    ? monitorMembers.find((member) => normalize(member.name) === normalize(curMember))
    : null;
  const current = currentMember ? [currentMember.name] : [];
  renderBackfillMemberOptions(current);
}

$("bmMemberOptions")?.addEventListener("change", syncBackfillMemberSelection);
$("bmMemberSelectAll")?.addEventListener("click", () => {
  const options = backfillMemberOptions();
  const selectAll = selectedBackfillMembers().length !== options.length;
  options.forEach((input) => { input.checked = selectAll; });
  syncBackfillMemberSelection();
});

function isValidArchiveMemberBlogUrl(value) {
  const rules = {
    "nogizaka46.com": "/s/n46/diary/",
    "sakurazaka46.com": "/s/s46/diary/",
    "hinatazaka46.com": "/s/official/diary/",
  };
  try {
    const url = new URL(String(value || "").trim());
    const host = url.hostname.toLowerCase().replace(/^www\./, "");
    const pathPrefix = rules[host];
    const port = url.port;
    const ct = url.searchParams.get("ct") || "";
    if (!pathPrefix || !["http:", "https:"].includes(url.protocol)) return false;
    if (url.username || url.password || (port && port !== "80" && port !== "443")) return false;
    return url.pathname.startsWith(pathPrefix) && /^\d+$/.test(ct);
  } catch (_) {
    return false;
  }
}

async function promptArchiveMessage() {
  const modal = $("backfillMessageModal");
  if (!modal) return;

  const memberInput = $("bmMemberInput");
  const modeRadios = document.querySelectorAll('input[name="bmMode"]');
  const customDateRow = $("bmCustomDateRow");
  const fromDateInput = $("bmFromDateInput");
  const cancelBtn = $("bmCancel");
  const confirmBtn = $("bmConfirm");

  // 若当前正在查看某位成员，默认勾选该成员；不勾选则保持“全部监控成员”的旧语义。
  backfillMemberSelectionForOpen();

  // 默认断点续传模式
  for (const r of modeRadios) {
    if (r.value === "incremental") r.checked = true;
  }
  customDateRow.style.display = "none";
  fromDateInput.value = "";

  const onModeChange = () => {
    const selected = document.querySelector('input[name="bmMode"]:checked');
    customDateRow.style.display = (selected && selected.value === "custom") ? "block" : "none";
  };

  for (const r of modeRadios) {
    r.addEventListener("change", onModeChange);
  }

  return new Promise((resolve) => {
    const cleanup = () => {
      modal.style.display = "none";
      for (const r of modeRadios) {
        r.removeEventListener("change", onModeChange);
      }
      cancelBtn.removeEventListener("click", onCancel);
      confirmBtn.removeEventListener("click", onConfirm);
      document.removeEventListener("keydown", onKeydown);
    };

    const onCancel = () => {
      cleanup();
      resolve();
    };

    const onKeydown = (e) => {
      if (e.key === "Escape") onCancel();
    };

    const onConfirm = async () => {
      const memberVal = selectedBackfillMembers().join(", ");
      if (memberInput) memberInput.value = memberVal;
      const selectedMode = (document.querySelector('input[name="bmMode"]:checked') || {}).value || "incremental";

      let payload = { member: memberVal, reset: false };
      if (selectedMode === "smart_full") {
        payload.reset = true;
      } else if (selectedMode === "custom") {
        const fromVal = (fromDateInput.value || "").trim();
        if (!fromVal) {
          showToast("请选择或输入有效的起始日期 (YYYY-MM-DD)", "warning");
          return;
        }
        payload.reset = true;
        payload.from_date = fromVal;
      }

      cleanup();
      resolve();

      try {
        const res = await fetch("/api/archive/messages/backfill", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (res.status === 409) {
          showToast(data.msg || "已有消息回填任务正在执行中，请勿重复启动！", "warning");
        } else if (res.ok && data.ok) {
          showToast(data.msg || "已成功启动消息归档回填任务，可在管理后台系统日志中查看进度。", "success");
        } else {
          const error = Array.isArray(data.errors) ? data.errors.join("；") : data.msg;
          showToast(error || `回填请求失败（HTTP ${res.status}）`, "error");
        }
      } catch (e) {
        showToast("请求异常: " + e, "error");
      }
    };

    cancelBtn.addEventListener("click", onCancel);
    confirmBtn.addEventListener("click", onConfirm);
    document.addEventListener("keydown", onKeydown);

    modal.style.display = "flex";
    setTimeout(() => {
      const firstOption = backfillMemberOptions()[0];
      (firstOption || $("bmMemberSelectAll") || confirmBtn).focus();
    }, 60);
  });
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
  hideMessageMonthFooter();
  setHtmlViewClass("letter");
  curLetterMember = mName;
  try { localStorage.setItem("archive_last_letter_member", mName); } catch (_) {}
  const mObj = members.find(m => m.name === mName) || { name: mName, display: mName };
  const disp = $("curLetterMemberDisplay");
  if (disp) disp.textContent = mObj.display || mName;
  const letterCount = Number(mObj.letters_total || 0);
  if ($("curLetterMemberCount")) $("curLetterMemberCount").textContent = "（" + letterCount.toLocaleString() + "）";
  syncNavTabs("letter");

  $('archiveHome').classList.remove('active');
  $('backTop').style.display = ''; $('backTop').classList.remove('force-hide');
  document.querySelector('.layout').style.display = '';
  $("timeline").style.display = "none";
  $("blogGrid").style.display = "none";
  if ($("galleryGrid")) $("galleryGrid").style.display = "none";
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
    updatePopoverExpandAllControl("letter", []);
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

  const visibleGroupKeys = [];
  groups.forEach(g => {
    const grpMems = filtered.filter(m => inferMemberGroup(m) === g.key);
    if (!grpMems.length) return;
    visibleGroupKeys.push(g.key);

    const groupHeader = createPopoverGroupHeader(g, grpMems.length, () => renderLetterMemberPopover(filterKeyword));
    list.appendChild(groupHeader.head);
    if (groupHeader.collapsed) return;

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
                       '</div>' +
                       '<span class="m-cnt">' + Number(m.letters_total || 0).toLocaleString() + ' 封</span>';
      item.addEventListener("click", () => {
        closeLetterMemberPopover();
        selectLetterMember(m.name);
      });
      list.appendChild(item);
    });
  });
  updatePopoverExpandAllControl("letter", visibleGroupKeys);
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

// ══════════════════════════════════════════════════════════════════
// 📷 纯享美图画廊 (Photo Gallery) 交互逻辑
// ══════════════════════════════════════════════════════════════════
let curGalleryMember = "";
let curGallerySource = "all";
let curGalleryYear = "";
let curGalleryOrder = "desc";
let curGalleryPage = 1;
let curGalleryTotal = 0;
let curGalleryHasMore = false;
let curGalleryLoading = false;
let curGalleryImages = [];
let galleryMembers = [];
let galleryMembersLoading = false;
let galleryYears = [];
let galleryYearsVersion = 0;

function syncGallerySourceChips() {
  const wrap = $("gallerySourceChips");
  if (!wrap) return;
  const chips = wrap.querySelectorAll(".chip");
  chips.forEach(c => {
    const s = c.getAttribute("data-source") || "all";
    c.classList.toggle("active", s === curGallerySource);
  });
}

let curGalleryLayout = (function() {
  try { return localStorage.getItem("archive_gallery_layout") || "masonry"; }
  catch (_) { return "masonry"; }
})();

function getMasonryColCount() {
  const w = window.innerWidth;
  if (w <= 768) return 2;
  return 4; // 桌面端固定 4 列，保证 100% 严密无空档
}

function syncGalleryLayoutToggle() {
  const btnMasonry = $("btnLayoutMasonry");
  const btnGrid = $("btnLayoutGrid");
  const isMasonry = curGalleryLayout === "masonry";
  if (btnMasonry) btnMasonry.classList.toggle("active", isMasonry);
  if (btnGrid) btnGrid.classList.toggle("active", !isMasonry);
  const cardsBox = $("galleryCards");
  if (cardsBox) {
    cardsBox.classList.toggle("layout-masonry", isMasonry);
    cardsBox.style.setProperty("--gallery-cols", 4);
  }
}

function rebalanceMasonry(cardsBox) {
  if (!cardsBox) cardsBox = $("galleryCards");
  if (!cardsBox) return;
  const cards = Array.from(cardsBox.querySelectorAll(".gallery-card"));
  if (!cards.length) return;

  cardsBox.innerHTML = "";
  cardsBox.classList.add("layout-masonry");
  const colCount = getMasonryColCount();
  const cols = [];
  for (let i = 0; i < colCount; i++) {
    const c = document.createElement("div");
    c.className = "masonry-col";
    cardsBox.appendChild(c);
    cols.push(c);
  }
  const colHeights = new Array(colCount).fill(0);
  const colW = cols[0].clientWidth || 220;
  cards.forEach(card => {
    let minIdx = 0;
    for (let i = 1; i < colCount; i++) {
      if (colHeights[i] < colHeights[minIdx]) minIdx = i;
    }
    cols[minIdx].appendChild(card);
    let h = card.offsetHeight;
    if (!h || h <= 100) {
      const ratioStr = card.style.getPropertyValue("--photo-ratio");
      if (ratioStr && ratioStr.includes("/")) {
        const parts = ratioStr.split("/").map(Number);
        if (parts[0] && parts[1]) {
          h = (parts[1] / parts[0]) * colW;
        }
      }
    }
    colHeights[minIdx] += (h || 250) + 14;
  });
}

function switchGalleryLayout(newLayout) {
  if (newLayout !== "masonry" && newLayout !== "grid") return;
  if (curGalleryLayout === newLayout) return;
  curGalleryLayout = newLayout;
  try { localStorage.setItem("archive_gallery_layout", curGalleryLayout); } catch (_) {}
  syncGalleryLayoutToggle();

  const cardsBox = $("galleryCards");
  if (!cardsBox) return;

  const cards = Array.from(cardsBox.querySelectorAll(".gallery-card"));
  if (!cards.length) return;

  if (curGalleryLayout === "masonry") {
    rebalanceMasonry(cardsBox);
  } else {
    cardsBox.innerHTML = "";
    cardsBox.classList.remove("layout-masonry");
    cardsBox.style.setProperty("--gallery-cols", 4);
    const frag = document.createDocumentFragment();
    cards.forEach(card => {
      card.style.position = "";
      card.style.left = "";
      card.style.top = "";
      card.style.width = "";
      card.style.height = "";
      frag.appendChild(card);
    });
    cardsBox.appendChild(frag);
  }
}

function syncGallerySortButton() {
  const btn = $("btnGallerySortOrder");
  const txt = $("gallerySortOrderText");
  const icon = $("gallerySortIcon");
  if (!btn) return;
  if (curGalleryOrder === "asc") {
    btn.classList.add("order-asc");
    btn.setAttribute("title", "当前最早优先（时间正序），点击切换为最新优先");
    if (txt) txt.textContent = "最早优先";
    if (icon) icon.textContent = "↑";
  } else {
    btn.classList.remove("order-asc");
    btn.setAttribute("title", "当前最新优先（时间倒序），点击切换为最早优先");
    if (txt) txt.textContent = "最新优先";
    if (icon) icon.textContent = "↓";
  }
}

function syncGalleryHash() {
  const params = new URLSearchParams();
  params.set("gallery", curGalleryMember);
  if (curGallerySource && curGallerySource !== "all") params.set("source", curGallerySource);
  if (curGalleryYear) params.set("year", curGalleryYear);
  if (curGalleryOrder && curGalleryOrder !== "desc") params.set("order", curGalleryOrder);
  selfHashUpdate = true;
  location.hash = params.toString();
  setTimeout(() => { selfHashUpdate = false; }, 0);
}

async function loadGalleryYears() {
  const container = $("galleryYearChips");
  if (!container) return false;
  const myVersion = ++galleryYearsVersion;
  try {
    let url = "/api/archive/gallery_years?source=" + encodeURIComponent(curGallerySource);
    if (curGalleryMember) url += "&member=" + encodeURIComponent(curGalleryMember);
    const res = await api(url);
    if (myVersion !== galleryYearsVersion) return false;
    if (res && res.ok && Array.isArray(res.years)) {
      galleryYears = res.years;
      if (curGalleryYear && !galleryYears.some(item => String(item.year) === String(curGalleryYear))) {
        curGalleryYear = "";
        syncGalleryHash();
      }
      renderGalleryYearChips();
      return true;
    }
  } catch (_) {}
  return false;
}

function renderGalleryYearChips() {
  const container = $("galleryYearChips");
  if (!container) return;
  container.innerHTML = "";

  if (curGalleryYear && !galleryYears.some(item => String(item.year) === String(curGalleryYear))) {
    curGalleryYear = "";
    syncGalleryHash();
  }

  // 全部年份 chip
  const allChip = document.createElement("button");
  allChip.type = "button";
  allChip.className = "chip" + (!curGalleryYear ? " active" : "");
  allChip.setAttribute("data-year", "");
  allChip.textContent = "全部年份";
  allChip.addEventListener("click", () => {
    if (curGalleryYear === "") return;
    curGalleryYear = "";
    container.querySelectorAll(".chip").forEach(c => c.classList.toggle("active", c === allChip));
    syncGalleryHash();
    loadGalleryPhotos(true);
  });
  container.appendChild(allChip);

  if (!galleryYears.length) return;

  galleryYears.forEach(item => {
    const yStr = String(item.year);
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip" + (curGalleryYear === yStr ? " active" : "");
    chip.setAttribute("data-year", yStr);
    chip.innerHTML = esc(yStr) + '年<span class="year-cnt">(' + Number(item.count).toLocaleString() + ')</span>';
    chip.addEventListener("click", () => {
      if (curGalleryYear === yStr) return;
      curGalleryYear = yStr;
      container.querySelectorAll(".chip").forEach(c => c.classList.toggle("active", c === chip));
      syncGalleryHash();
      loadGalleryPhotos(true);
    });
    container.appendChild(chip);
  });
}

function openGalleryLightbox(idx, placeholderUrl) {
  if (idx < 0 || idx >= curGalleryImages.length) return;
  images = curGalleryImages;
  const item = curGalleryImages[idx];
  openLightbox(idx, null, null, placeholderUrl || (item ? item.thumbUrl : ""));
}

async function loadGalleryMembers() {
  if (galleryMembers.length) return galleryMembers;
  if (galleryMembersLoading) return [];
  galleryMembersLoading = true;
  try {
    const res = await api("/api/archive/gallery_members");
    if (res && res.ok && Array.isArray(res.members)) {
      galleryMembers = res.members;
      updateGalleryMemberButtonDisplay();
      renderGalleryMemberPopover();
      return galleryMembers;
    }
  } catch (_) {}
  finally {
    galleryMembersLoading = false;
  }
  return [];
}

function findGalleryMemberObj(nameOrDisplay) {
  if (!nameOrDisplay) return null;
  const list = galleryMembers.length ? galleryMembers : members;
  const norm = nameOrDisplay.replace(/[\s_　]/g, "");
  return list.find(m => {
    const mNorm = (m.name || "").replace(/[\s_　]/g, "");
    const dNorm = (m.display || "").replace(/[\s_　]/g, "");
    return mNorm === norm || dNorm === norm;
  }) || null;
}

function getGalleryMemberCountNum(m) {
  if (!m) return 0;
  if (curGallerySource === "blog") {
    return m.blog_photos !== undefined ? m.blog_photos : (m.total || 0);
  } else if (curGallerySource === "message") {
    return m.msg_photos !== undefined ? m.msg_photos : (m.total || 0);
  } else {
    return m.total_photos !== undefined ? m.total_photos : (m.total || 0);
  }
}

function updateGalleryMemberButtonDisplay() {
  const disp = $("curGalleryMemberDisplay");
  const countDisp = $("curGalleryMemberCount");
  if (!disp) return;
  if (!curGalleryMember) {
    disp.textContent = "全部成员 (聚合)";
    if (countDisp) countDisp.textContent = "";
    return;
  }
  const mObj = findGalleryMemberObj(curGalleryMember);
  disp.textContent = (mObj && mObj.display) ? mObj.display : curGalleryMember;
  if (countDisp) {
    const cnt = mObj ? getGalleryMemberCountNum(mObj) : 0;
    countDisp.textContent = cnt ? "（" + Number(cnt).toLocaleString() + " 张）" : "";
  }
}

async function selectGalleryMember(mName, updateHash = true) {
  curMode = "gallery";
  hideMessageMonthFooter();
  setHtmlViewClass("gallery");
  curGalleryMember = mName || "";
  try { localStorage.setItem("archive_last_gallery_member", curGalleryMember); } catch (_) {}

  updateGalleryMemberButtonDisplay();
  syncGallerySourceChips();
  syncGallerySortButton();
  syncGalleryLayoutToggle();

  syncNavTabs("gallery");

  $('archiveHome').classList.remove('active');
  $('backTop').style.display = ''; $('backTop').classList.remove('force-hide');
  const layout = document.querySelector('.layout');
  if (layout) layout.style.display = '';
  if ($("timeline")) $("timeline").style.display = "none";
  if ($("blogGrid")) $("blogGrid").style.display = "none";
  if ($("letterGrid")) $("letterGrid").style.display = "none";
  if ($("galleryGrid")) $("galleryGrid").style.display = "block";
  if ($("archiveSide")) $("archiveSide").style.display = "none";

  const msgTb = document.querySelector(".msg-toolbar");
  if (msgTb) msgTb.style.display = "none";
  const searchTb = $("searchBox") ? $("searchBox").closest(".toolbar") : null;
  if (searchTb) searchTb.style.display = "none";

  if (!galleryMembers.length) {
    loadGalleryMembers();
  } else {
    renderGalleryMemberPopover();
  }
  await loadGalleryYears();
  if (updateHash) {
    syncGalleryHash();
  }
  await loadGalleryPhotos(true);
}

function getGalleryGridCols() {
  const w = window.innerWidth;
  if (w <= 768) return 2;
  return getMasonryColCount();
}

function getGalleryPerPage() {
  const cols = getMasonryColCount();
  const isMobile = window.innerWidth <= 768;
  const targetCards = isMobile ? 16 : 20;
  if (isMobile) return 16;
  if (cols === 5) return 20; // 4 行整整 20 张
  if (cols === 4) return 20; // 5 行整整 20 张
  if (cols === 3) return 21; // 7 行整整 21 张
  return targetCards;
}

let curGalleryPerPage = 20;
let galleryLoadVersion = 0;
let galleryObserver = null;

function checkGallerySentinelInView() {
  const sentinel = $("galleryLoadMore");
  const grid = $("galleryGrid");
  if (!sentinel || !grid || grid.style.display === "none") return;
  if (curGalleryLoading || !curGalleryHasMore) return;
  const rect = sentinel.getBoundingClientRect();
  const vh = window.innerHeight || document.documentElement.clientHeight;
  if (rect.top <= vh + 350) {
    curGalleryPage += 1;
    loadGalleryPhotos(false);
  }
}

function initGalleryObserver() {
  const sentinel = $("galleryLoadMore");
  if (!sentinel || typeof IntersectionObserver === "undefined") return;
  if (galleryObserver) return;
  galleryObserver = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting && !curGalleryLoading && curGalleryHasMore) {
        const grid = $("galleryGrid");
        if (!grid || grid.style.display === "none") return;
        curGalleryPage += 1;
        loadGalleryPhotos(false);
      }
    });
  }, {
    root: null,
    rootMargin: "350px 0px",
    threshold: 0.01,
  });
  galleryObserver.observe(sentinel);
}

async function loadGalleryPhotos(reset = true) {
  if (!reset && curGalleryLoading) return;
  const myVersion = reset ? ++galleryLoadVersion : galleryLoadVersion;
  curGalleryLoading = true;

  const cardsBox = $("galleryCards");
  const statsBox = $("galleryStats");
  const loadMoreBtn = $("galleryLoadMore");
  const endHint = $("galleryEndHint");
  if (!cardsBox) {
    curGalleryLoading = false;
    return;
  }

  if (reset) {
    curGalleryPage = 1;
    curGalleryImages = [];
    curGalleryPerPage = getGalleryPerPage();
    cardsBox.classList.remove("layout-masonry");
    cardsBox.innerHTML = '<div class="gallery-status-msg" style="grid-column:1/-1;width:100%;text-align:center;padding:50px;color:var(--muted);"><span class="sync-icon" style="display:inline-block;animation:spin 1s linear infinite;font-size:24px;">🔄</span><div style="margin-top:10px;">正在加载相册图片...</div></div>';
    if (statsBox) statsBox.textContent = "";
    if (loadMoreBtn) {
      loadMoreBtn.style.display = "none";
      loadMoreBtn.disabled = false;
      loadMoreBtn.textContent = "加载更多图片 ↓";
    }
    if (endHint) endHint.style.display = "none";
  } else {
    if (loadMoreBtn) {
      loadMoreBtn.disabled = true;
      loadMoreBtn.innerHTML = '<span class="sync-icon" style="display:inline-block;animation:spin 1s linear infinite;">🔄</span> 正在加载更多图片...';
      loadMoreBtn.style.display = "block";
    }
    if (endHint) endHint.style.display = "none";
  }

  try {
    let url = "/api/archive/gallery?page=" + curGalleryPage + "&per_page=" + curGalleryPerPage + "&source=" + encodeURIComponent(curGallerySource) + "&order=" + encodeURIComponent(curGalleryOrder);
    if (curGalleryMember) {
      url += "&member=" + encodeURIComponent(curGalleryMember);
    }
    if (curGalleryYear) {
      url += "&year=" + encodeURIComponent(curGalleryYear);
    }
    const data = await api(url);
    if (myVersion !== galleryLoadVersion) return;
    if (!data.ok) {
      if (reset) {
        cardsBox.classList.remove("layout-masonry");
        cardsBox.innerHTML = '<div class="gallery-status-msg" style="grid-column:1/-1;width:100%;text-align:center;padding:40px;color:var(--muted);">加载失败：' + esc((data.errors || []).join("; ")) + '</div>';
      } else {
        curGalleryPage = Math.max(1, curGalleryPage - 1);
        if (loadMoreBtn) {
          loadMoreBtn.disabled = false;
          loadMoreBtn.textContent = "加载失败，点击重试 🔄";
          loadMoreBtn.style.display = "block";
        }
      }
      return;
    }

    const list = data.photos || [];
    curGalleryTotal = data.total || 0;
    curGalleryHasMore = !!data.has_more;
    if (!reset && (!list.length || list.length < curGalleryPerPage)) {
      curGalleryHasMore = false;
    }

    if (statsBox) {
      statsBox.textContent = "共 " + curGalleryTotal.toLocaleString() + " 张图片";
    }

    if (reset && !list.length) {
      cardsBox.classList.remove("layout-masonry");
      cardsBox.innerHTML = '<div class="gallery-status-msg" style="grid-column:1/-1;width:100%;text-align:center;padding:60px 20px;color:var(--muted);background:var(--card);border:1px dashed var(--border);border-radius:16px;">' +
        '<div style="font-size:38px;margin-bottom:12px;">📷</div>' +
        '<div style="font-size:15px;font-weight:600;color:var(--text-strong);">暂无匹配的图片</div>' +
        '<div style="font-size:13px;margin-top:6px;">未在当前筛选条件下找到本地图片，可尝试切换成员或来源。</div>' +
        '</div>';
      if (loadMoreBtn) loadMoreBtn.style.display = "none";
      if (endHint) endHint.style.display = "none";
      return;
    }

    if (reset) {
      cardsBox.innerHTML = "";
    }

    const isMasonry = curGalleryLayout === "masonry";
    let masonryCols = [];
    let colHeights = [];
    if (isMasonry) {
      cardsBox.classList.add("layout-masonry");
      masonryCols = Array.from(cardsBox.querySelectorAll(".masonry-col"));
      const colCount = getMasonryColCount();
      if (masonryCols.length !== colCount) {
        cardsBox.innerHTML = "";
        masonryCols = [];
        for (let i = 0; i < colCount; i++) {
          const c = document.createElement("div");
          c.className = "masonry-col";
          cardsBox.appendChild(c);
          masonryCols.push(c);
        }
      }
      colHeights = masonryCols.map(c => c.offsetHeight);
    } else {
      cardsBox.classList.remove("layout-masonry");
    }

    const fragment = document.createDocumentFragment();
    const existingUrls = new Set(curGalleryImages.map(img => img.url));

    list.forEach((photo) => {
      if (!photo || !photo.url) return;
      if (existingUrls.has(photo.url)) return;
      existingUrls.add(photo.url);

      const globalIdx = curGalleryImages.length;
      let dateStr = photo.published_at || "";
      try {
        const dt = new Date(photo.published_at);
        if (!isNaN(dt.getTime())) {
          dateStr = dt.toLocaleDateString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" });
        }
      } catch (_) {}

      let thumbUrl = photo.url;
      if (photo.url && (photo.url.startsWith("/api/archive/media/") || photo.url.startsWith("/api/archive/blog_media/"))) {
        thumbUrl += (photo.url.includes("?") ? "&thumb=1" : "?thumb=1");
      }

      curGalleryImages.push({
        url: photo.url,
        thumbUrl: thumbUrl,
        caption: "【" + (photo.member_name || "") + "】" + dateStr + (photo.text ? " · " + photo.text.slice(0, 60) : ""),
        source: photo.source,
        blogId: photo.blog_id,
        groupKey: photo.group_key,
        messageId: photo.source === "message" ? photo.id : "",
        memberName: photo.member_name,
        memberDir: photo.member_dir || "",
        year: photo.year,
        month: photo.month,
      });

      const card = document.createElement("div");
      card.className = "gallery-card";
      card.setAttribute("role", "button");
      card.setAttribute("tabindex", "0");
      card.setAttribute("title", (photo.member_name ? "【" + photo.member_name + "】" : "") + (photo.text || "点击查看大图"));

      if (photo.w && photo.h) {
        card.style.setProperty("--photo-ratio", photo.w + " / " + photo.h);
      }

      const isBlog = photo.source === "blog";
      const badgeText = isBlog ? "📄 博客" : "💬 消息";
      const badgeClass = isBlog ? "gallery-badge blog" : "gallery-badge msg";

      card.innerHTML =
        '<div class="' + badgeClass + '">' + badgeText + '</div>' +
        '<img src="' + esc(thumbUrl) + '" loading="lazy" decoding="async" referrerpolicy="no-referrer" alt="图片" onload="this.classList.add(\'loaded\');this.parentElement.classList.add(\'has-loaded\');if(!this.parentElement.style.getPropertyValue(\'--photo-ratio\')&&this.naturalWidth&&this.naturalHeight){this.parentElement.style.setProperty(\'--photo-ratio\',this.naturalWidth+\' / \'+this.naturalHeight);}" onerror="this.classList.add(\'img-broken\');this.parentElement.classList.add(\'is-broken\');this.onerror=null;" />' +
        '<div class="gallery-overlay">' +
          '<div class="gallery-meta">' + esc(photo.member_name || "") + ' · ' + esc(dateStr) + '</div>' +
          (photo.text ? '<div class="gallery-caption">' + esc(photo.text) + '</div>' : '') +
        '</div>';

      const imgEl = card.querySelector("img");
      if (imgEl && imgEl.complete && imgEl.naturalWidth) {
        imgEl.classList.add("loaded");
        card.classList.add("has-loaded");
        if (!card.style.getPropertyValue("--photo-ratio")) {
          card.style.setProperty("--photo-ratio", imgEl.naturalWidth + " / " + imgEl.naturalHeight);
        }
      }

      card.addEventListener("click", () => openGalleryLightbox(globalIdx, thumbUrl));
      card.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          openGalleryLightbox(globalIdx, thumbUrl);
        }
      });

      if (isMasonry) {
        let minIdx = 0;
        for (let i = 1; i < masonryCols.length; i++) {
          if (colHeights[i] < colHeights[minIdx]) minIdx = i;
        }
        masonryCols[minIdx].appendChild(card);
        const colW = masonryCols[minIdx].clientWidth || 220;
        const estH = (photo.w && photo.h) ? ((photo.h / photo.w) * colW) : (colW * 1.33);
        colHeights[minIdx] += estH + 14;
      } else {
        fragment.appendChild(card);
      }
    });

    if (!isMasonry) {
      cardsBox.appendChild(fragment);
    }

    if (loadMoreBtn) {
      if (curGalleryHasMore) {
        loadMoreBtn.style.display = "block";
        loadMoreBtn.disabled = false;
        loadMoreBtn.textContent = "加载更多图片 ↓";
      } else {
        loadMoreBtn.style.display = "none";
      }
    }
    if (endHint) {
      endHint.style.display = (!curGalleryHasMore && curGalleryImages.length > 0) ? "block" : "none";
    }
    initGalleryObserver();
  } catch (err) {
    if (myVersion !== galleryLoadVersion) return;
    if (reset) {
      cardsBox.classList.remove("layout-masonry");
      cardsBox.innerHTML = '<div class="gallery-status-msg" style="grid-column:1/-1;width:100%;text-align:center;padding:40px;color:var(--muted);">加载异常：' + esc(err) + '</div>';
    } else {
      curGalleryPage = Math.max(1, curGalleryPage - 1);
      if (loadMoreBtn) {
        loadMoreBtn.disabled = false;
        loadMoreBtn.textContent = "加载失败，点击重试 🔄";
        loadMoreBtn.style.display = "block";
      }
    }
  } finally {
    if (myVersion === galleryLoadVersion) {
      curGalleryLoading = false;
      if (curGalleryHasMore) {
        requestAnimationFrame(() => {
          checkGallerySentinelInView();
        });
      }
    }
  }
}

function renderGalleryMemberPopover(filterKeyword = "") {
  const list = $("galleryMemberPopoverList");
  if (!list) return;
  list.innerHTML = "";
  const kw = filterKeyword.toLowerCase().trim();

  const activeList = galleryMembers.length ? galleryMembers : members;

  // 1. 顶部固定选项：全部成员 (聚合)
  const allItem = document.createElement("div");
  allItem.className = "member-popover-item" + (!curGalleryMember ? " active" : "");
  allItem.innerHTML =
    '<div class="m-name-txt">' +
    '<span class="mpi-avatar" style="background:var(--accent);color:#fff;">👥</span>' +
    '<span class="mpi-name">全部成员 (聚合)</span>' +
    '</div>' +
    '<span class="m-cnt">' + activeList.length + ' 人</span>';
  allItem.addEventListener("click", () => {
    closeGalleryMemberPopover();
    selectGalleryMember("");
  });
  list.appendChild(allItem);

  const filtered = activeList.filter(m => !kw || m.display.toLowerCase().includes(kw) || m.name.toLowerCase().includes(kw));
  if ($("galleryMemberTotalBadge")) {
    $("galleryMemberTotalBadge").textContent = "共 " + activeList.length + " 人" + (kw ? " · 匹配 " + filtered.length + " 人" : "");
  }

  const groups = [
    { key: "nogizaka", name: "乃木坂46", icon: "💜", cls: "nogi" },
    { key: "sakurazaka", name: "樱坂46", icon: "🌸", cls: "sakura" },
    { key: "hinatazaka", name: "日向坂46", icon: "🩵", cls: "hinata" }
  ];

  const visibleGroupKeys = [];
  groups.forEach(g => {
    const grpMems = filtered.filter(m => inferMemberGroup(m) === g.key);
    if (!grpMems.length) return;
    visibleGroupKeys.push(g.key);

    const groupHeader = createPopoverGroupHeader(g, grpMems.length, () => renderGalleryMemberPopover(filterKeyword));
    list.appendChild(groupHeader.head);
    if (groupHeader.collapsed) return;

    grpMems.forEach(m => {
      let avatarText = (m.display || "").replace(/[\s_　]/g, "");
      if (avatarText.length > 2) avatarText = avatarText.slice(-2);
      if (!avatarText) avatarText = "📷";

      let avatarHTML = '';
      if (m.avatar) {
        avatarHTML = '<img class="mpi-avatar-img" src="' + esc(m.avatar) + '" loading="lazy" decoding="async" alt="" onerror="this.style.display=\'none\';if(this.nextElementSibling)this.nextElementSibling.style.display=\'inline-flex\';" /><span class="mpi-avatar ' + g.cls + '" style="display:none;">' + esc(avatarText) + '</span>';
      } else {
        avatarHTML = '<span class="mpi-avatar ' + g.cls + '">' + esc(avatarText) + '</span>';
      }

      const curNorm = (curGalleryMember || "").replace(/[\s_　]/g, "");
      const mNorm = (m.name || "").replace(/[\s_　]/g, "");
      const dNorm = (m.display || "").replace(/[\s_　]/g, "");
      const isCur = curNorm && (curNorm === mNorm || curNorm === dNorm);

      const countNum = getGalleryMemberCountNum(m);
      const countText = countNum.toLocaleString() + " 张";

      const item = document.createElement("div");
      item.className = "member-popover-item " + g.cls + (isCur ? " active" : "");
      item.innerHTML = '<div class="m-name-txt">' +
                       avatarHTML +
                       '<span class="mpi-name">' + esc(m.display) + '</span>' +
                       '</div>' +
                       '<span class="m-cnt">' + esc(countText) + '</span>';

      item.addEventListener("click", () => {
        closeGalleryMemberPopover();
        selectGalleryMember(m.name);
      });
      list.appendChild(item);
    });
  });
  updatePopoverExpandAllControl("gallery", visibleGroupKeys);
}

function openGalleryMemberPopover() {
  const popover = $("galleryMemberPopover");
  if (!popover) return;
  popover.style.display = "block";
  const btn = $("btnGalleryMemberDropdown");
  if (btn) btn.classList.add("open");
  const input = $("galleryMemberSearchInput");
  if (input) {
    input.value = "";
    setTimeout(() => input.focus(), 60);
  }
  const clearBtn = $("btnGalleryMemberSearchClear");
  if (clearBtn) clearBtn.style.display = "none";
  if (!galleryMembers.length) {
    loadGalleryMembers();
  }
  renderGalleryMemberPopover("");
}

function closeGalleryMemberPopover() {
  const popover = $("galleryMemberPopover");
  if (!popover) return;
  popover.style.display = "none";
  const btn = $("btnGalleryMemberDropdown");
  if (btn) btn.classList.remove("open");
}

// 绑定相册交互事件
if ($("btnGalleryMemberDropdown")) {
  $("btnGalleryMemberDropdown").addEventListener("click", (e) => {
    e.stopPropagation();
    const pop = $("galleryMemberPopover");
    if (pop && pop.style.display !== "none") {
      closeGalleryMemberPopover();
    } else {
      openGalleryMemberPopover();
    }
  });
}

if ($("galleryMemberSearchInput")) {
  $("galleryMemberSearchInput").addEventListener("input", (e) => {
    const val = e.target.value;
    const clearBtn = $("btnGalleryMemberSearchClear");
    if (clearBtn) clearBtn.style.display = val ? "inline-flex" : "none";
    renderGalleryMemberPopover(val);
  });
}

if ($("btnGalleryMemberSearchClear")) {
  $("btnGalleryMemberSearchClear").addEventListener("click", (e) => {
    e.stopPropagation();
    const input = $("galleryMemberSearchInput");
    if (input) {
      input.value = "";
      input.focus();
    }
    $("btnGalleryMemberSearchClear").style.display = "none";
    renderGalleryMemberPopover("");
  });
}

document.addEventListener("click", (e) => {
  const wrap = $("galleryMemberDropdownWrap");
  if (wrap && !wrap.contains(e.target)) {
    closeGalleryMemberPopover();
  }
});

if ($("gallerySourceChips")) {
  const chips = $("gallerySourceChips").querySelectorAll(".chip");
  chips.forEach(chip => {
    chip.addEventListener("click", async () => {
      const src = chip.getAttribute("data-source") || "all";
      if (curGallerySource === src) return;
      curGallerySource = src;
      chips.forEach(c => c.classList.toggle("active", c === chip));
      updateGalleryMemberButtonDisplay();
      renderGalleryMemberPopover();
      await loadGalleryYears();
      syncGalleryHash();
      await loadGalleryPhotos(true);
    });
  });
}

if ($("btnGallerySortOrder")) {
  $("btnGallerySortOrder").addEventListener("click", () => {
    curGalleryOrder = curGalleryOrder === "desc" ? "asc" : "desc";
    syncGallerySortButton();
    syncGalleryHash();
    loadGalleryPhotos(true);
  });
}

if ($("galleryLoadMore")) {
  $("galleryLoadMore").addEventListener("click", () => {
    if (!curGalleryLoading) {
      if (curGalleryHasMore || $("galleryLoadMore").textContent.includes("重试")) {
        curGalleryPage += 1;
        loadGalleryPhotos(false);
      } else {
        $("galleryLoadMore").style.display = "none";
        if ($("galleryEndHint")) $("galleryEndHint").style.display = "block";
      }
    }
  });
  initGalleryObserver();
}

if ($("btnLayoutMasonry")) {
  $("btnLayoutMasonry").addEventListener("click", () => switchGalleryLayout("masonry"));
}
if ($("btnLayoutGrid")) {
  $("btnLayoutGrid").addEventListener("click", () => switchGalleryLayout("grid"));
}

let lastMasonryCols = getMasonryColCount();
let galleryResizeTimer = null;
window.addEventListener("resize", () => {
  if (curGalleryLayout !== "masonry") return;
  clearTimeout(galleryResizeTimer);
  galleryResizeTimer = setTimeout(() => {
    const newCols = getMasonryColCount();
    if (newCols !== lastMasonryCols) {
      lastMasonryCols = newCols;
      rebalanceMasonry();
    }
  }, 150);
});
