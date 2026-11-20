(() => {
  "use strict";
  const byId = (id) => document.getElementById(id);
  const panel = byId("memberDeliveryRecovery");
  const dialog = byId("memberRecoveryDialog");
  if (!panel || !dialog) return;
  let authorized = false;
  let loading = false;
  let submitting = false;
  let version = 0;
  let selected = null;
  let opener = null;

  async function request(path, options = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 12000);
    try {
      const response = await fetch(path, { credentials: "same-origin", cache: "no-store",
        ...options, signal: controller.signal });
      const body = await response.json();
      if (!response.ok || !body.ok) {
        if (response.status === 409) throw new Error("记录已发生变化，请关闭弹窗并刷新后重新核实。");
        if (response.status === 401 || response.status === 403) throw new Error("需要已登录的管理员账号，请重新登录。");
        throw new Error("操作未确认成功，请刷新记录后核对，避免重复提交。");
      }
      return body;
    } finally { clearTimeout(timer); }
  }

  function identity(item) {
    const members = typeof config === "object" && config && Array.isArray(config.monitor) ? config.monitor : [];
    const member = members.find((entry) => String(entry.m_id) === String(item.member_id)
      && (!entry.group_type || entry.group_type === item.group_type));
    const name = member?.m_name || `成员 ${item.member_id}`;
    const route = String(item.route_id || "");
    let target = route;
    if (route.startsWith("napcat:")) target = `OneBot 群 ${route.slice(7)}`;
    else if (route.startsWith("tg:")) target = `Telegram · ${route.slice(3)}`;
    else if (route.startsWith("official:")) target = `官方 QQ · ${route.slice(9)}`;
    return `${name} · 消息 ${item.message_id} · ${target}`;
  }

  function open(item, button) {
    if (submitting) return;
    selected = item;
    opener = button;
    byId("memberRecoveryIdentity").textContent = identity(item);
    byId("memberRecoveryForm").reset();
    byId("memberRecoveryRiskLabel").hidden = true;
    byId("memberRecoveryRisk").required = false;
    byId("memberRecoveryResult").textContent = "";
    dialog.showModal();
    byId("memberRecoveryReason").focus();
  }

  function render(items) {
    const list = byId("memberRecoveryItems");
    list.replaceChildren();
    items.forEach((item) => {
      const row = document.createElement("div");
      row.className = "admin-control-group";
      const text = document.createElement("p");
      text.textContent = `${identity(item)} · ${item.state === "held" ? "送达未确认" : "正在发送，请等待"}`;
      row.append(text);
      if (item.state === "held") {
        const button = document.createElement("button");
        button.className = "btn small";
        button.type = "button";
        button.textContent = "核实处理";
        button.addEventListener("click", () => open(item, button));
        row.append(button);
      }
      list.append(row);
    });
    byId("memberRecoveryStatus").textContent = items.length >= 100
      ? "当前显示最早 100 条记录，处理后刷新查看后续记录。" : `当前 ${items.length} 条待核实或发送中的记录。`;
    panel.hidden = items.length === 0;
  }

  async function load() {
    if (!authorized || loading || submitting) return;
    loading = true;
    const current = ++version;
    byId("btnRefreshMemberRecovery").disabled = true;
    try {
      const body = await request("/api/member-delivery/intents");
      if (current !== version || !authorized) return;
      render(Array.isArray(body.intents) ? body.intents : []);
    } catch (_) {
      if (current !== version || !authorized) return;
      panel.hidden = false;
      byId("memberRecoveryItems").replaceChildren();
      byId("memberRecoveryStatus").textContent = "暂时无法读取待核实投递，请刷新重试；不能据此判断没有积压。";
    } finally {
      loading = false;
      byId("btnRefreshMemberRecovery").disabled = false;
    }
  }

  byId("memberRecoveryAction").addEventListener("change", () => {
    const resend = byId("memberRecoveryAction").value === "authorize_resend";
    byId("memberRecoveryRiskLabel").hidden = !resend;
    byId("memberRecoveryRisk").required = resend;
    byId("memberRecoveryRisk").checked = false;
  });
  byId("btnCloseMemberRecovery").addEventListener("click", () => { if (!submitting) dialog.close(); });
  dialog.addEventListener("cancel", (event) => { if (submitting) event.preventDefault(); });
  dialog.addEventListener("close", () => { selected = null; if (opener?.isConnected) opener.focus(); });
  byId("btnRefreshMemberRecovery").addEventListener("click", load);
  byId("memberRecoveryForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (submitting || !authorized || !selected) return;
    const reason = byId("memberRecoveryReason").value.trim();
    const action = byId("memberRecoveryAction").value;
    const risk = byId("memberRecoveryRisk").checked;
    if (!reason || (action === "authorize_resend" && !risk)) return;
    submitting = true;
    const button = byId("btnSubmitMemberRecovery");
    button.disabled = true;
    byId("memberRecoveryResult").textContent = "正在保存核实决定…";
    let success = false;
    try {
      await request("/api/member-delivery/resolve", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: selected.token, action, reason, acknowledge_duplicate_risk: risk }) });
      success = true;
    } catch (error) {
      byId("memberRecoveryResult").textContent = error.name === "AbortError"
        ? "保存超时，结果未确认。请关闭弹窗并刷新记录后核对。" : error.message;
    } finally {
      submitting = false;
      button.disabled = false;
    }
    if (success) { dialog.close(); await load(); }
  });

  function syncAuthorization() {
    authorized = window.webAdminRole === "admin";
    if (!authorized) { ++version; panel.hidden = true; if (dialog.open) dialog.close(); }
    else load();
  }
  window.addEventListener("webadminauthready", syncAuthorization);
  if (window.webAdminRole !== "pending") syncAuthorization();
  setInterval(() => { if (!document.hidden && !dialog.open) load(); }, 30000);
})();
