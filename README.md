# nogizaka-message-push

坂道系（乃木坂46 / 日向坂46）成员消息监控与推送系统。

通过网页端获取 Token 和 Session，自动续期鉴权；轮询监听成员 Message；通过 NapCat/OneBot 推送到 QQ 群聊，同时支持 QQ 开放平台官方 Bot 单聊推送；可选同步至 Bilibili 动态。

## 功能

- **多账号管理** — 支持多个账号池，自动加载持久化凭证，首次运行从 `.env` 初始化
- **Token 自动续期** — 解码 JWT 检查 `exp`，剩余不足 5 分钟时主动刷新，API 返回 401 时触发被动刷新
- **多成员并行轮询** — 6 名成员并发拉取，日间 2~3 分钟/次，夜间 25~30 分钟/次自适应间隔
- **消息去重** — 基于消息 ID 的滑动窗口去重（每成员最多 500 条），O(1) 集合 + 有序列表
- **Gemini 翻译** — 日文消息自动翻译为中文，多模型级联容错（2.5-flash → 2.5-flash-lite → 2.5-pro），串行化限速
- **多通道 QQ 推送** — NapCat/OneBot HTTP 群聊推送 + QQ 开放平台官方 Bot 单聊推送，可独立开关
- **Bilibili 同步** — 可选将消息发布为 B 站文字动态，支持成员独立 Cookie
- **多媒体支持** — 图片、视频、语音消息完整转发（官方 Bot 支持媒体文件下载重传）
- **启动健康检查** — 启动时校验 NapCat 连接、QQ Bot access_token、账号凭证状态

## 快速开始

### 环境要求

- Python 3.8+
- Windows / Linux

### 安装

```bash
git clone https://github.com/TomisatoNao/nogizaka-message-push.git
cd nogizaka-message-push
pip install -r requirements.txt
```

### 配置

```bash
cp .env.example .env
```

编辑 `.env`，填入以下必要信息：

1. **Gemini API Key** — 用于日文翻译
2. **账号凭证** — 3 个坂道系账号的 JWT Token 和 Session Cookie（首次运行从 `.env` 读取，之后自动持久化）
3. **QQ 推送通道**（二选一或同时启用）：
   - NapCat/OneBot：设置 `ENABLE_NAPCAT_QQ=true` 和 `QQ_BOT_API`
   - QQ 官方 Bot：设置 `ENABLE_QQ_OFFICIAL_BOT=true` 及各 Bot 的 App ID / Secret / OpenID
4. **B 站 Cookie**（可选）— 如需同步 B 站动态

### 运行

```bash
python main.py
```

### 获取 QQ 用户 OpenID

如果使用 QQ 官方 Bot 推送，需要先获取目标用户的 OpenID：

```bash
python tools/get_qq_openid.py
```

按提示填入 Bot 的 App ID 和 Client Secret，连接 WebSocket 后请目标用户向 Bot 发送一条私聊消息，脚本会自动打印出 OpenID。

## 项目结构

```
nogizaka-message-push/
├── main.py                  # 入口（兼容启动器）
├── requirements.txt         # Python 依赖
├── .env.example             # 配置模板
├── .gitignore
├── config/
│   ├── config.py            # 全部常量和环境变量加载
│   └── credentials.py       # 凭证持久化、JWT 解码、Token 主动刷新
├── src/
│   ├── app.py               # 主循环：健康检查、依赖注入、轮询编排
│   ├── fetcher.py           # 核心拉取：API 轮询、消息过滤、分发调度
│   ├── dedup.py             # 消息 ID 去重（滑动窗口）
│   ├── translator.py        # Gemini 翻译（多模型容错、串行限速）
│   ├── notifier.py          # 多通道推送路由
│   ├── logger.py            # 日志系统（彩色终端 + 滚动文件）
│   └── platforms/
│       ├── napcat.py        # NapCat/OneBot HTTP 推送
│       ├── qq_official.py   # QQ 官方 Bot 单聊推送
│       └── bilibili.py      # B 站动态发布
├── tools/
│   └── get_qq_openid.py     # QQ Bot WebSocket 获取用户 OpenID
├── data/                    # 运行时数据（git-ignored）
│   ├── web_credentials/     # 持久化 JWT + Cookie
│   ├── sent_ids/            # 已发送消息 ID 去重记录
│   └── time_records/        # 各成员最后轮询时间戳
└── logs/                    # 运行日志（git-ignored）
```

