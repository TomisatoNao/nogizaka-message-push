# nogizaka-message-push

坂道系（乃木坂46 / 日向坂46）成员消息监控与推送系统。

支持**网页端 Cookie** 和**手机端 refresh_token** 两种认证方式，可按账号自由选择。自动续期鉴权；轮询监听成员 Message；通过 NapCat/OneBot 推送到 QQ 群聊，同时支持 QQ 开放平台官方 Bot 单聊推送；可选同步至 Bilibili 动态。

## 功能

- **双认证模式** — 网页端 Cookie 认证 + 手机端 refresh_token 认证，`config.json` 中按账号自由选择
- **多账号管理** — 支持多个账号池，自动加载持久化凭证，首次运行从 `.env` 初始化
- **Token 自动续期** — 解码 JWT 检查 `exp`，剩余不足 5 分钟时主动刷新；Web 端通过 Cookie 续期，移动端通过 refresh_token 续期
- **多成员并行轮询** — 成员并发拉取，日间 2~3 分钟/次，夜间 25~30 分钟/次自适应间隔，带 ±10% 随机抖动
- **消息去重** — 基于消息 ID 的滑动窗口去重（每成员最多 500 条），O(1) 集合 + 有序列表
- **Gemini 翻译** — 日文消息自动翻译为中文，多模型级联容错，串行化限速
- **多通道推送** — NapCat/OneBot QQ 群聊 + QQ 官方 Bot 单聊 + Telegram Bot，可独立开关；系统警报（Token 失效等）通过所有已启用通道发送
- **Bilibili 同步** — 可选将消息发布为 B 站文字动态，支持成员独立 Cookie
- **多媒体支持** — 图片、视频、语音消息完整转发（官方 Bot 支持媒体文件下载重传）
- **JST 时间显示** — 推送和 B 站动态中的消息时间统一使用日本標準時 (UTC+9)
- **自定义 API 域名** — 支持毕业生成员独立 API 域名（如 yodel），账号级配置 app_tag / api_base / web_origin
- **反反爬虫** — Header 仿真（Web Chrome / 移动 iOS）、随机间隔抖动、指数退避重试、成员随机轮询顺序
- **配置热重载（可选）** — 已提供 `config/watcher.py`，安装并接入 watchdog 后可自动重载；当前默认入口以启动时加载为主
- **启动健康检查** — 启动时校验 NapCat/TG Bot 连接、QQ Bot access_token、账号凭证状态
- **运行时状态摘要** — 每隔 N 轮自动输出通道成功率、Token 剩余时间、成员拉取/推送状态、分级错误报告，终端一眼判断系统健康度

## 快速开始

### 环境要求

- Python 3.10+
- Windows / Linux

### 安装

```bash
git clone https://github.com/TomisatoNao/nogizaka-message-push.git
cd nogizaka-message-push
pip install -r requirements.txt
```

### 配置

项目使用两个配置文件，职责分明：

| 文件 | 内容 |
|---|---|
| `.env` | **密钥和凭证**（敏感，不提交 Git）|
| `config/config.json` | **用户配置**（非敏感，可提交 Git）|

#### 1. `.env` — 密钥和凭证

```bash
cp .env.example .env
```

凭证通过命名约定自动匹配，**不再需要 `$ENV:VAR` 占位符**。你只需把值填到 `.env`：

```bash
# 对照 config.json 中 accounts 的 key：
#   "nogizaka_main"   → NOGIZAKA_MAIN_TOKEN + NOGIZAKA_MAIN_REFRESH_TOKEN
#   "nogizaka_shared" → NOGIZAKA_SHARED_TOKEN + NOGIZAKA_SHARED_COOKIE
#   "hinata_shared"   → HINATA_SHARED_TOKEN + HINATA_SHARED_COOKIE
#   "yodel_grad"      → YODEL_GRAD_TOKEN + YODEL_GRAD_COOKIE
```

| 类别 | 变量 | 说明 |
|---|---|---|
| Gemini | `GEMINI_API_KEY` | 翻译 API Key |
| Telegram Bot | `TG_BOT_TOKEN` | Bot Token（`@BotFather` 获取） |
| Web 账号 | `{KEY}_TOKEN`, `{KEY}_COOKIE` | JWT + 浏览器 Cookie |
| Mobile 账号 | `{KEY}_REFRESH_TOKEN`（必填）, `{KEY}_TOKEN`（可选） | refresh_token UUID + JWT（TOKEN 可为空，系统自动获取） |
| QQ 官方 Bot | `QQ_OFFICIAL_BOT{1,2}_{APP_ID,CLIENT_SECRET,TARGET_OPENID}` | 可选，不启用则留空 |
| B 站 | `BILIBILI_COOKIE` | 包含 `SESSDATA=xxx; bili_jct=yyy` |

