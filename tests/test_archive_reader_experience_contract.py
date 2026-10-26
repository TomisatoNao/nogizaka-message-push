from pathlib import Path
import json
import shutil
import subprocess
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

import pytest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "webui_static"


class _ArchiveReaderStaticHandler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        relative = urlsplit(path).path
        if relative == "/":
            relative = "/archive.html"
        elif relative.startswith("/static/"):
            relative = relative[len("/static"):]
        return str(STATIC / unquote(relative.lstrip("/")))

    def log_message(self, *_args):
        return


@pytest.fixture(scope="module")
def archive_reader_static_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ArchiveReaderStaticHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_archive_home_cache_is_user_scoped_and_removed_on_logout():
    script = (STATIC / "archive.js").read_text(encoding="utf-8")
    html = (STATIC / "archive.html").read_text(encoding="utf-8")

    assert 'key: "user:" + me.user.username' in script
    assert 'ARCHIVE_HOME_CACHE_LEGACY_KEY + ":v" + ARCHIVE_HOME_CACHE_VERSION + ":" + encodeURIComponent(identity.key)' in script
    assert 'parsed.scope === archiveCacheIdentity.key' in script
    assert "clearArchiveHomeCache(null);" in script
    assert 'archiveStorageGet("localStorage", ARCHIVE_HOME_CACHE_LEGACY_KEY)' not in script
    assert "await initAuth();" in script.split("async function showHome()", 1)[1]
    assert 'id="homeCacheNotice" role="status" aria-live="polite"' in html
    assert "当前显示的是本浏览器保存的归档首页缓存，正在更新。" in script

    begin_logout = script.split("function beginArchiveLogout(logoutButton)", 1)[1].split(
        "async function requestArchiveLogout", 1
    )[0]
    assert "clearArchiveBrowserStateForLogout(identity);" in begin_logout
    request_logout = script.split("async function requestArchiveLogout(logoutButton)", 1)[1].split(
        'const logoutBtn = $("logoutBtn");', 1
    )[0]
    assert 'fetch("/api/auth/logout", { method: "POST", cache: "no-store" })' in request_logout
    assert request_logout.index("beginArchiveLogout(logoutButton);") < request_logout.index(
        'await fetch("/api/auth/logout"'
    )
    assert request_logout.index('if (response && response.ok)') < request_logout.index('location.href = "/login";')
    assert 'archiveStorageRemove("localStorage", scopedKey);' in script
    assert 'archiveStorageRemove("sessionStorage", scopedKey);' in script
    assert 'archiveStorageRemove("localStorage", ARCHIVE_HOME_CACHE_LEGACY_KEY);' in script
    assert "localStorage.clear()" not in script


def test_archive_scroll_restore_requires_loaded_route_and_clears_on_route_change():
    script = (STATIC / "archive.js").read_text(encoding="utf-8")

    assert "function markArchiveReadingContentLoaded" in script
    assert "if (!activeReadingRouteKey || !activeReadingRouteLoaded || !archiveCacheIdentity) return;" in script
    assert "ARCHIVE_READING_SCROLL_MAX_AGE_MS" in script
    assert 'window.addEventListener("pagehide", saveCompletedArchiveReadingScroll);' in script
    assert "const nextFrame = window.requestAnimationFrame || ((callback) => setTimeout(callback, 16));" in script
    assert "try { window.scrollTo(0, pos); } catch (_) {}" in script

    message_load = script.split("async function loadPage()", 1)[1].split("function startSearch", 1)[0]
    assert message_load.index('if (!data.ok) { showEmpty') < message_load.index("markArchiveReadingContentLoaded();")
    assert "requestPage === 1 && !targetMsgId" in message_load
    assert "{ restore: false }" in message_load
    assert "beginArchiveReadingRouteLoad();" in script.split("async function selectMember(name, keepHash)", 1)[1].split("function readHashYM", 1)[0]
    assert "beginArchiveReadingRouteLoad();" in script.split("async function selectBlogGroup(key, author", 1)[1].split("async function loadBlogAuthors", 1)[0]
    assert "blogReaderSavedScrollReady ? blogReaderSavedScroll : null" in script
    assert "prepareArchiveReadingRouteChange();" in script


