import sys
import tempfile
import asyncio
from pathlib import Path
import pytest
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import member_directory
from src import auth


@pytest.fixture
def temp_auth_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_auth.db"
        orig_path = auth.AUTH_DB_PATH
        orig_conn = auth._auth_conn
        monkeypatch.setattr(auth, "AUTH_DB_PATH", db_path)
        auth._auth_conn = None
        try:
            yield db_path
        finally:
            if auth._auth_conn is not None:
                try:
                    auth._auth_conn.close()
                except Exception:
                    pass
                auth._auth_conn = None
            auth.AUTH_DB_PATH = orig_path
            auth._auth_conn = orig_conn


def test_save_and_get_subscriptions(temp_auth_db):
    groups = [
        {
            "id": "1001",
            "name": "冨里 奈央",
            "state": "active",
            "subscription": {
                "state": "active",
                "type": "monthly",
                "start_at": "2026-01-01T00:00:00Z",
                "end_at": "2026-12-31T23:59:59Z",
                "auto_renewing": True,
            },
            "thumbnail": "https://example.com/thumb1001.jpg"
        },
        {
            "id": "1002",
            "name": "五百城 茉央",
            "state": "active",
            "subscription": None
        }
    ]

    # Save
    member_directory.save_account_subscriptions("acc_nogi_1", groups)

    # Query from auth DB
    conn = auth.get_auth_db()
    rows = conn.execute("SELECT member_id, member_name, state, sub_type, auto_renewing FROM member_subscriptions WHERE account_id='acc_nogi_1'").fetchall()
    assert len(rows) == 2
    row_map = {r[0]: r for r in rows}

    assert row_map["1001"][1] == "冨里 奈央"
    assert row_map["1001"][2] == "active"
    assert row_map["1001"][3] == "monthly"
    assert row_map["1001"][4] == 1

    assert row_map["1002"][1] == "五百城 茉央"
    assert row_map["1002"][2] == "unsubscribed"


def test_member_directory_reports_timeout_without_hiding_cause(monkeypatch):
    class TimeoutClient:
        async def get(self, *_args, **_kwargs):
            raise httpx.ReadTimeout("simulated timeout")

    monkeypatch.setattr(member_directory.cfg, "ACCOUNTS", {
        "demo": {"group_type": "nogizaka46", "auth_method": "web"},
    })
    groups, error = asyncio.run(member_directory.fetch_member_directory(TimeoutClient(), "demo"))

    assert groups is None
    assert "请求超时" in error


def test_subscription_sync_continues_after_one_account_failure(monkeypatch):
    async def fake_fetch(_client, account_id):
        if account_id == "broken":
            return None, "HTTP 401：凭证已失效"
        return [{"id": "1"}], None

    monkeypatch.setattr(member_directory.cfg, "ACCOUNTS", {"healthy": {}, "broken": {}})
    monkeypatch.setattr(member_directory, "fetch_member_directory", fake_fetch)
    import config.credentials as credentials
    monkeypatch.setattr(credentials, "validate_account_cred", lambda _account: (True, ""))

    stats, failures = asyncio.run(
        member_directory.sync_all_accounts_subscriptions(client=object(), include_errors=True)
    )

    assert stats == {"healthy": 1}
    assert failures == {"broken": "HTTP 401：凭证已失效"}
