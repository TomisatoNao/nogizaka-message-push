"""模型优先级调度器与熔断冷却机制单元测试。"""

import asyncio
import time
from pathlib import Path
import sys
import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import translator  # noqa: E402
from src.config import config as cfg  # noqa: E402


@pytest.fixture(autouse=True)
def cleanup_cooldowns():
    """每次测试前清空冷却记录与翻译缓存。"""
    translator._model_cooldowns.clear()
    translator._trans_cache.clear()
    yield
    translator._model_cooldowns.clear()
    translator._trans_cache.clear()


def test_cooldown_lifecycle():
    """测试冷却标记、查询与解除生命周期。"""
    model_name = "test-model-alpha"
    assert not translator.is_model_cooling(model_name)

    # 标记冷却 60 秒
    translator.mark_model_cooldown(model_name, seconds=60.0, reason="test")
    assert translator.is_model_cooling(model_name)

    # 成功后解除冷却
    translator.clear_model_cooldown(model_name)
    assert not translator.is_model_cooling(model_name)


def test_cooldown_natural_expiry(monkeypatch):
    """测试冷却时间随 monotonic 自然过期。"""
    model_name = "test-model-beta"
    fake_time = 1000.0

    monkeypatch.setattr(time, "monotonic", lambda: fake_time)
    translator.mark_model_cooldown(model_name, seconds=30.0)
    assert translator.is_model_cooling(model_name)

    # 推进时间超过 30s
    fake_time = 1035.0
    assert not translator.is_model_cooling(model_name)


def test_ordered_models_healthy_first(monkeypatch):
    """测试健康模型始终优先，冷却模型自动降级至队尾。"""
    fake_models = [
        {"name": "gemini-2.5-flash-lite", "url": "https://example.com/1"},
        {"name": "gemini-2.5-flash", "url": "https://example.com/2"},
        {"name": "gemini-3.5-flash", "url": "https://example.com/3"},
    ]
    monkeypatch.setattr(translator, "_get_active_models", lambda *args, **kwargs: list(fake_models))

    # 初始状态全健康
    ordered = translator._get_ordered_models()
    assert [m["name"] for m in ordered] == [
        "gemini-2.5-flash-lite",
        "gemini-2.5-flash",
        "gemini-3.5-flash",
    ]

    # 首选模型遭遇 503 冷却
    translator.mark_model_cooldown("gemini-2.5-flash-lite", seconds=60.0)
    ordered = translator._get_ordered_models()
    # gemini-2.5-flash-lite 被降级到最后作为兜底
    assert [m["name"] for m in ordered] == [
        "gemini-2.5-flash",
        "gemini-3.5-flash",
        "gemini-2.5-flash-lite",
    ]


def test_ordered_models_all_cooling_fallback(monkeypatch):
    """若所有模型均处于冷却期，仍返回全量模型进行兜底尝试，不完全阻塞服务。"""
    fake_models = [
        {"name": "m1", "url": "https://example.com/1"},
        {"name": "m2", "url": "https://example.com/2"},
    ]
    monkeypatch.setattr(translator, "_get_active_models", lambda *args, **kwargs: list(fake_models))

    translator.mark_model_cooldown("m1", 60.0)
    translator.mark_model_cooldown("m2", 60.0)

    ordered = translator._get_ordered_models()
    assert len(ordered) == 2
    assert [m["name"] for m in ordered] == ["m1", "m2"]


def test_call_model_text_cooldown_on_503(monkeypatch):
    """测试单条文本翻译遇到 503 时触发模型冷却。"""
    model = {"name": "test-gemini", "url": "https://generativelanguage.googleapis.com/v1beta/models/test:generateContent", "provider": "gemini"}

    async def fake_post_json(*args, **kwargs):
        request = httpx.Request("POST", "https://example.com")
        return httpx.Response(503, request=request)

    monkeypatch.setattr(translator, "_post_json", fake_post_json)
    monkeypatch.setattr(cfg, "GEMINI_API_KEY", "dummy-key")

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(translator._call_model_text(model, "hello"))

    assert translator.is_model_cooling("test-gemini")