def test_archive_auth_identity_is_revalidated_on_resume_and_invalidated_during_check():
    script = (STATIC / "archive.js").read_text(encoding="utf-8")

    assert "async function initAuth({ refresh = false } = {})" in script
    assert "if (refresh && _authRefreshInFlight && _authInitPromise) return _authInitPromise;" in script
    assert "let lastConfirmedIdentity = null;" in script
    assert "if (archiveCacheIdentity) lastConfirmedIdentity = archiveCacheIdentity;" in script
    assert "hideArchiveHomeWhileIdentityIsUnconfirmed();" in script
    assert "_homeRequestVersion += 1;" in script
    assert 'if (portalContent) portalContent.style.display = "none";' in script
    assert "const previousIdentity = refresh ? (archiveCacheIdentity || lastConfirmedIdentity) : null;" in script
    assert "previousIdentity.key !== archiveCacheIdentity.key" in script
    assert "clearArchiveHomeCache(previousIdentity);" in script
    assert "clearArchiveReadingScroll(previousIdentity);" in script
    assert "function clearArchiveBrowserStateForLogout(identity = archiveCacheIdentity || lastConfirmedIdentity)" in script
    assert "archiveCacheIdentity = null;\n  lastConfirmedIdentity = null;" in script
    assert 'window.addEventListener("pageshow", (e) =>' in script
    assert 'if (e.persisted) revalidateArchiveIdentity({ reboot: true });' in script
    assert 'window.addEventListener("focus", () =>' in script
    assert 'if (!document.hidden) revalidateArchiveIdentity();' in script
    assert 'document.addEventListener("visibilitychange", () =>' in script
    assert 'window.addEventListener("storage", (e) =>' in script
    assert 'if (e.key === "webAdminToken") revalidateArchiveIdentity();' in script

    auth = script.split("async function initAuth({ refresh = false } = {})", 1)[1].split(
        "initAuth();", 1
    )[0]
    assert auth.index("if (!isArchiveAuthRequestCurrent(requestVersion)) return;") < auth.index(
        "setArchiveCacheIdentity(me);"
    )
    assert auth.count("if (!isArchiveAuthRequestCurrent(requestVersion)) return;") == 3
    assert "if (isArchiveAuthRequestCurrent(requestVersion)) setArchiveCacheIdentity(null);" in auth

    begin_logout = script.split("function beginArchiveLogout(logoutButton)", 1)[1].split(
        "async function requestArchiveLogout", 1
    )[0]
    assert "const identity = archiveCacheIdentity || lastConfirmedIdentity;" in begin_logout
    assert begin_logout.index("invalidateArchiveAuthRequestsForLogout();") < begin_logout.index(
        "clearArchiveBrowserStateForLogout(identity);"
    )
    assert begin_logout.index("clearArchiveBrowserStateForLogout(identity);") < begin_logout.index(
        'logoutButton.textContent = "⏳ 正在退出…";'
    )

    logout_request = script.split("async function requestArchiveLogout(logoutButton)", 1)[1].split(
        'const logoutBtn = $("logoutBtn");', 1
    )[0]
    assert logout_request.index("beginArchiveLogout(logoutButton);") < logout_request.index(
        'await fetch("/api/auth/logout", { method: "POST", cache: "no-store" })'
    )
    assert 'logoutButton.textContent = "⚠️ 服务器退出未确认，可重试";' in logout_request
    assert 'if (response && response.ok)' in logout_request
    assert 'location.href = "/login";' in logout_request
    assert 'requestArchiveLogout(logoutBtn);' in script


