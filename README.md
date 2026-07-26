# nogizaka-message-push

坂道系（乃木坂46 / 日向坂46）成员消息监控与推送系统。

支持**网页端 Cookie** 和**手机端 refresh_token** 两种认证方式，可按账号自由选择。自动续期鉴权；轮询监听成员 Message；通过 NapCat/OneBot 推送到 QQ 群聊，同时支持 QQ 开放平台官方 Bot 单聊推送和 Telegram Bot 推送。

## 功能

- **双认证模式** — 网页端 Cookie 认证 + 手机端 refresh_token 认证，`config.json` 中按账号自由选择
- **多账号管理** — 支持多个账号池，自动加载持久化凭证，首次运行从 `.env` 初始化
- **Token 自动续期** — 解码 JWT 检查 `exp`，剩余不足 5 分钟时主动刷新；Web 端通过 Cookie 续期，移动端通过 refresh_token 续期
- **多成员并行轮询** — 成员并发拉取，日间 2~3 分钟/次，夜间 25~30 分钟/次自适应间隔，带 ±10% 随机抖动
- **消息去重** — 基于消息 ID 的滑动窗口去重（每成员最多 500 条），O(1) 集合 + 有序列表
- **Gemini 翻译** — 日文消息自动翻译为中文，多模型级联容错，串行化限速
- **多通道推送** — NapCat/OneBot QQ 群聊 + QQ 官方 Bot 单聊 + Telegram Bot，可独立开关；系统警报（Token 失效等）通过所有已启用通道发送
- **多媒体支持** — 图片、视频、语音消息完整转发（官方 Bot 支持媒体文件下载重传）
- **JST 时间显示** — 推送消息中的时间统一使用日本標準時 (UTC+9)
- **自定义 API 域名** — 支持毕业生成员独立 API 域名（如 yodel），账号级配置 app_tag / api_base / web_origin
- **反反爬虫** — Header 仿真（Web Chrome / 移动 iOS）、随机间隔抖动、指数退避重试、成员随机轮询顺序
- **配置热重载（可选）** — 已提供 `config/watcher.py`，安装并接入 watchdog 后可自动重载；当前默认入口以启动时加载为主
- **网页管理端** — 浏览器完成日常配置和运维：状态总览（Token 剩余 / 通道成功率 / 巡查倒计时）、通道 / 账号池 / 监控成员 / 官方 Bot 增删改、从 API 拉成员列表点选添加、凭证直接填写（写入 `.env` 并自动轮换）、实时日志、立即巡查、配置历史回滚、一键重启；保存即校验 + 热重载（详见下方「网页管理端」）
- **消息归档** — 抓到的消息（含译文和图片/视频/语音文件）按 成员/年/月 自动落地本地归档，`/archive` 页面按聊天时间线浏览：月份导航、类型筛选、图片灯箱、视频拖进度；历史消息用回填工具补齐（详见下方「消息归档」）
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

#### 2. `config/config.json` — 用户配置

精简后 ~50 行，只包含你手动改的项：

- **channels** — 推送通道开关（`napcat` / `tg` / `qq_official`）
- **accounts** — 账号池，只需定义 `group` 和 `auth`（`"web"` 或 `"mobile"`），密钥自动从 `.env` 匹配
- **monitor** — 监控成员列表（`id` / `name` / `account` / `groups` / `tg`）
- **推送参数** — `day_interval`、`night_interval`、`sleep_hours`、`alert_cooldown`
- **可选覆盖** — `gemini_models`、`translate`、`qq_send_interval` 等

未写在 config.json 中的配置项（文件路径、超时、速率限制、调试开关等）使用内置默认值。完整默认值见 `config/config.py` 的 `_DEFAULTS` 字典。

#### 3. 从哪获取凭证？