def test_call_model_text_clears_cooldown_on_success(monkeypatch):
    """测试单条文本翻译成功后自动消除冷却标记。"""
    model = {"name": "test-gemini", "url": "https://generativelanguage.googleapis.com/v1beta/models/test:generateContent", "provider": "gemini"}
    translator.mark_model_cooldown("test-gemini", 60.0)
    assert translator.is_model_cooling("test-gemini")

    async def fake_post_json(*args, **kwargs):
        request = httpx.Request("POST", "https://example.com")
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "你好"}]}}]
        }, request=request)

    monkeypatch.setattr(translator, "_post_json", fake_post_json)
    monkeypatch.setattr(cfg, "GEMINI_API_KEY", "dummy-key")

    res = asyncio.run(translator._call_model_text(model, "こんにちは"))
    assert res == "你好"
    assert not translator.is_model_cooling("test-gemini")


def test_scenario_blog_quality_first_auto_reorder(monkeypatch):
    """测试在未显式配置 GEMINI_BLOG_MODELS 时，博客场景自动对 GEMINI_MODELS 进行质量优先重排。"""
    monkeypatch.setattr(cfg, "GEMINI_API_KEY", "dummy-key")
    monkeypatch.setattr(cfg, "GEMINI_BLOG_MODELS", [])
    monkeypatch.setattr(cfg, "GEMINI_MODELS", [
        {"name": "gemini-2.5-flash-lite", "url": "https://example.com/lite"},
        {"name": "gemini-3.5-flash", "url": "https://example.com/3.5"},
        {"name": "gemini-2.5-flash", "url": "https://example.com/2.5"},
    ])

    # 博客场景：3.5-flash 应该被重排到首位（质量天花板）
    blog_models = translator._get_ordered_models(scenario="blog")
    assert blog_models[0]["name"] == "gemini-3.5-flash"
    assert [m["name"] for m in blog_models] == [
        "gemini-3.5-flash",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
    ]

    # 消息场景：仍保持速度优先（2.5-flash-lite 首位）
    msg_models = translator._get_ordered_models(scenario="message")
    assert msg_models[0]["name"] == "gemini-2.5-flash-lite"


def test_scenario_blog_custom_models(monkeypatch):
    """测试显式配置 GEMINI_BLOG_MODELS 时，博客场景优先使用该专属配置。"""
    monkeypatch.setattr(cfg, "GEMINI_API_KEY", "dummy-key")
    monkeypatch.setattr(cfg, "GEMINI_MODELS", [
        {"name": "gemini-2.5-flash-lite", "url": "https://example.com/lite"},
    ])
    monkeypatch.setattr(cfg, "GEMINI_BLOG_MODELS", [
        {"name": "custom-blog-pro", "url": "https://example.com/pro"},
    ])

    blog_models = translator._get_ordered_models(scenario="blog")
    assert len(blog_models) == 1
    assert blog_models[0]["name"] == "custom-blog-pro"

    msg_models = translator._get_ordered_models(scenario="message")
    assert len(msg_models) == 1
    assert msg_models[0]["name"] == "gemini-2.5-flash-lite"


def test_immediate_failover_on_429_without_sleep(monkeypatch):
    """测试当存在后备模型时，遇到 429 立即转移给后备模型，不再对冷却模型原地等待重试。"""
    monkeypatch.setattr(cfg, "GEMINI_API_KEY", "dummy-key")
    call_counts = {"m1": 0, "m2": 0}

    async def fake_call(model, prompt, custom_client=None):
        m_name = model["name"]
        call_counts[m_name] += 1
        if m_name == "m1":
            req = httpx.Request("POST", "https://example.com")
            resp = httpx.Response(429, request=req)
            raise httpx.HTTPStatusError("429 Too Many Requests", request=req, response=resp)
        return "成功译文"

    monkeypatch.setattr(translator, "_call_model_text", fake_call)
    monkeypatch.setattr(translator, "_get_ordered_models", lambda *args, **kwargs: [
        {"name": "m1", "url": "https://example.com/1", "provider": "gemini"},
        {"name": "m2", "url": "https://example.com/2", "provider": "gemini"},
    ])

    res, used_model = asyncio.run(translator.translate_text_with_model("テスト", "member"))
    assert res == "成功译文"
    assert used_model == "m2"
    # m1 仅被调用 1 次，直接 failover 给 m2，未在已冷却模型上浪费第 2 次重试
    assert call_counts["m1"] == 1
    assert call_counts["m2"] == 1
    assert translator.is_model_cooling("m1")


