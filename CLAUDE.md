# CLAUDE.md — nogizaka-message-push

坂道系偶像团体（乃木坂46/日向坂46）成员消息监控推送系统。

## 架构概览

```
main.py → src/app.py (入口, 健康检查, 主循环)
  ├── config/config.py      (所有常量, MONITOR_LIST, 环境变量)
  ├── config/credentials.py (JWT Cookie 持久化, Token 刷新, 时间戳读写)
  ├── src/fetcher.py        (核心: 两阶段抓取+推送)
  ├── src/translator.py     (Gemini 翻译, 串行化限速)
  ├── src/notifier.py       (QQ 推送路由: NapCat → 官方Bot)
  ├── src/dedup.py          (已发送消息 ID 去重, 滑动窗口)
  ├── src/logger.py         (终端 + 轮转日志, response_debug.log)
  └── src/platforms/
      ├── napcat.py         (NapCat/OneBot HTTP 推送, 消息链构建)
      ├── qq_official.py    (QQ 官方 Bot, 多Bot, 媒体上传)
      └── bilibili.py       (B站动态发布)
```

## 数据流

```
MONITOR_LIST (config) → _fetch_member_messages (并发, Semaphore=3)
  → API 拉取 → 按 updated_at 排序 → 返回待推送数据
    → _push_member_messages (按 MONITOR_LIST 顺序串行, 每成员内逐条)
      → _handle_message: 翻译 → QQ推送 → B站 → 1.5s间隔 → 下一条
```

## 关键约定

- **Python 3.10+**，纯 asyncio，Windows 环境 (PowerShell 5.1)
- 凭证存 `data/web_credentials/`，git-ignored
- 时间戳存 `data/time_records/`，已发 ID 存 `data/sent_ids/`
- `config.py` 从 `.env` 加载环境变量
- 翻译仅在有假名字符时触发 (`_is_already_chinese` 检查 kana)
- 消息 push 阶段串行（同成员时间序），fetch 阶段并发
- `_handle_message` 内 push 失败 → 保留时间戳, 下轮重试

## 常用命令

```powershell
python main.py                          # 启动监控
python tools/get_qq_openid.py           # 获取 QQ 用户 OpenID
```

## 翻译模型优先级

Gemini 3.1 Flash Lite (免费) → 2.5 Flash → 2.5 Flash (fallback)，串行尝试，429 重试 2 次/模型。