def test_logout_invalidates_an_in_flight_archive_auth_refresh():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable for the auth refresh race simulation")

    source = (STATIC / "archive.js").read_text(encoding="utf-8")
    start = source.index("let _authInitPromise = null;")
    end = source.index("async function initAuth({ refresh = false } = {})", start)
    helpers = source[start:end]
    program = f"""
const vm = require("node:vm");
const helpers = {json.dumps(helpers)};
const context = vm.createContext({{
  archiveCacheIdentity: {{ key: "user:alice" }},
  lastConfirmedIdentity: {{ key: "user:alice" }},
  _portalHomeCached: {{ private: true }},
  _portalHomeCacheScopeKey: "alice-cache",
  _homeRequestVersion: 4,
  curMode: "messages",
  $: () => null,
  setArchiveHomeCacheNotice: () => {{}}
}});
vm.runInContext(helpers, context);
const result = vm.runInContext(`
  const staleRequest = ++_authRequestVersion;
  _authInitPromise = Promise.resolve("pending refresh");
  _authRefreshInFlight = true;
  invalidateArchiveAuthRequestsForLogout();
  JSON.stringify({{
    requestCurrent: isArchiveAuthRequestCurrent(staleRequest),
    invalidated: _archiveAuthInvalidatedForLogout,
    promiseCleared: _authInitPromise === null,
    refreshCleared: _authRefreshInFlight === false,
    versionAdvanced: _authRequestVersion > staleRequest,
    identityHidden: archiveCacheIdentity === null,
    homeRequestInvalidated: _homeRequestVersion === 5,
    confirmedScopeKept: lastConfirmedIdentity.key
  }})
`, context);
process.stdout.write(result);
"""
    completed = subprocess.run([node, "-e", program], capture_output=True, text=True, check=True)
    assert json.loads(completed.stdout) == {
        "requestCurrent": False,
        "invalidated": True,
        "promiseCleared": True,
        "refreshCleared": True,
        "versionAdvanced": True,
        "identityHidden": True,
        "homeRequestInvalidated": True,
        "confirmedScopeKept": "user:alice",
    }


