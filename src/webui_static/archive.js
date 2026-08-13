// archive.js
"use strict";
const $ = (id) => document.getElementById(id);
const authToken = localStorage.getItem("webAdminToken") || "";
const TYPES = [["", "全部"], ["text", "文字"], ["picture", "图片"], ["video", "视频"], ["voice", "语音"]];

let members = [];        // [{name, display, total, months}]
let blogGroups = [];     // [{key, total, first_date, last_date}]
let curMember = "";
let curBlogGroup = "";   // 非空 = 博客模式
let months = [];         // [{year, month, count}] 新的在前
let curYM = null;        // {year, month}
let curType = "";
let page = 1, totalPages = 1;
let images = [];         // 当月已渲染图片 [{url, caption}]，供灯箱翻页
let lastDay = "";
let searchQuery = "";    // 非空 = 搜索模式
let dayCounts = {};      // "YYYY-MM-DD" -> 条数（日历用）
let calYM = null;        // 日历当前显示的 {year, month}（可独立于时间线翻页）
let contentVersion = 0;  // 成员 / 月份 / 筛选变化后，旧响应不应覆盖新页面
let contentAbort = null;
let pageLoading = false;
let calendarVersion = 0;
let calendarAbort = null;
let lightboxOpener = null;
let memberVersion = 0;
let targetMsgId = "";    // 首页跳转目标消息 ID（避免被 syncHash 冲掉）
let curMode = "msg";     // "msg" 或 "blog"
let curBlogAuthor = "";  // 当前选中的博客作者
let curBlogDate = "";    // 当前选中的博客日期 (YYYY-MM-DD)
function esc(s) { const d = document.createElement("div"); d.textContent = String(s); return d.innerHTML; }
function mediaUrl(u) { return u + (authToken ? "?token=" + encodeURIComponent(authToken) : ""); }

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: authToken ? { "X-Auth-Token": authToken } : {},
    cache: "no-store",
    signal: options.signal,
  });
  const data = await resp.json();
  if (!resp.ok) throw new Error((data.errors || ["HTTP " + resp.status]).join("；"));
  return data;
}

function normalizedQuery(value) { return String(value || "").trim().slice(0, 100); }
function syncSearchInput() { $("searchBox").value = searchQuery; $("searchClear").hidden = !searchQuery; }
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
  $("loadMore").textContent = "加载更多 ↓";
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
  $("loadMore").textContent = loading ? "加载中…" : "加载更多 ↓";
}


// ── JST 时间 ─────────────────────────────────────
function toJst(utc) { return new Date(new Date(utc).getTime() + 9 * 3600 * 1000); }
function fmtDay(utc) {
  const d = toJst(utc);
  const w = "日一二三四五六"[d.getUTCDay()];
  return d.getUTCFullYear() + "/" + (d.getUTCMonth() + 1) + "/" + d.getUTCDate() + "（" + w + "）";
}
function fmtTime(utc) {
  const d = toJst(utc);
  return String(d.getUTCHours()).padStart(2, "0") + ":" + String(d.getUTCMinutes()).padStart(2, "0");
}
function fmtCopyTime(utc) {
  const d = toJst(utc);
  const m = String(d.getUTCMonth() + 1).padStart(2, "0");
  const day = String(d.getUTCDate()).padStart(2, "0");
  const hh = String(d.getUTCHours()).padStart(2, "0");
  const mm = String(d.getUTCMinutes()).padStart(2, "0");
  const ss = String(d.getUTCSeconds()).padStart(2, "0");
  return m + "/" + day + " " + hh + ":" + mm + ":" + ss;
}

function fmtDateKey(utc) {
  const d = toJst(utc);
  return d.getUTCFullYear() + "-" + String(d.getUTCMonth() + 1).padStart(2, "0") +
         "-" + String(d.getUTCDate()).padStart(2, "0");
}

// ── 日历 ─────────────────────────────────────────
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
    dayCounts = data.ok ? (data.days || {}) : {};
  } catch (e) {
    if (e.name === "AbortError" || version !== calendarVersion) return;
    dayCounts = {};
  }
  if (version !== calendarVersion) return;
  
  if (!calYM || Object.keys(dayCounts).length > 0) {
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
  if (!calYM) return;
  const { year, month } = calYM;
  $("calTitle").textContent = year + " 年 " + month + " 月";
  const grid = $("calGrid");
  grid.innerHTML = "";
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
    let cls = "cal-day" + (n > 0 ? " has" : "") +
      (n >= 6 ? " h3" : n >= 3 ? " h2" : n >= 1 ? " h1" : "");
    if (curMode === "blog" && curBlogDate === key) {
      cls += " active-day";
    }
    cell.className = cls;
    cell.textContent = d;
    if (n > 0) {
      cell.type = "button";
      cell.title = key + " · " + n + " 条";
      cell.setAttribute("aria-label", key + "，共 " + n + " 条消息，跳转到当天");
      const count = document.createElement("span");
      count.className = "n";
      count.textContent = n;
      cell.appendChild(count);
      cell.addEventListener("click", () => jumpToDay(key));
    }
    grid.appendChild(cell);
  }
  $("calFoot").textContent = monthTotal > 0 ? "本月 " + monthTotal + " 条 · 点日期跳转" : "本月无消息";
}

