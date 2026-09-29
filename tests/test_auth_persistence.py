"""Authentication persistence and refresh-rotation failure contracts."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from src import auth


@pytest.fixture
def isolated_auth(tmp_path, monkeypatch):
    old_conn = auth._auth_conn
    old_sessions = dict(auth._sessions)
    old_loaded = auth._sessions_loaded_from_db
    monkeypatch.setattr(auth, "AUTH_DB_PATH", tmp_path / "auth.db")
    auth._auth_conn = None
    auth._sessions.clear()
    auth._sessions_loaded_from_db = False
    try:
        yield auth
    finally:
        if auth._auth_conn is not None:
            auth._auth_conn.close()
        auth._auth_conn = old_conn
        auth._sessions.clear()
        auth._sessions.update(old_sessions)
        auth._sessions_loaded_from_db = old_loaded


def test_login_pair_is_atomic_when_refresh_insert_fails(isolated_auth):
    conn = isolated_auth.get_auth_db()
    conn.execute("""CREATE TRIGGER fail_refresh BEFORE INSERT ON refresh_tokens
                    BEGIN SELECT RAISE(ABORT, 'blocked'); END""")

    with pytest.raises(sqlite3.IntegrityError):
        isolated_auth.issue_login_tokens("demo", "admin", 3600, 30)

    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM refresh_tokens").fetchone()[0] == 0
    assert isolated_auth._sessions == {}


def test_refresh_rotation_rolls_back_on_session_insert_failure(isolated_auth):
    assert isolated_auth.add_user("demo", "long-password", "admin")[0]
    _, old_refresh = isolated_auth.issue_login_tokens("demo", "admin", 3600, 30)
    conn = isolated_auth.get_auth_db()
    conn.execute("""CREATE TRIGGER fail_session BEFORE INSERT ON sessions
                    BEGIN SELECT RAISE(ABORT, 'blocked'); END""")

    with pytest.raises(sqlite3.IntegrityError):
        isolated_auth.verify_and_rotate_refresh_token(old_refresh)

    assert conn.execute("SELECT COUNT(*) FROM refresh_tokens WHERE token=?",
                        (old_refresh,)).fetchone()[0] == 1


def test_concurrent_refresh_has_one_winner_and_one_stale_token(isolated_auth):
    assert isolated_auth.add_user("demo", "long-password", "admin")[0]
    _, old_refresh = isolated_auth.issue_login_tokens("demo", "admin", 3600, 30)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda _: isolated_auth.verify_and_rotate_refresh_token(old_refresh),
            range(2),
        ))

    assert sum(bool(user) for user, _, _ in results) == 1
    assert sum(not user for user, _, _ in results) == 1
    assert isolated_auth.refresh_token_count() == 1


def test_short_login_stays_short_after_refresh(isolated_auth):
    assert isolated_auth.add_user("demo", "long-password", "admin")[0]
    _, old_refresh = isolated_auth.issue_login_tokens("demo", "admin", 3600, 1)
    assert isolated_auth.refresh_token_ttl_days(old_refresh, 30) == 1

    user, _, new_refresh = isolated_auth.verify_and_rotate_refresh_token(
        old_refresh, refresh_ttl_days=30,
    )
    assert user and user["username"] == "demo"
    assert isolated_auth.refresh_token_ttl_days(new_refresh, 30) == 1
