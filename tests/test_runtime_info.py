from src.runtime_info import runtime_metadata


def test_runtime_metadata_uses_local_defaults(monkeypatch):
    for key in ("APP_VERSION", "APP_GIT_SHA", "APP_BUILD_TIME"):
        monkeypatch.delenv(key, raising=False)

    assert runtime_metadata() == {
        "version": "dev",
        "git_sha": "unknown",
        "build_time": "unknown",
    }


def test_runtime_metadata_reads_build_environment(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "sha-1234567")
    monkeypatch.setenv("APP_GIT_SHA", "1234567890abcdef")
    monkeypatch.setenv("APP_BUILD_TIME", "2026-09-19T00:00:00Z")

    assert runtime_metadata() == {
        "version": "sha-1234567",
        "git_sha": "1234567890abcdef",
        "build_time": "2026-09-19T00:00:00Z",
    }