def test_network_error_triggers_cooldown(monkeypatch):
    """测试网络连接错误 (ConnectError) 触发 60s 临时冷却。"""
    monkeypatch.setattr(cfg, "GEMINI_API_KEY", "dummy-key")

    async def fake_call(model, prompt, custom_client=None):
        req = httpx.Request("POST", "https://example.com")
        raise httpx.ConnectError("Connection refused", request=req)

    monkeypatch.setattr(translator, "_call_model_text", fake_call)
    monkeypatch.setattr(translator, "_get_ordered_models", lambda *args, **kwargs: [
        {"name": "m_unreachable", "url": "https://example.com/dead", "provider": "gemini"},
    ])

    res, _ = asyncio.run(translator.translate_text_with_model("テスト", "member"))
    assert res == "[翻译失败]"
    assert translator.is_model_cooling("m_unreachable")


def test_client_error_does_not_trigger_temporary_cooldown(monkeypatch):
    """测试永久性客户端错误（如 401 密钥失效）不应被错误当成暂时过载置入 60s 冷却。"""
    monkeypatch.setattr(cfg, "GEMINI_API_KEY", "dummy-key")

    async def fake_call(model, prompt, custom_client=None):
        req = httpx.Request("POST", "https://example.com")
        resp = httpx.Response(401, request=req)
        raise httpx.HTTPStatusError("401 Unauthorized", request=req, response=resp)

    monkeypatch.setattr(translator, "_call_model_text", fake_call)
    monkeypatch.setattr(translator, "_get_ordered_models", lambda *args, **kwargs: [
        {"name": "m_bad_key", "url": "https://example.com/auth", "provider": "gemini"},
    ])

    res, _ = asyncio.run(translator.translate_text_with_model("テスト", "member"))
    assert res == "[翻译失败]"
    assert not translator.is_model_cooling("m_bad_key")


def test_legacy_config_auto_derives_blog_models(monkeypatch):
    """测试旧版本 config.json（未配置 gemini_blog_models）加载后，博客能正确基于用户自定义的 gemini_models 派生。"""
    monkeypatch.setattr(cfg, "GEMINI_API_KEY", "dummy-key")
    # 模拟旧版 config.json 未声明 gemini_blog_models（采用 _DEFAULTS 的空列表）
    monkeypatch.setattr(cfg, "GEMINI_BLOG_MODELS", [])
    # 模拟旧版用户自定义了独立的 2 个模型
    custom_user_models = [
        {"name": "gemini-2.5-flash-lite", "url": "https://example.com/custom-lite"},
        {"name": "gemini-3.5-flash", "url": "https://example.com/custom-3.5"},
    ]
    monkeypatch.setattr(cfg, "GEMINI_MODELS", custom_user_models)

    # 博客场景：自动提取用户的 custom_user_models 并按质量重排（3.5-flash 优先）
    blog_models = translator._get_ordered_models(scenario="blog")
    assert [m["name"] for m in blog_models] == ["gemini-3.5-flash", "gemini-2.5-flash-lite"]
    assert blog_models[0]["url"] == "https://example.com/custom-3.5"

    # 消息场景：速度优先（lite 优先）
    msg_models = translator._get_ordered_models(scenario="message")
    assert [m["name"] for m in msg_models] == ["gemini-2.5-flash-lite", "gemini-3.5-flash"]