$("calPrev").addEventListener("click", () => {
  calYM = calYM.month === 1 ? { year: calYM.year - 1, month: 12 }
                            : { year: calYM.year, month: calYM.month - 1 };
  renderCalendar();
});
$("calNext").addEventListener("click", () => {
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

// ── 模式切换 ─────────────────────────────────────
function switchMainTab(mode, keepHash) {
  curMode = mode;
  $("tabMsg").classList.toggle("active", mode === "msg");
  $("tabBlog").classList.toggle("active", mode === "blog");
  $("memberChips").style.display = mode === "msg" ? "" : "none";
  $("blogGroupChips").style.display = mode === "blog" ? "" : "none";

  if (mode === "msg") {
    if (!keepHash) goHome();
  } else {
    if (!keepHash) {
      if (blogGroups && blogGroups.length > 0) {
        selectBlogGroup(blogGroups[0].key);
      } else {
        location.hash = "blog="; // 触发 hashchange 或 reload
        showBlogHome();
      }
    }
  }
}

$("tabMsg").addEventListener("click", () => switchMainTab("msg"));
$("tabBlog").addEventListener("click", () => switchMainTab("blog"));

function showBlogHome() {
  $('archiveHome').classList.remove('active');
  $('backTop').style.display = ''; $('backTop').classList.remove('force-hide');
  document.querySelector('.layout').style.display = '';
  $("timeline").innerHTML = '<div class="center">请在上方选择一个坂道组</div>';
  $("blogGrid").style.display = "none";
  $("timeline").style.display = "";
  $("loadMore").hidden = true;
  $("emptyHint").hidden = true;
  document.querySelectorAll(".toolbar")[0].style.display = "none";
  document.querySelectorAll(".toolbar")[1].style.display = "none";
  $("archiveSide").style.display = "none";
  syncChipHighlight();
}

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
  // 渲染成员 chips
  const box = $("memberChips");
  box.innerHTML = "";
  for (const m of members) {
    const b = document.createElement("button");
    b.className = "chip";
    if (m.name === curMember && curMode === "msg") b.classList.add("active");
    b.dataset.key = m.name;
    b.textContent = "💬 " + m.display + "（" + m.total + "）";
    b.addEventListener("click", () => { hideHome(); selectMember(m.name); });
    box.appendChild(b);
  }
  
  loadBlogGroupChips();

  // skipSelect=true 时只渲染 chips，不自动跳转（首页模式下使用）
  if (skipSelect) return;
  const wanted = curMember && members.some((m) => m.name === curMember) ? curMember : members[0].name;
  await selectMember(wanted, true);
}

async function loadBlogGroupChips() {
  try {
    const bg = await api("/api/archive/blog_groups");
    if (bg.ok) blogGroups = bg.groups;
    const box = $("blogGroupChips");
    box.innerHTML = "";
    const BLOG_NAMES = {hinatazaka:"📝 日向坂46", nogizaka:"📝 乃木坂46", sakurazaka:"📝 樱坂46"};
    for (const g of blogGroups) {
      const b = document.createElement("button");
      b.className = "chip";
      if (g.key === curBlogGroup && curMode === "blog") b.classList.add("active");
      b.dataset.key = g.key;
      b.textContent = (BLOG_NAMES[g.key] || g.key) + "（" + g.total + "）";
      b.addEventListener("click", () => { selectBlogGroup(g.key); });
      box.appendChild(b);
    }
    syncChipHighlight();
  } catch(e) {}
}

function _enterMemberMode() {
  curMode = "msg";
  curBlogGroup = "";
  $("archiveSide").style.display = "";
  $("blogGrid").style.display = "none";
  $("timeline").style.display = "";
  document.querySelectorAll(".toolbar").forEach(t => t.style.display = "");
  $("tagToggle").parentElement.style.display = "";
  $("searchBox").style.display = $("searchSubmit").style.display = $("searchClear").style.display = "";
}

// 根据 curMember / curBlogGroup 同步所有 chip 高亮状态
function syncChipHighlight() {
  $("memberChips").querySelectorAll(".chip").forEach(c => {
    c.classList.toggle("active", curMode === "msg" && c.dataset.key === curMember);
  });
  $("blogGroupChips").querySelectorAll(".chip").forEach(c => {
    c.classList.toggle("active", curMode === "blog" && c.dataset.key === curBlogGroup);
  });
}

// ── 博客相关逻辑 ─────────────────────────────────────
async function selectBlogGroup(key) {
  curMode = "blog";
  curMember = "";
  curBlogGroup = key;
  curBlogAuthor = "";
  curBlogDate = "";
  searchQuery = "";
  syncSearchInput();
  syncChipHighlight();
  location.hash = "blog=" + encodeURIComponent(key);
  
  $('archiveHome').classList.remove('active');
  $('backTop').style.display = ''; $('backTop').classList.remove('force-hide');
  document.querySelector('.layout').style.display = '';
  $("timeline").style.display = "none";
  $("blogGrid").style.display = "";
  $("archiveSide").style.display = "";
  document.querySelectorAll(".toolbar")[0].style.display = "none";
  document.querySelectorAll(".toolbar")[1].style.display = "";
  $("tagToggle").parentElement.style.display = "none";
  
  await loadBlogAuthors(key);
  await loadBlogCalendar();
  await loadBlogPage(1, true);
}

async function loadBlogAuthors(key) {
  try {
    const data = await api("/api/archive/blog_authors?group=" + encodeURIComponent(key));
    const bar = $("blogAuthorBar");
    bar.innerHTML = "";
    if (data.ok && data.authors) {
      const allBtn = document.createElement("button");
      allBtn.className = "blog-author-chip active";
      allBtn.textContent = "全部成员";
      allBtn.onclick = () => selectBlogAuthor("");
      bar.appendChild(allBtn);
      
      data.authors.forEach(a => {
        const btn = document.createElement("button");
        btn.className = "blog-author-chip";
        btn.textContent = a.name;
        btn.onclick = () => selectBlogAuthor(a.name);
        bar.appendChild(btn);
      });
    }
  } catch (e) {}
}

function selectBlogAuthor(author) {
  curBlogAuthor = author;
  curBlogDate = "";
  renderCalendar();
  const btns = $("blogAuthorBar").querySelectorAll(".blog-author-chip");
  btns.forEach(b => {
    if ((author === "" && b.textContent === "全部成员") || b.textContent === author) {
      b.classList.add("active");
    } else {
      b.classList.remove("active");
    }
  });
  
  const p = new URLSearchParams({ blog: curBlogGroup });
  if (author) p.set("author", author);
  selfHashUpdate = true;
  location.hash = p.toString();
  setTimeout(() => { selfHashUpdate = false; }, 0);
  
  loadBlogCalendar();
  loadBlogPage(1);
}

// ── 渲染博客网格 ─────────────────────────────────────
async function loadBlogPage(pageNum) {
  page = pageNum;
  $("blogCards").innerHTML = "";
  $("blogHero").style.display = "none";
  $("blogHero").innerHTML = "";
  $("emptyHint").hidden = true;
  $("blogPagination").style.display = "none";
  $("blogPagination").innerHTML = "";
  $("loadMore").hidden = true;
  
  setPageLoading(true);
  try {
    let perPage = (pageNum === 1 && !searchQuery && !curBlogDate) ? 25 : 24;
    let url = "/api/archive/blogs?group=" + encodeURIComponent(curBlogGroup) + "&page=" + pageNum + "&per_page=" + perPage;
    if (curBlogAuthor) url += "&author=" + encodeURIComponent(curBlogAuthor);
    if (curBlogDate) url += "&date=" + encodeURIComponent(curBlogDate);
    if (searchQuery) url += "&q=" + encodeURIComponent(searchQuery);
    
    const data = await api(url);
    if (!data.ok) throw new Error("加载失败");
    
    totalPages = data.total_pages;
    if (data.posts.length === 0) {
      $("emptyHint").textContent = curBlogDate ? (curBlogDate + " 暂无符合条件的博客") : "没有找到博客";
      $("emptyHint").hidden = false;
    } else {
      let posts = data.posts;
      if (pageNum === 1 && posts.length > 0 && !searchQuery && !curBlogDate) {
        renderBlogHero(posts[0]);
        posts = posts.slice(1);
      }
      posts.forEach(p => {
        renderBlogMiniCard(p, $("blogCards"));
      });
      renderBlogPagination(pageNum, totalPages);
      
      if (pageNum > 1) {
        window.scrollTo({ top: 0, behavior: 'smooth' });
      }
    }
  } catch (e) {
    $("emptyHint").textContent = "加载错误: " + e.message;
    $("emptyHint").hidden = false;
  }
  setPageLoading(false);
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
  
  let images = [], paths = [];
  try { images = JSON.parse(post.images_json || "[]"); } catch(e) {}
  try { paths = JSON.parse(post.image_paths_json || "[]"); } catch(e) {}
  let bodyHtml = post.body_html || "";
  bodyHtml = _replaceImgUrls(bodyHtml, images, paths);
  
  // 提取第一张图作为 Hero 封面图
  let coverUrl = _getCoverUrl(bodyHtml);

  let coverHtml = '';
  if (coverUrl) {
    coverHtml = '<div class="bh-cover" style="background-image: url(\'' + esc(coverUrl) + '\')"><img src="' + esc(coverUrl) + '" alt=""></div>';
  } else {
    // 无封面链接：保留原有无封面样式（📝 占位）
    coverHtml = '<div class="bh-cover no-pic" style="font-size:48px; color:var(--muted)">📝</div>';
  }

  hero.innerHTML =
    coverHtml +
    '<div class="bh-info">' +
      '<div class="bh-meta"><span class="bh-author">' + esc(post.author) + '</span><span class="bh-date">' + esc(dateStr) + '</span></div>' +
      '<h2 class="bh-title">' + highlightQuery(post.title || '无题', searchQuery) + '</h2>' +
      '<div class="bh-excerpt">' + esc(bodyHtml.replace(/<[^>]+>/g, '').substring(0, 150)) + '...</div>' +
    '</div>';

  // 封面图加载失败（404/防盗链/资源不存在）→ 降级为 📝 占位
  const heroCoverImg = hero.querySelector('.bh-cover img');
  if (heroCoverImg) {
    heroCoverImg.addEventListener('error', () => {
      const cover = heroCoverImg.parentElement;
      if (cover) {
        cover.outerHTML = '<div class="bh-cover no-pic" style="font-size:48px; color:var(--muted)">📝</div>';
      }
    });
  }

  hero.onclick = function(e) {
    if (e.target.tagName === 'A') return;
    openBlogReader(post, bodyHtml);
  };
}

function renderBlogMiniCard(post, container) {
  const grid = container || $("blogCards");
  const dateStr = (post.date || "").substring(0, 16);
  
  let images = [], paths = [];
  try { images = JSON.parse(post.images_json || "[]"); } catch(e) {}
  try { paths = JSON.parse(post.image_paths_json || "[]"); } catch(e) {}
  let bodyHtml = post.body_html || "";
  bodyHtml = _replaceImgUrls(bodyHtml, images, paths);
  
  let coverUrl = _getCoverUrl(bodyHtml);

  const card = document.createElement("div");
  card.className = "bmc-card blog-card-mini";
  card.dataset.date = (post.date || "").substring(0, 10);

  let html = '';
  if (coverUrl) {
    html += '<div class="bc-cover"><img src="' + esc(coverUrl) + '" alt="" loading="lazy"></div>';
  } else {
    // 无封面链接：保留原有无封面样式（📝 占位）
    html += '<div class="bc-cover no-pic">📝</div>';
  }

  let excerpt = "";
  if (searchQuery) {
    let fullText = "";
    if (post.body_text) fullText += post.body_text.replace(/\s+/g, " ");
    else if (post.body_html) fullText += post.body_html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
    if (post.translation) fullText += " " + post.translation.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");
    
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
      const cover = coverImg.parentElement;
      if (cover) {
        cover.outerHTML = '<div class="bc-cover no-pic">📝</div>';
      }
    });
  }

  card.onclick = function(e) {
    if (e.target.tagName === 'A') return;
    openBlogReader(post, bodyHtml);
  };

  grid.appendChild(card);
}

