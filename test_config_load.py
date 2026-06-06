"""验证 config.json → config.py 加载正确性"""
import json, sys, os

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Test 1: 所有变量加载并类型正确
print("=== Test 1: 变量加载 ===")
from config.config import (
    ENABLE_NAPCAT_QQ, ENABLE_QQ_OFFICIAL_BOT, QQ_BOT_API, QQ_USER_AGENT,
    QQ_OFFICIAL_TOKEN_URL, QQ_OFFICIAL_API_BASE, QQ_OFFICIAL_MIN_INTERVAL,
    QQ_OFFICIAL_TIMEOUT, QQ_OFFICIAL_MEDIA_MAX_BYTES, QQ_OFFICIAL_BOTS,
    ACCOUNTS, MONITOR_LIST, ENABLE_TRANSLATION,
    SKIP_PUBLISH_TYPES, MEDIA_TYPE_MAP,
    DAY_START_HOUR, NIGHT_START_HOUR, DAY_INTERVAL, NIGHT_INTERVAL,
    BACKTRACK_HOURS, ALERT_COOLDOWN_SECONDS,
    HTTP_SEMAPHORE_LIMIT, QQ_SEND_INTERVAL, TOKEN_REFRESH_BEFORE_SECONDS,
    CRED_DIR, TIME_RECORD_DIR, SENT_IDS_DIR,
    ERROR_LOG_FILE, RESPONSE_LOG_FILE, SENT_IDS_MAX,
    DEBUG_LOG_RESPONSE, DEBUG_LOG_QQ_PAYLOAD,
    GEMINI_API_KEY, GEMINI_MODELS, GEMINI_MIN_INTERVAL,
    TRANSLATE_MAX_LENGTH, TRANSLATE_TIMEOUT,
    BILIBILI_FULL_COOKIE, BILIBILI_BILI_JCT, BILIBILI_POST_API, BILIBILI_MIN_INTERVAL,
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

# Test 2: SKIP_PUBLISH_TYPES 是 set（向后兼容）
print("=== Test 2: 类型正确性 ===")
assert SKIP_PUBLISH_TYPES == {"birthday"}, f"SKIP_PUBLISH_TYPES mismatch: {SKIP_PUBLISH_TYPES}"
assert MEDIA_TYPE_MAP["video"] == "video"
assert MEDIA_TYPE_MAP["voice"] == "record"
assert len(DAY_INTERVAL) == 2
assert len(NIGHT_INTERVAL) == 2
print("✅ Test 2 通过\n")

# Test 3: yodel account has extra fields
print("=== Test 3: Yodel 账号结构 ===")
yodel = ACCOUNTS.get("yodel_graduated")
assert yodel is not None, "yodel_graduated account missing"
assert yodel["group_type"] == "hinatazaka46"
assert yodel["app_tag"] == "yodel"
assert yodel["api_base"] == "https://api.service.yodel-app.com"
assert yodel["web_origin"] == "https://service.yodel-app.com"
print("✅ Test 3 通过\n")

# Test 4: optional member-specific bilibili_cookie
print("=== Test 4: 成员专属 B站 Cookie（可选） ===")
members_with_bili_cookie = [m for m in MONITOR_LIST if "bilibili_cookie" in m]
if members_with_bili_cookie:
    for member in members_with_bili_cookie:
        assert isinstance(member["bilibili_cookie"], str), (
            f"bilibili_cookie should be str for member {member['m_id']}"
        )
        print(
            f"  {member['m_name']} (id={member['m_id']}) "
            f"bilibili_cookie (空=未设env): {bool(member['bilibili_cookie'])!r}"
        )
else:
    print("  当前 monitor_list 未配置成员专属 bilibili_cookie，跳过专项检查")
print("✅ Test 4 通过\n")

# Test 5: $ENV resolution
print("=== Test 5: $ENV 占位符解析 ===")
os.environ["TEST_DUMMY"] = "hello_world"
from config.config import _resolve_env
result = _resolve_env({"a": "$ENV:TEST_DUMMY", "b": [1, "$ENV:TEST_DUMMY"], "c": "plain"})
assert result["a"] == "hello_world", f"$ENV resolution failed: {result['a']}"
assert result["b"] == [1, "hello_world"], f"nested $ENV failed: {result['b']}"
assert result["c"] == "plain", f"non-$ENV string modified: {result['c']}"
del os.environ["TEST_DUMMY"]

# 未设置的环境变量 → 空字符串
result2 = _resolve_env({"x": "$ENV:NONEXISTENT_VAR_12345"})
assert result2["x"] == "", f"unset env should be empty: {result2['x']!r}"
print("✅ Test 5 通过\n")

# Test 6: reload() works
print("=== Test 6: 热重载 ===")
from config.config import reload, get
ok = reload()
assert ok, "reload() should succeed with unchanged config"
print("✅ Test 6 通过\n")

print("=" * 50)
print("🎉 全部测试通过！config.py facade 工作正常")
