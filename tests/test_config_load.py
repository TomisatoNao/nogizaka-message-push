"""验证 config.json → config.py 加载正确性

运行: python tests/test_config_load.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def main() -> None:
    # Test 1: 所有变量加载并类型正确
    print("=== Test 1: 变量加载 ===")
    from config.config import (
        ENABLE_NAPCAT_QQ, ENABLE_QQ_OFFICIAL_BOT, QQ_BOT_API, QQ_USER_AGENT,
        QQ_OFFICIAL_TOKEN_URL, QQ_OFFICIAL_API_BASE, QQ_OFFICIAL_MIN_INTERVAL,
        QQ_OFFICIAL_TIMEOUT, QQ_OFFICIAL_MEDIA_MAX_BYTES, QQ_OFFICIAL_BOTS,
        ACCOUNTS, MONITOR_LIST, ENABLE_TRANSLATION,
        SKIP_PUBLISH_TYPES, MEDIA_TYPE_MAP,
        DAY_START_HOUR, NIGHT_START_HOUR, DAY_INTERVAL, NIGHT_INTERVAL,
        SLEEP_START_HOUR, SLEEP_END_HOUR,
        BACKTRACK_HOURS, ALERT_COOLDOWN_SECONDS,
        HTTP_SEMAPHORE_LIMIT, QQ_SEND_INTERVAL, TOKEN_REFRESH_BEFORE_SECONDS,
        CRED_DIR, TIME_RECORD_DIR, SENT_IDS_DIR,
        ERROR_LOG_FILE, RESPONSE_LOG_FILE, SENT_IDS_MAX,
        DEBUG_LOG_RESPONSE, DEBUG_LOG_QQ_PAYLOAD,
        GEMINI_API_KEY, GEMINI_MODELS, GEMINI_MIN_INTERVAL,
        TRANSLATE_MAX_LENGTH, TRANSLATE_TIMEOUT,
    )

    # 类型断言
    assert isinstance(ENABLE_NAPCAT_QQ, bool), "ENABLE_NAPCAT_QQ should be bool"
    assert isinstance(ENABLE_QQ_OFFICIAL_BOT, bool), "ENABLE_QQ_OFFICIAL_BOT should be bool"
    assert isinstance(QQ_OFFICIAL_BOTS, list), "QQ_OFFICIAL_BOTS should be list"
    assert isinstance(ACCOUNTS, dict), "ACCOUNTS should be dict"
    assert isinstance(MONITOR_LIST, list), "MONITOR_LIST should be list"
    assert isinstance(SKIP_PUBLISH_TYPES, set), "SKIP_PUBLISH_TYPES should be set"
    assert isinstance(MEDIA_TYPE_MAP, dict), "MEDIA_TYPE_MAP should be dict"
    assert isinstance(DAY_INTERVAL, tuple), "DAY_INTERVAL should be tuple"
    assert isinstance(NIGHT_INTERVAL, tuple), "NIGHT_INTERVAL should be tuple"
    assert isinstance(GEMINI_MODELS, list), "GEMINI_MODELS should be list"
    assert isinstance(SENT_IDS_MAX, int), "SENT_IDS_MAX should be int"
    assert isinstance(QQ_BOT_API, str) and QQ_BOT_API, "QQ_BOT_API should be non-empty str"

    # 轮询节奏必须是可用的数值（缺省值兜底后不应为 None）
    for name, val in (
        ("DAY_START_HOUR", DAY_START_HOUR), ("NIGHT_START_HOUR", NIGHT_START_HOUR),
        ("SLEEP_START_HOUR", SLEEP_START_HOUR), ("SLEEP_END_HOUR", SLEEP_END_HOUR),
        ("BACKTRACK_HOURS", BACKTRACK_HOURS), ("ALERT_COOLDOWN_SECONDS", ALERT_COOLDOWN_SECONDS),
    ):
        assert isinstance(val, int), f"{name} should be int, got {val!r}"

    # 路径断言
    assert os.path.isabs(CRED_DIR), f"CRED_DIR should be absolute: {CRED_DIR}"
    assert os.path.isabs(TIME_RECORD_DIR), f"TIME_RECORD_DIR should be absolute: {TIME_RECORD_DIR}"
    assert os.path.isabs(SENT_IDS_DIR), f"SENT_IDS_DIR should be absolute: {SENT_IDS_DIR}"

    print(f"  MONITOR_LIST: {len(MONITOR_LIST)} members")
    for m in MONITOR_LIST:
        print(f"    - {m['m_name']} (id={m['m_id']}, account={m['account_id']})")
    print(f"  ACCOUNTS: {list(ACCOUNTS.keys())}")
    print(f"  GEMINI_MODELS: {len(GEMINI_MODELS)} models")
    print("✅ Test 1 通过\n")

    # Test 2: 类型转换正确性（向后兼容）
    print("=== Test 2: 类型正确性 ===")
    assert SKIP_PUBLISH_TYPES == {"birthday"}, f"SKIP_PUBLISH_TYPES mismatch: {SKIP_PUBLISH_TYPES}"
    assert MEDIA_TYPE_MAP["video"] == "video"
    assert MEDIA_TYPE_MAP["voice"] == "record"
    assert len(DAY_INTERVAL) == 2
    assert len(NIGHT_INTERVAL) == 2
    print("✅ Test 2 通过\n")

    # Test 3: yodel 账号的自定义域名字段（config.json 是用户可改的运行配置，
    # 该账号不存在时跳过，不强求示例配置原样保留）
    print("=== Test 3: Yodel 账号结构 ===")
    yodel = ACCOUNTS.get("yodel_grad")
    if yodel is None:
        print("  （当前配置没有 yodel_grad 账号，跳过）")
    else:
        assert yodel["group_type"] == "hinatazaka46"
        assert yodel["app_tag"] == "yodel"
        assert yodel["api_base"] == "https://api.service.yodel-app.com"
        assert yodel["web_origin"] == "https://service.yodel-app.com"
    print("✅ Test 3 通过\n")

    # Test 4: 每个成员的规范化字段齐全
    print("=== Test 4: monitor 规范化 ===")
    for m in MONITOR_LIST:
        for key in ("account_id", "group_type", "m_id", "m_name", "target_groups", "tg_chat_id"):
            assert key in m, f"member {m.get('m_name')} missing key {key}"
        assert isinstance(m["target_groups"], list), "target_groups should be list"
        assert m["account_id"] in ACCOUNTS, f"未定义的账号: {m['account_id']}"
        assert m["group_type"], f"{m['m_name']} 的 group_type 为空（账号缺少 group 字段？）"
    print(f"  {len(MONITOR_LIST)} 个成员字段齐全，账号引用有效")
    print("✅ Test 4 通过\n")

    # Test 5: 热重载
    print("=== Test 5: 热重载 ===")
    from config.config import reload
    assert reload(), "reload() should succeed with unchanged config"
    print("✅ Test 5 通过\n")

    print("=" * 50)
    print("🎉 全部测试通过！config.py facade 工作正常")


if __name__ == "__main__":
    main()