let currentBlogReaderPost = null;
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

function renderBlocks(blocks, mode) {
  const parts = [];
  for (const b of blocks) {
    if (b.type === "img") {
      parts.push('<img src="' + esc(b.src || "") + '" referrerpolicy="no-referrer" loading="lazy">');
      continue;
    }
    const jp = esc(b.jp || "").replace(/\n/g, "<br>");
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
    if (window._isLoggedIn && hasTrans) {
      delBtn.style.display = "inline-block";
    } else {
      delBtn.style.display = "none";
    }
  }
}

function renderCurrentBlogContent() {
  if (!currentBlogReaderPost) return;
  const blocks = getStructuredBlocks(currentBlogReaderPost);
  const images = JSON.parse(currentBlogReaderPost.images_json || "[]");
  const paths = JSON.parse(currentBlogReaderPost.image_paths_json || "[]");

  let bodyHtml = "";
  if (blocks && currentTransMode !== "ja-only") {
    // 中文 / 日中对照：从解耦的结构化数据渲染（日中对照按 jp/zh 插值）
    bodyHtml = _replaceImgUrls(renderBlocks(blocks, currentTransMode), images, paths);
  } else {
    // 日文（或暂无结构化译文）：直接渲染原始日文 body_html，100% 完整不丢任何段落/图片
    bodyHtml = _replaceImgUrls(currentBlogReaderPost.body_html || "", images, paths);
  }

  // 翻译模型标记：仅在「日中对照/中文」视图且存在译文时展示，右对齐次级灰字
  const modelName = currentBlogReaderPost.translation_model || "";
  const showModel = blocks && currentTransMode !== "ja-only" && modelName;
  const modelTag = showModel
    ? '<div class="br-model-tag">翻译模型：' + esc(modelName) + '</div>'
    : '';

  $("brContent").innerHTML =
    '<div class="br-meta">' +
      '<div><span class="br-author">' + esc(currentBlogReaderPost.author) + '</span><span style="margin-left:12px">' + esc((currentBlogReaderPost.date || "").substring(0, 16)) + '</span></div>' +
      '<a class="br-link" href="' + esc(currentBlogReaderPost.url) + '" target="_blank">阅读原文 ↗</a>' +
    '</div>' +
    modelTag +
    '<h1 style="margin-top:0; font-size:24px;">' + esc(currentBlogReaderPost.title || "无题") + '</h1>' +
    bodyHtml;

  updateModeSelectorUI();
}