## 架构

```
Member Message API          Gemini API            NapCat/OneBot
(nogizaka46.com /    <--   (translate)   -->   (QQ 群聊推送)
 hinatazaka46.com)            |                      |
       |                      |                QQ Official Bot
       v                      v               (QQ 单聊推送)
  [fetcher.py] --------> [translator.py]           |
       |                      |                     |
       |                 [notifier.py] <------------+
       |                      |
       v                      v
  [dedup.py]           [napcat.py] / [qq_official.py]
  [credentials.py]           |
       |                     v
  [data/]              [bilibili.py] (可选)
                        B 站动态 API
```

### 核心设计

- **依赖注入** — `fetcher`、`napcat`、`qq_official` 模块通过 `initialize()` 接收共享的 `httpx.AsyncClient` 和 `asyncio.Semaphore`，避免全局状态
- **串行化限速** — 翻译和 B 站发帖使用 `asyncio.Lock` + 时间检查，无论多少成员并发拉取，翻译和发帖始终串行
- **原子写入** — 时间记录、去重列表、凭证文件均采用「写入临时文件 + `os.replace`」模式，防止写入中断导致文件损坏
- **Token 生命周期** — 每轮轮询前解码 JWT 检查 `exp`，不足 300 秒则主动刷新；API 返回 401 时被动重试刷新后重试
- **NapCat 视频/语音分离** — 部分 NapCat 版本在混排图文消息时会吞掉文字，代码将视频/语音拆分为独立消息批次
- **容错语义** — NapCat 推送失败会阻止该成员的时间戳推进（下一轮重试），QQ 官方 Bot 失败仅记录日志（避免因限频导致群聊重复推送）

## 依赖

| 包 | 用途 |
|---|---|
| [httpx](https://www.python-httpx.org/) | 全异步 HTTP 客户端 |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | `.env` 环境变量加载 |
| [websockets](https://websockets.readthedocs.io/) | QQ Bot WebSocket 连接（仅 `tools/get_qq_openid.py`） |

## 监控成员

| 成员 | 所属团体 | m_id | 使用账号 |
|---|---|---|---|
| 冨里 奈央 | 乃木坂46 | 55 | nogizaka_main |
| 金村 美玖 | 日向坂46 | 34 | hinata_shared |
| 小坂 菜绪 | 日向坂46 | 36 | hinata_shared |
| 大野 愛実 | 日向坂46 | 84 | hinata_main |
| 片山 紗希 | 日向坂46 | 85 | hinata_shared |
| 佐藤 優羽 | 日向坂46 | 88 | hinata_shared |

## 配置参考

完整配置项见 `.env.example`，主要分类：

- **Gemini** — `GEMINI_API_KEY`
- **QQ 推送通道** — `ENABLE_NAPCAT_QQ`、`ENABLE_QQ_OFFICIAL_BOT`、`QQ_BOT_API`
- **QQ 官方 Bot** — `QQ_OFFICIAL_BOT{1,2}_{APP_ID,CLIENT_SECRET,TARGET_OPENID}`
- **Bilibili** — `BILIBILI_FULL_COOKIE`、`BILIBILI_BILI_JCT`、`MEMBER_85_BILIBILI_COOKIE`
- **账号凭证** — `ACCOUNT_{NOGIZAKA_MAIN,HINATA_SHARED,HINATA_MAIN}_{TOKEN,COOKIE}`

## License

MIT
