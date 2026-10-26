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
  const members = document.getElementById("routeDiagnosticsMembers");
  const refreshButton = document.getElementById("btnRefreshRouteDiagnostics");
  const retryButton = document.getElementById("btnRetryRouteDiagnostics");
  if (!panel || !status || !error || !members || !refreshButton || !retryButton) return;

  let authorized = false;
  let requestInFlight = false;
  let requestVersion = 0;
  let activeController = null;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
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

  function fact(label, value, note = "", unknown = false) {
    const item = element("div", "admin-route-fact");
    item.append(element("span", "admin-route-fact-label", label));
    item.append(element("strong", "admin-route-fact-value" + (unknown ? " is-unknown" : ""), value));
    if (note) item.append(element("p", "admin-route-fact-note", note));
    return item;
  }

  function subscriptionFact(subscription) {
    const stateValue = subscription && typeof subscription.state === "string" ? subscription.state : "unknown";
    if (stateValue === "active") return fact("订阅缓存", "缓存显示已订阅", "以只读缓存状态为准。");
    if (stateValue === "inactive") return fact("订阅缓存", "缓存显示当前无有效订阅", "来自现有订阅缓存记录。");
    const reason = subscription && typeof subscription.reason === "string" ? subscription.reason : "subscription_state_unknown";
    return fact("订阅缓存", "未知 / 未同步", reasonLabel(reason), true);
  }

  function routeItem(route, routeNumber) {
    const applicable = route && route.status === "applicable";
    const item = element("li", "admin-route-item");
    item.append(element("span", "admin-route-item-name", `路由 ${routeNumber}`));
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
    card.append(heading);

    const configuredCount = countValue(summary && summary.configured_route_count, channelRoutes.length);
    const applicableCount = countValue(summary && summary.applicable_route_count,
      channelRoutes.filter((route) => route && route.status === "applicable").length);
    card.append(element("p", "admin-route-channel-count", `${applicableCount} / ${configuredCount} 条路由符合条件`));

    if (channelRoutes.length) {
      const list = element("ul", "admin-route-list");
      channelRoutes.forEach((route, index) => list.append(routeItem(route, index + 1)));
      card.append(list);
    } else {
      card.append(element("p", "admin-route-empty", "当前没有已配置的推送路由。"));
      const reasons = summary && Array.isArray(summary.reasons) ? summary.reasons : [];
      const labels = reasons.map(reasonLabel).filter(Boolean);
      if (labels.length) card.append(element("p", "admin-route-channel-reasons", labels.join("；")));
    }
    return card;
  }

  function memberCard(report) {
    const member = report && typeof report === "object" ? report : {};
    const card = element("article", "admin-route-member");
    const heading = element("div", "admin-route-member-heading");
    heading.append(element("h3", "", member.member_name || "未命名成员"));
    card.append(heading);

    const fetchAccount = member.fetch_account && typeof member.fetch_account === "object" ? member.fetch_account : {};
    const facts = element("div", "admin-route-member-facts");
    if (fetchAccount.status === "configured" && fetchAccount.account_id) {
      facts.append(fact("抓取账号关联", `已关联 · ${fetchAccount.account_id}`, "用于该成员的抓取账号。"));
    } else {
      facts.append(fact("抓取账号关联", "未关联或账号未配置", reasonLabel("missing_fetch_account"), true));
    }
    facts.append(subscriptionFact(member.subscription));
    card.append(facts);

    const routes = Array.isArray(member.routes) ? member.routes : [];
    const routeSummary = member.route_summary && typeof member.route_summary === "object" ? member.route_summary : {};
    const eligibleCount = countValue(routeSummary.applicable_route_count,
      routes.filter((route) => route && route.status === "applicable").length);
    const summary = element("div", "admin-route-summary " + (eligibleCount ? "is-applicable" : "is-blocked"));
    summary.append(element("strong", "", eligibleCount
      ? `${eligibleCount} 条路由符合条件`
      : "没有推送路由符合当前条件"));
    summary.append(element("span", "", "这只表示配置条件匹配，不代表已尝试推送或已送达。"));
    card.append(summary);

    const summaries = Array.isArray(member.channels) ? member.channels : [];
    const channelGrid = element("div", "admin-route-channel-grid");
    CHANNEL_ORDER.forEach((channel) => {
      const channelSummary = summaries.find((item) => item && item.channel === channel);
      const channelRoutes = routes.filter((route) => route && route.channel === channel);
      channelGrid.append(channelCard(channel, channelSummary, channelRoutes));
    });
    card.append(channelGrid);
    return card;
  }

  function renderReports(reports) {
    members.replaceChildren();
    reports.forEach((report) => members.append(memberCard(report)));
    if (reports.length) {
      setStatus(`已载入 ${reports.length} 位成员的路由诊断。`, "ready");
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
      members.replaceChildren();
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

  refreshButton.addEventListener("click", loadDiagnostics);
  retryButton.addEventListener("click", loadDiagnostics);
  window.addEventListener("webadminauthready", syncAuthorization, { once: true });
  if (window.webAdminRole !== "pending") syncAuthorization();
})();