function openBlogReader(post, bodyHtml) {
  currentBlogReaderPost = post;
  // 进入博客时，若已有译文则默认选中「日中对照」
  if (hasTranslation(post)) {
    currentTransMode = "ja-zh";
  }
  $("brTitle").textContent = post.title || "无题";
  
  const transBtn = $("brTranslate");
  if (transBtn) {
    if (!window._isArchiveAdmin) {
      transBtn.style.display = "none";
    } else {
      transBtn.style.display = "";
      if (hasTranslation(post)) {
        transBtn.textContent = "✓ 已翻译";
        transBtn.disabled = true;
      } else {
        transBtn.textContent = "🌐 翻译";
        transBtn.disabled = false;
      }
    }
  }

  renderCurrentBlogContent();
  $("blogReader").style.display = "";
  $("blogReader").scrollTop = 0;
  
  document.body.style.overflow = "hidden";
  if (typeof handleBackTopScroll === "function") handleBackTopScroll();

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

const brCloseBtn = $("brClose");
if (brCloseBtn) {
  brCloseBtn.addEventListener("click", () => {
    $("blogReader").style.display = "none";
    document.body.style.overflow = "";
    $("brContent").innerHTML = "";
    if (typeof handleBackTopScroll === "function") handleBackTopScroll();
  });
}

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
          transBtn.textContent = "🌐 翻译";
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
    
    brTranslateBtn.textContent = "⏳ 翻译中 (需10~30秒)...";
    brTranslateBtn.disabled = true;
    
    try {
      const res = await fetch("/api/archive/blogs/translate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: currentBlogReaderPost.id })
      });
      const data = await res.json();
      if (data.ok && data.html) {
        currentBlogReaderPost.translation = data.html;
        if (data.content_json) currentBlogReaderPost.content_json = data.content_json;
        if (data.translation_model) currentBlogReaderPost.translation_model = data.translation_model;
        currentTransMode = "ja-zh";  // 翻译完成自动切回「日中对照」
        if ($("blogReader").style.display !== "none") {
          renderCurrentBlogContent();
          brTranslateBtn.textContent = "✓ 已翻译";
        }
      } else {
        alert(data.msg || "翻译失败");
        brTranslateBtn.textContent = "🌐 重试翻译";
        brTranslateBtn.disabled = false;
      }
    } catch(err) {
      alert("网络异常: " + err);
      brTranslateBtn.textContent = "🌐 重试翻译";
      brTranslateBtn.disabled = false;
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
    let localPath = paths[i] || "";
    if (localPath) {
        localPath = localPath.replace(/\\/g, '/');
    }
    const encodedPath = localPath ? localPath.split('/').map(encodeURIComponent).join('/') : "";
    const local = encodedPath ? "/api/archive/blog_media/" + encodedPath : orig;
    result = result.split(orig).join(local);
    try { result = result.split(esc(orig)).join(local); } catch(e) {}
    
    // 乃木坂/樱坂的 body_html 含相对 src，尝试仅用 URL 路径部分匹配
    try {
      // 提供 base url 避免 orig 是相对路径时 throw error
      const u = new URL(orig, "https://dummy.com");
      const relPath = u.pathname + u.search;
      if (relPath && relPath !== orig && relPath !== '/') {
        result = result.split(relPath).join(local);
        try { result = result.split(esc(relPath)).join(local); } catch(e) {}
      }
    } catch(e) {}
  }
  return result;
}

// Removed old renderBlogBubble

async function selectMember(name, keepHash) {
  const version = ++memberVersion;
  curMember = name;
  curBlogGroup = "";     // 切换到成员模式，清空博客分组
  syncChipHighlight();  // 同步 chip 高亮
  if (!keepHash) searchQuery = "";
  syncSearchInput();
  const data = await api("/api/archive/months?member=" + encodeURIComponent(name));
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
  if (!months.length) { showEmpty("该成员还没有归档内容"); return; }
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
  selfHashUpdate = true;
  const p = new URLSearchParams({ member: curMember, y: curYM.year, m: curYM.month });
  if (curType) p.set("t", curType);
  if (searchQuery) p.set("q", searchQuery);
  location.hash = p.toString();
  setTimeout(() => { selfHashUpdate = false; }, 0);
}