def test_logout_clears_scoped_state_before_pending_logout_fetch_resolves():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable for the pending logout simulation")

    source = (STATIC / "archive.js").read_text(encoding="utf-8")
    clear_start = source.index("function clearArchiveBrowserStateForLogout(")
    clear_end = source.index("// 旧版缓存没有身份范围", clear_start)
    clear_state = source[clear_start:clear_end]
    auth_start = source.index("let _authInitPromise = null;")
    auth_end = source.index("async function initAuth({ refresh = false } = {})", auth_start)
    auth_helpers = source[auth_start:auth_end]
    logout_start = source.index("function beginArchiveLogout(logoutButton)")
    logout_end = source.index('const logoutBtn = $("logoutBtn");', logout_start)
    logout_helpers = source[logout_start:logout_end]
    program = f"""
const vm = require("node:vm");
const clearState = {json.dumps(clear_state)};
const authHelpers = {json.dumps(auth_helpers)};
const logoutHelpers = {json.dumps(logout_helpers)};
const elements = {{
  portalContent: {{ style: {{ display: "" }} }},
  homeSkeleton: {{ classList: {{ add() {{}} }} }},
  portalHero: {{ textContent: "private hero" }},
  photoStrip: {{ textContent: "private photos" }},
  portalSections: {{ textContent: "private sections" }},
  homeFeedList: {{ textContent: "private feed" }},
  homeCacheNotice: {{ hidden: false, textContent: "cached" }}
}};
const homeCache = new Set(["user:alice"]);
const readingCache = new Set(["user:alice"]);
const readingScrollMemory = new Map([["user:alice\\u001froute", 90]]);
const context = vm.createContext({{
  archiveCacheIdentity: {{ key: "user:alice" }},
  lastConfirmedIdentity: {{ key: "user:alice" }},
  _portalHomeCached: {{ private: true }},
  _portalHomeCacheScopeKey: "alice-cache",
  blogReaderSavedScroll: 90,
  blogReaderSavedScrollReady: true,
  blogListLoadedRouteKey: "blog:alice",
  _homePhotoCleanup: () => {{}},
  _homeRequestVersion: 4,
  curMode: "home",
  homeCache,
  readingCache,
  readingScrollMemory,
  activeReadingRouteKey: "route",
  activeReadingRouteLoaded: true,
  readingScrollSaveTimer: null,
  clearArchiveHomeCache: (identity) => {{ if (identity) homeCache.delete(identity.key); }},
  clearArchiveReadingScroll: (identity) => {{
    if (identity) {{
      readingCache.delete(identity.key);
      for (const key of readingScrollMemory.keys()) if (key.startsWith(identity.key + "\\u001f")) readingScrollMemory.delete(key);
    }}
  }},
  archiveStorageRemove: () => {{}},
  setArchiveHomeCacheNotice: () => {{}},
  $: (id) => elements[id] || null,
  elements,
  fetchCalls: [],
  logoutButton: {{
    disabled: false,
    textContent: "退出登录",
    attributes: {{}},
    setAttribute(name, value) {{ this.attributes[name] = value; }},
    removeAttribute(name) {{ delete this.attributes[name]; }}
  }},
  location: {{ href: "" }}
}});
vm.runInContext(clearState, context);
vm.runInContext(authHelpers, context);
vm.runInContext(logoutHelpers, context);
const result = vm.runInContext(`
  const staleRequest = ++_authRequestVersion;
  _authInitPromise = Promise.resolve("pending auth refresh");
  _authRefreshInFlight = true;
  fetch = (path, options) => {{
    fetchCalls.push({{
      path,
      method: options.method,
      homeClearedBeforeFetch: !homeCache.has("user:alice"),
      readingClearedBeforeFetch: !readingCache.has("user:alice"),
      requestInvalidatedBeforeFetch: !isArchiveAuthRequestCurrent(staleRequest)
    }});
    return new Promise(() => {{}});
  }};
  requestArchiveLogout(logoutButton);
  JSON.stringify({{
    fetchCall: fetchCalls[0],
    authInvalidated: _archiveAuthInvalidatedForLogout,
    authPromiseCleared: _authInitPromise === null,
    homeMemoryCleared: _portalHomeCached === null,
    identityCleared: archiveCacheIdentity === null && lastConfirmedIdentity === null,
    readerMemoryCleared: blogReaderSavedScroll === 0 && blogReaderSavedScrollReady === false,
    scrollMemoryCleared: readingScrollMemory.size === 0,
    homeHidden: elements.portalContent.style.display === "none",
    renderedHomeCleared: elements.portalHero.textContent === "" && elements.homeFeedList.textContent === "",
    buttonPending: logoutButton.disabled && logoutButton.attributes["aria-busy"] === "true" && logoutButton.textContent === "⏳ 正在退出…",
    stillOnPage: location.href === ""
  }})
`, context);
process.stdout.write(result);
"""
    completed = subprocess.run([node, "-e", program], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["fetchCall"] == {
        "path": "/api/auth/logout",
        "method": "POST",
        "homeClearedBeforeFetch": True,
        "readingClearedBeforeFetch": True,
        "requestInvalidatedBeforeFetch": True,
    }
    assert result["authInvalidated"] is True
    assert result["authPromiseCleared"] is True
    assert result["homeMemoryCleared"] is True
    assert result["identityCleared"] is True
    assert result["readerMemoryCleared"] is True
    assert result["scrollMemoryCleared"] is True
    assert result["homeHidden"] is True
    assert result["renderedHomeCleared"] is True
    assert result["buttonPending"] is True
    assert result["stillOnPage"] is True


def test_failed_logout_revalidates_identity_without_reusing_previous_user_scope():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable for the failed logout revalidation simulation")

    source = (STATIC / "archive.js").read_text(encoding="utf-8")
    auth_start = source.index("let _authInitPromise = null;")
    auth_end = source.index("\ninitAuth();", auth_start)
    auth_code = source[auth_start:auth_end]
    identity_start = source.index("function setArchiveCacheIdentity(me)")
    identity_end = source.index("\nfunction archiveHomeCacheKey", identity_start)
    identity_code = source[identity_start:identity_end]
    logout_start = source.index("function beginArchiveLogout(logoutButton)")
    logout_end = source.index('const logoutBtn = $("logoutBtn");', logout_start)
    logout_code = source[logout_start:logout_end]
    revalidate_start = source.index("async function revalidateArchiveIdentity(")
    revalidate_end = source.index("\n// 从 bfcache 恢复", revalidate_start)
    revalidate_code = source[revalidate_start:revalidate_end]

    program = f"""
const vm = require("node:vm");
const authCode = {json.dumps(auth_code)};
const identityCode = {json.dumps(identity_code)};
const logoutCode = {json.dumps(logout_code)};
const revalidateCode = {json.dumps(revalidate_code)};

async function runScenario(mode) {{
  const makeElement = () => ({{ hidden: false, style: {{}}, textContent: "", href: "", title: "", innerHTML: "" }});
  const logoutButton = {{
    disabled: false,
    hidden: false,
    textContent: "退出登录",
    style: {{}},
    attributes: {{}},
    setAttribute(name, value) {{ this.attributes[name] = value; }},
    removeAttribute(name) {{ delete this.attributes[name]; }}
  }};
  const elements = {{
    portalContent: makeElement(),
    homeSkeleton: {{ classList: {{ add() {{}} }} }},
    adminLink: makeElement(),
    interactionChips: makeElement(),
    whoami: makeElement(),
    userMenuName: makeElement(),
    userMenuRole: makeElement(),
    userDropdown: makeElement(),
    changePwBtn: makeElement(),
    logoutBtn: logoutButton
  }};
  const homeCache = new Set(["user:alice", "user:bob"]);
  const homeCacheReadScopes = [];
  const routeReloadScopes = [];
  const clearedScopes = [];
  const fetchCalls = [];
  const context = vm.createContext({{
    mode,
    window: {{ _isLoggedIn: true, _canDownloadBlogCard: false }},
    archiveCacheIdentity: {{ key: "user:alice", label: "此账号" }},
    lastConfirmedIdentity: {{ key: "user:alice", label: "此账号" }},
    _portalHomeCached: {{ private: true }},
    _portalHomeCacheScopeKey: "alice-cache",
    _homeRequestVersion: 4,
    curMode: "home",
    homeCache,
    homeCacheReadScopes,
    routeReloadScopes,
    clearedScopes,
    fetchCalls,
    logoutButton,
    elements,
    location: {{ href: "" }},
    $: (id) => elements[id] || null,
    setArchiveHomeCacheNotice: () => {{}},
    clearArchiveHomeCache: (identity) => {{
      if (identity) {{ clearedScopes.push(identity.key); homeCache.delete(identity.key); }}
    }},
    clearArchiveReadingScroll: () => {{}},
    archiveStorageRemove: () => {{}},
    updateBlogCardDownloadVisibility: () => {{}},
    _updateAdminUI: () => {{}}
  }});
  vm.runInContext(`
    clearArchiveBrowserStateForLogout = (identity) => {{
      if (identity) {{ clearedScopes.push(identity.key); homeCache.delete(identity.key); }}
      archiveCacheIdentity = null;
      lastConfirmedIdentity = null;
      _portalHomeCached = null;
      _portalHomeCacheScopeKey = "";
    }};
    showHome = async () => {{
      if (!archiveCacheIdentity) throw new Error("home cache read started before identity confirmation");
      homeCacheReadScopes.push(archiveCacheIdentity.key);
    }};
    boot = async () => {{
      if (!archiveCacheIdentity) throw new Error("route reload started before identity confirmation");
      routeReloadScopes.push(archiveCacheIdentity.key);
    }};
  `, context);
  vm.runInContext(identityCode, context);
  vm.runInContext(authCode, context);
  vm.runInContext(logoutCode, context);
  vm.runInContext(revalidateCode, context);

  const serialized = await vm.runInContext(`
    (async () => {{
      fetch = async (path, options) => {{
        fetchCalls.push({{ path, method: options && options.method, identityBeforeRequest: archiveCacheIdentity && archiveCacheIdentity.key }});
        if (path === "/api/auth/logout") {{
          if (mode === "network") throw new Error("network failure");
          return {{ ok: false, status: 503 }};
        }}
        return {{ json: async () => ({{ auth_enabled: true, user: {{ username: "bob", role: "user" }} }}) }};
      }};
      await requestArchiveLogout(logoutButton);
      const firstRevalidation = {{
        invalidated: _archiveAuthInvalidatedForLogout,
        confirmedIdentity: archiveCacheIdentity && archiveCacheIdentity.key,
        requestCurrent: isArchiveAuthRequestCurrent(_authRequestVersion),
        buttonEnabled: !logoutButton.disabled,
        busyRemoved: !Object.prototype.hasOwnProperty.call(logoutButton.attributes, "aria-busy"),
        serverUnconfirmedStatus: logoutButton.textContent === "⚠️ 服务器退出未确认，可重试",
        location: location.href,
        cacheReadScopes: [...homeCacheReadScopes],
        routeReloadScopes: [...routeReloadScopes],
        clearedScopes: [...clearedScopes],
        cacheScopes: [...homeCache]
      }};
      await revalidateArchiveIdentity();
      return JSON.stringify({{
        firstRevalidation,
        afterLaterRevalidation: archiveCacheIdentity && archiveCacheIdentity.key,
        authMeCalls: fetchCalls.filter((call) => call.path === "/api/auth/me").length,
        cacheReadScopes: homeCacheReadScopes,
        routeReloadScopes,
        fetchCalls
      }});
    }})()
  `, context);
  return JSON.parse(serialized);
}}

(async () => {{
  const results = [];
  for (const mode of ["http", "network"]) results.push(await runScenario(mode));
  process.stdout.write(JSON.stringify(results));
}})().catch((error) => {{
  process.stderr.write(error.stack || String(error));
  process.exitCode = 1;
}});
"""
    completed = subprocess.run([node, "-e", program], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    results = json.loads(completed.stdout)
    assert len(results) == 2
    for result in results:
        failed = result["firstRevalidation"]
        assert failed["invalidated"] is False
        assert failed["confirmedIdentity"] == "user:bob"
        assert failed["requestCurrent"] is True
        assert failed["buttonEnabled"] is True
        assert failed["busyRemoved"] is True
        assert failed["serverUnconfirmedStatus"] is True
        assert failed["location"] == ""
        assert failed["cacheReadScopes"] == []
        assert failed["routeReloadScopes"] == ["user:bob"]
        assert failed["clearedScopes"] == ["user:alice"]
        assert failed["cacheScopes"] == ["user:bob"]
        assert result["afterLaterRevalidation"] == "user:bob"
        assert result["authMeCalls"] == 2
        assert result["cacheReadScopes"] == ["user:bob"]
        assert result["routeReloadScopes"] == ["user:bob"]


def test_archive_storage_initializers_survive_unavailable_or_throwing_storage():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable for the storage fallback simulation")

    source = (STATIC / "archive.js").read_text(encoding="utf-8")
    helper = source[source.index("function archiveStorage(name)") : source.index("function setArchiveCacheIdentity")]
    initializers = "\n".join(
        line for line in source.splitlines()
        if line.startswith("let isFuriganaActive = ") or line.startswith("let showTags = ")
    )
    assert 'archiveStorageGet("localStorage", "archive_furigana")' in initializers
    assert 'archiveStorageGet("localStorage", "archiveShowTags")' in initializers
    assert "localStorage." not in source
    assert "sessionStorage." not in source
    assert "localStorage.getItem(\"archive_furigana\")" not in source
    assert "localStorage.getItem(\"archiveShowTags\")" not in source

    program = f"""
const vm = require("node:vm");
const helper = {json.dumps(helper)};
const initializers = {json.dumps(initializers)};
const cases = [{{}}, {{ get localStorage() {{ throw new Error("storage denied"); }} }}];
const values = cases.map((window) => {{
  const context = vm.createContext({{ window }});
  vm.runInContext(helper + "\\n" + initializers, context);
  return vm.runInContext("[isFuriganaActive, showTags]", context);
}});
process.stdout.write(JSON.stringify(values));
"""
    completed = subprocess.run([node, "-e", program], capture_output=True, text=True, check=True)
    assert json.loads(completed.stdout) == [[False, True], [False, True]]


def test_archive_home_cache_notice_and_reader_restore_across_viewports_and_themes(archive_reader_static_server):
    playwright = pytest.importorskip("playwright.sync_api")
    member = "Sample"
    route_key = 'msg:' + json.dumps([member, 2026, 9, "", False, "desc", ""], ensure_ascii=False, separators=(",", ":"))
    cache_key = "archive_portal_home_cache:v2:user%3Aalice"
    scroll_key = "archive_reading_scroll_v1:user%3Aalice"
    home_cache = {
        "version": 2,
        "scope": "user:alice",
        "ts": 1793000000000,
        "data": {
            "ok": True,
            "summary": {"total_messages": 12, "member_count": 1},
            "members": [],
            "blog_groups": [],
            "recent_pics": [],
            "recent_feed": [],
        },
    }
    api_requests = []

    def handle_api(route):
        path = urlsplit(route.request.url).path
        api_requests.append(path)
        if path == "/api/auth/me":
            payload = {"ok": True, "auth_enabled": True, "user": {"username": "alice", "role": "user"}}
        elif path == "/api/archive/home":
            payload = {"ok": True, "members": [], "blog_groups": []}
        elif path == "/api/archive/members":
            payload = {"ok": True, "members": [{
                "name": member, "display": member, "group": "nogizaka", "total": 20,
                "stats": {"total": 20, "months": 1},
            }], "monitor_members": []}
        elif path == "/api/archive/months":
            payload = {"ok": True, "months": [{"year": 2026, "month": 9, "count": 20}]}
        elif path == "/api/archive/calendar":
            payload = {"ok": True, "days": {"2026-09-20": 20}}
        elif path == "/api/archive/messages":
            messages = [{
                "id": f"m{index:03d}", "type": "text",
                "text": (f"Reader position restoration sample {index}. Additional text provides page height. " * 5),
                "translation": "", "published_at": "2026-09-20T12:00:00Z", "upload_at": "",
                "group": "nogizaka", "media_url": None, "tags": "", "custom_tags": "",
                "is_favorite": False, "is_liked": False,
            } for index in range(1, 21)]
            payload = {
                "ok": True, "member": member, "group": "nogizaka", "year": 2026, "month": 9,
                "total": len(messages), "page": 1, "total_pages": 1, "order": "desc", "messages": messages,
            }
        else:
            payload = {"ok": True, "members": [], "groups": [], "months": [], "days": {}}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload, ensure_ascii=False))

    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        for width, theme in ((1280, "light"), (390, "dark"), (1280, "dark"), (390, "light")):
            context = browser.new_context(
                viewport={"width": width, "height": 760}, service_workers="block",
            )
            context.route("**/api/**", handle_api)
            page = context.new_page()
            page_errors = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            seed = f"""(() => {{
              localStorage.setItem("sakamichiTheme", {json.dumps(theme)});
              const homeCache = {json.dumps(json.dumps(home_cache, ensure_ascii=False))};
              const parsedHomeCache = JSON.parse(homeCache);
              parsedHomeCache.ts = Date.now();
              localStorage.setItem({json.dumps(cache_key)}, JSON.stringify(parsedHomeCache));
              sessionStorage.setItem({json.dumps(scroll_key)}, JSON.stringify({{
                version: 1,
                entries: {{[{json.dumps(route_key)}]: {{y: 360, ts: Date.now()}}}}
              }}));
            }})();"""
            page.add_init_script(seed)
            page.goto(archive_reader_static_server + "/archive.html#home", wait_until="domcontentloaded")
            try:
                page.wait_for_function(
                    "document.querySelector('#homeCacheNotice') && !document.querySelector('#homeCacheNotice').hidden",
                    timeout=8000,
                )
            except Exception as error:
                state = page.evaluate("""() => ({
                  hash: location.hash,
                  mode: typeof curMode === "undefined" ? null : curMode,
                  identity: typeof archiveCacheIdentity === "undefined" ? null : archiveCacheIdentity,
                  readyState: document.readyState,
                  bootType: typeof boot,
                  noticeHidden: document.querySelector('#homeCacheNotice')?.hidden,
                  noticeText: document.querySelector('#homeCacheNotice')?.textContent,
                  portalDisplay: document.querySelector('#portalContent')?.style.display,
                })""")
                pytest.fail(f"home cache notice did not appear at {width}px/{theme}; state={state}; requests={api_requests}; page_errors={page_errors}; {error}")
            notice = page.evaluate("""() => {
              const element = document.querySelector('#homeCacheNotice');
              const rect = element.getBoundingClientRect();
              const style = getComputedStyle(element);
              return {
                text: element.textContent,
                background: style.backgroundColor,
                color: style.color,
                left: rect.left,
                right: rect.right,
                viewport: window.innerWidth,
              };
            }""")
            assert "当前仍显示本浏览器缓存" in notice["text"] or "正在更新" in notice["text"]
            assert notice["right"] <= notice["viewport"] + 1, f"cache notice overflowed at {width}px/{theme}: {notice}"
            assert notice["background"] not in ("rgba(0, 0, 0, 0)", "transparent")

            page.goto(
                archive_reader_static_server + "/archive.html#member=" + member + "&y=2026&m=9",
                wait_until="domcontentloaded",
            )
            page.wait_for_function("activeReadingRouteLoaded && Math.abs(window.scrollY - 360) <= 10", timeout=10000)
            assert page.evaluate("activeReadingRouteKey") == route_key
            context.close()
        browser.close()