| 凭证 | 来源 |
|---|---|
| `TOKEN`（JWT） | 浏览器 DevTools → Network → 任意 API 请求 → Request Headers 中 `Authorization: Bearer eyJ...` |
| `COOKIE`（Web 端） | 浏览器 DevTools → Application → Cookies → 复制 `session` 等关键字段 |
| `REFRESH_TOKEN`（Mobile） | 手机抓包（如 Charles / Proxyman）→ 拦截 `update_token` 请求 → Body 中的 `refresh_token` UUID |
| `GEMINI_API_KEY` | [Google AI Studio](https://aistudio.google.com/apikey) |
| `TG_BOT_TOKEN` | Telegram 找 `@BotFather` → `/newbot` |

**monitor 字段说明：**

| 字段 | 含义 | 示例 |
|---|---|---|
| `id` | 成员 ID（`m_id`），见下方速查表 | `"55"` |
| `name` | 成员名称（推送显示用） | `"冨里奈央"` |
| `account` | 使用哪个账号轮询（对应 `accounts` 的 key） | `"nogizaka_main"` |
| `groups` | 推送到哪些 QQ 群（NapCat 群号）。只推 TG 时可省略 | `[533072575]` |
| `tg` | 可选的 TG 频道/群 chat_id（Bot 需是管理员） | `"-1004219007326"` |

`groups`、`tg`、**已启用的 QQ 官方 Bot** 三者至少要有一个能覆盖该成员，否则启动健康检查会报「没有任何可用推送目标」。官方 Bot 推的是全局 `TARGET_OPENID`、不区分成员，所以只要它可用，成员就不需要再配 `groups` 或 `tg`。

### 获取成员 ID

配好 `.env` 和 `config.json` 的 accounts 之后，用自带的工具查询：

```bash
python tools/list_members.py                  # 列出所有账号能看到的成员
python tools/list_members.py nogizaka_main    # 只列指定账号（可传多个）
```

它直接复用项目的账号池和凭证，不需要额外配置，输出形如：

```
▸ nogizaka_main (nogizaka46 · mobile · https://api.n46.glastonr.net)

  ── 5期生 ──
    [ 55] 🟢 冨里 奈央
    [ 56] 🟢 中西 アルノ

  共 38 项，其中 35 个在籍（🟢 open）
  config.json 的 monitor 里这样写：
    { "id": "55", "name": "冨里 奈央", "account": "nogizaka_main", "groups": [你的QQ群号] }
```

🟢 = 在籍（`state=open`），⚫ = 已毕业/关闭。Token 剩余时间不足时会自动续期并写回 `data/web_credentials/`，跑这个工具不会影响主程序的凭证；续期失败也只打印在本地，不会往 QQ 群或 TG 频道发告警。

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
22:22:25  [INFO]   通道: napcat ✅ 14/14 | tg ✅ 14/14
22:22:25  [INFO]   成员: 9/9 拉取正常 ✅ · 9/9 推送正常 ✅
22:22:25  [INFO]   Token: nogizaka_main 38min · yodel_grad 失效 🔴
```

通道计数是**自上次摘要以来**的发送成功/总数（每输出一次摘要就清零），一轮里推送多条消息会累加多次。

### 网页管理端

主程序启动后（`config.json` 中 `web_admin.enabled` 为 `true` 时），浏览器打开：

```
http://127.0.0.1:8787/
```

六个标签页覆盖日常配置和运维操作：

| 标签页 | 能做什么 |
|---|---|
| 状态 | 运行总览（默认页）：巡查轮次、下次巡查倒计时、各账号 Token 实时剩余、通道成功率、成员拉取/推送状态、近期错误；「⏩ 立即巡查」按钮跳过等待立刻跑一轮（休眠时段也能唤醒） |
| 基本设置 | 推送通道开关、NapCat API 地址、QQ 官方 Bot 增删改、轮询/休眠节奏、翻译参数、TG Token / Gemini Key 填写 |
| 账号池 | 增删改账号（团体 / 登录方式 / yodel 自定义域名），凭证状态展示 + 「填凭证」直接填写 |
| 监控成员 | 表格内直接增删改成员；「📋 从账号拉取成员列表」直接从官方 API 拉全团成员点选添加，不用手查 ID |
| 日志 | 实时日志（内存环 500 条，2 秒增量刷新，含 DEBUG 级）+ error / response 日志文件尾部查看，内容自动脱敏 |
| 高级（JSON） | 整份配置的 JSON 编辑 + **历史版本回滚**（每次保存前自动快照，保留最近 10 份，恢复本身也可撤销） |

点「保存并热重载」后，服务端按 `config.schema.json` 校验（外加账号引用完整性检查），校验通过才原子写回 `config.json` 并立即热重载 —— 大多数修改**无需重启**。

**凭证也可以直接在网页填**（账号 Token / Cookie / refresh_token、官方 Bot 的 Client Secret、Gemini API Key、TG Bot Token）：值通过 `POST /api/secrets` 写入 `.env`（与手动编辑同一存放处，白名单变量名校验），**只进不出** —— 接口只回报有/无，绝不回显值。填账号凭证时会自动执行完整轮换（写 `.env` → 删除旧的磁盘凭证 → 热重载重建），所以**立即生效、无需重启**；例外是 TG Bot Token（启动时创建 Bot 实例，改完需重启）。

**网页重启主程序**：右上角「⟳ 重启主程序」按钮触发优雅停机（与 SIGTERM 相同的清理流程），随后用 `os.execv` 原地拉起全新进程 —— 同 PID 自替换，`.env` 和所有模块状态完全重新加载，docker / systemd 下同样适用。页面会自动等待服务恢复。适用场景：改了 TG Bot Token、手动编辑了 `.env`、更新了代码。仅内嵌模式可用（独立运行 `python -m src.webui` 时按钮自动隐藏）。

注意事项：

- **新账号**：先在账号池添加并「保存并热重载」，再点「填凭证」。
- **保存会重新生成 config.json**：使用标准分区注释，手写的自定义注释会丢失。
- **安全**：默认只监听 `127.0.0.1`，接口是明文 HTTP —— 如需局域网访问，先在 `.env` 设置 `WEB_ADMIN_TOKEN`（页面首次访问时会提示输入），再把 `web_admin.host` 改成 `0.0.0.0`，且仅建议在可信内网使用；`WEB_ADMIN_TOKEN` 本身不允许通过网页修改。
- 主程序没跑时也可以单独起管理端：`python -m src.webui`（此时填写的凭证在主程序下次启动时生效）。

### 消息归档

开启后（`config.json` 的 `archive.enabled`，默认已开启），主程序每抓到一条新消息就自动归档到本地——**推送归推送，档案归档案**，退订了、App 关服了，消息还在你硬盘上：

```
data/archive/{成员名}/{YYYY}/{MM}/
    messages.json     # 该月全部消息（原文 + _translation 译文 + 本地媒体路径）
    images/ videos/ audio/
```

- **浏览**：管理端右上角「📚 消息归档」或直接开 http://127.0.0.1:8787/archive ——多成员切换、月份导航、类型筛选（文字/图片/视频/语音）、日文原文与中文译文对照、图片灯箱、视频/语音在线播放（支持拖进度）
- **历史回填**：实时归档只覆盖开启之后的消息，历史用工具补：
  ```bash
  python tools/backfill_archive.py                   # 回填所有监控成员的全部历史
  python tools/backfill_archive.py 冨里奈央 --from 2023-01-01
  ```
  断点续传（进度存 `data/archive_progress.json`），已归档自动跳过，媒体下载失败的会重试。
- **写入语义**：按消息 id 幂等合并、原子写；先落 JSON 再补媒体文件，进程中断最多丢媒体不丢消息
- 媒体文件较占空间（参考：单成员三年约 3-4 GB），`archive.media` 设为 `false` 可只存文字

### 账号系统（登录与权限）

默认关闭（本机访问无需登录）。开启后管理端需要登录，且分两种角色：

| 角色 | 权限 |
|---|---|
| `admin` | 管理端全部功能（配置 / 凭证 / 日志 / 重启）+ 归档 |
| `viewer` | 只能访问归档查看器 `/archive` |

**启用步骤**：

1. 创建第一个管理员（密码交互式输入，不进命令行历史，存储为 scrypt 加盐哈希）：
   ```bash
   python tools/manage_users.py add 你的用户名              # admin
   python tools/manage_users.py add 朋友的用户名 --viewer    # 只能看归档
   python tools/manage_users.py list / passwd / role / del
   ```
2. `config.json` 里打开：
   ```json5
   "auth": { "enabled": true, "archive_public": false, "session_hours": 12 }
   ```
   `archive_public: true` 时归档页对所有人开放（无需登录），管理端仍受保护。
3. 重启主程序，访问时会跳转到 `/login`。

**安全说明**：

- 密码用 **scrypt 加盐哈希**存 `data/users.json`（文件权限 600，git-ignored），永不存明文、不可逆
- 会话是随机 token + **HttpOnly / SameSite=Strict** cookie，仅存进程内存（重启即失效），支持滑动续期和登出；改密码会立即踢掉该用户所有会话
- 登录失败按 IP 限流（10 分钟内 5 次 → 锁定 15 分钟），密码校验用常时比较，用户不存在时也走一次哈希（防时序探测）
- 写请求校验 `Origin`（配合 SameSite cookie 防 CSRF），绑定回环地址时校验 `Host`（防 DNS rebinding）
- `WEB_ADMIN_TOKEN` 继续可用，作为脚本 / 自动化的 API 通道（等价 admin 身份）
- ⚠️ 服务仍是**明文 HTTP**：局域网使用请挂 TLS 反代（如 Caddy），否则密码和会话 cookie 在链路上是明文

### 每日摘要与开机自启

**每日运行摘要**（`config.json` 的 `daily_summary`，默认每天 JST 23:00）：通过已启用的推送通道发一条当日报告——各成员今日消息数、巡查轮次、Token 状态、待处理错误。它同时是**反向监控（死人开关）**：系统挂了不会有报错通知，但"今天没收到摘要"本身就是告警。

**开机自启 / 崩溃自拉起**（Windows）：

```powershell
powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1            # 安装
powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1 -Status    # 状态
powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1 -Uninstall # 卸载
```

注册一个计划任务：登录时自动启动 `python main.py`（后台无窗口运行），进程崩溃后 1 分钟自动重启（最多连续 10 次）。日志照常写 `logs/`，管理端照常在 http://127.0.0.1:8787/ 。

### 常见操作

**加一个成员：** 在 `config.json` 的 `monitor` 数组末尾添加一项：
```json5
{ "id": "39", "name": "筒井 あやめ", "account": "nogizaka_shared", "groups": [752269366] }
```
热重载 (`config.reload()`) 或重启后生效。成员 ID 见下方速查表。

**换一个 Token / Cookie：** 最简单的方式是网页管理端「账号池」→「填凭证」，它会自动完成下述全部步骤并热重载。手动操作的话：磁盘凭证的优先级**高于** `.env`（Token 会自动续期，磁盘上的值才是最新的），所以光改 `.env` 不会生效。正确步骤：

1. 编辑 `.env` 中对应账号的 `{KEY}_TOKEN` / `{KEY}_COOKIE` / `{KEY}_REFRESH_TOKEN`
2. 删除该账号的持久化凭证：`data/web_credentials/{account}.json`
3. 重启（`.env` 只在进程启动时读取一次，热重载读不到新值）

如果只做了第 1 步，下次启动会看到「⚠️ xxx 的 .env 凭证已修改，但磁盘凭证优先」的提醒。

**只开 TG 不开 NapCat：** 在 `config.json` 中 `channels` 里设 `"napcat": false`，`"tg": true`。此时成员可以只配 `tg` 而不写 `groups`：

```json5
{ "id": "39", "name": "筒井 あやめ", "account": "nogizaka_shared", "tg": "-1004219007326" }
```

**调轮询频率：** 修改 `config.json` 中 `day_interval` / `night_interval`（秒）。`sleep_hours` 控制休眠时段。

**添加一个新账号：** 最简单的方式是网页管理端：「账号池」→「添加账号」→「保存并热重载」→「填凭证」，全程无需重启。手动操作的话：在 `config.json` 的 `accounts` 中添加一项，然后在 `.env` 里按命名约定填入凭证：

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

⚠️ **手动改 `.env` 新增账号必须重启**：`.env` 只在进程启动时加载一次，热重载读不到新增的凭证变量。（热重载只能识别 `config.json` 里新增的、且磁盘上已有凭证文件的账号。）通过网页「填凭证」则没有这个限制——它会同步进程环境变量并立即轮换。

**启用 QQ 官方 Bot：** 三步（Bot 数量不限）：

1. `config.json` 的 `channels` 中设 `"qq_official": true`，并声明 Bot（网页管理端「基本设置」里也能直接加）：
   ```json5
   "qq_official_bots": [
       { "name": "qq_official_bot1", "app_id": "你的AppID", "target_openid": "目标用户OpenID" }
   ]
   ```
2. `.env` 中填密钥（变量名 = Bot 名称大写 + `_CLIENT_SECRET`）：
   ```bash
   QQ_OFFICIAL_BOT1_CLIENT_SECRET=你的Secret
   ```
3. 获取目标用户 OpenID（见下方「获取 QQ 用户 OpenID」）

`app_id` / `target_openid` 也可以不写在 config.json，改放 `.env` 的 `Bot名称大写_APP_ID` / `_TARGET_OPENID`。旧配置兼容：`config.json` 未声明 `qq_official_bots` 时，自动扫描 `.env` 的 `QQ_OFFICIAL_BOT{1..20}_*` 编号槽位。

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
│   ├── constants.py         # 跨模块消息结构常量（翻译分隔线、消息段角色标记）
│   ├── dedup.py             # 消息 ID 去重（滑动窗口）
│   ├── health.py            # 运行时健康追踪：通道/Token/成员状态、定期摘要
│   ├── translator.py        # Gemini 翻译（多模型容错、串行限速）
│   ├── notifier.py          # 多通道推送路由 + 系统警报
│   ├── logger.py            # 日志系统（彩色终端 + 滚动文件）
│   ├── utils.py             # 公共工具：JST 时间转换、时段判断、速率限制器
│   ├── webui.py             # 网页管理端：配置编辑 / 凭证写入 / 状态 / 日志 / 历史回滚 / 重启
│   ├── auth.py              # 账号系统：scrypt 密码哈希、用户库、会话、登录限流
│   ├── member_directory.py  # 成员目录拉取（/v2/groups），list_members 工具与网页端共用
│   ├── webui_static/
│   │   └── index.html       # 管理页面（零依赖单页应用）
│   └── platforms/
│       ├── napcat.py        # NapCat/OneBot HTTP 推送
│       ├── qq_official.py   # QQ 官方 Bot 单聊推送
│       └── tgbot.py         # Telegram Bot 推送
├── tools/
│   ├── list_members.py      # 列出账号可监控的成员及 m_id
│   ├── get_qq_openid.py     # QQ Bot WebSocket 获取用户 OpenID
│   └── test_models.py       # Gemini 模型序列连通性 + 响应结构诊断
├── tests/
│   ├── test_config_load.py  # config.json → config.py 加载校验
│   ├── test_units.py        # 时间解析 / 日志截断 / HTML 转义 / 消息链提取
│   └── test_webui.py        # 网页管理端：序列化往返 / 校验 / HTTP 端点
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
       |                      |                Telegram Bot
       |                 [notifier.py] <------------+
       |                      |
       v                      v
  [dedup.py]           [napcat.py] / [qq_official.py] / [tgbot.py]
  [credentials.py]
       |
       v
  [data/]
```

### 核心设计

- **双认证模式** — `accounts` 中每个账号可设 `auth_method: "web"`（Cookie + Bearer）或 `"mobile"`（refresh_token → JWT）。Web 端使用 Chrome 头仿真，移动端使用 iOS 头仿真，按 `is_mobile` 在 URL / Header / 401 三处分发
- **依赖注入** — `fetcher`、`napcat`、`qq_official` 模块通过 `initialize()` 接收共享的 `httpx.AsyncClient` 和 `asyncio.Semaphore`，避免全局状态
- **串行化限速** — 翻译使用 `asyncio.Lock` + 时间检查，无论多少成员并发拉取，翻译请求始终串行
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
| [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) | Telegram Bot SDK（已在 `requirements.txt` 中；不用 TG 推送可以不装，缺失时只会禁用该通道） |
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
| 松田 好花 | 日向坂46（毕业） | 77 | yodel_grad |
| 松田好花 Staff | 日向坂46（毕业） | 81 | yodel_grad |
| 丹生 明里 | 日向坂46（毕业） | 47 | yodel_grad |

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
| `.env` | 密钥和凭证（模板见 `.env.example`；也可通过网页管理端「填凭证」写入） |
| `config/config.json` | 用户配置（schema 见 `config/config.schema.json`，~50 行） |
| `config/config.py` `_DEFAULTS` | 内置默认值（文件路径、超时、速率限制、调试开关等，一般无需修改） |

加载顺序：内置默认值 → `config.json` 覆盖 → `.env` 补充密钥。账号凭证通过命名约定自动从 `.env` 匹配（`foo_bar` → `FOO_BAR_TOKEN` / `FOO_BAR_COOKIE`），无需在 config.json 中写 `$ENV:VAR` 占位符。

- `health_summary_interval` — 默认 10 轮输出一次状态摘要
- `alert_cooldown` — 默认 3600s，防止 Token 告警刷屏

## License

MIT