// 同一标签页里粘贴另一个深链（或前进/后退）时，hash 变了但 SPA 不会重载，
// 需要手动把新状态应用上
window.addEventListener("hashchange", async () => {
  if (selfHashUpdate) return;
  const p = new URLSearchParams(location.hash.slice(1));
  const m = p.get("member") || "";
  const y = parseInt(p.get("y"), 10), mo = parseInt(p.get("m"), 10);
  const t = p.get("t") || "";
  const q = normalizedQuery(p.get("q"));
  if (t !== curType) {
    curType = t;
    $("typeChips").querySelectorAll(".chip").forEach((c, i) =>
      c.classList.toggle("active", TYPES[i][0] === curType));
    loadCalendar();
  }
  if (m && m !== curMember && members.some((x) => x.name === m)) {
    searchQuery = q;
    syncSearchInput();
    await selectMember(m, true);
  } else if (p.has("blog")) {
    const b = p.get("blog") || "";
    const a = p.get("author") || "";
    switchMainTab("blog", true);
    if (b && (b !== curBlogGroup || a !== curBlogAuthor)) {
      curBlogGroup = b;
      curBlogAuthor = a;
      await selectBlogGroup(b);
      if (a) selectBlogAuthor(a);
    } else if (!b) {
      showBlogHome();
    }
  } else if (q !== searchQuery) {
    searchQuery = q;
    syncSearchInput();
    if (searchQuery) startSearch(searchQuery, false);
    else if (curYM) selectMonth(curYM.year, curYM.month);
  } else if (y && mo && curYM && (y !== curYM.year || mo !== curYM.month)) {
    await selectMonth(y, mo);
  } else if (t !== undefined && curYM) {
    await selectMonth(curYM.year, curYM.month);
  }
});

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
      "&type=" + curType + "&page=" + page + "&per_page=50"
    : "/api/archive/messages?member=" + encodeURIComponent(curMember) +
      "&year=" + curYM.year + "&month=" + curYM.month +
      "&type=" + curType + "&page=" + page + "&per_page=50";
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
      (data.capped ? "（已达上限，仅显示最新 500 条）" : "");
    if (!data.messages.length && page === 1) showEmpty("没有匹配「" + searchQuery + "」的消息");
  } else {
    $("stats").textContent = curYM.year + "/" + curYM.month + " · " + data.total + " 条";
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
  if (updateHash) syncHash();
  if (!searchQuery) {
    if (curMode === "blog") { loadBlogPage(1, true); return; }
    if (curYM) { selectMonth(curYM.year, curYM.month); return; }
    return;
  }
  if (curMode === "blog") {
    loadBlogPage(1, true);
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
  let html = '<div class="time">' + fmtTime(msg.published_at) + " · " + esc(msg.type);
  if (searchQuery && msg.year) {
    const dateKey = fmtDateKey(msg.published_at);
    html += ' · <a href="#" class="jump" data-date="' + dateKey +
            '" style="color:var(--accent); text-decoration:none">查看当日 →</a>';
  }
  const hasText = Boolean((msg.text && msg.text.trim()) || (msg.translation && msg.translation.trim()));
  if (hasText) {
    html += '<button type="button" class="copy-btn" title="复制整条消息与译文">📋 复制</button>';
  }
  html += "</div>";



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
    html += '<div class="miss">⚠️ 媒体文件下载失败（可用回填工具重试）</div>';
  }
  if (msg.text) html += '<div class="text">' + highlightQuery(msg.text, searchQuery) + "</div>";
  if (msg.translation) html += '<div class="trans">' + highlightQuery(msg.translation, searchQuery) + "</div>";
  b.innerHTML = html;

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
        const resp = await fetch("/api/archive/tags?member=" + encodeURIComponent(curMember), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: msgId, year: msgYear, month: msgMonth, custom_tags: val }),
        });
        const data = await resp.json();
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
          alert("保存失败：" + (data.errors || []).join("；"));
        }
      } catch (e) { alert("保存失败：" + e.message); }
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
      const mName = curMember || "成员";
      const timeStr = fmtCopyTime(msg.published_at);
      parts.push(mName + " " + timeStr);

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
function openLightbox(i, opener) {
  if (i < 0 || i >= images.length) return;
  if (opener) lightboxOpener = opener;
  lbIndex = i;
  $("lbImg").src = images[i].url;
  $("lbImg").alt = images[i].caption || "归档图片";
  $("lbCounter").textContent = (i + 1) + " / " + images.length;
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
  const adminIds = ["adminLink", "archiveToolsDropdown", "btnArchiveMember", "btnArchiveMessage"];
  adminIds.forEach(id => {
    const el = $(id);
    if (el) {
      el.hidden = !isAdmin;
      el.style.display = isAdmin ? (el.classList.contains("header-dropdown") ? "inline-block" : "inline-flex") : "none";
    }
  });
}

// ── 成员工具下拉菜单点击切换与点击外部自动关闭 ────────────────
const archiveDropdownEl = $("archiveToolsDropdown");
const archiveBtnEl = $("btnArchiveMenu");
if (archiveBtnEl && archiveDropdownEl) {
  archiveBtnEl.addEventListener("click", (e) => {
    e.stopPropagation();
    archiveDropdownEl.classList.toggle("open");
  });
  document.addEventListener("click", (e) => {
    if (!archiveDropdownEl.contains(e.target)) {
      archiveDropdownEl.classList.remove("open");
    }
  });
}