@pytest.mark.parametrize("status_code", [500, 502, 504])
def test_call_model_text_cooldown_on_all_5xx(monkeypatch, status_code):
    """测试单条文本翻译遇到 500, 502, 504 等服务端错误均触发冷却。"""
    model = {"name": f"gemini-err-{status_code}", "url": "https://example.com/api", "provider": "gemini"}

    async def fake_post_json(*args, **kwargs):
        request = httpx.Request("POST", "https://example.com")
        return httpx.Response(status_code, request=request)

    monkeypatch.setattr(translator, "_post_json", fake_post_json)
    monkeypatch.setattr(cfg, "GEMINI_API_KEY", "dummy-key")

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(translator._call_model_text(model, "hello"))

    assert translator.is_model_cooling(f"gemini-err-{status_code}")


def test_message_translation_failover_and_cooldown_on_500(monkeypatch):
    """测试消息翻译端到端遭遇 500 故障时，将故障模型置入冷却并秒切备用模型，下一次请求自动跳过故障模型。"""
    monkeypatch.setattr(cfg, "GEMINI_API_KEY", "dummy-key")

    async def fake_post_json(url, *args, **kwargs):
        req = httpx.Request("POST", url)
        if "err-m1" in url:
            return httpx.Response(500, request=req)
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "消息成功"}]}}]
        }, request=req)

    monkeypatch.setattr(translator, "_post_json", fake_post_json)
    monkeypatch.setattr(translator, "_get_ordered_models", lambda *args, **kwargs: [
        {"name": "err-m1", "url": "https://example.com/err-m1", "provider": "gemini"},
        {"name": "good-m2", "url": "https://example.com/good-m2", "provider": "gemini"},
    ])

    # 第一次翻译：err-m1 遇到 500，应被置入冷却，由 good-m2 完成翻译
    res, used_model = asyncio.run(translator.translate_text_with_model("こんにちは", "member"))
    assert res == "消息成功"
    assert used_model == "good-m2"
    assert translator.is_model_cooling("err-m1")


def test_legacy_config_file_e2e_load_fixture(tmp_path, monkeypatch):
    """端到端测试：从磁盘实际加载一份未包含 gemini_blog_models 的旧版完整配置文件，验证系统向下兼容能力。"""
    legacy_config_content = """{
  "channels": {
    "qq_official_bot": false,
    "napcat_qq": false,
    "push_deer": false,
    "custom_post": false,
    "telegram_bot": false
  },
  "accounts": {},
  "monitor": [],
  "translate": true,
  "gemini_models": [
    {"name": "custom-legacy-lite", "url": "https://example.com/legacy-lite"},
    {"name": "gemini-3.5-flash", "url": "https://example.com/legacy-3.5"}
  ]
}"""
    legacy_cfg_file = tmp_path / "config.json"
    legacy_cfg_file.write_text(legacy_config_content, encoding="utf-8")

    orig_config_path = cfg._CONFIG_PATH
    monkeypatch.setattr(cfg, "_CONFIG_PATH", legacy_cfg_file)
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-key")
    success = cfg.reload()
    assert success is True
    monkeypatch.setattr(cfg, "GEMINI_API_KEY", "dummy-key")

    try:
        # 1. 验证内存中 GEMINI_BLOG_MODELS 保持为 []，未被默认值强行覆盖
        assert cfg.GEMINI_BLOG_MODELS == []
        assert len(cfg.GEMINI_MODELS) == 2

        # 2. 验证端到端派生：博客场景按质量等级派生出 3.5-flash 在前
        blog_models = translator._get_ordered_models(scenario="blog")
        assert [m["name"] for m in blog_models] == ["gemini-3.5-flash", "custom-legacy-lite"]

        # 3. 验证端到端派生：消息场景维持原配置顺序（lite 在前）
        msg_models = translator._get_ordered_models(scenario="message")
        assert [m["name"] for m in msg_models] == ["custom-legacy-lite", "gemini-3.5-flash"]
    finally:
        monkeypatch.setattr(cfg, "_CONFIG_PATH", orig_config_path)
        cfg.reload()
