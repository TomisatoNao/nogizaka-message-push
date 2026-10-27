(() => {
  "use strict";

  const REQUEST_TIMEOUT_MS = 12000;
  const CHANNELS = Object.freeze({
    napcat: "OneBot QQ",
    qq_official: "QQ 官方",
    tg: "Telegram",
  });
  const CHANNEL_ORDER = Object.freeze(["napcat", "qq_official", "tg"]);
  const REASONS = Object.freeze({
    applicable_route: "符合当前路由配置条件",
    channel_disabled: "推送通道已关闭",
    route_disabled: "该路由已停用",
    no_target: "该路由未配置目标",
    member_filter_miss: "成员筛选条件未匹配",
    no_applicable_route: "没有符合条件的路由",
    missing_fetch_account: "抓取账号未关联或当前未配置",
    unknown_member_id: "成员编号不可用",
    subscription_active: "订阅缓存显示有效",
    subscription_inactive: "订阅缓存显示当前无有效订阅",
    subscription_not_synced_or_unavailable: "订阅缓存未同步或不可读取，不能据此判断为未订阅",
    subscription_read_failed: "订阅缓存读取失败，不能据此判断为未订阅",
    subscription_state_unknown: "订阅缓存状态未知，不能据此判断为未订阅",
  });

  const panel = document.getElementById("routeDiagnosticsPanel");
  const status = document.getElementById("routeDiagnosticsStatus");
  const error = document.getElementById("routeDiagnosticsError");
  const membersContainer = document.getElementById("routeDiagnosticsMembers");
  const refreshButton = document.getElementById("btnRefreshRouteDiagnostics");
  const retryButton = document.getElementById("btnRetryRouteDiagnostics");
  const searchInput = document.getElementById("routeDiagnosticsSearch");
  const filterAllBtn = document.getElementById("btnRouteFilterAll");
  const filterIssuesBtn = document.getElementById("btnRouteFilterIssues");
  const expandAllBtn = document.getElementById("btnRouteExpandAll");
  const collapseAllBtn = document.getElementById("btnRouteCollapseAll");

  if (!panel || !status || !error || !membersContainer || !refreshButton || !retryButton) return;

  let authorized = false;
  let requestInFlight = false;
  let requestVersion = 0;
  let activeController = null;
  let cachedReports = [];
  let currentFilter = "all"; // "all" | "issues"
  let currentSearch = "";

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function reasonLabel(reason) {
    return Object.prototype.hasOwnProperty.call(REASONS, reason)
      ? REASONS[reason]
      : "配置原因暂不可识别";
  }

  function channelLabel(channel) {
    return Object.prototype.hasOwnProperty.call(CHANNELS, channel) ? CHANNELS[channel] : "其他通道";
  }

  function countValue(value, fallback = 0) {
    return Number.isSafeInteger(value) && value >= 0 ? value : fallback;
  }

  function setStatus(message, tone = "") {
    status.className = "admin-route-diagnostics-status" + (tone ? ` is-${tone}` : "");
    status.textContent = message;
  }

  // 格式化具名路由标签：优先使用管理员设置的备注名称（如“地偶痴群bot · 群聊”、“闹闹群”），无备注时回退到具名类型标签
  function formatRouteLabel(routeOrLabel, fallbackIndex) {
    if (!routeOrLabel) return `路由 ${fallbackIndex}`;
    const isObj = typeof routeOrLabel === "object";
    const rawLabel = (isObj ? (typeof routeOrLabel.label === "string" ? routeOrLabel.label : "") : String(routeOrLabel)).trim();
    const remark = (isObj && typeof routeOrLabel.remark === "string" ? routeOrLabel.remark : "").trim();
    const targetType = (isObj && typeof routeOrLabel.target_type === "string" ? routeOrLabel.target_type : "").trim();

    const officialMatch = /^Official bot (\d+) (private|group)$/i.exec(rawLabel);
    if (officialMatch) {
      const type = (targetType || officialMatch[2]).toLowerCase() === "private" ? "私聊" : "群聊";
      if (remark) {
        return `${remark} · ${type}`;
      }
      return `QQ 官方 Bot ${officialMatch[1]} · ${type}`;
    }
    const napcatMatch = /^NapCat route (\d+)$/i.exec(rawLabel);
    if (napcatMatch) {
      if (remark) {
        return remark;
      }
      return `OneBot 配置 ${napcatMatch[1]}`;
    }
    const tgMatch = /^Telegram bot (\d+)$/i.exec(rawLabel);
    if (tgMatch) {
      if (remark) {
        return remark;
      }
      return `Telegram Bot ${tgMatch[1]}`;
    }

    if (remark) {
      if (targetType === "private") return `${remark} · 私聊`;
      if (targetType === "group") return `${remark} · 群聊`;
      return remark;
    }
    return rawLabel || `路由 ${fallbackIndex}`;
  }

  function evaluateMember(report) {
    const member = report && typeof report === "object" ? report : {};
    const fetchAccount = member.fetch_account && typeof member.fetch_account === "object" ? member.fetch_account : {};
    const subscription = member.subscription && typeof member.subscription === "object" ? member.subscription : {};
    const routes = Array.isArray(member.routes) ? member.routes : [];
    const applicableRoutes = routes.filter((r) => r && r.status === "applicable");
    const applicableCount = applicableRoutes.length;

    const hasAccount = fetchAccount.status === "configured" && !!fetchAccount.account_id;
    const subState = subscription.state || "unknown";
    const isSubInactive = subState === "inactive";
    const isSubUnknown = subState === "unknown";
    const isNoApplicableRoute = applicableCount === 0;

    // 需关注处理项：缺账号、所有通道无匹配、订阅明确已失效
    const needsAttention = !hasAccount || isNoApplicableRoute || isSubInactive;

    return {
      member,
      fetchAccount,
      subscription,
      routes,
      applicableRoutes,
      applicableCount,
      hasAccount,
      subState,
      isSubInactive,
      isSubUnknown,
      isNoApplicableRoute,
      needsAttention,
    };
  }

  function routeItem(route, routeNumber) {
    const applicable = route && route.status === "applicable";
    const item = element("li", "admin-route-item" + (applicable ? " is-applicable" : ""));
    item.append(element("span", "admin-route-item-name", formatRouteLabel(route, routeNumber)));
    item.append(element("span", "admin-route-state " + (applicable ? "is-applicable" : "is-blocked"),
      applicable ? "符合条件" : "未满足"));
    item.append(element("p", "admin-route-item-reason", applicable
      ? reasonLabel("applicable_route")
      : reasonLabel(route && typeof route.reason === "string" ? route.reason : "")));
    return item;
  }

  function channelCard(channel, summary, channelRoutes) {
    const card = element("section", "admin-route-channel");
    const heading = element("div", "admin-route-channel-heading");
    heading.append(element("h4", "", channelLabel(channel)));

    const configuredCount = countValue(summary && summary.configured_route_count, channelRoutes.length);
    const matched = channelRoutes.filter((r) => r && r.status === "applicable");
    const unmatched = channelRoutes.filter((r) => !r || r.status !== "applicable");
    const applicableCount = matched.length;

    const enabled = summary && summary.enabled !== false;
    heading.append(element("span", "admin-route-channel-count", enabled
      ? `${applicableCount} / ${configuredCount} 条符合条件`
      : "通道已关闭"));
    card.append(heading);

    if (!channelRoutes.length) {
      card.append(element("p", "admin-route-empty", "当前没有已配置的推送路由。"));
      const reasons = summary && Array.isArray(summary.reasons) ? summary.reasons : [];
      const labels = reasons.map(reasonLabel).filter(Boolean);
      if (labels.length) card.append(element("p", "admin-route-channel-reasons", labels.join("；")));
      return card;
    }

    // 1. 优先展示符合条件的有效分支
    if (matched.length) {
      const matchedList = element("ul", "admin-route-list");
      matched.forEach((route, idx) => matchedList.append(routeItem(route, idx + 1)));
      card.append(matchedList);
    }

    // 2. 将未匹配/停用/未使用的分支折叠聚合，避免 18 条分支撑爆整行
    if (unmatched.length) {
      const details = element("details", "admin-route-unmatched-details");
      const sumNode = element("summary", "admin-route-unmatched-summary",
        `其他未匹配/未配置分支 (${unmatched.length}) ▾`);
      details.append(sumNode);
      const unmatchedList = element("ul", "admin-route-list");
      unmatched.forEach((route, idx) => unmatchedList.append(routeItem(route, matched.length + idx + 1)));
      details.append(unmatchedList);
      card.append(details);
    }

    return card;
  }

  function memberCard(report, index) {
    const info = evaluateMember(report);
    const memberName = info.member.member_name || "未命名成员";

    const card = element("article", "admin-route-member");
    if (!info.hasAccount) card.classList.add("status-err");
    else if (info.needsAttention) card.classList.add("status-warn");
    else if (info.isSubUnknown) card.classList.add("status-neutral");
    else card.classList.add("status-ok");

    card.dataset.memberName = memberName;
    card.dataset.needsAttention = info.needsAttention ? "true" : "false";

    // ── 1. 紧凑摘要头部（默认折叠） ──
    const headerBtn = element("button", "admin-route-member-header");
    headerBtn.type = "button";
    headerBtn.setAttribute("aria-expanded", "false");
    const bodyId = `routeMemberBody-${index}`;
    headerBtn.setAttribute("aria-controls", bodyId);

    // 左侧：折叠指示箭头 + 姓名 + 状态徽章
    const lead = element("div", "admin-route-member-lead");
    lead.append(element("span", "admin-route-chevron", "▸"));
    lead.append(element("span", "admin-route-member-name", memberName));

    let badgeClass = "is-ok";
    let badgeText = "✅ 匹配正常";
    if (!info.hasAccount) {
      badgeClass = "is-err";
      badgeText = "⚠️ 抓取账号未关联";
    } else if (info.isNoApplicableRoute) {
      badgeClass = "is-warn";
      badgeText = "⚠️ 无匹配路由";
    } else if (info.isSubInactive) {
      badgeClass = "is-warn";
      badgeText = "⚠️ 订阅缓存已失效";
    } else if (info.isSubUnknown) {
      badgeClass = "is-neutral";
      badgeText = "订阅待确认";
    }
    lead.append(element("span", `admin-route-badge ${badgeClass}`, badgeText));
    headerBtn.append(lead);

    // 中间：关键属性速览 + 三通道匹配数
    const mid = element("div", "admin-route-member-mid");
    const accountStr = info.hasAccount ? `账号: ${info.fetchAccount.account_id}` : "未关联账号";
    mid.append(element("span", "admin-route-meta-chip", accountStr));

    const subText = info.subState === "active" ? "订阅有效" : info.subState === "inactive" ? "无有效订阅" : "订阅未知";
    mid.append(element("span", "admin-route-meta-chip", `订阅: ${subText}`));

    // 通道匹配数汇总
    const summaries = Array.isArray(info.member.channels) ? info.member.channels : [];
    const napcatSummary = summaries.find((c) => c && c.channel === "napcat");
    const qqSummary = summaries.find((c) => c && c.channel === "qq_official");
    const tgSummary = summaries.find((c) => c && c.channel === "tg");

    const napcatCount = napcatSummary ? countValue(napcatSummary.applicable_route_count, 0) : 0;
    const qqCount = qqSummary ? countValue(qqSummary.applicable_route_count, 0) : 0;
    const tgCount = tgSummary ? countValue(tgSummary.applicable_route_count, 0) : 0;

    const channelSummaryText = `匹配推送：OneBot ${napcatCount} · QQ 官方 ${qqCount} · Telegram ${tgCount}`;
    const channelPill = element("span", "admin-route-channel-pills", channelSummaryText);
    const matchedNames = info.applicableRoutes.map((r, i) => formatRouteLabel(r, i + 1));
    if (matchedNames.length) {
      channelPill.title = `符合条件的推送目标：\n${matchedNames.join("\n")}`;
    }
    mid.append(channelPill);
    headerBtn.append(mid);

    // 右侧：展开/收起按钮文字
    const tail = element("div", "admin-route-member-tail");
    const toggleLabel = element("span", "admin-route-toggle-label", "查看详情 ▾");
    tail.append(toggleLabel);
    headerBtn.append(tail);

    card.append(headerBtn);

    // ── 2. 详情内容（默认隐藏） ──
    const body = element("div", "admin-route-member-body");
    body.id = bodyId;
    body.hidden = true;

    // 顶部诊断信息行与通道跳转入口
    const metaBar = element("div", "admin-route-meta-bar");
    const facts = element("div", "admin-route-meta-facts");

    // 抓取账号关联说明
    const factAcc = element("div", "admin-route-meta-fact-item");
    factAcc.append(element("span", "", "抓取账号关联："));
    factAcc.append(element("strong", "", info.hasAccount ? `已关联 (${info.fetchAccount.account_id})` : "未关联或账号未配置"));
    facts.append(factAcc);

    // 订阅缓存说明
    const factSub = element("div", "admin-route-meta-fact-item");
    factSub.append(element("span", "", "订阅缓存："));
    if (info.subState === "active") {
      factSub.append(element("strong", "", "缓存显示已订阅（以只读缓存状态为准）"));
    } else if (info.subState === "inactive") {
      factSub.append(element("strong", "", "缓存显示当前无有效订阅"));
    } else {
      factSub.append(element("strong", "", "未知 / 未同步（不能据此判断为未订阅）"));
    }
    facts.append(factSub);

    metaBar.append(facts);

    // 通往推送通道页签事件入口
    const gotoChannelsBtn = element("button", "btn small admin-route-goto-channels", "📢 前往推送通道设置");
    gotoChannelsBtn.type = "button";
    gotoChannelsBtn.title = "跳转至推送通道配置页签进行编辑调整";
    gotoChannelsBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const tabChannels = document.querySelector('button.nav-tab[data-tab="channels"]');
      if (tabChannels) tabChannels.click();
    });
    metaBar.append(gotoChannelsBtn);

    body.append(metaBar);

    // 通道卡片三列网格
    const channelGrid = element("div", "admin-route-channel-grid");
    CHANNEL_ORDER.forEach((channel) => {
      const channelSummary = summaries.find((item) => item && item.channel === channel);
      const channelRoutes = info.routes.filter((route) => route && route.channel === channel);
      channelGrid.append(channelCard(channel, channelSummary, channelRoutes));
    });
    body.append(channelGrid);

    card.append(body);

    // 折叠展开点击交互
    headerBtn.addEventListener("click", () => {
      const isExpanded = card.classList.toggle("is-expanded");
      body.hidden = !isExpanded;
      headerBtn.setAttribute("aria-expanded", isExpanded ? "true" : "false");
      toggleLabel.textContent = isExpanded ? "收起 ▴" : "查看详情 ▾";
    });

    return card;
  }

  function applyFilters() {
    const term = currentSearch.trim().toLowerCase();
    const cards = membersContainer.querySelectorAll(".admin-route-member");
    let visibleCount = 0;

    cards.forEach((card) => {
      const name = (card.dataset.memberName || "").toLowerCase();
      const needsAttention = card.dataset.needsAttention === "true";

      const matchesSearch = !term || name.includes(term);
      const matchesFilter = currentFilter === "all" || (currentFilter === "issues" && needsAttention);

      const visible = matchesSearch && matchesFilter;
      card.hidden = !visible;
      if (visible) visibleCount += 1;
    });

    if (cachedReports.length > 0) {
      if (visibleCount === 0) {
        setStatus("没有符合当前筛选条件的成员。", "empty");
      } else {
        const issuesTotal = cachedReports.filter((r) => evaluateMember(r).needsAttention).length;
        const note = currentFilter === "issues"
          ? `（需处理 ${visibleCount} 位）`
          : (issuesTotal ? `（其中 ${issuesTotal} 位需处理）` : "（全部配置正常）");
        setStatus(`已载入 ${cachedReports.length} 位成员的路由诊断${note}；配置匹配不代表已尝试推送或已送达。`, "ready");
      }
    }
  }

  function renderReports(reports) {
    cachedReports = Array.isArray(reports) ? reports : [];
    membersContainer.replaceChildren();

    cachedReports.forEach((report, index) => {
      membersContainer.append(memberCard(report, index + 1));
    });

    if (cachedReports.length) {
      applyFilters();
    } else {
      setStatus("当前没有可诊断的监控成员。", "empty");
    }
  }

  function setLoading() {
    setStatus("正在读取成员路由诊断…", "loading");
    error.hidden = true;
    retryButton.hidden = true;
    refreshButton.disabled = true;
  }

  function setFailure() {
    setStatus("路由诊断暂时无法加载。", "error");
    error.textContent = "检查网络或管理员权限后重试。";
    error.hidden = false;
    retryButton.hidden = false;
    retryButton.disabled = false;
  }

  async function loadDiagnostics() {
    if (!authorized || requestInFlight) return;
    requestInFlight = true;
    const version = ++requestVersion;
    const controller = new AbortController();
    activeController = controller;
    setLoading();
    const timeoutId = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    try {
      const response = await fetch("/api/route-diagnostics", {
        method: "GET",
        cache: "no-store",
        signal: controller.signal,
      });
      if (version !== requestVersion || !authorized) return;
      if (!response.ok) throw new Error("request_failed");
      const payload = await response.json();
      if (version !== requestVersion || !authorized) return;
      if (!payload || payload.ok !== true || !Array.isArray(payload.diagnostics)) throw new Error("invalid_response");
      error.hidden = true;
      retryButton.hidden = true;
      renderReports(payload.diagnostics);
    } catch (_error) {
      if (version === requestVersion && authorized) setFailure();
    } finally {
      window.clearTimeout(timeoutId);
      if (version === requestVersion) {
        requestInFlight = false;
        activeController = null;
        refreshButton.disabled = false;
        retryButton.disabled = false;
      }
    }
  }

  function syncAuthorization() {
    const isAdmin = window.webAdminRole === "admin";
    if (!isAdmin) {
      authorized = false;
      requestVersion += 1;
      if (activeController) activeController.abort();
      activeController = null;
      requestInFlight = false;
      panel.hidden = true;
      membersContainer.replaceChildren();
      error.hidden = true;
      retryButton.hidden = true;
      refreshButton.disabled = false;
      setStatus("等待管理员身份确认。");
      return;
    }

    if (authorized) return;
    authorized = true;
    panel.hidden = false;
    loadDiagnostics();
  }

  // 绑定工具栏搜索与筛选事件
  if (searchInput) {
    searchInput.addEventListener("input", (e) => {
      currentSearch = e.target.value;
      applyFilters();
    });
  }

  if (filterAllBtn && filterIssuesBtn) {
    filterAllBtn.addEventListener("click", () => {
      currentFilter = "all";
      filterAllBtn.classList.add("active");
      filterIssuesBtn.classList.remove("active");
      applyFilters();
    });
    filterIssuesBtn.addEventListener("click", () => {
      currentFilter = "issues";
      filterIssuesBtn.classList.add("active");
      filterAllBtn.classList.remove("active");
      applyFilters();
    });
  }

  if (expandAllBtn && collapseAllBtn) {
    expandAllBtn.addEventListener("click", () => {
      membersContainer.querySelectorAll(".admin-route-member:not([hidden])").forEach((card) => {
        card.classList.add("is-expanded");
        const body = card.querySelector(".admin-route-member-body");
        const header = card.querySelector(".admin-route-member-header");
        const toggle = card.querySelector(".admin-route-toggle-label");
        if (body) body.hidden = false;
        if (header) header.setAttribute("aria-expanded", "true");
        if (toggle) toggle.textContent = "收起 ▴";
      });
    });

    collapseAllBtn.addEventListener("click", () => {
      membersContainer.querySelectorAll(".admin-route-member").forEach((card) => {
        card.classList.remove("is-expanded");
        const body = card.querySelector(".admin-route-member-body");
        const header = card.querySelector(".admin-route-member-header");
        const toggle = card.querySelector(".admin-route-toggle-label");
        if (body) body.hidden = true;
        if (header) header.setAttribute("aria-expanded", "false");
        if (toggle) toggle.textContent = "查看详情 ▾";
      });
    });
  }

  refreshButton.addEventListener("click", loadDiagnostics);
  retryButton.addEventListener("click", loadDiagnostics);
  window.addEventListener("webadminauthready", syncAuthorization, { once: true });
  if (window.webAdminRole !== "pending") syncAuthorization();
})();