// ── 登录状态 ─────────────────────────────────────
window._isLoggedIn = false;
(async function initAuth() {
  try {
    const me = await (await fetch("/api/auth/me", { cache: "no-store" })).json();
    if (!me.auth_enabled) { 
      window._isLoggedIn = true; 
      _updateAdminUI(true);
      $("logoutBtn").hidden = true;
      $("logoutBtn").style.display = "none";
      return; 
    }
    if (me.user) {
      window._isLoggedIn = true;
      $("whoami").textContent = "👤 " + me.user.username;
      $("logoutBtn").hidden = false;
      $("logoutBtn").style.display = "inline-flex";
      _updateAdminUI(me.user.role === "admin");
    } else {
      window._isLoggedIn = false;
      $("whoami").textContent = "";
      $("logoutBtn").hidden = true;
      $("logoutBtn").style.display = "none";
      _updateAdminUI(false);
    }
  } catch (e) { /* 忽略 */ }
})();
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
function fmtDate(utc) {
  if (!utc) return "";
  const d = new Date(utc.endsWith("Z") ? utc : utc + "Z");
  if (isNaN(d.getTime())) return utc.slice(0, 10);
  return (d.getMonth()+1) + "/" + d.getDate() + " " +
    String(d.getHours()).padStart(2,"0") + ":" + String(d.getMinutes()).padStart(2,"0");
}

function fmtDateShort(utc) {
  if (!utc) return "";
  const d = new Date(utc + "Z");
  if (isNaN(d.getTime())) return utc.slice(0, 10);
  return (d.getMonth()+1) + "/" + d.getDate();
}

async function showHome() {
  document.querySelector('.layout').style.display = 'none';
  $('backTop').classList.remove('show'); $('backTop').classList.add('force-hide');
  $('archiveHome').classList.add('active');
  // 骨架屏
  $('homeSkeleton').classList.add('active');
  $('archiveHome').querySelector('.home-hero').style.display = 'none';
  try {
    const data = await api("/api/archive/home");
    if (!data.members.length) {
      // 空状态
      $('homeSkeleton').classList.remove('active');
      $('archiveHome').innerHTML =
        '<div class="home-empty active"><div class="ee-icon">📭</div>' +
        '<div class="ee-title">还没有归档数据</div>' +
        '<div class="ee-desc">确认 config.json 的 archive.enabled 已开启。<br>新消息会自动归档；历史消息用 <code>python tools/backfill_archive.py</code> 回填。<br><br><a href="/">⚙️ 前往管理端</a></div></div>';
      return;
    }
    renderHome(data.aggregated, data.members);
    $('homeSkeleton').classList.remove('active');
    $('archiveHome').querySelector('.home-hero').style.display = '';
  } catch (e) {
    $('homeSkeleton').classList.remove('active');
    $('archiveHome').innerHTML = '<div style="text-align:center;color:var(--err);padding:60px 20px">加载失败：' + esc(e.message) + '</div>';
  }
}

