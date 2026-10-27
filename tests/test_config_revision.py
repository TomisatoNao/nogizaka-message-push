"""Optimistic concurrency and secret-safe config preview contracts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
import os
from pathlib import Path
from threading import Barrier, Event, Lock, Thread
import urllib.error
import urllib.request

import json5
import pytest

ROOT = Path(__file__).resolve().parent.parent

VALID_CONFIG = {
    "channels": {"napcat": False, "tg": False, "qq_official": False},
    "napcat_api": "http://127.0.0.1:3000/send_group_msg",
    "napcat_api_token": "CONFIG_SECRET_MUST_NOT_APPEAR_IN_PREVIEW",
    "napcat_media_signing_secret": "ANOTHER_SECRET_MUST_NOT_APPEAR",
    "web_admin": {"enabled": True, "host": "127.0.0.1", "port": 46046},
    "accounts": {"nogizaka_main": {"group": "nogizaka46"}},
    "monitor": [],
    "day_interval": [120, 180],
    "night_interval": [1500, 1800],
    "sleep_hours": [2, 7],
    "alert_cooldown": 3600,
    "translate": True,
    "gemini_min_interval": 7.0,
}


def _http(method: str, url: str, body: dict | None = None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


@pytest.fixture
def config_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    import src.config.config as cfg
    from src import webui
    from src.social import ig_session

    config_path = tmp_path / "config.json"
    env_path = tmp_path / ".env"
    config_path.write_text(webui.serialize_config(VALID_CONFIG), encoding="utf-8")
    env_path.write_text("KEEP_TOKEN='before'\n", encoding="utf-8")

    monkeypatch.setattr(webui, "CONFIG_PATH", config_path)
    monkeypatch.setattr(webui, "ENV_PATH", env_path)
    monkeypatch.setattr(ig_session, "DB_PATH", str(tmp_path / "social.db"))
    monkeypatch.setattr(ig_session, "_cleanup_legacy_files", lambda: None)
    monkeypatch.setattr(ig_session, "check_session", lambda *_args, **_kwargs: {"valid": None, "detail": "test"})
    monkeypatch.setattr(webui, "SCHEMA_PATH", ROOT / "config" / "config.schema.json")
    monkeypatch.setattr(cfg, "AUTH_ENABLED", False)
    monkeypatch.delenv("WEB_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("HINATA_SHARED_TOKEN", raising=False)
    monkeypatch.setattr(webui, "_trigger_reload_report", lambda: {
        "reloaded": True,
        "reload": {"config": "loaded", "components": "pending"},
    })
    monkeypatch.setattr(webui, "_trigger_reload", lambda: True)

    server = webui.start_webui(host="127.0.0.1", port=0)
    assert server is not None
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield webui, base_url, config_path, env_path
    finally:
        server.shutdown()
        server.server_close()


def _read_state(base_url: str) -> dict:
    status, response = _http("GET", base_url + "/api/config")
    assert status == 200 and response.get("revision")
    return response


def test_concurrent_writes_with_same_revision_only_commit_once(config_server):
    _webui, base_url, config_path, _env_path = config_server
    revision = _read_state(base_url)["revision"]
    barrier = Barrier(2)

    def save_with(value: int):
        body = json.loads(json.dumps(VALID_CONFIG))
        body["alert_cooldown"] = value
        body["expected_revision"] = revision
        barrier.wait()
        return _http("PUT", base_url + "/api/config", body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(save_with, (7000, 8000)))

    assert sorted(status for status, _ in outcomes) == [200, 409]
    success = next(response for status, response in outcomes if status == 200)
    conflict = next(response for status, response in outcomes if status == 409)
    assert success["saved"] is True
    assert conflict["error"] == "revision_conflict"
    assert conflict["revision"] == success["revision"]
    assert json5.loads(config_path.read_text(encoding="utf-8"))["alert_cooldown"] in {7000, 8000}
    assert len(list((config_path.parent / "history").glob("config-*.json"))) == 1


def test_concurrent_secret_writes_with_same_revision_only_commit_once(config_server, monkeypatch: pytest.MonkeyPatch):
    _webui, base_url, _config_path, env_path = config_server
    monkeypatch.delenv("HINATA_SHARED_TOKEN", raising=False)
    revision = _read_state(base_url)["revision"]
    barrier = Barrier(2)

    def write_secret(value: str):
        barrier.wait()
        return _http("POST", base_url + "/api/secrets", {
            "values": {"HINATA_SHARED_TOKEN": value},
            "expected_revision": revision,
        })

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(write_secret, ("SECRET_FIRST_PRIVATE", "SECRET_SECOND_PRIVATE")))

    assert sorted(status for status, _ in outcomes) == [200, 409]
    success = next(response for status, response in outcomes if status == 200)
    conflict = next(response for status, response in outcomes if status == 409)
    assert success["saved"] is True and success["revision"] != revision
    assert conflict["error"] == "revision_conflict"
    assert conflict["revision"] == success["revision"]
    assert "SECRET_FIRST_PRIVATE" not in json.dumps(conflict)
    assert "SECRET_SECOND_PRIVATE" not in json.dumps(conflict)
    content = env_path.read_text(encoding="utf-8")
    assert ("SECRET_FIRST_PRIVATE" in content) != ("SECRET_SECOND_PRIVATE" in content)

    before = env_path.read_bytes()
    status, missing = _http("POST", base_url + "/api/secrets", {
        "values": {"HINATA_SHARED_TOKEN": "SECRET_MISSING_REVISION"},
    })
    assert status == 428 and missing["error"] == "expected_revision_required"
    assert env_path.read_bytes() == before
    assert "SECRET_MISSING_REVISION" not in json.dumps(missing)


def test_concurrent_instagram_cookie_saves_with_same_revision_only_commit_once(
    config_server,
):
    _webui, base_url, _config_path, env_path = config_server
    from src.social import ig_session

    revision = _read_state(base_url)["revision"]
    barrier = Barrier(2)
    csrf_values = ("CSRF_FIRST_PRIVATE", "CSRF_SECOND_PRIVATE")

    def save_cookie(csrf: str):
        barrier.wait()
        return _http("POST", base_url + "/api/social/ig_session", {
            "cookies": f"sessionid=SHARED_SESSION_PRIVATE; csrftoken={csrf}",
            "expected_revision": revision,
        })

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(save_cookie, csrf_values))

    assert sorted(status for status, _ in outcomes) == [200, 409]
    success_index = next(index for index, (status, _) in enumerate(outcomes) if status == 200)
    success = outcomes[success_index][1]
    conflict = next(response for status, response in outcomes if status == 409)
    assert success["revision"] != revision
    assert conflict["error"] == "revision_conflict"
    assert conflict["revision"] == success["revision"]
    assert all(value not in json.dumps(conflict) for value in csrf_values)
    assert ig_session.read_cookie_file() == {
        "sessionid": "SHARED_SESSION_PRIVATE",
        "csrftoken": csrf_values[success_index],
    }
    assert "INSTAGRAM_SESSIONID='SHARED_SESSION_PRIVATE'" in env_path.read_text(encoding="utf-8")
    assert _read_state(base_url)["revision"] == success["revision"]


def test_instagram_clear_rejects_stale_revision_and_returns_new_revision(config_server):
    _webui, base_url, _config_path, env_path = config_server
    from src.social import ig_session

    initial_revision = _read_state(base_url)["revision"]
    status, saved = _http("POST", base_url + "/api/social/ig_session", {
        "cookies": "sessionid=CLEAR_TARGET_PRIVATE; csrftoken=CSRF_TARGET_PRIVATE",
        "expected_revision": initial_revision,
    })
    assert status == 200 and saved["revision"] != initial_revision
    before_env = env_path.read_bytes()
    before_cookies = ig_session.read_cookie_file()

    stale_status, stale = _http("POST", base_url + "/api/social/ig_session/clear", {
        "expected_revision": initial_revision,
    })
    assert stale_status == 409 and stale["error"] == "revision_conflict"
    assert stale["revision"] == saved["revision"]
    assert env_path.read_bytes() == before_env
    assert ig_session.read_cookie_file() == before_cookies

    clear_status, cleared = _http("POST", base_url + "/api/social/ig_session/clear", {
        "expected_revision": saved["revision"],
    })
    assert clear_status == 200 and cleared["ok"] is True
    assert cleared["revision"] != saved["revision"]
    assert ig_session.read_cookie_file() == {}
    assert "INSTAGRAM_SESSIONID" not in env_path.read_text(encoding="utf-8")
    assert _read_state(base_url)["revision"] == cleared["revision"]


class _DirectJsonHandler:
    def __init__(self):
        self.wfile = BytesIO()
        self.status = None

    def send_response(self, status):
        self.status = status

    def send_header(self, _name, _value):
        pass

    def end_headers(self):
        pass

    def response_json(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _setup_ig_cookie_transaction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src import webui
    from src.social import ig_session
    from src.webui_modules import config_service, social_handlers

    env_path = tmp_path / ".env"
    env_path.write_bytes(
        b"# keep exact bytes\r\n"
        b"KEEP_TOKEN='before'\r\n"
        b"INSTAGRAM_SESSIONID='OLD_SESSION_PRIVATE'\r\n"
        b"INSTAGRAM_DS_USER_ID='12345'\r\n"
    )
    monkeypatch.setattr(webui, "ENV_PATH", env_path)
    monkeypatch.setattr(ig_session, "DB_PATH", str(tmp_path / "social.db"))
    monkeypatch.setattr(ig_session, "_cleanup_legacy_files", lambda: None)

    old_cookies = {"sessionid": "OLD_SESSION_PRIVATE", "csrftoken": "OLD_CSRF_PRIVATE"}
    ig_session.write_cookie_file(old_cookies)
    old_state = ig_session.save_state(valid=True, reason="old-session")
    monkeypatch.setenv("INSTAGRAM_SESSIONID", old_cookies["sessionid"])
    monkeypatch.setenv("INSTAGRAM_DS_USER_ID", "12345")

    def update_env_file(values, **kwargs):
        path = kwargs.pop("path", env_path)
        return config_service.update_env_file(values, path=path, **kwargs)

    return {
        "env_path": env_path,
        "before_env": env_path.read_bytes(),
        "old_cookies": old_cookies,
        "old_state": old_state,
        "ig_session": ig_session,
        "social_handlers": social_handlers,
        "config_service": config_service,
        "update_env_file": update_env_file,
    }


def _invoke_ig_cookie_handler(
    handler_fn, *, update_env_file, expected_revision="test-revision", body=None, env_path=None,
):
    handler = _DirectJsonHandler()
    common = {
        "update_env_file": update_env_file,
        "trigger_reload": lambda: True,
        "mutation_lock": Lock(),
        "expected_revision": expected_revision,
        "check_revision_locked": lambda _handler, revision: revision,
        "get_revision": lambda: "updated-revision",
    }
    if handler_fn.__name__ == "handle_ig_session_save":
        common["load_raw_config"] = lambda: {}
        handler_fn(handler, body or {}, **common)
    else:
        handler_fn(handler, env_path=env_path, **common)
    return handler.status, handler.response_json()


def test_instagram_cookie_save_env_replace_failure_keeps_cookie_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    state = _setup_ig_cookie_transaction(tmp_path, monkeypatch)
    social_handlers = state["social_handlers"]
    config_service = state["config_service"]
    logs: list[str] = []
    monkeypatch.setattr(social_handlers, "log_all", lambda message, **_kwargs: logs.append(message))
    real_replace = config_service.os.replace
    new_session = "NEW_SESSION_PRIVATE_MARKER"

    def fail_env_replace(source, destination):
        if Path(destination) == state["env_path"]:
            raise OSError(f"simulated .env failure {new_session}")
        return real_replace(source, destination)

    monkeypatch.setattr(config_service.os, "replace", fail_env_replace)
    status, response = _invoke_ig_cookie_handler(
        social_handlers.handle_ig_session_save,
        update_env_file=state["update_env_file"],
        body={"cookies": f"sessionid={new_session}; csrftoken=NEW_CSRF_PRIVATE"},
    )

    assert status == 500
    assert state["ig_session"].read_cookie_file() == state["old_cookies"]
    assert state["ig_session"].load_state() == state["old_state"]
    assert state["env_path"].read_bytes() == state["before_env"]
    assert os.environ["INSTAGRAM_SESSIONID"] == state["old_cookies"]["sessionid"]
    assert os.environ["INSTAGRAM_DS_USER_ID"] == "12345"
    rendered_error = json.dumps(response) + "".join(logs)
    assert new_session not in rendered_error
    assert "NEW_CSRF_PRIVATE" not in rendered_error


def test_instagram_cookie_save_storage_failure_restores_env_and_process_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    state = _setup_ig_cookie_transaction(tmp_path, monkeypatch)
    ig_session = state["ig_session"]
    social_handlers = state["social_handlers"]
    logs: list[str] = []
    monkeypatch.setattr(social_handlers, "log_all", lambda message, **_kwargs: logs.append(message))
    new_session = "NEW_SESSION_PRIVATE_MARKER"

    def fail_cookie_write(_cookies):
        assert f"INSTAGRAM_SESSIONID='{new_session}'" in state["env_path"].read_text(encoding="utf-8")
        assert os.environ["INSTAGRAM_SESSIONID"] == new_session
        raise OSError(f"simulated Cookie store failure {new_session}")

    monkeypatch.setattr(ig_session, "write_cookie_file", fail_cookie_write)
    status, response = _invoke_ig_cookie_handler(
        social_handlers.handle_ig_session_save,
        body={"cookies": f"sessionid={new_session}; csrftoken=NEW_CSRF_PRIVATE"},
        update_env_file=state["update_env_file"],
    )

    assert status == 500
    assert ig_session.read_cookie_file() == state["old_cookies"]
    assert ig_session.load_state() == state["old_state"]
    assert state["env_path"].read_bytes() == state["before_env"]
    assert os.environ["INSTAGRAM_SESSIONID"] == state["old_cookies"]["sessionid"]
    assert os.environ["INSTAGRAM_DS_USER_ID"] == "12345"
    rendered_error = json.dumps(response) + "".join(logs)
    assert new_session not in rendered_error
    assert "NEW_CSRF_PRIVATE" not in rendered_error


def test_instagram_cookie_save_file_rollback_failure_still_restores_process_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    state = _setup_ig_cookie_transaction(tmp_path, monkeypatch)
    ig_session = state["ig_session"]
    social_handlers = state["social_handlers"]
    config_service = state["config_service"]
    logs: list[str] = []
    monkeypatch.setattr(social_handlers, "log_all", lambda message, **_kwargs: logs.append(message))
    new_session = "NEW_SESSION_PRIVATE_MARKER"

    def fail_cookie_write(_cookies):
        assert os.environ["INSTAGRAM_SESSIONID"] == new_session
        raise OSError(f"simulated Cookie store failure {new_session}")

    def fail_file_restore(_path, _before):
        raise OSError(f"simulated file rollback failure {new_session} OLD_SESSION_PRIVATE")

    monkeypatch.setattr(ig_session, "write_cookie_file", fail_cookie_write)
    monkeypatch.setattr(config_service, "_restore_file_bytes", fail_file_restore)
    status, response = _invoke_ig_cookie_handler(
        social_handlers.handle_ig_session_save,
        body={"cookies": f"sessionid={new_session}; csrftoken=NEW_CSRF_PRIVATE"},
        update_env_file=state["update_env_file"],
    )

    assert status == 500
    assert ig_session.read_cookie_file() == state["old_cookies"]
    assert ig_session.load_state() == state["old_state"]
    assert f"INSTAGRAM_SESSIONID='{new_session}'" in state["env_path"].read_text(encoding="utf-8")
    assert os.environ["INSTAGRAM_SESSIONID"] == state["old_cookies"]["sessionid"]
    assert os.environ["INSTAGRAM_DS_USER_ID"] == "12345"
    rendered_error = json.dumps(response) + "".join(logs)
    for secret in (new_session, "NEW_CSRF_PRIVATE", "OLD_SESSION_PRIVATE", "OLD_CSRF_PRIVATE"):
        assert secret not in rendered_error


def test_instagram_cookie_clear_env_replace_failure_keeps_cookie_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    state = _setup_ig_cookie_transaction(tmp_path, monkeypatch)
    social_handlers = state["social_handlers"]
    config_service = state["config_service"]
    logs: list[str] = []
    monkeypatch.setattr(social_handlers, "log_all", lambda message, **_kwargs: logs.append(message))
    real_replace = config_service.os.replace

    def fail_env_replace(source, destination):
        if Path(destination) == state["env_path"]:
            raise OSError("simulated .env failure OLD_SESSION_PRIVATE")
        return real_replace(source, destination)

    monkeypatch.setattr(config_service.os, "replace", fail_env_replace)
    status, response = _invoke_ig_cookie_handler(
        social_handlers.handle_ig_session_clear,
        update_env_file=state["update_env_file"],
        env_path=state["env_path"],
    )

    assert status == 500
    assert state["ig_session"].read_cookie_file() == state["old_cookies"]
    assert state["ig_session"].load_state() == state["old_state"]
    assert state["env_path"].read_bytes() == state["before_env"]
    assert os.environ["INSTAGRAM_SESSIONID"] == state["old_cookies"]["sessionid"]
    assert os.environ["INSTAGRAM_DS_USER_ID"] == "12345"
    rendered_error = json.dumps(response) + "".join(logs)
    assert "OLD_SESSION_PRIVATE" not in rendered_error
    assert "OLD_CSRF_PRIVATE" not in rendered_error


def test_instagram_cookie_clear_storage_failure_restores_env_and_process_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    state = _setup_ig_cookie_transaction(tmp_path, monkeypatch)
    ig_session = state["ig_session"]
    social_handlers = state["social_handlers"]
    logs: list[str] = []
    monkeypatch.setattr(social_handlers, "log_all", lambda message, **_kwargs: logs.append(message))

    def fail_cookie_clear():
        assert "INSTAGRAM_SESSIONID" not in state["env_path"].read_text(encoding="utf-8")
        assert "INSTAGRAM_DS_USER_ID" not in state["env_path"].read_text(encoding="utf-8")
        assert "INSTAGRAM_SESSIONID" not in os.environ
        assert "INSTAGRAM_DS_USER_ID" not in os.environ
        raise OSError("simulated Cookie clear failure OLD_SESSION_PRIVATE")

    monkeypatch.setattr(ig_session, "clear", fail_cookie_clear)
    status, response = _invoke_ig_cookie_handler(
        social_handlers.handle_ig_session_clear,
        update_env_file=state["update_env_file"],
        env_path=state["env_path"],
    )

    assert status == 500
    assert ig_session.read_cookie_file() == state["old_cookies"]
    assert ig_session.load_state() == state["old_state"]
    assert state["env_path"].read_bytes() == state["before_env"]
    assert os.environ["INSTAGRAM_SESSIONID"] == state["old_cookies"]["sessionid"]
    assert os.environ["INSTAGRAM_DS_USER_ID"] == "12345"
    rendered_error = json.dumps(response) + "".join(logs)
    assert "OLD_SESSION_PRIVATE" not in rendered_error
    assert "OLD_CSRF_PRIVATE" not in rendered_error


def test_secret_save_replace_failure_preserves_env_and_returns_failure(
    config_server, monkeypatch: pytest.MonkeyPatch,
):
    _webui, base_url, _config_path, env_path = config_server
    from src.webui_modules import config_service

    monkeypatch.delenv("HINATA_SHARED_TOKEN", raising=False)
    before_env = env_path.read_bytes()
    revision = _read_state(base_url)["revision"]
    real_replace = config_service.os.replace

    def fail_env_replace(source, destination):
        if Path(destination) == env_path:
            raise OSError("simulated .env replace failure")
        return real_replace(source, destination)

    monkeypatch.setattr(config_service.os, "replace", fail_env_replace)
    status, response = _http("POST", base_url + "/api/secrets", {
        "values": {"HINATA_SHARED_TOKEN": "SECRET_MUST_NOT_BE_SAVED"},
        "expected_revision": revision,
    })

    temp_path = env_path.with_suffix(env_path.suffix + ".tmp")
    assert status == 500 and response["ok"] is False
    assert "saved" not in response or response["saved"] is False
    assert env_path.read_bytes() == before_env
    assert not temp_path.exists()
    assert "SECRET_MUST_NOT_BE_SAVED" not in json.dumps(response)


def test_nested_preview_reports_paths_without_values():
    from src.webui_modules.config_service import config_preview_diff

    current = {
        "channels": {"tg": False},
        "monitor": [{"account": "private-account-id", "id": "private-member-id", "name": "旧名称"}],
        "accounts": {"main": {"token": "PRIVATE_TOKEN_OLD", "enabled": True}},
    }
    target = {
        "channels": {"tg": True},
        "monitor": [{"account": "private-account-id", "id": "private-member-id", "name": "新名称"}],
        "accounts": {"main": {"token": "PRIVATE_TOKEN_NEW", "enabled": True}},
    }

    preview = config_preview_diff(current, target)
    serialized = json.dumps(preview, ensure_ascii=False)
    paths = {item["path"] for group in preview["groups"] for item in group["changes"]}
    assert "channels.tg" in paths
    assert "monitor.[项目].name" in paths
    assert "accounts.main.token" in paths
    assert "旧名称" not in serialized and "新名称" not in serialized
    assert "PRIVATE_TOKEN_OLD" not in serialized and "PRIVATE_TOKEN_NEW" not in serialized
    assert "private-account-id" not in serialized and "private-member-id" not in serialized


def test_manual_reload_waits_for_config_and_env_commit(config_server, monkeypatch: pytest.MonkeyPatch):
    webui, base_url, config_path, env_path = config_server
    config_written = Event()
    finish_env_write = Event()
    reload_handler_started = Event()
    report_called = Event()
    observed_states = []

    def paused_commit(raw, **kwargs):
        webui.save_config(raw, path=config_path)
        config_written.set()
        assert finish_env_write.wait(timeout=5)
        updates = kwargs["secret_updates"]
        webui.update_env_file(updates, path=env_path)
        return {"updated": sorted(updates), "removed": [], "renamed": []}

    def report_reload():
        config = json5.loads(config_path.read_text(encoding="utf-8"))
        env = env_path.read_text(encoding="utf-8")
        observed_states.append((config["alert_cooldown"], "HINATA_SHARED_TOKEN='new-value'" in env))
        report_called.set()
        return {"reloaded": True, "reload": {"config": "loaded", "components": "pending"}}

    original_auth = webui._Handler._check_auth

    def observe_reload_auth(handler):
        if handler.path.split("?", 1)[0] == "/api/reload":
            reload_handler_started.set()
        return original_auth(handler)

    monkeypatch.setattr(webui, "commit_config_and_secrets", paused_commit)
    monkeypatch.setattr(webui, "_trigger_reload_report", report_reload)
    monkeypatch.setattr(webui._Handler, "_check_auth", observe_reload_auth)
    monkeypatch.delenv("HINATA_SHARED_TOKEN", raising=False)

    revision = _read_state(base_url)["revision"]
    new_config = json.loads(json.dumps(VALID_CONFIG))
    new_config["alert_cooldown"] = 8400
    commit_result = {}
    reload_result = {}
    commit_thread = Thread(target=lambda: commit_result.setdefault("result", _http(
        "PUT", base_url + "/api/config/commit", {
            "config": new_config,
            "secret_updates": {"HINATA_SHARED_TOKEN": "new-value"},
            "expected_revision": revision,
        },
    )))
    reload_thread = Thread(target=lambda: reload_result.setdefault("result", _http(
        "POST", base_url + "/api/reload",
    )))

    try:
        commit_thread.start()
        assert config_written.wait(timeout=5)
        assert json5.loads(config_path.read_text(encoding="utf-8"))["alert_cooldown"] == 8400
        assert "HINATA_SHARED_TOKEN" not in env_path.read_text(encoding="utf-8")

        reload_thread.start()
        assert reload_handler_started.wait(timeout=5)
        premature_reload = report_called.wait(timeout=0.2)
    finally:
        finish_env_write.set()

    commit_thread.join(timeout=5)
    reload_thread.join(timeout=5)
    assert not commit_thread.is_alive() and not reload_thread.is_alive()
    assert not premature_reload
    assert commit_result["result"][0] == 200 and commit_result["result"][1]["saved"] is True
    assert reload_result["result"][0] == 200 and reload_result["result"][1]["reloaded"] is True
    assert observed_states == [(8400, True), (8400, True)]


def test_revision_is_opaque_and_covers_config_and_env(tmp_path: Path):
    from src.webui_modules.config_service import (
        config_state_revision,
        redact_config_error_messages,
        redact_config_preview,
    )

    config_path = tmp_path / "config.json"
    env_path = tmp_path / ".env"
    config_path.write_text('{"secret":"VERY_PRIVATE_VALUE"}', encoding="utf-8")
    env_path.write_text("API_TOKEN='ENV_PRIVATE_VALUE'\n", encoding="utf-8")

    initial = config_state_revision(config_path, env_path)
    assert initial == config_state_revision(config_path, env_path)
    assert "VERY_PRIVATE_VALUE" not in initial
    assert "ENV_PRIVATE_VALUE" not in initial
    url_preview = redact_config_preview({
        "napcat_api": "https://bot.example/send?access_token=URL_PRIVATE_VALUE",
    })
    assert url_preview["napcat_api"] == "[redacted]"
    invalid_secret_error = redact_config_error_messages(
        ["{'nested': 'INVALID_SECRET_VALUE'} is not of type 'string'"],
        {"napcat_api_token": {"nested": "INVALID_SECRET_VALUE"}},
    )
    assert "INVALID_SECRET_VALUE" not in invalid_secret_error[0]

    env_path.write_text("API_TOKEN='CHANGED_VALUE'\n", encoding="utf-8")
    env_changed = config_state_revision(config_path, env_path)
    assert env_changed != initial
    config_path.write_text('{"secret":"CHANGED_VALUE"}', encoding="utf-8")
    assert config_state_revision(config_path, env_path) != env_changed


def test_all_config_write_routes_require_revision_and_reject_stale_state(
    config_server, monkeypatch: pytest.MonkeyPatch,
):
    webui, base_url, config_path, env_path = config_server
    history_dir = config_path.parent / "history"
    history_dir.mkdir()
    snapshot_name = "config-20261024-000000-000.json"
    (history_dir / snapshot_name).write_text(webui.serialize_config(VALID_CONFIG), encoding="utf-8")

    missing_requests = [
        ("PUT", "/api/config", dict(VALID_CONFIG)),
        ("PUT", "/api/config/commit", {"config": VALID_CONFIG}),
        ("POST", "/api/config/restore", {"name": snapshot_name}),
        ("POST", "/api/config/rollback", {"name": snapshot_name}),
    ]
    for method, path, body in missing_requests:
        status, response = _http(method, base_url + path, body)
        assert status == 428
        assert response["error"] == "expected_revision_required"

    status, invalid = _http("PUT", base_url + "/api/config", {
        **VALID_CONFIG,
        "expected_revision": "版本不是 ASCII revision",
    })
    assert status == 400 and invalid["error"] == "expected_revision_invalid"

    initial = _read_state(base_url)["revision"]
    changed = json.loads(json.dumps(VALID_CONFIG))
    changed["alert_cooldown"] = 7200
    status, saved = _http("PUT", base_url + "/api/config", {
        **changed,
        "expected_revision": initial,
    })
    assert status == 200 and saved["saved"] is True
    assert saved["revision"] != initial
    assert "expected_revision" not in json5.loads(config_path.read_text(encoding="utf-8"))
    assert saved["reload"] == {"config": "loaded", "components": "pending"}

    failed_config = json.loads(json.dumps(changed))
    failed_config["alert_cooldown"] = 9000
    monkeypatch.setattr(webui, "_trigger_reload_report", lambda: {
        "reloaded": False,
        "reload": {"config": "failed", "components": "not_scheduled"},
    })
    status, saved_with_reload_failure = _http("PUT", base_url + "/api/config", {
        **failed_config,
        "expected_revision": saved["revision"],
    })
    assert status == 200 and saved_with_reload_failure["saved"] is True
    assert saved_with_reload_failure["reload"] == {
        "config": "failed", "components": "not_scheduled",
    }
    assert json5.loads(config_path.read_text(encoding="utf-8"))["alert_cooldown"] == 9000

    # A credential-only change must invalidate a staged config+secret commit.
    env_path.write_text("KEEP_TOKEN='external-change'\n", encoding="utf-8")
    stale = initial
    before_config = config_path.read_bytes()
    before_env = env_path.read_bytes()
    before_history = sorted(path.read_bytes() for path in history_dir.glob("config-*.json"))
    stale_requests = [
        ("PUT", "/api/config", {**changed, "expected_revision": stale}),
        ("PUT", "/api/config/commit", {
            "config": changed,
            "secret_updates": {"HINATA_SHARED_TOKEN": "DO_NOT_WRITE"},
            "expected_revision": stale,
        }),
        ("POST", "/api/config/restore", {"name": snapshot_name, "expected_revision": stale}),
        ("POST", "/api/config/rollback", {"name": snapshot_name, "expected_revision": stale}),
    ]
    for method, path, body in stale_requests:
        status, response = _http(method, base_url + path, body)
        assert status == 409
        assert response["error"] == "revision_conflict"
        assert response["revision"] == _read_state(base_url)["revision"]
        serialized = json.dumps(response)
        assert "CONFIG_SECRET_MUST_NOT_APPEAR_IN_PREVIEW" not in serialized
        assert "ANOTHER_SECRET_MUST_NOT_APPEAR" not in serialized
        assert "DO_NOT_WRITE" not in serialized
        assert config_path.read_bytes() == before_config
        assert env_path.read_bytes() == before_env
        assert sorted(path.read_bytes() for path in history_dir.glob("config-*.json")) == before_history
        assert "HINATA_SHARED_TOKEN" not in os.environ


def test_history_preview_contains_only_safe_paths_and_redacts_secret_values(config_server):
    webui, base_url, config_path, _env_path = config_server
    history_dir = config_path.parent / "history"
    history_dir.mkdir()
    target = json.loads(json.dumps(VALID_CONFIG))
    target["alert_cooldown"] = 4800
    snapshot_name = "config-20261024-000000-001.json"
    (history_dir / snapshot_name).write_text(webui.serialize_config(target), encoding="utf-8")

    status, response = _http("GET", base_url + "/api/config/history?name=" + snapshot_name)
    assert status == 200 and response["content_redacted"] is True
    assert response["restore_uses_server_snapshot"] is True
    assert json5.loads(response["content"])["napcat_api_token"] == "[redacted]"
    assert json5.loads(response["content"])["napcat_media_signing_secret"] == "[redacted]"
    serialized = json.dumps(response, ensure_ascii=False)
    assert "CONFIG_SECRET_MUST_NOT_APPEAR_IN_PREVIEW" not in serialized
    assert "ANOTHER_SECRET_MUST_NOT_APPEAR" not in serialized
    assert "alert_cooldown" in serialized
    assert "4800" not in json.dumps(response["preview"])
    assert response["preview"]["groups"] == [{
        "label": "alert_cooldown",
        "changes": [{"path": "alert_cooldown", "kind": "changed"}],
    }]


def test_invalid_history_restore_has_no_file_or_history_side_effects(config_server):
    webui, base_url, config_path, env_path = config_server
    history_dir = config_path.parent / "history"
    history_dir.mkdir()
    invalid = json.loads(json.dumps(VALID_CONFIG))
    invalid["monitor"] = [{
        "id": "1", "name": "invalid", "account": "missing_account", "groups": [],
    }]
    snapshot_name = "config-20261024-000000-002.json"
    (history_dir / snapshot_name).write_text(webui.serialize_config(invalid), encoding="utf-8")
    before_config = config_path.read_bytes()
    before_env = env_path.read_bytes()
    before_history = {path.name: path.read_bytes() for path in history_dir.glob("config-*.json")}

    revision = _read_state(base_url)["revision"]
    status, response = _http("POST", base_url + "/api/config/restore", {
        "name": snapshot_name,
        "expected_revision": revision,
    })

    assert status == 400 and response["ok"] is False
    assert "missing_account" in " ".join(response["errors"])
    assert config_path.read_bytes() == before_config
    assert env_path.read_bytes() == before_env
    assert {path.name: path.read_bytes() for path in history_dir.glob("config-*.json")} == before_history


def test_failed_atomic_config_write_restores_file_and_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    import src.webui as webui
    from src.webui_modules import config_service

    config_path = tmp_path / "config.json"
    config_path.write_text(webui.serialize_config(VALID_CONFIG), encoding="utf-8")
    history_dir = tmp_path / "history"
    history_dir.mkdir()
    previous_snapshot = history_dir / "config-20261001-000000-000.json"
    previous_snapshot.write_text("previous snapshot\n", encoding="utf-8")
    before_config = config_path.read_bytes()
    before_history = {path.name: path.read_bytes() for path in history_dir.glob("config-*.json")}

    real_replace = config_service.os.replace

    def fail_primary_replace(source, destination):
        if Path(destination) == config_path and Path(source).name == "config.json.tmp":
            raise OSError("simulated config replace failure")
        return real_replace(source, destination)

    monkeypatch.setattr(config_service.os, "replace", fail_primary_replace)
    changed = json.loads(json.dumps(VALID_CONFIG))
    changed["alert_cooldown"] = 9999
    with pytest.raises(OSError, match="simulated config replace failure"):
        config_service.save_config(changed, path=config_path)

    assert config_path.read_bytes() == before_config
    assert {path.name: path.read_bytes() for path in history_dir.glob("config-*.json")} == before_history


def test_reload_report_does_not_claim_component_effectiveness(monkeypatch: pytest.MonkeyPatch):
    import src.config.config as cfg
    import src.logger as logger
    from src.webui_modules import config_service

    monkeypatch.setattr(logger, "log_all", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cfg, "reload", lambda: True)
    monkeypatch.setattr(config_service, "_on_reload_cb", None)
    unavailable = config_service._trigger_reload_report()
    assert unavailable["reloaded"] is True
    assert unavailable["reload"]["config"] == "loaded"
    assert unavailable["reload"]["components"] == "unavailable"
    assert unavailable["reload"]["id"]
    assert unavailable["reload"]["application"]["state"] == "not_started"

    monkeypatch.setattr(config_service, "_on_reload_cb", lambda _ok: (_ for _ in ()).throw(RuntimeError("PRIVATE")))
    failed_dispatch = config_service._trigger_reload_report()
    assert failed_dispatch["reload"]["config"] == "loaded"
    assert failed_dispatch["reload"]["components"] == "dispatch_failed"
    assert failed_dispatch["reload"]["application"]["state"] == "not_started"
    assert "PRIVATE" not in json.dumps(failed_dispatch)

    monkeypatch.setattr(cfg, "reload", lambda: False)
    monkeypatch.setattr(config_service, "_on_reload_cb", lambda _ok: None)
    not_loaded = config_service._trigger_reload_report()
    assert not_loaded["reload"]["config"] == "failed"
    assert not_loaded["reload"]["components"] == "not_scheduled"
    assert not_loaded["reload"]["application"]["state"] == "not_started"