#### 2. `config/config.json` — 用户配置

精简后 ~50 行，只包含你手动改的项：

- **channels** — 推送通道开关（`napcat` / `tg` / `qq_official`）
- **accounts** — 账号池，只需定义 `group` 和 `auth`（`"web"` 或 `"mobile"`），密钥自动从 `.env` 匹配
- **monitor** — 监控成员列表（`id` / `name` / `account` / `groups` / `tg`）
- **推送参数** — `day_interval`、`night_interval`、`sleep_hours`、`alert_cooldown`
- **可选覆盖** — `gemini_models`、`translate`、`qq_send_interval` 等

未写在 config.json 中的配置项（文件路径、超时、速率限制、调试开关等）使用内置默认值。完整默认值见 `config/config.py` 的 `_DEFAULTS` 字典。

### 获取成员 ID

使用 [nogizaka-monitor](https://github.com/TomisatoNao/nogizaka-monitor) 项目的 `list_members.py`，通过手机端 API 查询：

```bash
git clone https://github.com/TomisatoNao/nogizaka-monitor.git ../nogizaka-monitor
cd ../nogizaka-monitor
python list_members.py nogizaka    # 乃木坂46
python list_members.py hinatazaka  # 日向坂46
python list_members.py yodel       # yodel（毕业生）
```

也可以直接查阅下方速查表，手动填入 `m_id`。

### 前置条件

运行前确保至少一个推送通道已在本地启动：

- **NapCat** — [NapCat](https://github.com/NapNeko/NapCatQQ) 或 [Lagrange](https://github.com/LagrangeDev/Lagrange.Core) 连接 QQ 并开启 HTTP API（默认 `http://127.0.0.1:3000`）
- **TG Bot** — 通过 `@BotFather` 创建 Bot，把 Token 填入 `.env`

启动时会自动检查所有已启用通道的连通性。

### 运行

```bash
python main.py
```

启动后会先输出健康检查结果，然后进入轮询循环。终端输出示例：

```
22:00:03  [INFO] 🟢 NapCat QQ 连通正常
22:00:03  [INFO] 🟢 TG Bot 连通正常 (@nogizaka_push_bot)
22:00:05  [INFO] ✅ 冨里奈央 推送 2 条新消息
22:00:05  [INFO] 🔍 巡查完毕 [冨里奈央 · 賀喜遥香 · ...]
22:22:25  [INFO] 📊 [状态摘要 #10 · 运行 22m]
22:22:25  [INFO]   通道: napcat ✅ 10/10 | tg ✅ 10/10
22:22:25  [INFO]   Token: nogizaka_main 38min · yodel_grad 失效 🔴
```

### 常见操作

**加一个成员：** 在 `config.json` 的 `monitor` 数组末尾添加一项：
```json5
{ "id": "39", "name": "筒井 あやめ", "account": "nogizaka_shared", "groups": [752269366] }
```
热重载 (`config.reload()`) 或重启后生效。成员 ID 见下方速查表。

**换一个 Token：** 编辑 `.env` 中对应账号的 `{KEY}_TOKEN`，然后触发热重载或重启。

**只开 TG 不开 NapCat：** 在 `config.json` 中 `channels` 里设 `"napcat": false`，`"tg": true`。

**调轮询频率：** 修改 `config.json` 中 `day_interval` / `night_interval`（秒）。`sleep_hours` 控制休眠时段。

**添加一个新账号：** 在 `config.json` 的 `accounts` 中添加一项，然后在 `.env` 里按命名约定填入凭证：

```json5
// config.json
"accounts": {
    "my_new_account": { "group": "nogizaka46" }
}
```

```bash
# .env
MY_NEW_ACCOUNT_TOKEN=eyJ...
MY_NEW_ACCOUNT_COOKIE=session=xxx
```

如果是 mobile 账号（`"auth": "mobile"`）：`{KEY}_REFRESH_TOKEN` 必填，`{KEY}_TOKEN` 可选（留空则首次运行时自动通过 refresh_token 获取）。也可不配 `{KEY}_REFRESH_TOKEN`，使用全局 `NOGIZAKA_REFRESH_TOKEN` 作为 fallback。

**启用 QQ 官方 Bot：** 三步：

1. `config.json` 的 `channels` 中加 `"qq_official": true`
2. `.env` 中填 Bot 凭证：
   ```bash
   QQ_OFFICIAL_BOT1_APP_ID=你的AppID
   QQ_OFFICIAL_BOT1_CLIENT_SECRET=你的Secret
   QQ_OFFICIAL_BOT1_TARGET_OPENID=目标用户OpenID
   ```
3. 获取目标用户 OpenID（见下方「获取 QQ 用户 OpenID」）

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
├── .env.example             # 敏感配置模板
├── .gitignore
├── config/
│   ├── config.json          # 纯数据配置（非敏感项：账号、成员、轮询参数）
│   ├── config.schema.json   # config.json 的结构定义（自动校验）
│   ├── config.py            # 配置 facade：加载 JSON → 校验 → 暴露变量
│   ├── credentials.py       # 凭证管理：Web/移动端双模式 Token 刷新、Header 构建
│   └── watcher.py           # 配置文件热重载（watchdog 监控）
├── src/
│   ├── app.py               # 主循环：健康检查、依赖注入、轮询编排
│   ├── fetcher.py           # 核心拉取：API 轮询、消息过滤、分发调度
│   ├── dedup.py             # 消息 ID 去重（滑动窗口）
│   ├── health.py            # 运行时健康追踪：通道/Token/成员状态、定期摘要
│   ├── translator.py        # Gemini 翻译（多模型容错、串行限速）
│   ├── notifier.py          # 多通道推送路由 + 系统警报
│   ├── logger.py            # 日志系统（彩色终端 + 滚动文件）
│   ├── utils.py             # 公共工具：JST 时间转换、时段判断、速率限制器
│   └── platforms/
│       ├── napcat.py        # NapCat/OneBot HTTP 推送
│       ├── qq_official.py   # QQ 官方 Bot 单聊推送
│       ├── tgbot.py         # Telegram Bot 推送
│       └── bilibili.py      # B 站动态发布
├── tools/
│   └── get_qq_openid.py     # QQ Bot WebSocket 获取用户 OpenID
├── data/                    # 运行时数据（git-ignored）
│   ├── web_credentials/     # 持久化 Token + Cookie / refresh_token
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

- **双认证模式** — `accounts` 中每个账号可设 `auth_method: "web"`（Cookie + Bearer）或 `"mobile"`（refresh_token → JWT）。Web 端使用 Chrome 头仿真，移动端使用 iOS 头仿真，按 `is_mobile` 在 URL / Header / 401 三处分发
- **依赖注入** — `fetcher`、`napcat`、`qq_official` 模块通过 `initialize()` 接收共享的 `httpx.AsyncClient` 和 `asyncio.Semaphore`，避免全局状态
- **串行化限速** — 翻译和 B 站发帖使用 `asyncio.Lock` + 时间检查，无论多少成员并发拉取，翻译和发帖始终串行
- **原子写入** — 时间记录、去重列表、凭证文件均采用「写入临时文件 + `os.replace`」模式，防止写入中断导致文件损坏
- **Token 生命周期** — 启动时为移动端账号执行初始刷新；每轮轮询前解码 JWT 检查 `exp`，不足 300 秒则主动刷新；API 返回 401 时触发被动刷新后重试
- **反爬虫** — 轮询间隔 ±10% 随机抖动；消息发送间隔 1.2~2.0s 随机；网络错误指数退避重试（base × 2^attempt + jitter）；成员处理顺序每轮 shuffle
- **NapCat 视频/语音分离** — 部分 NapCat 版本在混排图文消息时会吞掉文字，代码将视频/语音拆分为独立消息批次
- **容错语义** — NapCat 推送失败会阻止该成员的时间戳推进（下一轮重试），QQ 官方 Bot 和 TG Bot 失败仅记录日志（避免因限频导致群聊重复推送）
- **系统警报多通道覆盖** — Token 刷新失败时通过所有已启用通道（NapCat + QQ 官方 Bot + TG）发送告警，带冷却保护防止刷屏
- **健康状态追踪** — `health.py` 纯内存追踪每轮通道成功率、Token 剩余时间、成员拉取/推送状态；每 N 轮自动输出分级摘要（TRANSIENT 临时错误 vs PERSISTENT 需人工介入）

## 依赖

| 包 | 用途 |
|---|---|
| [httpx](https://www.python-httpx.org/) | 全异步 HTTP 客户端 |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | `.env` 环境变量加载 |
| [json5](https://github.com/dpranke/pyjson5) | 解析 `config.json` 的 JSONC 格式（支持注释） |
| [jsonschema](https://github.com/python-jsonschema/jsonschema) | `config.json` 结构校验 |
| [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) | Telegram Bot SDK（仅启用 TG 推送时需要） |
| [websockets](https://websockets.readthedocs.io/) | QQ Bot WebSocket 连接（仅 `tools/get_qq_openid.py`） |

## 监控成员

> 当前激活的监控列表（`config.json` 中的 `monitor`）

| 成员 | 所属团体 | m_id | 使用账号 |
|---|---|---|---|
| 冨里 奈央 | 乃木坂46 | 55 | nogizaka_main |
| 賀喜 遥香 | 乃木坂46 | 30 | nogizaka_shared |
| 金村 美玖 | 日向坂46 | 34 | hinata_shared |
| 小坂 菜绪 | 日向坂46 | 36 | hinata_shared |
| 大野 愛実 | 日向坂46 | 84 | hinata_shared |
| 佐藤 優羽 | 日向坂46 | 88 | hinata_shared |
| 松田 好花 | 日向坂46（毕业） | 77 | yodel_graduated |
| 松田好花 Staff | 日向坂46（毕业） | 81 | yodel_graduated |
| 丹生 明里 | 日向坂46（毕业） | 47 | yodel_graduated |

### 乃木坂46 现役成员 ID 速查

> 以下为通过手机端 API 拉取的全部现役成员（`state=open`），方便快速添加到 `monitor`。
> 成员 ID 可能随运营调整变化，以实际 API 返回为准。

| m_id | 成员 | 期别 |
|------|------|------|
| 17 | 伊藤 理々杏 | 3期 |
| 18 | 岩本 蓮加 | 3期 |
| 19 | 梅澤 美波 | 3期 |
| 27 | 吉田 綾乃クリスティー | 3期 |
| 29 | 遠藤 さくら | 4期 |
| 30 | 賀喜 遥香 | 4期 |
| 32 | 金川 紗耶 | 4期 |
| 34 | 黒見 明香 | 4期 |
| 36 | 柴田 柚菜 | 4期 |
| 38 | 田村 真佑 | 4期 |
| 39 | 筒井 あやめ | 4期 |
| 41 | 林 瑠奈 | 4期 |
| 44 | 弓木 奈於 | 4期 |
| 46 | 五百城 茉央 | 5期 |
| 47 | 池田 瑛紗 | 5期 |
| 48 | 一ノ瀬 美空 | 5期 |
| 49 | 井上 和 | 5期 |
| 50 | 岡本 姫奈 | 5期 |
| 51 | 小川 彩 | 5期 |
| 52 | 奥田 いろは | 5期 |
| 53 | 川﨑 桜 | 5期 |
| 54 | 菅原 咲月 | 5期 |
| 55 | 冨里 奈央 | 5期 |
| 56 | 中西 アルノ | 5期 |
| 60 | 愛宕 心響 | 6期 |
| 61 | 大越 ひなの | 6期 |
| 62 | 海邉 朱莉 | 6期 |
| 63 | 川端 晃菜 | 6期 |
| 64 | 鈴木 佑捺 | 6期 |
| 65 | 瀬戸口 心月 | 6期 |
| 66 | 長嶋 凛桜 | 6期 |
| 67 | 増田 三莉音 | 6期 |
| 68 | 森平 麗心 | 6期 |
| 69 | 矢田 萌華 | 6期 |
| 74 | 小津 玲奈 | 6期 |

## 配置参考

| 文件 | 职责 |
|---|---|
| `.env` | 密钥和凭证（模板见 `.env.example`，~24 行） |
| `config/config.json` | 用户配置（schema 见 `config/config.schema.json`，~50 行） |
| `config/config.py` `_DEFAULTS` | 内置默认值（文件路径、超时、速率限制、调试开关等，一般无需修改） |

加载顺序：内置默认值 → `config.json` 覆盖 → `.env` 补充密钥。账号凭证通过命名约定自动从 `.env` 匹配（`foo_bar` → `FOO_BAR_TOKEN` / `FOO_BAR_COOKIE`），无需在 config.json 中写 `$ENV:VAR` 占位符。

- `health_summary_interval` — 默认 10 轮输出一次状态摘要
- `alert_cooldown` — 默认 3600s，防止 Token 告警刷屏

## License

MIT