function renderHome(agg, members) {
  // ── Hero 卡片 ──
  const single = members.length === 1;
  let heroHTML = '<div class="hc-icon">' + (single ? '⛩️' : '🏠') + '</div>';
  heroHTML += '<div class="hc-name">' + (single ? esc(members[0].display) : members.length + ' 位成员') + '</div>';
  if (members.length > 1) {
    heroHTML += '<div class="hc-sub">' + members.map(m => esc(m.display)).join(' · ') + '</div>';
  }
  const totalMonths = members.reduce((s, m) => s + m.stats.months, 0);
  const ws = agg.week_stats || {};
  heroHTML += '<div class="hc-stats">';
  heroHTML += '<span class="hc-stat">📨 <b>' + agg.total_msgs.toLocaleString() + '</b> 条消息</span>';
  heroHTML += '<span class="hc-stat">📅 跨越 <b>' + totalMonths + '</b> 个月</span>';
  if (ws.this_week > 0) {
    let weekStr = '本周 <b>' + ws.this_week + '</b> 条';
    if (ws.last_week > 0 && ws.this_week !== ws.last_week) {
      const diff = ws.this_week - ws.last_week;
      weekStr += ' · 较上周 ' + (diff > 0 ? '↑' : '↓') + Math.abs(diff);
    }
    heroHTML += '<span class="hc-stat">📊 ' + weekStr + '</span>';
  }
  heroHTML += '</div>';
  heroHTML += '<div class="hc-range">' + (agg.first_date || '?') + ' — ' + (agg.last_date || '?') + '</div>';
  // 今日动态 + 最后更新
  const today = new Date();
  const todayKey = today.getFullYear() + "-" + String(today.getMonth()+1).padStart(2,"0") + "-" + String(today.getDate()).padStart(2,"0");
  const todayCount = members.reduce((s, m) => s + ((m.days || {})[todayKey] || 0), 0);
  const lu = agg.last_updated ? fmtDate(agg.last_updated) : '';
  let badgeHTML = '';
  if (todayCount > 0) {
    badgeHTML += '<button class="hc-today" id="hcTodayBtn">🆕 今日 ' + todayCount + ' 条</button> ';
  }
  if (lu) {
    badgeHTML += '<span style="font-size:11.5px;color:var(--muted)">最近更新 ' + lu + '</span>';
  }
  if (badgeHTML) heroHTML += '<div style="margin-top:10px">' + badgeHTML + '</div>';
  $("homeMember").innerHTML = heroHTML;

  // 今日按钮点击
  const todayBtn = $("hcTodayBtn");
  if (todayBtn) {
    const defaultMember = members[0].name;
    const latestMonth = members[0].monthly && members[0].monthly[0];
    const ty = latestMonth ? latestMonth.year : today.getFullYear();
    const tm = latestMonth ? latestMonth.month : (today.getMonth() + 1);

    const goToday = async (e) => {
      e.preventDefault();
      curMode = "msg";
      switchMainTab("msg", true);
      hideHome();

      // 1. 切换成员
      curMember = defaultMember;
      curType = "";
      searchQuery = "";
      syncSearchInput();
      targetMsgId = "";

      // 2. 更新 hash
      selfHashUpdate = true;
      location.hash = "member=" + encodeURIComponent(defaultMember) + "&y=" + ty + "&m=" + tm;
      setTimeout(() => { selfHashUpdate = false; }, 100);

      // 3. 等待成员月份加载（selectMember -> selectMonth -> loadPage）
      await selectMember(defaultMember, false);

      // 4. 加载当月剩余页直到今日 day-sep 出现
      const todayDateKey = today.getFullYear() + "-"
        + String(today.getMonth() + 1).padStart(2, "0") + "-"
        + String(today.getDate()).padStart(2, "0");
      const loadVersion = contentVersion;
      while (loadVersion === contentVersion && page < totalPages) {
        const sep = document.querySelector('.day-sep[data-date="' + todayDateKey + '"]');
        if (sep) break;
        page++;
        await loadPage();
      }

      // 5. 精确滚动：用 getBoundingClientRect 扣除固定 Header 高度
      const scrollToToday = () => {
        const sep = document.querySelector('.day-sep[data-date="' + todayDateKey + '"]');
        if (!sep) return false;
        const headerEl = document.querySelector("header, .header, nav.header, #header");
        const headerH = headerEl ? headerEl.getBoundingClientRect().height : 64;
        const targetY = sep.getBoundingClientRect().top + window.scrollY - headerH - 16;
        window.scrollTo({ top: Math.max(0, targetY), behavior: "smooth" });
        return true;
      };

      if (!scrollToToday()) {
        // 未找到 day-sep：今日可能无消息，提示
        return;
      }

      // 6. 图片懒加载后二次校正（防止 DOM 高度变化导致偏移）
      setTimeout(scrollToToday, 400);
      setTimeout(scrollToToday, 1000);

      // 7. 高亮今日第一条消息 bubble
      setTimeout(() => {
        const sep = document.querySelector('.day-sep[data-date="' + todayDateKey + '"]');
        if (!sep) return;
        // 高亮 sep 本身
        sep.classList.add("flash");
        setTimeout(() => sep.classList.remove("flash"), 2500);
        // 同时对 sep 后面紧接的第一个 bubble 施加脉冲高亮
        let next = sep.nextElementSibling;
        while (next && !next.classList.contains("bubble")) {
          next = next.nextElementSibling;
        }
        if (next) {
          next.classList.add("today-flash");
          setTimeout(() => next.classList.remove("today-flash"), 1600);
        }
      }, 600);
    };

    todayBtn.addEventListener("click", goToday);
    todayBtn.addEventListener("touchend", goToday);
  }

  // ── Section: 最新写真 ──
  const strip = $("photoStrip");
  if (agg.pics.length) {
    strip.innerHTML = agg.pics.map(p =>
      '<div class="photo-card" data-member="' + esc(p.member) + '" data-year="' + p.year + '" data-month="' + p.month + '" data-id="' + p.id + '">' +
        (members.length > 1 ? '<span class="pc-member">' + esc(p.member_display) + '</span>' : '') +
        '<img src="' + mediaUrl(p.url) + '" alt="" loading="lazy">' +
        (p.text ? '<div class="pc-cap">' + esc(p.text) + '</div>' : '') +
      '</div>'
    ).join('');
    strip.querySelectorAll('.photo-card').forEach(el => {
      el.addEventListener('click', () => {
        curMode = "msg";
        switchMainTab("msg", true);
        hideHome();
        curMember = el.dataset.member;
        curType = "";
        searchQuery = "";
        syncSearchInput();
        targetMsgId = el.dataset.id;
        selfHashUpdate = true;
        location.hash = "member=" + encodeURIComponent(el.dataset.member) + "&y=" + el.dataset.year + "&m=" + el.dataset.month;
        setTimeout(() => { selfHashUpdate = false; }, 100);
        loadMembers();
      });
    });
  } else {
    strip.innerHTML = '<div style="color:var(--muted);padding:30px 10px;text-align:center">暂无图片</div>';
  }

  // 图片条自动滚动
  let photoTimer = null;
  let photoScrolling = false;
  function photoAdvance() {
    if (photoScrolling) return;
    if (strip.scrollWidth <= strip.clientWidth) return;
    const step = (strip.querySelector('.photo-card')?.offsetWidth || 172) + 10;
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
  function startPhotoScroll() { if (!photoTimer) photoTimer = setInterval(photoAdvance, 2200); }
  function stopPhotoScroll() { clearInterval(photoTimer); photoTimer = null; photoScrolling = false; }
  startPhotoScroll();
  strip.addEventListener("touchstart", stopPhotoScroll, { once: true });
  strip.addEventListener("wheel", stopPhotoScroll, { once: true });
  strip.addEventListener("mouseenter", stopPhotoScroll);
  strip.addEventListener("mouseleave", startPhotoScroll);

  // 桌面端鼠标拖拽滑动（带惯性）
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
    e.preventDefault();
  });
  window.addEventListener("mousemove", (e) => {
    if (!dragOn) return;
    strip.scrollLeft = dragStartScroll + (dragStartX - e.clientX);
    if (Math.abs(e.clientX - dragStartX) > 4) dragMoved = true;
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
  });
  strip.addEventListener("click", (e) => {
    if (dragMoved) { e.stopPropagation(); e.stopImmediatePropagation(); e.preventDefault(); }
  }, true);

  // ── Section: 最近动态 ──
  const msgDiv = $("homeMsgList");
  if (agg.latest_msgs.length) {
    let html = '';
    const multi = members.length > 1;
    agg.latest_msgs.forEach((msg, i) => {
      const d = new Date(msg.published_at + "Z");
      const dateStr = isNaN(d.getTime()) ? '' : (d.getMonth()+1) + '/' + d.getDate();
      const timeStr = isNaN(d.getTime()) ? '' : String(d.getHours()).padStart(2,"0") + ':' + String(d.getMinutes()).padStart(2,"0");
      html += '<div class="msg-preview' + (multi ? '' : ' msg-single') + '" style="animation-delay:' + (i * .05) + 's" data-member="' + esc(msg.member) + '" data-year="' + msg.year + '" data-month="' + msg.month + '" data-id="' + msg.id + '">';
      if (multi) {
        html += '<div class="mp-left">';
        html += '<div class="mp-date">' + dateStr + '</div>';
        html += '<div class="mp-date" style="font-weight:600">' + timeStr + '</div>';
        html += '<div class="mp-mem">' + esc(msg.member_display) + '</div>';
        html += '</div>';
      }
      html += '<div class="mp-body">';
      if (!multi) html += '<div class="mp-date">' + dateStr + ' ' + timeStr + '</div>';
      html += '<div class="mp-text">' + esc(msg.text) + '</div>';
      if (msg.translation) html += '<div class="mp-trans">' + esc(msg.translation) + '</div>';
      html += '</div></div>';
    });
    msgDiv.innerHTML = html;
    msgDiv.querySelectorAll('.msg-preview').forEach(el => {
      el.addEventListener('click', () => {
        hideHome();
        curMember = el.dataset.member;
        curType = "";
        searchQuery = "";
        syncSearchInput();
        targetMsgId = el.dataset.id;
        selfHashUpdate = true;
        location.hash = "member=" + encodeURIComponent(el.dataset.member) + "&y=" + el.dataset.year + "&m=" + el.dataset.month;
        setTimeout(() => { selfHashUpdate = false; }, 100);
        loadMembers();
      });
    });
  } else {
    msgDiv.innerHTML = '<div style="text-align:center;color:var(--muted);padding:20px">暂无文字消息</div>';
  }

  // ── Section: 时光隧道 ──
  const tunnelDiv = $("homeTimeTunnel");
  if (agg.random_msgs && agg.random_msgs.length) {
    let html = '';
    const multi = members.length > 1;
    agg.random_msgs.forEach((msg, i) => {
      const d = new Date(msg.published_at + "Z");
      const dateStr = isNaN(d.getTime()) ? '' : (d.getFullYear()) + '/' + (d.getMonth()+1) + '/' + d.getDate();
      const timeStr = isNaN(d.getTime()) ? '' : String(d.getHours()).padStart(2,"0") + ':' + String(d.getMinutes()).padStart(2,"0");
      html += '<div class="msg-preview tunnel' + (multi ? '' : ' msg-single') + '" style="animation-delay:' + (i * .08) + 's" data-member="' + esc(msg.member) + '" data-year="' + msg.year + '" data-month="' + msg.month + '" data-id="' + msg.id + '">';
      if (multi) {
        html += '<div class="mp-left">';
        html += '<div class="mp-date">' + dateStr + '</div>';
        html += '<div class="mp-date" style="font-weight:600">' + timeStr + '</div>';
        html += '<div class="mp-mem">' + esc(msg.member_display) + '</div>';
        html += '</div>';
      }
      html += '<div class="mp-body">';
      if (!multi) html += '<div class="mp-date">' + dateStr + ' ' + timeStr + '</div>';
      html += '<div class="mp-text">' + esc(msg.text) + '</div>';
      if (msg.translation) html += '<div class="mp-trans">' + esc(msg.translation) + '</div>';
      html += '</div></div>';
    });
    tunnelDiv.innerHTML = html;
    tunnelDiv.querySelectorAll('.msg-preview').forEach(el => {
      el.addEventListener('click', () => {
        curMode = "msg";
        switchMainTab("msg", true);
        hideHome();
        curMember = el.dataset.member;
        curType = "";
        searchQuery = "";
        syncSearchInput();
        targetMsgId = el.dataset.id;
        selfHashUpdate = true;
        location.hash = "member=" + encodeURIComponent(el.dataset.member) + "&y=" + el.dataset.year + "&m=" + el.dataset.month;
        setTimeout(() => { selfHashUpdate = false; }, 100);
        loadMembers();
      });
    });
  } else {
    tunnelDiv.innerHTML = '<div style="text-align:center;color:var(--muted);padding:20px">暂无历史消息</div>';
  }

}

