import config.config as cfg
from src.platforms.tgbot import TGBot
from src.platforms.qq_official import QQOfficialBot


def test_normalize_napcat_routes_remark():
    raw_cfg = {
        "version": 2,
        "channels": {"napcat": True},
        "napcat_routes": [
            {"group_id": 123456},
            {"group_id": 789012, "remark": "乃木坂主群"}
        ],
        "monitor": []
    }
    
    normalized = cfg._normalize_config(raw_cfg)
    routes = normalized.get("napcat_routes", [])
    assert len(routes) == 2
    assert routes[0]["group_id"] == 123456
    assert routes[0]["remark"] == ""
    assert routes[1]["group_id"] == 789012
    assert routes[1]["remark"] == "乃木坂主群"


def test_tg_bots_remark_normalization():
    raw_cfg = {
        "enable_tg_bot": True,
        "tg_bots": [
            {"name": "tg_bot1", "target_chat": "-100123"},
            {"name": "tg_bot2", "target_chat": "-100456", "remark": "5期生频道"}
        ]
    }
    
    built = cfg._build_tg_bots(raw_cfg)
    bots = built.get("tg_bots", [])
    assert len(bots) == 2
    assert bots[0]["remark"] == ""
    assert bots[1]["remark"] == "5期生频道"


def test_qq_official_bots_remark_normalization():
    raw_cfg = {
        "enable_qq_official_bot": True,
        "qq_official_bots": [
            {"name": "official_bot1", "app_id": "102000"},
            {"name": "official_bot2", "app_id": "102001", "remark": "官方测试群"}
        ]
    }
    
    built = cfg._build_qq_official_bots(raw_cfg)
    bots = built.get("qq_official_bots", [])
    assert len(bots) == 2
    assert bots[0]["remark"] == ""
    assert bots[1]["remark"] == "官方测试群"


def test_tgbot_remark_initialization():
    bot = TGBot(
        name="test_bot",
        token="123456:ABC-DEF",
        target_chat="-100123",
        remark="测试TG备注"
    )
    assert bot.name == "test_bot"
    assert bot.remark == "测试TG备注"
    assert bot.target_chat == "-100123"


def test_qq_official_bot_remark_initialization():
    bot = QQOfficialBot(
        name="test_qq_bot",
        app_id="10203040",
        client_secret="sec123",
        target_openid="openid123",
        group_openid="groupid123",
        remark="测试QQ备注"
    )
    assert bot.name == "test_qq_bot"
    assert bot.remark == "测试QQ备注"
    assert bot.app_id == "10203040"
