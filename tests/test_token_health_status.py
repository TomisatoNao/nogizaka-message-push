"""Token 状态应区分访问 Token 过期与续期确认失败。"""

import base64
import json
import time

from src.config import credentials


def _jwt_with_exp(exp: int) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp}).encode("utf-8")
    ).rstrip(b"=").decode("ascii")
    return f"header.{payload}.signature"


def test_expired_access_token_waits_for_patrol_renewal(monkeypatch):
    account_creds = {
        "demo": {
            "token": _jwt_with_exp(int(time.time()) - 1),
            "refresh_token": "refresh-token",
        }
    }
    monkeypatch.setattr(credentials, "ACCOUNT_CREDS", account_creds)
    monkeypatch.setattr(credentials, "_refresh_state", {})
    monkeypatch.setattr(
        credentials.cfg,
        "ACCOUNTS",
        {"demo": {"auth_method": "mobile"}},
        raising=False,
    )

    status = credentials.get_token_health("demo")

    assert status["remaining"] == 0
    assert status["status"] == "pending_renewal"
    assert status["healthy"] is False


def test_only_confirmed_renewal_rejection_is_invalid(monkeypatch):
    account_creds = {
        "demo": {
            "token": _jwt_with_exp(int(time.time()) - 1),
            "refresh_token": "refresh-token",
        }
    }
    monkeypatch.setattr(credentials, "ACCOUNT_CREDS", account_creds)
    monkeypatch.setattr(
        credentials.cfg,
        "ACCOUNTS",
        {"demo": {"auth_method": "mobile"}},
        raising=False,
    )
    monkeypatch.setattr(
        credentials,
        "_refresh_state",
        {
            "demo": {
                "kind": "credential_invalid",
                "blocked_until": float("inf"),
                "cred_ref": id(account_creds["demo"]),
            }
        },
    )

    status = credentials.get_token_health("demo")

    assert status["status"] == "invalid"


def test_transient_renewal_failure_is_not_invalid(monkeypatch):
    account_creds = {
        "demo": {
            "token": _jwt_with_exp(int(time.time()) - 1),
            "refresh_token": "refresh-token",
        }
    }
    monkeypatch.setattr(credentials, "ACCOUNT_CREDS", account_creds)
    monkeypatch.setattr(
        credentials.cfg,
        "ACCOUNTS",
        {"demo": {"auth_method": "mobile"}},
        raising=False,
    )
    monkeypatch.setattr(
        credentials,
        "_refresh_state",
        {
            "demo": {
                "kind": "transient_network",
                "blocked_until": time.monotonic() + 30,
                "cred_ref": id(account_creds["demo"]),
            }
        },
    )

    status = credentials.get_token_health("demo")

    assert status["status"] == "renewal_retry"