function goHome() {
  curMember = ""; curBlogGroup = "";
  curType = ""; searchQuery = "";
  syncSearchInput();
  switchMainTab("msg", true);
  location.hash = "";
  showHome();
}

function hideHome() {
  $('archiveHome').classList.remove('active');
  $('backTop').classList.remove('force-hide');
  document.querySelector('.layout').style.display = '';
  _enterMemberMode();
}

// ── 入口（支持 #member=&y=&m=&t= / #blog=深链）────────────
function boot() {
  const p = new URLSearchParams(location.hash.slice(1));
  curType = p.get("t") || "";
  searchQuery = normalizedQuery(p.get("q"));
  syncSearchInput();
  initTypeChips();
  
  if (p.has("blog")) {
    curMode = "blog";
    curBlogGroup = p.get("blog") || "";
    curBlogAuthor = p.get("author") || "";
    switchMainTab("blog", true);
    loadMembers(true); // 后台加载 chips
    if (curBlogGroup) {
      selectBlogGroup(curBlogGroup).then(() => {
        if (curBlogAuthor) selectBlogAuthor(curBlogAuthor);
      });
    } else {
      showBlogHome();
    }
  } else {
    curMode = "msg";
    curMember = p.get("member") || "";
    switchMainTab("msg", true);
    if (!curMember) {
      showHome();
      loadMembers(true); 
    } else {
      hideHome();
      loadMembers();
    }
  }
}
boot();
// 从浏览器 bfcache 恢复时重新加载内容
window.addEventListener("pageshow", (e) => { if (e.persisted) boot(); });

// hash 变化：无 member 和 blog 回到首页
window.addEventListener("hashchange", () => {
  if (selfHashUpdate) return;
  const p = new URLSearchParams(location.hash.slice(1));
  if (!p.has("blog") && !(p.get("member") || "")) {
    if (curMember || curBlogGroup) {
      goHome();
      loadMembers(true);
    }
  }
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
    message: "请输入要补全历史消息的成员姓名（留空代表处理全部监控成员）：",
    placeholder: "例：冨里奈央（支持多姓名或留空）",
    icon: "💬",
    confirmText: "开始回填消息"
  });

  if (result === null) return;

  try {
    const res = await fetch("/api/archive/messages/backfill", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ member: result.value || "" })
    });
    const data = await res.json();
    showToast(data.msg || (data.ok ? "已成功启动消息归档回填任务！" : "操作失败"), data.ok ? "success" : "error");
  } catch(e) {
    showToast("请求异常: " + e, "error");
  }
}

