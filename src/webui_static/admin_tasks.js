(() => {
  "use strict";

  const POLL_INTERVAL_MS = 2000;
  const IDLE_POLL_INTERVAL_MS = 15000;
  const REQUEST_TIMEOUT_MS = 12000;

  const TASK_PHASE_LABELS = {
    pending: "排队准备中",
    starting: "启动中",
    running: "执行中",
    completed: "已完成",
    interrupted: "任务已中断",
    failed: "执行失败",
    partial: "部分完成",
    fetching: "消息抓取中",
    media: "媒体文件下载中",
    saving: "写入归档数据库",
    scanning: "扫描博文列表",
    articles: "下载博文与图片",
    translating: "博客翻译中",
    archive_db_quick_check: "消息数据库快速检查",
    blog_db_quick_check: "博客数据库快速检查",
    dirty_marker: "检查消息索引标记",
    message_index_comparison: "对照消息与数据库索引",
    message_json_scan: "扫描消息结构与本地媒体",
    blog_media_references: "检查博客图片与本地文件",
  };
  const TASK_KINDS = {
    message_backfill: "消息补档",
    archive_integrity: "归档完整性检查",
  };
  const ITEM_TYPES = {
    member: "成员",
    media: "媒体",
    page: "页面",
    article: "文章",
    task: "任务执行",
  };
  const TASK_STATUSES = {
    queued: { label: "排队中", tone: "muted" },
    running: { label: "执行中", tone: "running" },
    completed: { label: "已完成", tone: "ok" },
    partial: { label: "部分完成", tone: "warn" },
    failed: { label: "失败", tone: "err" },
    interrupted: { label: "已中断", tone: "warn" },
  };
  const INTEGRITY_REPORT_STATUSES = {
    needs_repair: { label: "发现需要处理的问题", tone: "warn" },
    check_failed: { label: "检查失败，无法确认完整性", tone: "err" },
    unavailable: { label: "部分数据不可用，无法得出完整结论", tone: "warn" },
    not_checked: { label: "尚无已完成的检查结果", tone: "muted" },
  };
  const INTEGRITY_CHECK_LABELS = {
    dirty_marker: "消息索引状态标记",
    message_json_scan: "消息 JSON 结构与本地媒体引用",
    archive_db_quick_check: "消息数据库 quick_check",
    message_index_comparison: "消息 JSON 与数据库索引对照",
    blog_db_quick_check: "博客数据库 quick_check",
    blog_media_references: "博客图片本地文件引用",
    integrity_worker: "完整性检查进程",
  };
  const INTEGRITY_CHECK_STATUSES = {
    healthy: { label: "正常", tone: "ok" },
    needs_repair: { label: "发现问题", tone: "warn" },
    check_failed: { label: "检查失败", tone: "err" },
    unavailable: { label: "不可用", tone: "warn" },
    not_checked: { label: "未检查", tone: "muted" },
  };
  const INTEGRITY_PHASE_LABELS = {
    queued: "排队中",
    dirty_marker: "检查索引状态",
    message_json_scan: "检查消息 JSON 与索引",
    blog_media_references: "检查博客图片引用",
    completed: "已完成",
    failed: "检查失败",
  };
  const INTEGRITY_ISSUE_LABELS = {
    archive_scan_failed: "无法读取部分归档目录",
    archive_symlink_directory_skipped: "检测到符号链接目录并已跳过",
    archive_month_out_of_range: "归档月份超出有效范围",
    message_json_read_failed: "无法读取消息 JSON 文件",
    message_json_symlink_skipped: "消息 JSON 是符号链接，已跳过",
    message_json_invalid: "消息 JSON 文件格式无效或检查期间发生变化",
    message_json_changed_during_check: "检查期间消息 JSON 文件发生变化",
    message_json_record_invalid: "消息记录缺少有效字段",
    duplicate_message_id: "发现重复的消息编号",
    message_local_file_invalid: "消息媒体路径无效",
    message_media_download_failed: "消息媒体曾下载失败",
    message_media_missing: "消息媒体文件缺失",
    message_media_reference_unsafe: "消息媒体路径不安全",
    message_media_reference_uncheckable: "无法确认消息媒体文件状态",
    message_missing_from_index: "消息记录缺少数据库索引项",
    message_index_entry_mismatch: "消息记录与数据库索引不一致",
    message_index_entry_without_json: "数据库索引项没有对应的 JSON 记录",
    archive_db_quick_check_failed: "消息数据库检查未通过",
    archive_db_read_failed: "无法读取消息数据库",
    archive_db_path_uncheckable: "无法安全检查消息数据库路径",
    archive_db_changed_during_check: "检查期间消息数据库发生变化",
    blog_db_path_uncheckable: "无法安全检查博客数据库路径",
    blog_db_quick_check_failed: "博客数据库检查未通过",
    blog_db_read_failed: "无法读取博客数据库",
    blog_db_changed_during_check: "检查期间博客数据库发生变化",
    blog_image_paths_invalid: "博客图片路径数据无效",
    blog_image_path_invalid: "博客图片路径无效",
    blog_media_missing: "博客图片文件缺失",
    blog_media_reference_unsafe: "博客图片路径不安全",
    blog_media_reference_uncheckable: "无法确认博客图片文件状态",
    message_index_dirty_marker_present: "消息数据库索引待更新标记仍存在",
    dirty_marker_uncheckable: "无法确认消息索引状态标记",
    integrity_worker_failed: "完整性检查进程未能完成",
  };
  const MESSAGE_ISSUE_CODES = new Set([
    "message_json_record_invalid", "duplicate_message_id", "message_local_file_invalid",
    "message_media_download_failed", "message_media_missing", "message_media_reference_unsafe",
    "message_media_reference_uncheckable", "message_missing_from_index", "message_index_entry_mismatch",
    "message_index_entry_without_json",
  ]);
  const BLOG_ISSUE_CODES = new Set([
    "blog_image_paths_invalid", "blog_image_path_invalid", "blog_media_missing",
    "blog_media_reference_unsafe", "blog_media_reference_uncheckable",
  ]);

  const notice = document.getElementById("archiveTaskNotice");
  const errorNotice = document.getElementById("archiveTaskError");
  const taskList = document.getElementById("archiveTaskList");
  const refreshButton = document.getElementById("btnRefreshArchiveTasks");
  const btnLaunchMessageBackfill = document.getElementById("btnLaunchMessageBackfill");
  const btnLaunchBlogBackfill = document.getElementById("btnLaunchBlogBackfill");
  const filterToolbar = document.getElementById("archiveTaskFilters");
  const dialogArchiveMessage = document.getElementById("dialogArchiveMessage");
  const dialogArchiveBlog = document.getElementById("dialogArchiveBlog");
  const taskSection = document.getElementById("tab-tasks");

  let currentFilterType = "all";
  let currentFilterStatus = "all";
  const integrityPanel = document.getElementById("archiveIntegrityPanel");
  const integrityStatus = document.getElementById("archiveIntegrityStatus");
  const integrityReport = document.getElementById("archiveIntegrityReport");
  const integrityActivity = document.getElementById("archiveIntegrityActivity");
  const integrityProgress = document.getElementById("archiveIntegrityProgress");
  const integrityProgressText = document.getElementById("archiveIntegrityProgressText");
  const integrityTaskId = document.getElementById("archiveIntegrityTaskId");
  const integrityStaleNote = document.getElementById("archiveIntegrityStaleNote");
  const runIntegrityButton = document.getElementById("btnRunArchiveIntegrity");
  const refreshIntegrityButton = document.getElementById("btnRefreshArchiveIntegrity");
  if (!notice || !errorNotice || !taskList || !refreshButton || !taskSection) return;
  if (runIntegrityButton) runIntegrityButton.disabled = true;
  if (refreshIntegrityButton) refreshIntegrityButton.disabled = true;

  let authorized = false;
  let taskTabActive = false;
  let lifecycleGeneration = 0;
  let listTimer = null;
  let listController = null;
  let hasLoadedTasks = false;
  let integrityReportData = null;
  let integrityTaskData = null;
  let integrityError = "";
  let integrityMessage = "";
  let integrityConflictMessage = "";
  let integrityStateVersion = 0;
  let integrityLoading = false;
  let integrityManualLoading = false;
  let integrityRequestController = null;
  let integrityRunController = null;
  let integrityTimer = null;
  let hasLoadedIntegrity = false;
  const cards = new Map();
  const taskRecords = new Map();
  const detailControllers = new Map();
  const retryControllers = new Map();
  const detailStates = new Map();

  function isActiveTask(task) {
    const status = String(task && task.status || "").toLowerCase();
    return status === "queued" || status === "running";
  }

  function isTerminalTask(task) {
    const status = String(task && task.status || "").toLowerCase();
    return status === "completed" || status === "partial" || status === "failed" || status === "interrupted";
  }

  function statusMeta(status) {
    const key = String(status || "").toLowerCase();
    return Object.prototype.hasOwnProperty.call(TASK_STATUSES, key)
      ? TASK_STATUSES[key]
      : { label: key || "未知状态", tone: "muted" };
  }

  function taskKindLabel(task) {
    const kind = asText(task.kind, "未知任务");
    if (kind === "blog_backfill") {
      if (task.scope === "groups") return "批量博客回填";
      if (task.scope === "member_url") return "单成员博客归档";
      return "博客回填";
    }
    return Object.prototype.hasOwnProperty.call(TASK_KINDS, kind) ? TASK_KINDS[kind] : kind;
  }

  function asText(value, fallback = "—") {
    if (value === null || value === undefined || value === "") return fallback;
    return String(value);
  }

  function formatTimestamp(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    }).format(date);
  }

  function taskTime(task) {
    if (task.finished_at) return { label: "结束时间", value: task.finished_at };
    if (task.updated_at) return { label: "更新时间", value: task.updated_at };
    if (task.started_at) return { label: "开始时间", value: task.started_at };
    if (task.created_at) return { label: "创建时间", value: task.created_at };
    return { label: "时间", value: null };
  }

  function errorMessage(error, fallback) {
    if (error && error.message) return error.message;
    return fallback;
  }

  function responseError(data, fallback) {
    if (data && typeof data.error === "string" && data.error) return data.error;
    if (data && typeof data.msg === "string" && data.msg) return data.msg;
    if (data && Array.isArray(data.errors) && data.errors.length) {
      return data.errors.map(String).join("；");
    }
    return fallback;
  }

  function showListError(message) {
    errorNotice.textContent = message;
    errorNotice.hidden = false;
  }

  function clearListError() {
    errorNotice.textContent = "";
    errorNotice.hidden = true;
  }

  function setNotice(message) {
    notice.textContent = message;
    notice.hidden = !message;
  }

  function isActiveIntegrityTask(task) {
    return Boolean(task && (String(task.status || "").toLowerCase() === "queued"
      || String(task.status || "").toLowerCase() === "running"));
  }

  function integrityTaskIsStale(task) {
    return Boolean(task && task.heartbeat_stale === true);
  }

  function canStartIntegrityCheck() {
    if (!hasLoadedIntegrity || integrityLoading || integrityError || integrityConflictMessage) return false;
    return !isActiveIntegrityTask(integrityTaskData)
      || (integrityTaskIsStale(integrityTaskData) && integrityTaskData.restart_blocked === false);
  }

  function integrityRestartNote(task) {
    if (!task || typeof task.heartbeat_stale !== "boolean") {
      return "服务端尚未提供明确的检查心跳状态，已禁止重新检查。请刷新状态后由管理员人工确认。";
    }
    if (task.restart_blocked === false) {
      return "检查心跳已超时，服务端已确认可以重新检查。请由管理员手动发起。";
    }
    const reasons = {
      heartbeat_stale_worker_alive: "检查心跳已超时，但检查进程仍在运行。",
      worker_alive_or_owner_uncertain: "检查心跳已超时，但进程仍在运行或无法确认其归属。",
      worker_owner_uncertain: "检查心跳已超时，无法确认检查进程的归属或是否已退出。",
      another_archive_task_active: "另有归档任务正在运行。",
    };
    const reason = Object.prototype.hasOwnProperty.call(reasons, task.restart_reason)
      ? reasons[task.restart_reason]
      : "检查心跳已超时，服务端尚未明确允许重新检查。";
    return `${reason}已禁止重新检查，请管理员人工确认进程状态后刷新。`;
  }

  function readCount(value) {
    if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) return value;
    if (typeof value === "string" && /^\d+$/.test(value)) {
      const parsed = Number(value);
      return Number.isSafeInteger(parsed) ? parsed : null;
    }
    return null;
  }

  function setIntegrityStatus(message, tone = "") {
    if (!integrityStatus) return;
    const className = `admin-table-state admin-integrity-status${tone ? ` is-${tone}` : ""}`;
    if (integrityStatus.textContent === message && integrityStatus.className === className) return;
    integrityStatus.textContent = message;
    integrityStatus.className = className;
  }

  function appendIntegrityMessage(container, message, className = "admin-note") {
    const paragraph = document.createElement("p");
    paragraph.className = className;
    paragraph.textContent = message;
    container.append(paragraph);
    return paragraph;
  }

  function makeIntegritySection(title, className) {
    const section = document.createElement("section");
    section.className = `admin-integrity-section ${className || ""}`.trim();
    const heading = document.createElement("h3");
    heading.textContent = title;
    section.append(heading);
    return section;
  }

  function integrityReportMeta(report) {
    if (!report || typeof report !== "object") {
      return { label: "尚未执行完整性检查", tone: "muted" };
    }
    const status = String(report.status || "").toLowerCase();
    if (status === "healthy") {
      const checks = Array.isArray(report.checks) ? report.checks : [];
      const issues = Array.isArray(report.issues) ? report.issues : [];
      const issueCount = readCount(report.issue_count);
      const omittedCount = readCount(report.omitted_issue_count);
      const allChecksHealthy = checks.length > 0
        && checks.every((check) => check && String(check.status || "").toLowerCase() === "healthy");
      if (allChecksHealthy && issueCount === 0 && omittedCount === 0 && issues.length === 0) {
        return { label: "检查完成：各检查项均正常", tone: "ok" };
      }
      return { label: "报告信息不完整，无法确认健康", tone: "warn" };
    }
    return Object.prototype.hasOwnProperty.call(INTEGRITY_REPORT_STATUSES, status)
      ? INTEGRITY_REPORT_STATUSES[status]
      : { label: "检查状态未知，无法确认完整性", tone: "warn" };
  }

  function integrityIssueHref(issue) {
    if (!issue || typeof issue !== "object") return "";
    const code = String(issue.code || "");
    if (code === "message_index_entry_without_json") {
      // 索引缺对应 JSON/跨月项在原归档月无文件，不提供无效查看链接
      return "";
    }
    const params = new URLSearchParams();
    if (MESSAGE_ISSUE_CODES.has(code)) {
      const member = typeof issue.member === "string" ? issue.member : "";
      const msgId = issue.msg_id === null || issue.msg_id === undefined ? "" : String(issue.msg_id);
      const year = readCount(issue.year);
      const month = readCount(issue.month);
      if (!member || member.length > 120 || !msgId || msgId.length > 120 || year === null || year < 1
        || month === null || month < 1 || month > 12) return "";
      params.set("member", member);
      params.set("y", String(year));
      params.set("m", String(month));
      params.set("msg_id", msgId);
      return `/archive#${params.toString()}`;
    }
    if (BLOG_ISSUE_CODES.has(code)) {
      const blogId = readCount(issue.blog_id);
      if (blogId === null) return "";
      params.set("id", String(blogId));
      return `/archive#${params.toString()}`;
    }
    return "";
  }

  function renderIntegrityIssue(issue) {
    const wrapper = document.createElement("article");
    wrapper.className = "admin-integrity-issue";
    const code = typeof issue.code === "string" ? issue.code : "";
    let category = "other";
    if (code === "message_missing_from_index") category = "missing";
    else if (code === "message_index_entry_mismatch") category = "mismatch";
    else if (code === "message_index_entry_without_json") category = "no_json";
    wrapper.dataset.category = category;

    const heading = document.createElement("div");
    heading.className = "admin-integrity-issue-heading";
    const label = document.createElement("strong");
    label.textContent = Object.prototype.hasOwnProperty.call(INTEGRITY_ISSUE_LABELS, code)
      ? INTEGRITY_ISSUE_LABELS[code]
      : "发现一条需要人工确认的问题";
    heading.append(label);
    if (code) {
      const codeNode = document.createElement("code");
      codeNode.textContent = code;
      heading.append(codeNode);
    }
    wrapper.append(heading);

    const locationParts = [];
    if (issue.member !== null && issue.member !== undefined && String(issue.member) !== "") {
      locationParts.push(`成员：${String(issue.member)}`);
    }
    if (issue.year !== null && issue.year !== undefined) locationParts.push(`年份：${String(issue.year)}`);
    if (issue.month !== null && issue.month !== undefined) locationParts.push(`月份：${String(issue.month)}`);
    if (issue.msg_id !== null && issue.msg_id !== undefined) locationParts.push(`消息编号：${String(issue.msg_id)}`);
    if (issue.blog_id !== null && issue.blog_id !== undefined) locationParts.push(`博客编号：${String(issue.blog_id)}`);
    if (locationParts.length) appendIntegrityMessage(wrapper, locationParts.join(" · "), "admin-integrity-issue-location");

    if (code === "message_missing_from_index") {
      appendIntegrityMessage(wrapper, "💡 消息已存在于月度 JSON，但数据库缺失索引（多由历史跨成员同编号覆盖导致）。", "admin-integrity-manual-note");
    } else if (code === "message_index_entry_mismatch") {
      appendIntegrityMessage(wrapper, "💡 数据库索引字段与月度 JSON 权威内容不一致（多由历史同编号覆盖导致）。", "admin-integrity-manual-note");
    } else if (code === "message_index_entry_without_json") {
      appendIntegrityMessage(wrapper, "💡 原归档月未检索到对应 JSON，可能为跨月漂移或孤立调试记录；无法直接定位月文件。", "admin-integrity-manual-note");
    }

    const href = integrityIssueHref(issue);
    if (href) {
      const link = document.createElement("a");
      link.className = "admin-integrity-issue-link";
      link.href = href;
      link.textContent = BLOG_ISSUE_CODES.has(code) ? "在博客归档中查看" : "在消息归档中查看";
      wrapper.append(link);
    } else if (code !== "message_index_entry_without_json") {
      appendIntegrityMessage(wrapper, "请结合检查日志和任务详情人工确认；本检查不会自动修改数据。", "admin-integrity-manual-note");
    }
    return wrapper;
  }

  function renderIntegrityReport(report, { inProgress = false } = {}) {
    if (!integrityReport) return;
    integrityReport.replaceChildren();
    if (!report || typeof report !== "object") {
      appendIntegrityMessage(integrityReport, "尚未执行过完整性检查。点击“开始只读检查”后，结果会显示在此处。", "admin-integrity-empty");
      return;
    }

    const meta = inProgress && String(report.status || "").toLowerCase() === "healthy"
      ? { label: "检查尚未完成，无法确认健康", tone: "warn" }
      : integrityReportMeta(report);
    const summary = document.createElement("div");
    summary.className = `admin-integrity-result-summary is-${meta.tone}`;
    const badge = document.createElement("span");
    badge.className = `admin-status-badge ${meta.tone}`;
    badge.textContent = meta.label;
    summary.append(badge);

    const issueCount = readCount(report.issue_count);
    const omittedCount = readCount(report.omitted_issue_count);
    const issueTotal = issueCount === null ? "未知" : String(issueCount);
    const countNode = document.createElement("span");
    countNode.className = "admin-note";
    const omittedText = omittedCount === null ? "未知" : String(omittedCount);
    countNode.textContent = inProgress || String(report.status || "").toLowerCase() === "not_checked"
      ? `当前已发现问题：${issueTotal}（检查尚未完成） · 已省略：${omittedText} 条`
      : `问题总数：${issueTotal} · 已省略：${omittedText} 条`;
    summary.append(countNode);
    integrityReport.append(summary);

    const issues = Array.isArray(report.issues) ? report.issues.filter((issue) => issue && typeof issue === "object") : [];

    if (issueCount > 0 && !inProgress) {
      const missingCount = issues.filter((i) => i.code === "message_missing_from_index").length;
      const mismatchCount = issues.filter((i) => i.code === "message_index_entry_mismatch").length;
      const noJsonCount = issues.filter((i) => i.code === "message_index_entry_without_json").length;

      const guidanceCard = document.createElement("div");
      guidanceCard.className = "admin-integrity-guidance-card";

      const gTitle = document.createElement("h4");
      gTitle.className = "admin-integrity-guidance-title";
      gTitle.textContent = "📋 完整性异常诊断与处置建议";
      guidanceCard.append(gTitle);

      const gLead = document.createElement("p");
      gLead.className = "admin-integrity-guidance-lead";
      gLead.textContent = "原始月度消息 JSON 与本地媒体引用检查通过。所发现问题属于数据库索引对应异常（主要由历史跨成员同编号消息的主键覆盖引起），可能影响搜索与列表归属。原始数据完全完好，无需重新下载全部媒体。";
      guidanceCard.append(gLead);

      const gStats = document.createElement("div");
      gStats.className = "admin-integrity-guidance-stats";
      if (missingCount > 0) {
        const p1 = document.createElement("span");
        p1.className = "admin-integrity-stat-pill";
        p1.textContent = `缺索引：${missingCount} 条（含同编号）`;
        gStats.append(p1);
      }
      if (mismatchCount > 0) {
        const p2 = document.createElement("span");
        p2.className = "admin-integrity-stat-pill";
        p2.textContent = `内容不一致：${mismatchCount} 条`;
        gStats.append(p2);
      }
      if (noJsonCount > 0) {
        const p3 = document.createElement("span");
        p3.className = "admin-integrity-stat-pill";
        p3.textContent = `索引无对应 JSON：${noJsonCount} 条`;
        gStats.append(p3);
      }
      guidanceCard.append(gStats);

      const gAdvice = document.createElement("div");
      gAdvice.className = "admin-integrity-guidance-advice";
      const strong = document.createElement("strong");
      strong.textContent = "推荐处置：";
      const desc = document.createTextNode("系统现已支持 (member_dir, id) 复合身份隔离。可在终端使用数据安全迁移工具预演并应用修复，无需全量重新下载媒体：");
      const br1 = document.createElement("br");
      const code1 = document.createElement("code");
      code1.textContent = "python tools/migrate_archive_identity.py --dry-run";
      const br2 = document.createElement("br");
      const code2 = document.createElement("code");
      code2.textContent = "python tools/migrate_archive_identity.py --apply";
      gAdvice.append(strong, desc, br1, code1, br2, code2);
      guidanceCard.append(gAdvice);

      integrityReport.append(guidanceCard);
    }

    const timestamps = document.createElement("div");
    timestamps.className = "admin-integrity-timestamps";
    const started = document.createElement("span");
    started.textContent = `开始时间：${formatTimestamp(report.started_at)}`;
    const finished = document.createElement("span");
    finished.textContent = `完成时间：${formatTimestamp(report.finished_at)}`;
    timestamps.append(started, finished);
    integrityReport.append(timestamps);

    const checks = Array.isArray(report.checks) ? report.checks : [];
    const checksSection = makeIntegritySection("检查项目", "admin-integrity-check-section");
    const checkList = document.createElement("div");
    checkList.className = "admin-integrity-check-list";
    if (!checks.length) {
      appendIntegrityMessage(checkList, "报告中没有检查项目。", "admin-integrity-empty");
    }
    for (const check of checks) {
      if (!check || typeof check !== "object") continue;
      const item = document.createElement("article");
      item.className = "admin-integrity-check";
      const head = document.createElement("div");
      head.className = "admin-integrity-check-heading";
      const label = document.createElement("strong");
      const code = typeof check.code === "string" ? check.code : "";
      label.textContent = Object.prototype.hasOwnProperty.call(INTEGRITY_CHECK_LABELS, code)
        ? INTEGRITY_CHECK_LABELS[code]
        : "其他检查项目";
      const checkStatus = String(check.status || "").toLowerCase();
      const statusMeta = Object.prototype.hasOwnProperty.call(INTEGRITY_CHECK_STATUSES, checkStatus)
        ? INTEGRITY_CHECK_STATUSES[checkStatus]
        : { label: "状态未知", tone: "muted" };
      const statusBadge = document.createElement("span");
      statusBadge.className = `admin-status-badge ${statusMeta.tone}`;
      statusBadge.textContent = statusMeta.label;
      head.append(label, statusBadge);
      item.append(head);
      if (code) {
        const codeNode = document.createElement("code");
        codeNode.className = "admin-integrity-check-code";
        codeNode.textContent = code;
        item.append(codeNode);
      }
      const counters = [];
      for (const [field, title] of [["checked", "已检查"], ["missing", "缺失"], ["mismatched", "不一致"], ["invalid", "无效"], ["unavailable", "不可用"]]) {
        const value = readCount(check[field]);
        if (value !== null) counters.push(`${title} ${value}`);
      }
      if (counters.length) appendIntegrityMessage(item, counters.join(" · "), "admin-integrity-check-counts");
      checkList.append(item);
    }
    checksSection.append(checkList);
    integrityReport.append(checksSection);

    const issuesSection = makeIntegritySection(`问题明细（显示 ${issues.length} 条）`, "admin-integrity-issues-section");
    const issueList = document.createElement("div");
    issueList.className = "admin-integrity-issue-list";

    if (issues.length > 5) {
      const filterBar = document.createElement("div");
      filterBar.className = "admin-integrity-filter-bar";
      const missingTotal = issues.filter((i) => i.code === "message_missing_from_index").length;
      const mismatchTotal = issues.filter((i) => i.code === "message_index_entry_mismatch").length;
      const noJsonTotal = issues.filter((i) => i.code === "message_index_entry_without_json").length;

      const filterCategories = [
        { id: "all", label: `全部 (${issues.length})` },
        { id: "missing", label: `缺索引 (${missingTotal})` },
        { id: "mismatch", label: `内容不一致 (${mismatchTotal})` },
        { id: "no_json", label: `索引无 JSON (${noJsonTotal})` },
      ].filter((cat) => cat.id === "all" || !cat.label.includes("(0)"));

      let activeCategory = "all";
      const filterButtons = [];
      for (const cat of filterCategories) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = `admin-integrity-filter-btn${cat.id === activeCategory ? " is-active" : ""}`;
        btn.textContent = cat.label;
        btn.addEventListener("click", () => {
          activeCategory = cat.id;
          for (const b of filterButtons) b.classList.toggle("is-active", b === btn);
          const issueCards = issueList.querySelectorAll(".admin-integrity-issue");
          for (const card of issueCards) {
            const cardCat = card.dataset.category || "other";
            card.hidden = activeCategory !== "all" && cardCat !== activeCategory;
          }
        });
        filterButtons.push(btn);
        filterBar.append(btn);
      }
      issuesSection.append(filterBar);
    }
    if (!issues.length) {
      const reportStatus = String(report.status || "").toLowerCase();
      const noIssueMessage = reportStatus === "healthy" && !inProgress
        ? "报告没有列出具体问题。"
        : reportStatus === "healthy" && inProgress
          ? "检查尚未完成，当前没有问题明细。"
        : reportStatus === "needs_repair"
          ? "报告状态指示存在问题，但没有返回问题明细。"
          : reportStatus === "not_checked"
            ? "检查尚未完成，当前没有问题明细。"
            : "检查未能完成或部分数据不可用，无法得出问题明细。";
      appendIntegrityMessage(issueList, issueCount > 0
        ? "没有可显示的问题明细，请检查报告状态和日志。"
        : noIssueMessage, "admin-integrity-empty");
    }
    for (const issue of issues) issueList.append(renderIntegrityIssue(issue));
    if (omittedCount !== null && omittedCount > 0) {
      appendIntegrityMessage(issueList, `另有 ${omittedCount} 条问题因显示数量上限而省略。`, "admin-integrity-omitted");
    }
    issuesSection.append(issueList);
    integrityReport.append(issuesSection);

    for (const [field, title] of [["coverage", "本次检查范围"], ["not_covered", "当前未覆盖"]]) {
      const entries = Array.isArray(report[field]) ? report[field] : [];
      const section = makeIntegritySection(title, `admin-integrity-${field}-section`);
      const list = document.createElement("ul");
      list.className = "admin-integrity-scope-list";
      if (!entries.length) appendIntegrityMessage(section, "没有提供范围说明。", "admin-integrity-empty");
      for (const entry of entries) {
        const row = document.createElement("li");
        row.textContent = asText(entry, "未提供说明");
        list.append(row);
      }
      if (entries.length) section.append(list);
      integrityReport.append(section);
    }
  }

  function renderIntegrityPanel() {
    if (!integrityPanel) return;
    const active = isActiveIntegrityTask(integrityTaskData);
    const stale = active && integrityTaskIsStale(integrityTaskData);
    if (integrityActivity) integrityActivity.hidden = !active;
    if (integrityStaleNote) {
      integrityStaleNote.hidden = !integrityConflictMessage
        && !(active && (stale || typeof integrityTaskData.heartbeat_stale !== "boolean"));
      integrityStaleNote.textContent = integrityConflictMessage
        ? `${integrityConflictMessage} 请刷新状态；成功获取最新状态前禁止再次提交。`
        : integrityRestartNote(integrityTaskData);
    }
    if (integrityTaskId) {
      integrityTaskId.textContent = active && integrityTaskData.id !== null && integrityTaskData.id !== undefined
        ? `任务 ID：${String(integrityTaskData.id)}`
        : "";
    }
    if (integrityConflictMessage) {
      setIntegrityStatus(`${integrityConflictMessage} 请刷新状态确认；成功获取最新状态前禁止再次提交。${integrityError ? ` 刷新失败：${integrityError}` : ""}`, "warn");
    } else if (integrityLoading && (integrityManualLoading || !hasLoadedIntegrity)) {
      setIntegrityStatus("正在刷新完整性检查结果…");
    } else if (integrityError) {
      setIntegrityStatus(`当前检查状态不可用：${integrityError}`, "error");
    } else if (active) {
      const progress = integrityTaskData.progress && typeof integrityTaskData.progress === "object"
        ? integrityTaskData.progress
        : integrityTaskData;
      const done = readCount(progress.done) ?? 0;
      const total = readCount(progress.total) ?? 0;
      const phase = String(progress.phase || integrityTaskData.phase || "queued");
      if (integrityProgress) {
        integrityProgress.max = total > 0 ? total : 1;
        if (total > 0) integrityProgress.value = Math.min(done, total);
        else integrityProgress.removeAttribute("value");
      }
      if (integrityProgressText) {
        integrityProgressText.textContent = total > 0
          ? `${INTEGRITY_PHASE_LABELS[phase] || "正在检查"} · ${done} / ${total}`
          : done > 0
            ? `${INTEGRITY_PHASE_LABELS[phase] || "正在检查"} · 已检查 ${done} 条（总量未知）`
            : `${INTEGRITY_PHASE_LABELS[phase] || "正在检查"} · 进度统计准备中`;
      }
      if (stale) {
        if (integrityProgressText) {
          integrityProgressText.textContent = "检查已中断 · 等待手动重试";
        }
        const restartNote = integrityRestartNote(integrityTaskData);
        const staleMessage = integrityMessage ? `${integrityMessage} ${restartNote}` : restartNote;
        setIntegrityStatus(staleMessage, "warn");
      } else if (integrityMessage) {
        setIntegrityStatus(integrityMessage, "ok");
      } else {
        setIntegrityStatus("检查任务正在运行；离开本页签或页面不可见时会暂停自动刷新。", "running");
      }
    } else if (integrityMessage) {
      setIntegrityStatus(integrityMessage, "ok");
    } else if (integrityReportData) {
      setIntegrityStatus("已加载最近一次只读完整性检查报告：", "ok");
    } else if (hasLoadedIntegrity) {
      setIntegrityStatus("尚无历史完整性报告。点击“开始只读检查”发起一次检查。");
    }

    if (runIntegrityButton) {
      runIntegrityButton.disabled = !authorized || Boolean(integrityRunController) || !canStartIntegrityCheck();
      runIntegrityButton.textContent = active && stale ? "手动重新检查" : "开始只读检查";
    }
    if (refreshIntegrityButton) refreshIntegrityButton.disabled = !authorized || integrityLoading;

    if (integrityReport) {
      if (integrityError) {
        integrityReport.replaceChildren();
        appendIntegrityMessage(integrityReport, "本次请求未能读取完整性报告。刷新后可重新获取；页面不会将旧报告显示为当前健康状态。", "admin-integrity-unavailable");
      } else if (integrityLoading && (integrityManualLoading || !hasLoadedIntegrity)) {
        integrityReport.replaceChildren();
        appendIntegrityMessage(integrityReport, "正在获取最新结果…", "admin-integrity-empty");
      } else if (active && !integrityReportData) {
        integrityReport.replaceChildren();
        appendIntegrityMessage(integrityReport, "本次任务尚未生成报告；完成后会显示检查项目和问题明细。", "admin-integrity-empty");
      } else {
        renderIntegrityReport(integrityReportData, { inProgress: active });
      }
    }
  }

  function scheduleIntegrityPoll() {
    if (!authorized || !taskTabActive || document.hidden || !isActiveIntegrityTask(integrityTaskData) || integrityTimer !== null) return;
    integrityTimer = window.setTimeout(() => {
      integrityTimer = null;
      void refreshIntegrityOverview();
    }, POLL_INTERVAL_MS);
  }

  async function refreshIntegrityOverview({ manual = false } = {}) {
    if (!authorized || !taskTabActive || document.hidden || !integrityPanel) return;
    if (integrityRequestController && !manual) return;
    if (integrityTimer !== null) {
      window.clearTimeout(integrityTimer);
      integrityTimer = null;
    }
    if (integrityRequestController && manual) integrityRequestController.abort();
    const controller = new AbortController();
    const generation = lifecycleGeneration;
    const stateVersion = integrityStateVersion;
    integrityRequestController = controller;
    integrityLoading = true;
    integrityManualLoading = manual;
    integrityError = "";
    integrityMessage = "";
    renderIntegrityPanel();
    try {
      const data = await requestWithTimeout("/api/archive/integrity", controller);
      if (controller.signal.aborted || generation !== lifecycleGeneration || stateVersion !== integrityStateVersion) return;
      if (!data || data.ok !== true || (data.latest_report !== null && data.latest_report !== undefined && typeof data.latest_report !== "object")
        || (data.current_task !== null && data.current_task !== undefined && typeof data.current_task !== "object")) {
        throw new Error(responseError(data, "完整性检查结果返回格式异常。"));
      }
      const current = data.current_task && typeof data.current_task === "object" ? data.current_task : null;
      integrityTaskData = current;
      integrityConflictMessage = "";
      integrityReportData = current && isActiveIntegrityTask(current)
        ? (current.report && typeof current.report === "object" ? current.report : null)
        : (data.latest_report && typeof data.latest_report === "object" ? data.latest_report : null);
      integrityError = "";
      integrityMessage = "";
      hasLoadedIntegrity = true;
    } catch (error) {
      if (controller.signal.aborted || generation !== lifecycleGeneration) return;
      if (stateVersion !== integrityStateVersion) return;
      integrityError = errorMessage(error, "暂时无法读取完整性检查结果。");
    } finally {
      if (integrityRequestController === controller) {
        integrityRequestController = null;
      }
      if (integrityRequestController === null && generation === lifecycleGeneration) {
        integrityLoading = false;
        integrityManualLoading = false;
        renderIntegrityPanel();
        scheduleIntegrityPoll();
      }
    }
  }

  async function startIntegrityCheck() {
    if (!authorized || !taskTabActive || document.hidden || !integrityPanel || integrityRunController) return;
    if (!canStartIntegrityCheck()) return;
    integrityStateVersion += 1;
    const controller = new AbortController();
    const generation = lifecycleGeneration;
    integrityRunController = controller;
    integrityMessage = "正在提交只读检查请求…";
    integrityError = "";
    renderIntegrityPanel();
    let submissionMessage = "";
    try {
      const data = await requestWithTimeout("/api/archive/integrity/check", controller, { method: "POST" });
      if (controller.signal.aborted || generation !== lifecycleGeneration) return;
      if (!data || data.ok !== true || !data.task || typeof data.task !== "object") {
        throw new Error(responseError(data, "服务器未接受完整性检查请求。"));
      }
      integrityStateVersion += 1;
      integrityTaskData = data.task;
      integrityReportData = data.task.report && typeof data.task.report === "object" ? data.task.report : null;
      integrityError = "";
      const taskId = data.task_id || data.task.id || "";
      submissionMessage = data.reused === true
        ? `已有检查任务仍处于活动状态，已复用该任务${taskId ? `（${String(taskId)}）` : ""}。`
        : `只读检查已提交${taskId ? `（任务 ${String(taskId)}）` : ""}。`;
      integrityMessage = submissionMessage;
      hasLoadedIntegrity = true;
      renderIntegrityPanel();
      await refreshIntegrityOverview({ manual: true });
      if (!controller.signal.aborted && generation === lifecycleGeneration && !integrityError) {
        integrityMessage = submissionMessage;
        renderIntegrityPanel();
      }
    } catch (error) {
      if (controller.signal.aborted || generation !== lifecycleGeneration) return;
      if (error && error.status === 409) {
        integrityStateVersion += 1;
        const data = error.data;
        if (data && data.task && typeof data.task === "object" && !Array.isArray(data.task)) {
          integrityTaskData = { ...integrityTaskData, ...data.task };
        }
        integrityConflictMessage = data && typeof data.msg === "string" && data.msg
          ? data.msg : "服务端拒绝了重复检查请求，当前检查进程状态需要人工确认。";
        integrityError = "";
        integrityMessage = "";
        return;
      }
      integrityError = error && error.code === "TASK_REQUEST_TIMEOUT"
        ? "检查请求超时。请刷新状态确认服务端是否已接收任务。"
        : errorMessage(error, "暂时无法启动完整性检查。");
      integrityMessage = "";
    } finally {
      if (integrityRunController === controller) integrityRunController = null;
      if (generation === lifecycleGeneration) {
        renderIntegrityPanel();
        scheduleIntegrityPoll();
      }
    }
  }

  function stopIntegrityPolling() {
    if (integrityTimer !== null) {
      window.clearTimeout(integrityTimer);
      integrityTimer = null;
    }
    if (integrityRequestController) {
      integrityRequestController.abort();
      integrityRequestController = null;
    }
    if (integrityRunController) {
      integrityRunController.abort();
      integrityRunController = null;
      integrityMessage = "检查请求已取消或页面离开；返回后请刷新确认服务端状态。";
    }
    integrityLoading = false;
    integrityManualLoading = false;
  }

  if (runIntegrityButton) runIntegrityButton.addEventListener("click", () => void startIntegrityCheck());
  if (refreshIntegrityButton) refreshIntegrityButton.addEventListener("click", () => void refreshIntegrityOverview({ manual: true }));

  function createMeta(label, field) {
    const wrapper = document.createElement("span");
    wrapper.className = `admin-task-meta admin-task-${field}`;
    const labelNode = document.createElement("span");
    labelNode.className = "admin-task-meta-label";
    labelNode.textContent = label;
    const valueNode = document.createElement("span");
    valueNode.className = "admin-task-meta-value";
    valueNode.dataset.taskField = field;
    wrapper.append(labelNode, valueNode);
    return wrapper;
  }

  function createTaskCard(id) {
    const details = document.createElement("details");
    details.className = "admin-task-card";
    details.dataset.taskId = id;

    const summary = document.createElement("summary");
    summary.className = "admin-task-summary";

    const main = document.createElement("span");
    main.className = "admin-task-main";
    const kind = document.createElement("span");
    kind.className = "admin-task-kind";
    kind.dataset.taskField = "kind";
    const badge = document.createElement("span");
    badge.className = "admin-status-badge admin-task-status-badge muted";
    badge.dataset.taskField = "status";
    main.append(kind, badge);

    const phase = createMeta("阶段", "phase");
    const progress = createMeta("进度 / 失败项", "progress");
    const time = createMeta("时间", "time");
    const chevron = document.createElement("span");
    chevron.className = "admin-task-chevron";
    chevron.setAttribute("aria-hidden", "true");
    chevron.textContent = "⌄";
    const progressBar = document.createElement("div");
    progressBar.className = "admin-task-progress-bar";
    progressBar.hidden = true;
    const progressFill = document.createElement("div");
    progressFill.className = "admin-task-progress-fill";
    progressBar.append(progressFill);
    summary.append(main, phase, progress, time, chevron, progressBar);

    const panel = document.createElement("div");
    panel.className = "admin-task-detail";
    const identity = document.createElement("p");
    identity.className = "admin-task-id admin-note";
    identity.append(document.createTextNode("任务 ID："));
    const idCode = document.createElement("code");
    idCode.textContent = id;
    identity.append(idCode);

    const detailStatus = document.createElement("p");
    detailStatus.className = "admin-task-detail-status";
    detailStatus.setAttribute("role", "status");
    const heartbeatStaleNote = document.createElement("p");
    heartbeatStaleNote.className = "admin-note";
    heartbeatStaleNote.hidden = true;
    const taskError = document.createElement("p");
    taskError.className = "admin-task-top-error";
    taskError.hidden = true;
    const items = document.createElement("div");
    items.className = "admin-task-items";
    const interruptedNote = document.createElement("p");
    interruptedNote.className = "admin-task-interrupted-note admin-note";
    interruptedNote.hidden = true;

    const actions = document.createElement("div");
    actions.className = "admin-actions admin-task-actions";
    const refreshDetail = document.createElement("button");
    refreshDetail.className = "btn small";
    refreshDetail.type = "button";
    refreshDetail.textContent = "刷新详情";
    const retryButton = document.createElement("button");
    retryButton.className = "btn primary admin-task-retry";
    retryButton.type = "button";
    retryButton.textContent = "只重试失败项";
    retryButton.hidden = true;
    actions.append(refreshDetail, retryButton);

    const retryFeedback = document.createElement("p");
    retryFeedback.className = "admin-task-retry-feedback";
    retryFeedback.setAttribute("role", "status");
    retryFeedback.hidden = true;

    panel.append(identity, detailStatus, heartbeatStaleNote, taskError, interruptedNote, items, retryFeedback, actions);
    details.append(summary, panel);

    details.addEventListener("toggle", () => {
      if (details.open) {
        const state = detailStates.get(id);
        if (!state || state.status === "idle" || state.status === "error") {
          void loadTaskDetail(id);
        }
      } else {
        abortDetailRequest(id);
      }
    });
    refreshDetail.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      void loadTaskDetail(id, { force: true });
    });
    retryButton.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      void retryFailedItems(id);
    });

    const card = {
      element: details,
      kind,
      badge,
      phase: summary.querySelector('[data-task-field="phase"]'),
      progress: summary.querySelector('[data-task-field="progress"]'),
      time: summary.querySelector('[data-task-field="time"]'),
      progressBar,
      progressFill,
      detailStatus,
      heartbeatStaleNote,
      taskError,
      items,
      interruptedNote,
      refreshDetail,
      retryButton,
      retryFeedback,
      retryMessage: "",
      retryError: false,
    };
    cards.set(id, card);
    detailStates.set(id, { status: "idle", items: [], error: "", retrySupported: false });
    return card;
  }

  function updateTaskCard(task) {
    const id = String(task.id);
    const card = cards.get(id) || createTaskCard(id);
    const meta = statusMeta(task.status);
    card.kind.textContent = taskKindLabel(task);
    card.badge.textContent = meta.label;
    card.badge.className = `admin-status-badge admin-task-status-badge ${meta.tone}`;
    const rawPhase = String(task.phase || "");
    if (task.status === "stale") {
      card.phase.textContent = "已中断";
    } else if (task.status === "cancelled") {
      card.phase.textContent = "已取消";
    } else if (task.status === "failed") {
      card.phase.textContent = "已失败";
    } else if (task.status === "completed") {
      card.phase.textContent = INTEGRITY_PHASE_LABELS[rawPhase] || "已完成";
    } else {
      card.phase.textContent = task.kind === "archive_integrity"
        ? (INTEGRITY_PHASE_LABELS[rawPhase] || "正在检查")
        : (TASK_PHASE_LABELS[rawPhase] || INTEGRITY_PHASE_LABELS[rawPhase] || asText(task.phase) || "处理中");
    }
    const done = task.done === null || task.done === undefined ? "—" : asText(task.done);
    const total = task.total === null || task.total === undefined ? "—" : asText(task.total);
    const failures = task.failure_count === null || task.failure_count === undefined ? "0" : asText(task.failure_count);
    const progressLabel = card.progress.parentElement.querySelector(".admin-task-meta-label");
    if (task.kind === "archive_integrity") {
      card.progress.textContent = `${done} / ${total}`;
      if (progressLabel) progressLabel.textContent = "检查进度";
    } else {
      card.progress.textContent = `${done} / ${total} · 失败 ${failures}`;
      if (progressLabel) progressLabel.textContent = "进度 / 失败项";
      if (typeof task.total === "number" && task.total > 0 && typeof task.done === "number") {
        const pct = Math.min(100, Math.max(0, Math.round((task.done / task.total) * 100)));
        card.progress.textContent = `${done} / ${total} (${pct}%) · 失败 ${failures}`;
      } else if (task.total === 0 && Number(failures) > 0) {
        card.progress.textContent = `未产出项 · 失败 ${failures}`;
      }
    }
    if (card.progressBar && card.progressFill) {
      if (typeof task.total === "number" && task.total > 0 && typeof task.done === "number") {
        const pct = Math.min(100, Math.max(0, Math.round((task.done / task.total) * 100)));
        card.progressFill.style.width = pct + "%";
        card.progressFill.className = `admin-task-progress-fill ${meta.tone}`;
        card.progressBar.hidden = false;
        card.progressBar.title = `完成进度: ${pct}%`;
      } else {
        card.progressBar.hidden = true;
      }
    }
    const time = taskTime(task);
    const formattedTime = formatTimestamp(time.value);
    card.time.textContent = formattedTime;
    const timeLabel = card.time.parentElement.querySelector(".admin-task-meta-label");
    if (timeLabel) timeLabel.textContent = time.label;
    card.time.title = time.value ? String(time.value) : "";
    renderDetailState(id);
    return card.element;
  }

  function renderTasks(tasks) {
    const incoming = new Set();
    const orderedElements = [];
    for (const task of tasks) {
      if (!task || task.id === null || task.id === undefined || String(task.id) === "") continue;
      const id = String(task.id);
      incoming.add(id);
      taskRecords.set(id, task);
      orderedElements.push(updateTaskCard(task));
    }
    for (const [id, card] of cards) {
      if (!incoming.has(id)) {
        abortDetailRequest(id);
        cards.delete(id);
        detailStates.delete(id);
        taskRecords.delete(id);
        card.element.remove();
      }
    }
    orderedElements.forEach((element, index) => {
      const current = taskList.children[index];
      if (current !== element) taskList.insertBefore(element, current || null);
    });
    while (taskList.children.length > orderedElements.length) {
      taskList.lastElementChild.remove();
    }
    applyFilters();
  }

  function taskMatchesFilter(task) {
    if (!task) return false;
    if (currentFilterType !== "all" && task.kind !== currentFilterType) return false;
    if (currentFilterStatus !== "all") {
      const s = String(task.status || "").toLowerCase();
      if (currentFilterStatus === "running") {
        if (s !== "running" && s !== "queued" && s !== "starting") return false;
      } else if (currentFilterStatus === "issues") {
        const isIssue = s === "failed" || s === "interrupted" || (task.failure_count && Number(task.failure_count) > 0);
        if (!isIssue) return false;
      } else if (currentFilterStatus === "completed") {
        if (s !== "completed") return false;
      }
    }
    return true;
  }

  function applyFilters() {
    let visibleCount = 0;
    for (const [id, card] of cards) {
      const task = taskRecords.get(id);
      const visible = taskMatchesFilter(task);
      card.element.hidden = !visible;
      if (visible) visibleCount += 1;
    }
    if (hasLoadedTasks) {
      const totalCount = taskRecords.size;
      const isFiltered = currentFilterType !== "all" || currentFilterStatus !== "all";
      if (totalCount === 0) {
        setNotice("没有最近的归档任务。启动归档任务后会在此显示。");
      } else if (isFiltered) {
        setNotice(`已加载 ${totalCount} 个最近任务（筛选显示 ${visibleCount} 个）；任务中心打开时实时同步。`);
      } else {
        setNotice(`已加载 ${totalCount} 个最近任务；任务中心打开时实时同步。`);
      }
    }
  }

  function makeErrorItem(item) {
    const wrapper = document.createElement("article");
    wrapper.className = "admin-task-item";
    const heading = document.createElement("div");
    heading.className = "admin-task-item-heading";
    const type = document.createElement("strong");
    type.className = "admin-task-item-type";
    const rawType = asText(item.item_type, "失败项");
    type.textContent = Object.prototype.hasOwnProperty.call(ITEM_TYPES, rawType) ? ITEM_TYPES[rawType] : rawType;
    const key = document.createElement("code");
    key.className = "admin-task-item-key";
    key.textContent = asText(item.item_key, "未提供项目标识");
    heading.append(type, key);

    const meta = document.createElement("p");
    meta.className = "admin-task-item-meta admin-note";
    const attempts = item.attempts === null || item.attempts === undefined ? "—" : asText(item.attempts);
    const itemStatus = String(item.status || "").toLowerCase() === "failed" ? "失败" : asText(item.status);
    meta.textContent = `状态：${itemStatus} · 尝试次数：${attempts}`;

    const errorCode = item.error_code === null || item.error_code === undefined
      ? ""
      : String(item.error_code).trim();
    const message = document.createElement("p");
    message.className = "admin-task-error-message";
    message.textContent = item.error_message ? String(item.error_message) : "未提供明确错误原因。";
    wrapper.append(heading, meta);
    if (errorCode) {
      const code = document.createElement("p");
      code.className = "admin-task-error-code";
      code.textContent = `错误代码：${errorCode}`;
      wrapper.append(code);
    }
    wrapper.append(message);
    return wrapper;
  }

  function renderDetailState(id) {
    const card = cards.get(id);
    const state = detailStates.get(id);
    if (!card || !state) return;

    card.items.replaceChildren();
    card.interruptedNote.hidden = true;
    const task = taskRecords.get(id);
    const integrityTask = Boolean(task && task.kind === "archive_integrity");
    card.retryButton.hidden = integrityTask || state.status !== "loaded" || state.retrySupported !== true;
    const heartbeatStale = task
      && String(task.status || "").toLowerCase() === "running"
      && task.heartbeat_stale === true;
    card.heartbeatStaleNote.textContent = heartbeatStale
      ? "任务心跳已超时。请检查服务进程和任务运行状态，确认是否仍在执行。"
      : "";
    card.heartbeatStaleNote.hidden = !heartbeatStale;
    card.taskError.textContent = task && task.error ? `任务错误：${String(task.error)}` : "";
    card.taskError.hidden = !card.taskError.textContent;
    card.refreshDetail.disabled = state.status === "loading" || retryControllers.has(id);
    card.retryButton.disabled = state.status !== "loaded" || !isTerminalTask(task) || retryControllers.has(id);
    card.retryFeedback.textContent = card.retryMessage;
    card.retryFeedback.hidden = !card.retryMessage;
    card.retryFeedback.classList.toggle("is-error", card.retryError);

    if (state.status === "loading") {
      card.detailStatus.textContent = "正在加载任务详情…";
      card.detailStatus.className = "admin-task-detail-status";
      return;
    }
    if (state.status === "error") {
      card.detailStatus.textContent = `任务详情加载失败：${state.error}`;
      card.detailStatus.className = "admin-task-detail-status is-error";
      return;
    }
    if (state.status !== "loaded") {
      card.detailStatus.textContent = integrityTask
        ? "完整性检查报告显示在下方的独立面板中。"
        : "展开后载入失败项详情。";
      card.detailStatus.className = "admin-task-detail-status";
      return;
    }

    if (integrityTask) {
      if (state.report && typeof state.report === "object") {
        const rep = state.report;
        const checks = Array.isArray(rep.checks) ? rep.checks : [];
        const issueCount = readCount(rep.issue_count) || 0;
        card.detailStatus.textContent = issueCount === 0
          ? `检查完成：各检查项均正常（共 ${checks.length} 项检查，无异常）。完整检查报告亦可在下方独立面板中查看。`
          : `检查完成：发现 ${issueCount} 处问题，详情可参考下方独立面板。`;
        card.detailStatus.className = `admin-task-detail-status${issueCount > 0 ? " is-error" : ""}`;
        if (checks.length > 0) {
          checks.forEach((chk) => {
            const itemArticle = document.createElement("article");
            itemArticle.className = "admin-task-item";
            const heading = document.createElement("div");
            heading.className = "admin-task-item-heading";
            const name = document.createElement("strong");
            name.className = "admin-task-item-type";
            name.textContent = INTEGRITY_CHECK_LABELS[chk.name] || chk.name;
            const badge = document.createElement("span");
            const stMeta = INTEGRITY_CHECK_STATUSES[chk.status] || { label: chk.status, tone: "muted" };
            badge.className = `admin-status-badge ${stMeta.tone}`;
            badge.textContent = stMeta.label;
            heading.append(name, badge);
            itemArticle.append(heading);
            card.items.append(itemArticle);
          });
        }
      } else {
        card.detailStatus.textContent = "完整性检查报告显示在下方的独立面板中；此任务不支持重试失败项。";
        card.detailStatus.className = "admin-task-detail-status";
      }
      return;
    }

    const failedItems = state.items.filter((item) => String(item && item.status || "").toLowerCase() === "failed");
    const total = task && task.total !== null && task.total !== undefined ? asText(task.total) : "—";
    const failureCount = task && task.failure_count !== null && task.failure_count !== undefined
      ? asText(task.failure_count)
      : "0";
    card.detailStatus.textContent = Number(failureCount) > 0
      ? state.retrySupported === true
        ? `发现 ${failureCount} 个失败项，共 ${total} 项。`
        : `发现 ${failureCount} 个失败项，但当前没有可安全重试的项，共 ${total} 项。`
      : `没有可重试的失败项，共 ${total} 项。`;
    card.detailStatus.className = "admin-task-detail-status";
    if (task && task.status === "interrupted") {
      card.interruptedNote.textContent = "此任务已中断。请先检查服务运行状态和任务错误，再从相应归档入口重新发起；如仍有失败项，可使用下方操作重试。本页不会自动恢复中断任务。";
      card.interruptedNote.hidden = false;
    }
    for (const item of failedItems) card.items.append(makeErrorItem(item));
    const canRetry = isTerminalTask(task) && state.retrySupported === true;
    card.retryButton.hidden = !canRetry;
    card.retryButton.disabled = !canRetry || retryControllers.has(id);
  }

  function requestWithTimeout(path, controller, options = {}) {
    let timedOut = false;
    const timer = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, REQUEST_TIMEOUT_MS);
    return api(path, { ...options, cache: "no-store", signal: controller.signal })
      .catch((error) => {
        if (timedOut) {
          const timeoutError = new Error("请求超时，请检查服务状态后重试。");
          timeoutError.code = "TASK_REQUEST_TIMEOUT";
          throw timeoutError;
        }
        throw error;
      })
      .finally(() => window.clearTimeout(timer));
  }

  function scheduleNextPoll() {
    if (!authorized || !taskTabActive || document.hidden || listTimer !== null) return;
    const hasActive = Array.from(taskRecords.values()).some((t) => {
      const s = String(t && t.status || "").toLowerCase();
      return s === "running" || s === "starting" || s === "queued";
    });
    const interval = hasActive ? POLL_INTERVAL_MS : IDLE_POLL_INTERVAL_MS;
    listTimer = window.setTimeout(() => {
      listTimer = null;
      void refreshTaskList();
    }, interval);
  }

  async function refreshTaskList({ manual = false } = {}) {
    if (!authorized || !taskTabActive || document.hidden) return;
    if (listController && !manual) return;
    if (listTimer !== null) {
      window.clearTimeout(listTimer);
      listTimer = null;
    }
    if (listController && manual) listController.abort();

    const controller = new AbortController();
    const generation = lifecycleGeneration;
    listController = controller;
    clearListError();
    if (!hasLoadedTasks || manual) setNotice(manual ? "正在刷新最近任务…" : "正在加载最近任务…");

    try {
      const data = await requestWithTimeout("/api/archive/tasks", controller);
      if (controller.signal.aborted || generation !== lifecycleGeneration) return;
      if (!data || data.ok !== true || !Array.isArray(data.tasks)) {
        throw new Error(responseError(data, "任务列表返回格式异常。"));
      }
      for (const task of data.tasks) {
        if (!task || task.id === null || task.id === undefined || String(task.id) === "") {
          throw new Error("任务列表中有记录缺少任务 ID，无法安全显示。");
        }
      }
      const previousTasks = new Map(taskRecords);
      renderTasks(data.tasks);
      hasLoadedTasks = true;
      setNotice(data.tasks.length
        ? `已加载 ${data.tasks.length} 个最近任务；任务中心打开时每约 2 秒同步一次。`
        : "没有最近的归档任务。启动归档任务后会在此显示。");
      for (const [id, card] of cards) {
        const task = taskRecords.get(id);
        const transitionedToTerminal = isActiveTask(previousTasks.get(id)) && isTerminalTask(task);
        if (card.element.open && (isActiveTask(task) || transitionedToTerminal)) {
          if (transitionedToTerminal) abortDetailRequest(id);
          void loadTaskDetail(id, { force: true });
        }
      }
    } catch (error) {
      if (controller.signal.aborted || generation !== lifecycleGeneration) return;
      if (error && error.code === "TASK_REQUEST_TIMEOUT") {
        showListError(hasLoadedTasks
          ? "任务列表请求超时。当前仍显示上次成功加载的数据；可以手动刷新。"
          : "任务列表请求超时。请检查服务状态后重试。");
      } else {
        const message = errorMessage(error, "暂时无法加载任务列表。");
        showListError(hasLoadedTasks
          ? `任务列表更新失败：${message} 当前仍显示上次成功加载的数据。`
          : `任务列表加载失败：${message}`);
      }
      if (!hasLoadedTasks) setNotice("最近任务尚未加载。");
    } finally {
      if (listController === controller) {
        listController = null;
        if (generation === lifecycleGeneration) scheduleNextPoll();
      }
    }
  }

  async function loadTaskDetail(id, { force = false } = {}) {
    if (!authorized || !taskTabActive || document.hidden || detailControllers.has(id)) return;
    const current = detailStates.get(id);
    if (!force && current && current.status === "loaded") return;
    const controller = new AbortController();
    const generation = lifecycleGeneration;
    detailControllers.set(id, controller);
    const state = current || { status: "idle", items: [], error: "", retrySupported: false };
    state.status = "loading";
    state.error = "";
    detailStates.set(id, state);
    renderDetailState(id);
    try {
      const data = await requestWithTimeout(`/api/archive/tasks/${encodeURIComponent(id)}`, controller);
      if (controller.signal.aborted || generation !== lifecycleGeneration) return;
      if (!data || data.ok !== true || !data.task || !Array.isArray(data.items)
        || typeof data.retry_supported !== "boolean") {
        throw new Error(responseError(data, "任务详情返回格式异常。"));
      }
      if (data.task.id !== null && data.task.id !== undefined) {
        const task = { ...(taskRecords.get(id) || {}), ...data.task };
        taskRecords.set(id, task);
        updateTaskCard(task);
      }
      state.status = "loaded";
      state.items = data.items;
      state.retrySupported = data.retry_supported;
      state.error = "";
      const card = cards.get(id);
      if (card && card.retryError) {
        card.retryMessage = "";
        card.retryError = false;
      }
    } catch (error) {
      if (controller.signal.aborted || generation !== lifecycleGeneration) return;
      state.status = "error";
      state.error = errorMessage(error, "暂时无法加载任务详情。");
    } finally {
      if (detailControllers.get(id) === controller) detailControllers.delete(id);
      if (generation === lifecycleGeneration) renderDetailState(id);
    }
  }

  async function retryFailedItems(id) {
    if (!authorized || !taskTabActive || document.hidden || retryControllers.has(id)) return;
    if (taskRecords.get(id) && taskRecords.get(id).kind === "archive_integrity") return;
    const state = detailStates.get(id);
    if (!state || state.status !== "loaded" || state.retrySupported !== true
      || !isTerminalTask(taskRecords.get(id))) return;

    const controller = new AbortController();
    const generation = lifecycleGeneration;
    const card = cards.get(id);
    if (!card) return;
    retryControllers.set(id, controller);
    card.retryMessage = "正在提交失败项重试请求…";
    card.retryError = false;
    renderDetailState(id);
    try {
      const data = await requestWithTimeout(`/api/archive/tasks/${encodeURIComponent(id)}/retry`, controller, { method: "POST" });
      if (controller.signal.aborted || generation !== lifecycleGeneration) return;
      if (!data || data.ok !== true) {
        throw new Error(responseError(data, "服务器未接受失败项重试请求。"));
      }
      const requestId = data.request_id ? ` 请求编号：${String(data.request_id)}。` : "";
      card.retryMessage = `失败项重试请求已提交。${requestId}`;
      card.retryError = false;
      if (detailStates.has(id)) detailStates.get(id).status = "idle";
      await refreshTaskList({ manual: true });
      if (taskTabActive && !document.hidden) void loadTaskDetail(id, { force: true });
    } catch (error) {
      if (controller.signal.aborted || generation !== lifecycleGeneration) return;
      card.retryMessage = error && error.code === "TASK_REQUEST_TIMEOUT"
        ? "重试请求超时；请刷新任务列表和详情确认服务器是否已接收。"
        : `失败项重试请求未完成：${errorMessage(error, "请稍后重试。")}`;
      card.retryError = true;
    } finally {
      if (retryControllers.get(id) === controller) retryControllers.delete(id);
      if (generation === lifecycleGeneration) renderDetailState(id);
    }
  }

  function abortDetailRequest(id) {
    const controller = detailControllers.get(id);
    if (!controller) return;
    controller.abort();
    detailControllers.delete(id);
    const state = detailStates.get(id);
    if (state && state.status === "loading") {
      state.status = "idle";
      renderDetailState(id);
    }
  }

  function stopTaskPolling() {
    lifecycleGeneration += 1;
    stopIntegrityPolling();
    if (listTimer !== null) {
      window.clearTimeout(listTimer);
      listTimer = null;
    }
    if (listController) {
      listController.abort();
      listController = null;
    }
    for (const controller of detailControllers.values()) controller.abort();
    detailControllers.clear();
    for (const [id, controller] of retryControllers) {
      controller.abort();
      const card = cards.get(id);
      const state = detailStates.get(id);
      if (card) {
        card.retryMessage = "离开或暂停时请求已取消；返回后请刷新详情确认处理结果。";
        card.retryError = true;
      }
      if (state) state.status = "idle";
    }
    retryControllers.clear();
    for (const [id, state] of detailStates) {
      if (state.status === "loading") state.status = "idle";
      renderDetailState(id);
    }
  }

  function syncArchiveTaskPolling(active) {
    taskTabActive = authorized && active === true;
    if (!taskTabActive || document.hidden) {
      stopTaskPolling();
      return;
    }
    if (listTimer !== null) {
      window.clearTimeout(listTimer);
      listTimer = null;
    }
    if (!listController) void refreshTaskList();
    void refreshIntegrityOverview({ manual: true });
  }

  window.syncArchiveTaskPolling = syncArchiveTaskPolling;
  refreshButton.addEventListener("click", () => void refreshTaskList({ manual: true }));

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopTaskPolling();
    } else {
      syncArchiveTaskPolling(taskSection.classList.contains("active"));
    }
  });
  window.addEventListener("pagehide", stopTaskPolling);
  window.addEventListener("pageshow", () => {
    if (!document.hidden) syncArchiveTaskPolling(taskSection.classList.contains("active"));
  });

  function initializeForRole() {
    if (window.webAdminRole !== "admin") {
      authorized = false;
      renderIntegrityPanel();
      if (taskSection.classList.contains("active") && typeof window.switchAdminTab === "function") {
        window.switchAdminTab("status", true);
      }
      return;
    }
    authorized = true;
    renderIntegrityPanel();
    setupLaunchModals();
    setupFilterPills();
    syncArchiveTaskPolling(taskSection.classList.contains("active"));
  }


  // ── 任务筛选胶囊与发起弹窗控制 ───────────────────────────
  function setupFilterPills() {
    if (!filterToolbar || filterToolbar.dataset.bound === "true") return;
    filterToolbar.dataset.bound = "true";
    filterToolbar.querySelectorAll(".admin-task-filter-pill").forEach((btn) => {
      btn.addEventListener("click", () => {
        const typeFilter = btn.dataset.filterType;
        const statusFilter = btn.dataset.filterStatus;
        if (typeFilter !== undefined) {
          currentFilterType = typeFilter;
          filterToolbar.querySelectorAll('[data-filter-group="type"] .admin-task-filter-pill').forEach((p) => {
            p.classList.toggle("active", p === btn);
          });
        }
        if (statusFilter !== undefined) {
          currentFilterStatus = statusFilter;
          filterToolbar.querySelectorAll('[data-filter-group="status"] .admin-task-filter-pill').forEach((p) => {
            p.classList.toggle("active", p === btn);
          });
        }
        applyFilters();
      });
    });
  }

  function setupLaunchModals() {
    if (!dialogArchiveMessage || !dialogArchiveBlog) return;
    if (dialogArchiveMessage.dataset.bound === "true") return;
    dialogArchiveMessage.dataset.bound = "true";

    const msgMemberList = document.getElementById("archiveMsgMemberList");
    const msgSelectAll = document.getElementById("btnArchiveMsgSelectAll");
    const msgClear = document.getElementById("btnArchiveMsgClear");
    const msgSelectionSummary = document.getElementById("archiveMsgSelectionSummary");
    const msgDateRow = document.getElementById("archiveMsgDateRow");
    const msgStartDate = document.getElementById("archiveMsgStartDate");
    const msgDialogError = document.getElementById("archiveMsgDialogError");
    const btnConfirmMsg = document.getElementById("btnConfirmArchiveMessage");

    const BACKFILL_GROUP_LABELS = {
      nogizaka46: "乃木坂",
      nogizaka: "乃木坂",
      hinatazaka46: "日向坂",
      hinatazaka: "日向坂",
      sakurazaka46: "樱坂",
      sakurazaka: "樱坂",
      yodel: "yodel",
    };

    async function fetchBackfillMembers() {
      try {
        const res = await api("/api/archive/members");
        if (res && res.ok && Array.isArray(res.monitor_members) && res.monitor_members.length > 0) {
          return res.monitor_members;
        }
      } catch (_) {}

      let monitors = Array.isArray(window.config && window.config.monitor) ? window.config.monitor : null;
      if (!monitors) {
        try {
          const cfgRes = await api("/api/config");
          if (cfgRes && cfgRes.config && Array.isArray(cfgRes.config.monitor)) {
            monitors = cfgRes.config.monitor;
            window.config = cfgRes.config;
          }
        } catch (_) {}
      }

      if (Array.isArray(monitors) && monitors.length > 0) {
        return monitors.map((m) => {
          const name = String(m.name || m.id || "").trim();
          const acc = String(m.account || "").toLowerCase();
          const grp = acc.includes("nogi") ? "nogizaka46" : acc.includes("sakura") ? "sakurazaka46" : acc.includes("hinata") ? "hinatazaka46" : "yodel";
          return { name, display: name, group: grp };
        }).filter((m) => Boolean(m.name));
      }

      return [];
    }

    function syncMessageMemberSelection() {
      if (!msgMemberList) return;
      const checkboxes = Array.from(msgMemberList.querySelectorAll('input[type="checkbox"]'));
      const checked = checkboxes.filter((cb) => cb.checked);
      checkboxes.forEach((cb) => {
        cb.closest(".bm-member-option")?.classList.toggle("selected", cb.checked);
      });
      if (msgSelectionSummary) {
        msgSelectionSummary.textContent = checked.length
          ? `已选择 ${checked.length} 位成员`
          : "未选择：全部监控成员";
      }
    }

    async function renderMessageMembers() {
      if (!msgMemberList) return;
      msgMemberList.innerHTML = '<p class="admin-note">正在加载监控成员列表...</p>';
      const members = await fetchBackfillMembers();
      if (!members.length) {
        msgMemberList.innerHTML = '<div class="bm-member-empty">暂无当前监控成员；请先在管理端配置 Message 监控成员。</div>';
        syncMessageMemberSelection();
        return;
      }
      msgMemberList.replaceChildren();
      members.forEach((m) => {
        const label = document.createElement("label");
        label.className = "bm-member-option";
        const input = document.createElement("input");
        input.type = "checkbox";
        input.name = "archiveMsgMember";
        input.value = m.name;
        input.setAttribute("aria-label", `选择 ${m.display || m.name}`);
        const name = document.createElement("span");
        name.className = "bm-member-option-name";
        name.textContent = m.display || m.name;
        const group = document.createElement("span");
        group.className = "bm-member-option-group";
        group.textContent = BACKFILL_GROUP_LABELS[m.group] || m.group || "成员";
        label.append(input, name, group);
        input.addEventListener("change", syncMessageMemberSelection);
        msgMemberList.append(label);
      });
      syncMessageMemberSelection();
    }

    if (btnLaunchMessageBackfill) {
      btnLaunchMessageBackfill.addEventListener("click", () => {
        void renderMessageMembers();
        if (msgDialogError) msgDialogError.hidden = true;
        if (msgDateRow) msgDateRow.hidden = true;
        const defaultRadio = dialogArchiveMessage.querySelector('input[name="archiveMsgMode"][value="incremental"]');
        if (defaultRadio) defaultRadio.checked = true;
        if (msgStartDate) msgStartDate.value = "";
        dialogArchiveMessage.showModal();
      });
    }

    if (msgSelectAll) {
      msgSelectAll.addEventListener("click", () => {
        msgMemberList.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
          cb.checked = true;
        });
        syncMessageMemberSelection();
      });
    }

    if (msgClear) {
      msgClear.addEventListener("click", () => {
        msgMemberList.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
          cb.checked = false;
        });
        syncMessageMemberSelection();
      });
    }

    dialogArchiveMessage.querySelectorAll('input[name="archiveMsgMode"]').forEach((radio) => {
      radio.addEventListener("change", () => {
        if (msgDateRow) msgDateRow.hidden = radio.value !== "custom";
      });
    });

    if (btnConfirmMsg) {
      btnConfirmMsg.addEventListener("click", async () => {
        if (msgDialogError) msgDialogError.hidden = true;
        const checkedBoxes = Array.from(msgMemberList.querySelectorAll('input[type="checkbox"]:checked'));
        const memberStr = checkedBoxes.map((cb) => cb.value).join(", ");
        const selectedMode = dialogArchiveMessage.querySelector('input[name="archiveMsgMode"]:checked')?.value || "incremental";
        const fromDate = selectedMode === "custom" ? (msgStartDate && msgStartDate.value ? msgStartDate.value.trim() : "") : "";

        if (selectedMode === "custom" && !fromDate) {
          if (msgDialogError) {
            msgDialogError.textContent = "请选择起始日期。";
            msgDialogError.hidden = false;
          }
          return;
        }

        btnConfirmMsg.disabled = true;
        try {
          const res = await api("/api/archive/messages/backfill", {
            method: "POST",
            body: JSON.stringify({
              member: memberStr,
              reset: selectedMode === "all",
              from_date: fromDate,
            }),
          });
          if (res && res.ok === false) {
            throw new Error(res.msg || "发起消息补档失败");
          }
          dialogArchiveMessage.close();
          setNotice("已成功启动消息补档任务，正在同步进度…");
          void refreshTaskList({ manual: true });
        } catch (err) {
          if (msgDialogError) {
            msgDialogError.textContent = err.message || "请求失败，请稍后重试。";
            msgDialogError.hidden = false;
          }
        } finally {
          btnConfirmMsg.disabled = false;
        }
      });
    }

    // 2. 博客回填弹窗
    const chkNogi = document.getElementById("chkArchiveBlogNogi");
    const chkSakura = document.getElementById("chkArchiveBlogSakura");
    const chkHinata = document.getElementById("chkArchiveBlogHinata");
    const btnSelectAllBlog = document.getElementById("btnArchiveBlogSelectAll");
    const blogUrlInput = document.getElementById("archiveBlogMemberUrl");
    const blogDialogError = document.getElementById("archiveBlogDialogError");
    const btnConfirmBlog = document.getElementById("btnConfirmArchiveBlog");

    function syncBlogConfirmState() {
      const hasGroup = [chkNogi, chkSakura, chkHinata].some((cb) => cb && cb.checked);
      const hasUrl = Boolean(blogUrlInput && blogUrlInput.value.trim());
      if (btnConfirmBlog) btnConfirmBlog.disabled = !hasGroup && !hasUrl;
      [chkNogi, chkSakura, chkHinata].forEach((cb) => {
        if (cb) cb.closest(".archive-group-card")?.classList.toggle("selected", cb.checked);
      });
    }

    if (btnLaunchBlogBackfill) {
      btnLaunchBlogBackfill.addEventListener("click", () => {
        if (blogDialogError) blogDialogError.hidden = true;
        if (blogUrlInput) blogUrlInput.value = "";
        [chkNogi, chkSakura, chkHinata].forEach((cb) => {
          if (cb) cb.checked = false;
        });
        syncBlogConfirmState();
        dialogArchiveBlog.showModal();
      });
    }

    [chkNogi, chkSakura, chkHinata].forEach((cb) => {
      if (cb) cb.addEventListener("change", syncBlogConfirmState);
    });
    if (blogUrlInput) blogUrlInput.addEventListener("input", syncBlogConfirmState);

    if (btnSelectAllBlog) {
      btnSelectAllBlog.addEventListener("click", () => {
        const allChecked = [chkNogi, chkSakura, chkHinata].every((cb) => cb && cb.checked);
        [chkNogi, chkSakura, chkHinata].forEach((cb) => {
          if (cb) cb.checked = !allChecked;
        });
        syncBlogConfirmState();
      });
    }

    if (btnConfirmBlog) {
      btnConfirmBlog.addEventListener("click", async () => {
        if (blogDialogError) blogDialogError.hidden = true;
        const memberUrl = blogUrlInput && blogUrlInput.value ? blogUrlInput.value.trim() : "";
        const selectedGroups = [
          chkNogi && chkNogi.checked ? "nogizaka" : null,
          chkSakura && chkSakura.checked ? "sakurazaka" : null,
          chkHinata && chkHinata.checked ? "hinatazaka" : null,
        ].filter(Boolean);

        btnConfirmBlog.disabled = true;
        try {
          let res;
          if (memberUrl) {
            res = await api("/api/archive/blogs/archive_member", {
              method: "POST",
              body: JSON.stringify({ url: memberUrl }),
            });
          } else {
            res = await api("/api/archive/blogs/archive_groups", {
              method: "POST",
              body: JSON.stringify({ groups: selectedGroups }),
            });
          }
          if (res && res.ok === false) {
            throw new Error(res.msg || "发起博客归档失败");
          }
          dialogArchiveBlog.close();
          setNotice("已成功启动后台博客归档任务，正在同步进度…");
          void refreshTaskList({ manual: true });
        } catch (err) {
          if (blogDialogError) {
            blogDialogError.textContent = err.message || "请求失败，请稍后重试。";
            blogDialogError.hidden = false;
          }
        } finally {
          btnConfirmBlog.disabled = false;
        }
      });
    }

    [dialogArchiveMessage, dialogArchiveBlog].forEach((dlg) => {
      dlg.querySelectorAll("[data-dialog-close]").forEach((btn) => {
        btn.addEventListener("click", () => dlg.close());
      });
      dlg.addEventListener("click", (e) => {
        if (e.target === dlg) dlg.close();
      });
    });
  }

  if (window.webAdminRole === "pending") {
    window.addEventListener("webadminauthready", initializeForRole, { once: true });
  } else {
    initializeForRole();
  }
})();
