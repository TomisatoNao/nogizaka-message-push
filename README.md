# nogizaka-message-push

坂道系（乃木坂46 / 日向坂46）成员付费信息的**监控、推送与归档**系统。

自动轮询成员 Message，翻译成中文，推送到 QQ 群 / Telegram / QQ 官方 Bot，并把每条消息连同图片视频**永久归档到本地**——退订了、App 关服了，档案还在你硬盘上。日常运维全部在浏览器里完成。

```
成员发消息  →  抓取  →  翻译  →  推送到你的 QQ / TG
                 └────→  归档到本地（原文 + 译文 + 媒体）→  网页浏览 / 搜索
```

---

## 目录

| 章节 | 内容 |
|---|---|
| [1. 快速开始](#1-快速开始) | 从零到跑起来 |
| [2. 核心概念](#2-核心概念) | 账号 / 成员 / 通道的关系，必读 |
| [3. 日常操作](#3-日常操作) | 加成员、换凭证、调频率……按任务查 |
| [4. 网页管理端](#4-网页管理端) | 六个页签能做什么 |
| [5. 消息归档](#5-消息归档) | 浏览、搜索、回填历史 |
| [6. 账号与权限](#6-账号与权限) | 登录、给朋友开访客号 |
| [7. 部署与运维](#7-部署与运维) | 长期运行、监控、备份 |
| [8. 故障排查](#8-故障排查) | 症状 → 原因 → 解决 |
| [9. 参考](#9-参考) | 配置项、命令、结构、成员 ID 表 |

---

## 1. 快速开始

### 1.1 环境与安装

需要 **Python 3.10+**（Windows / Linux 均可）。

```bash
git clone https://github.com/TomisatoNao/nogizaka-message-push.git
cd nogizaka-message-push
pip install -r requirements.txt
cp .env.example .env        # Windows: copy .env.example .env
```

### 1.2 准备一个推送通道

至少要有一个地方接收消息，先准备好其中一个：

| 通道 | 准备工作 |
|---|---|
| **Telegram**（最省事） | 找 [@BotFather](https://t.me/BotFather) 发 `/newbot` 建一个 Bot，拿到 Token；把 Bot 拉进你的频道并设为**管理员** |
| **QQ 群** | 本机跑起 [NapCat](https://github.com/NapNeko/NapCatQQ) 或 [Lagrange](https://github.com/LagrangeDev/Lagrange.Core)，开启 HTTP API（默认 `http://127.0.0.1:3000`） |
| **QQ 官方 Bot** | 见 [3.6 启用 QQ 官方 Bot](#36-启用-qq-官方-bot) |

### 1.3 填最小配置

编辑 `.env`，只填这两项就能起步（其余留空）：

```bash
TG_BOT_TOKEN=123456:ABC-DEF          # 用 TG 推送才需要
GEMINI_API_KEY=AIza...               # 中文翻译，去 https://aistudio.google.com/apikey 免费申请
```

编辑 `config/config.json`，把账号和要监控的成员改成你自己的：

```json5
{
  "channels": { "napcat": false, "tg": true, "qq_official": false },

  "accounts": {
    "nogizaka_main": { "group": "nogizaka46" }     // 账号 ID 随便起，小写下划线
  },

  "monitor": [
    { "id": "55", "name": "冨里奈央", "account": "nogizaka_main", "tg": "-1004219007326" }
  ]
}
```

> 成员 `id` 怎么查？先跑起来，在网页管理端「监控成员 → 📋 从账号拉取成员列表」里点选即可；也可查 [9.5 成员 ID 速查](#95-乃木坂46-现役成员-id-速查)。

### 1.4 填账号凭证

账号凭证（Token / Cookie）**从浏览器或手机抓包获取**：

| 凭证 | 从哪里来 |
|---|---|
| `TOKEN`（JWT） | 电脑浏览器登录 message 网站 → F12 → Network → 任意 API 请求 → 请求头 `authorization: Bearer eyJ...` |
| `COOKIE` | 同一请求的请求头 `cookie:` 整串复制 |
| `REFRESH_TOKEN`（手机端账号用） | 手机抓包（Charles / Proxyman）拦截 `update_token` 请求 → 请求体里的 `refresh_token` |

变量名规则 = **账号 ID 大写** + 后缀：

```bash
# 对应 accounts 里的 "nogizaka_main"
NOGIZAKA_MAIN_TOKEN=eyJ...
NOGIZAKA_MAIN_COOKIE=session=xxx; other=yyy
```

> 💡 更省事的做法：先空着跑起来，然后在网页管理端「账号池 → 填凭证」里粘贴，会自动写入 `.env` 并立即生效。

### 1.5 运行

```bash
python main.py
```

启动会先做健康检查，然后进入轮询：

```
22:00:03 [INFO] 🟢 TG Bot 连通正常 (@your_bot)
22:00:03 [INFO] 🟢 账号凭证完整（1 个账号）
22:00:05 [INFO] 🔍 巡查完毕 [冨里奈央]
```

### 1.6 打开管理端

浏览器访问 **http://127.0.0.1:8787/** ——之后的所有配置、凭证、日志、归档都在这里操作，不用再手动编辑文件。

---

## 2. 核心概念

### 2.1 账号、成员、通道的关系

```
账号（accounts）          成员（monitor）              通道（channels）
你的订阅凭证        →     用这个账号去拉谁的消息   →    消息推送到哪里
"nogizaka_main"           冨里奈央 (id=55)             TG 频道 -100xxx
                          賀喜遥香 (id=30)             QQ 群 533072575
```

- **一个账号可以拉多个成员**（只要该账号订阅了他们）
- **每个成员必须指定用哪个账号**（`monitor[].account` 指向 `accounts` 的 key）
- **每个成员至少要有一个推送目标**：QQ 群（`groups`）、TG 频道（`tg`），或已启用的官方 Bot（它推全局单聊、不区分成员）。都没有的话启动检查会报错。

### 2.2 两个配置文件

| 文件 | 放什么 | 能否提交 Git |
|---|---|---|
| `config/config.json` | 通道开关、账号定义、成员列表、轮询节奏 | ✅ 可以（不含密钥） |
| `.env` | 所有密钥和凭证（Token / Cookie / API Key） | ❌ 已 gitignore |

配置加载顺序：**内置默认值 → `config.json` 覆盖 → `.env` 补充密钥**。

### 2.3 凭证优先级（重要）

Token 会自动续期，续期后的**新值写在 `data/web_credentials/`**（磁盘），所以：

> **磁盘凭证优先级高于 `.env`。光改 `.env` 不会生效。**

换凭证的正确做法见 [3.3 更换账号凭证](#33-更换账号凭证)——用网页端填就自动处理好了，手动改则需要删除磁盘凭证文件。

---

## 3. 日常操作

> 以下操作**优先用网页管理端**（http://127.0.0.1:8787/ ），大多数改动保存即生效、无需重启。手动方式作为备选一并列出。

### 3.1 添加 / 删除监控成员

**网页**：「监控成员」页 →「📋 从账号拉取成员列表」→ 选账号 → 点成员旁的「添加」→ 底部「保存并热重载」。
手动填也行：「＋ 手动添加」后逐格填写。删除点每行的「删除」。

**手动**：在 `config.json` 的 `monitor` 数组增删项目：
```json5
{ "id": "39", "name": "筒井あやめ", "account": "nogizaka_main", "groups": [752269366] }
```

### 3.2 添加账号

**网页**：「账号池」→「＋ 添加账号」→ 填账号 ID、选团体和登录方式 →「保存并热重载」→ 再点该行的「填凭证」。全程无需重启。

**手动**：`config.json` 加 `accounts` 条目，`.env` 按命名规则加凭证，然后**重启**（`.env` 只在启动时读取）。

### 3.3 更换账号凭证

**网页**（推荐）：「账号池」→ 对应行「填凭证」→ 粘贴新 Token / Cookie → 保存。系统会自动完成"写 `.env` → 删除旧磁盘凭证 → 热重载"，**立即生效**。

**手动**：三步缺一不可——
1. 改 `.env` 里的 `{账号ID大写}_TOKEN` / `_COOKIE` / `_REFRESH_TOKEN`
2. **删除** `data/web_credentials/{账号ID}.json`
3. 重启主程序

漏了第 2 步的话，启动时会看到「⚠️ xxx 的 .env 凭证已修改，但磁盘凭证优先」的提醒。

### 3.4 开关推送通道

「基本设置」→ 推送通道开关。只用 TG 的话关掉 napcat 即可，此时成员可以只配 `tg` 不配 `groups`。

### 3.5 调整轮询节奏

「基本设置」→ 轮询节奏。日间/深夜间隔（秒）、休眠时段（JST 小时）。默认日间 2-3 分钟、深夜 25-30 分钟、凌晨 2-7 点休眠。

### 3.6 启用 QQ 官方 Bot

Bot 数量不限。三步：

1. 「基本设置」→ 打开「QQ 官方 Bot」通道 →「＋ 添加 Bot」→ 填名称和 App ID
2. Client Secret 点该行「填写」（存入 `.env`，变量名 = **Bot 名称大写** + `_CLIENT_SECRET`）
3. 目标 OpenID —— **知道就直接填**；不知道就点该行的「🎯 自动获取」：

   系统会连上 Bot 网关等待，**让目标用户给 Bot 发一条私聊消息**，捕获成功后自动填回表格（5 分钟超时，可随时停止）。最后点「保存并热重载」即生效。

   > OpenID 只能从「用户主动私聊 Bot」的事件里拿到，这是官方接口的限制，所以必须有人发一条消息。
   > 命令行等价工具：`python tools/get_qq_openid.py [APP_ID] [SECRET]`

> 旧配置兼容：`config.json` 未声明 `qq_official_bots` 时，自动扫描 `.env` 的 `QQ_OFFICIAL_BOT{1..20}_*` 编号槽位。

### 3.7 验证推送是否正常

「状态」页 →「📨 测试推送」→ 选通道和目标 → 发送。会往目标发一条测试消息，用来确认群号 / chat_id 配置正确，不必等真实消息。

### 3.8 立即巡查一次

「状态」页 →「⏩ 立即巡查」。跳过等待立刻跑一轮（休眠时段也能唤醒），按钮会等到这一轮真正跑完再给结果。适合刚改完凭证想立刻验证。

---

## 4. 网页管理端

默认地址 **http://127.0.0.1:8787/** （`config.json` 的 `web_admin` 控制开关、监听地址和端口）。

| 页签 | 能做什么 |
|---|---|
| **状态** | 巡查轮次与下次倒计时、各账号 Token 实时剩余、通道成功率、每个成员的拉取/推送状态、近期错误分级；「立即巡查」「测试推送」按钮 |
| **基本设置** | 通道开关、NapCat 地址、官方 Bot 增删改、轮询/休眠节奏、翻译参数、归档与每日摘要开关、TG Token 与 Gemini Key 填写 |
| **账号池** | 增删改账号，凭证状态一目了然，「填凭证」直接粘贴 |
| **监控成员** | 表格内直接编辑；从官方 API 拉成员列表点选添加 |
| **日志** | 实时日志（含 DEBUG 级，2 秒刷新）+ 日志文件尾部；支持「仅错误」和关键词过滤，内容自动脱敏 |
| **用户**（启用账号系统后，仅 admin 可见） | 增删用户、改密码、切换角色 |
| **高级（JSON）** | 整份配置的 JSON 编辑 + **历史版本回滚**（每次保存前自动快照，保留 10 份） |

**通用能力**：

- **保存即校验**：点「保存并热重载」后，服务端按 schema 校验（外加账号引用完整性检查），通过才原子写回 `config.json` 并热重载
- **凭证只进不出**：所有密钥填写走同一套对话框，写入 `.env`，接口只回报"有/无"，绝不回显值；密码框还屏蔽了密码管理器的自动填充干扰
- **重启主程序**：右上角「⟳ 重启主程序」，优雅停机后进程自替换（PID 不变），用于让改动过的 `.env` 或代码生效
- **主题**：右上角按钮在「跟随系统 / 浅色 / 深色」间循环，选择记在浏览器本地

⚠️ **保存会重新生成 `config.json`**（使用标准分区注释），手写的自定义注释会丢失。

---

## 5. 消息归档

`config.json` 的 `archive.enabled` 控制（默认开启）。每抓到一条新消息就自动落地：

```
data/archive/{成员名}/{年}/{月}/
    messages.json          # 该月全部消息：原文 + 中文译文 + 本地媒体路径
    images/ videos/ audio/ # 媒体文件，命名 {时间戳}_{消息id}.{ext}
```

### 5.1 浏览

管理端右上角「📚 消息归档」，或直接开 **http://127.0.0.1:8787/archive** ：

- **左侧日历**：每天显示消息条数（热力深浅），点某天直接跳到时间线对应位置
- **时间线**：聊天气泡样式，按天分隔，JST 时间，日文原文 + 中文译文对照
- **筛选**：全部 / 文字 / 图片 / 视频 / 语音（日历会跟着筛选变化）
- **搜索**：跨全部月份，原文和译文都匹配，空格分词为「与」关系
- **媒体**：图片灯箱（键盘翻页），视频/语音在线播放并支持拖进度
- **深链**：URL 带 `#member=&y=&m=&t=`，可复制分享给别人直达某月某筛选

### 5.2 回填历史消息

实时归档只覆盖启用之后的消息，更早的用工具补：

```bash
python tools/backfill_archive.py                            # 全部成员、全部历史
python tools/backfill_archive.py 冨里奈央 --from 2023-01-01   # 指定成员和起始日期
```

断点续传（进度存 `data/archive_progress.json`），已归档的自动跳过，媒体下载失败的会重试，页间隔自适应（顺畅提速、遇限流退避）。

⚠️ **回填前必须停主程序**——两个进程同时刷新同一账号的 Token 会让其中一个凭证作废。工具会自动检测并拒绝启动（确认无冲突可加 `--force`）。

### 5.3 注意事项

- **空间**：媒体占大头，参考值单成员 3.5 年约 **1.7 GB**。只想存文字的话把 `archive.media` 设为 `false`
- **写入语义**：按消息 id 幂等合并、原子写；先落 JSON 再补媒体，进程中断最多丢媒体不丢消息
- **备份**：`data/` 不进 Git，归档是**唯一副本**。重要档案建议定期拷贝到移动硬盘或网盘

---

## 6. 账号与权限

默认关闭（本机访问无需登录）。开启后分两种角色：

| 角色 | 权限 |
|---|---|
| `admin` | 管理端全部功能 + 归档 |
| `viewer` | **只能访问归档** `/archive`，碰不到配置和凭证 |

### 6.1 启用

1. 创建第一个管理员（密码交互输入，不留在命令行历史）：
   ```bash
   python tools/manage_users.py add 你的用户名
   ```
2. `config.json` 里打开（或在网页「高级 JSON」改）：
   ```json5
   "auth": { "enabled": true, "archive_public": false, "session_hours": 12 }
   ```
3. 重启主程序。之后访问会跳转到登录页。

### 6.2 给朋友开访客账号

启用后在「用户」页操作最方便：「＋ 添加用户」→ 角色选 **viewer** →「🎲 生成随机密码」→「📋 复制」→ 创建，把用户名密码转告对方即可。

命令行等价操作：
```bash
python tools/manage_users.py add 朋友名字 --viewer
python tools/manage_users.py list / passwd <用户名> / role <用户名> <角色> / del <用户名>
```

若希望归档**完全公开**（无需登录即可看），把 `auth.archive_public` 设为 `true`，管理端仍然受保护。

### 6.3 安全说明

- 密码用 **scrypt 加盐哈希**存 `data/users.json`（权限 600，git-ignored），不可逆、永不回显
- 会话 = 随机 token + **HttpOnly / SameSite=Strict** cookie，仅存进程内存（重启即失效），滑动续期；改密码立即踢掉该用户所有会话
- 登录失败按 IP 限流（10 分钟 5 次 → 锁 15 分钟）；密码校验常时比较，用户不存在时也走一次哈希（防时序探测）
- 写请求校验 `Origin`（防 CSRF），绑定回环地址时校验 `Host`（防 DNS rebinding）
- 归档媒体用 `private, no-cache` + ETag：登出后浏览器缓存里的图片也无法再显示
- 防误锁：不能删除或降级最后一个 admin，也不能删除当前登录的账号
- `WEB_ADMIN_TOKEN`（`.env`）继续可用，作为脚本/自动化的 API 通道（等价 admin）
- ⚠️ 服务是**明文 HTTP**。局域网使用请挂 TLS 反代（如 Caddy），否则密码和会话 cookie 在链路上裸奔

---

## 7. 部署与运维

### 7.1 每日摘要（死人开关）

`config.json` 的 `daily_summary`（默认每天 JST 23:00）。通过已启用通道发一条当日报告：各成员消息数、巡查轮次、Token 状态、待处理错误、归档占用与磁盘剩余。

它的核心价值是**反向监控**：系统挂了不会有报错通知，但「今天没收到摘要」本身就是告警。发送失败会每 30 分钟补发，最多 3 次。

### 7.2 长期运行（开机自启 + 崩溃自拉起）

程序自身已有内层容错（单成员失败不影响其他、异常不终止主循环、Token 自动续期）。下面配置的是外层：进程整个挂掉时由系统拉起。

**Windows —— 计划任务**

```powershell
powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1 -Start   # 安装并启动
powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1 -Status  # 查看
powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1 -Uninstall
Stop-ScheduledTask -TaskName NogizakaMessagePush                              # 彻底停止
```

登录时自动启动、后台无窗口、崩溃后 60 秒自动拉起（守护逻辑在 `tools/run_service.ps1`，日志 `logs/service.log`）。

- 拉起由守护脚本负责，**不依赖** Task Scheduler 自带的"失败后重启"（那个策略只在任务整体失败时触发，捕捉不到"子进程被杀而包装器正常退出"）
- 退出码 0 视为主动停止不再拉起，非 0 视为崩溃则重启；启动即崩溃会拉长退避
- ⚠️ 想停服务用 `Stop-ScheduledTask`，只杀 python 进程的话守护循环会把它拉回来
- ⚠️ 安装前先停掉手动启动的实例，否则抢 8787 端口
- ⚠️ `.ps1` 必须保持 **UTF-8 with BOM**（PowerShell 5.1 用系统 ANSI 读无 BOM 文件，中文变乱码导致脚本无法解析；单元测试会守住这点）

**Linux —— systemd**

```bash
bash tools/install_systemd.sh              # 用户级（推荐，无需 root，自动开 linger）
bash tools/install_systemd.sh --system     # 系统级（需 sudo）
bash tools/install_systemd.sh --status
bash tools/install_systemd.sh --logs       # journald 日志跟踪
bash tools/install_systemd.sh --uninstall
```

`Restart=always`，异常退出 60 秒后重启；停止时发 `SIGTERM`，主程序走优雅停机（关连接池、等归档任务收尾），比 Windows 强杀更干净。脚本会自动识别 `.venv/bin/python`。

**容器**：跑 `python main.py` 即可，注意三点——`data/` 和 `logs/` 挂卷（归档和凭证在里面）、`.env` 用 secrets 注入、`web_admin.host` 设 `0.0.0.0` 并映射端口（此时**务必启用账号系统或 `WEB_ADMIN_TOKEN`**）。重启策略交给编排器，不要再叠加上面的脚本。

### 7.3 备份清单

| 路径 | 内容 | 丢了会怎样 |
|---|---|---|
| `data/archive/` | 消息归档（唯一副本） | **不可恢复** |
| `.env` | 所有密钥凭证 | 需重新抓包获取 |
| `config/config.json` | 配置 | 需重配（网页有历史快照可回滚） |
| `data/users.json` | 网页账号 | 重建即可 |

---

## 8. 故障排查

| 症状 | 原因与解决 |
|---|---|
| **启动报「没有任何可用推送目标」** | 该成员既没配 `groups` 也没配 `tg`，且官方 Bot 不可用。补一个推送目标即可 |
| **TG 报 `Chat not found`** | Bot 不在该频道，或不是管理员。把 Bot 加进频道并授予「发布消息」权限；chat_id 可用 [@getidsbot](https://t.me/getidsbot) 转发频道消息获取 |
| **QQ 推送 `All connection attempts failed`** | NapCat / Lagrange 没启动，或 `napcat_api` 地址不对 |
| **日志刷屏「Token 刷新失败 / Cookie 已死亡」** | 凭证过期，重新抓包后走 [3.3 更换账号凭证](#33-更换账号凭证) |
| **改了 `.env` 但没生效** | `.env` 只在**启动时**读取；且磁盘凭证优先于 `.env`。用网页「填凭证」，或删除 `data/web_credentials/{账号}.json` 后重启 |
| **抓取报 `KeyError: cookies`** | 账号在 web / mobile 之间切换过而凭证格式没跟上。新版会自动识别并重建，升级后重启即可 |
| **消息有原文没译文** | `GEMINI_API_KEY` 没配或额度耗尽。在「基本设置 → 翻译」检查 Key 状态，日志里搜「翻译失败」看具体原因 |
| **回填工具拒绝启动** | 检测到主程序在跑（会互相作废 Token）。先停主程序，或确认无冲突后加 `--force` |
| **归档页一片空白** | 还没有归档数据。确认 `archive.enabled` 已开，或先跑一次回填工具 |
| **登出后仍能看到归档图片** | 旧版缓存策略问题，已修复。按 `Ctrl+Shift+R` 强制刷新清掉本地旧缓存 |
| **管理端打不开 / 端口被占** | 可能有两个实例（手动启动 + 计划任务）。用 `Get-NetTCPConnection -LocalPort 8787` 查占用进程 |
| **忘记网页登录密码** | 你有服务器本机权限，直接重置：`python tools/manage_users.py passwd <用户名>` |
| **PowerShell 脚本报一堆乱码语法错误** | `.ps1` 文件的 UTF-8 BOM 丢了，见 [7.2](#72-长期运行开机自启--崩溃自拉起) 的说明 |

排查通用手段：管理端「日志」页开「仅错误」筛选，或直接看 `logs/error_debug.log`。

---

## 9. 参考

### 9.1 配置项

`config/config.json` 全部段落（schema 定义见 `config/config.schema.json`）：

| 段落 | 说明 |
|---|---|
| `channels` | `napcat` / `tg` / `qq_official` 三个通道开关 |
| `napcat_api` | NapCat HTTP API 地址 |
| `qq_official_bots` | 官方 Bot 列表（`name` / `app_id` / `target_openid`），数量不限 |
| `web_admin` | 管理端 `enabled` / `host` / `port` |
| `archive` | 归档 `enabled` / `dir` / `media` |
| `daily_summary` | 每日摘要 `enabled` / `hour`（JST） |
| `auth` | 账号系统 `enabled` / `archive_public` / `session_hours` |
| `accounts` | 账号池：`group`（团体）/ `auth`（web·mobile）/ `app_tag` / `api_base` / `web_origin` |
| `monitor` | 成员列表：`id` / `name` / `account` / `groups` / `tg` |
| `day_interval` `night_interval` | 轮询间隔 `[最小, 最大]` 秒 |
| `sleep_hours` | 休眠时段 `[起, 止]` JST 小时 |
| `alert_cooldown` | 告警冷却秒数，防刷屏 |
| `translate` `gemini_models` `gemini_min_interval` `translate_timeout` | 翻译相关 |
| `qq_send_interval` | QQ 消息发送间隔秒数 |

未列出的项（文件路径、超时、并发数、调试开关等）用内置默认值，见 `config/config.py` 的 `_DEFAULTS`。

`.env` 变量：

| 变量 | 用途 |
|---|---|
| `{账号ID大写}_TOKEN` / `_COOKIE` / `_REFRESH_TOKEN` | 账号凭证 |
| `GEMINI_API_KEY` | 翻译 |
| `TG_BOT_TOKEN` | Telegram Bot |
| `{Bot名称大写}_CLIENT_SECRET` | QQ 官方 Bot 密钥 |
| `WEB_ADMIN_TOKEN` | 管理端 API token（脚本用；启用账号系统后仍有效） |

### 9.2 命令行工具

| 命令 | 用途 |
|---|---|
| `python main.py` | 启动主程序 |
| `python -m src.webui` | 只起管理端（主程序没跑时也能改配置） |
| `python tools/list_members.py [账号ID]` | 列出账号可见的成员及 ID |
| `python tools/backfill_archive.py [成员] [--from 日期]` | 回填历史消息 |
| `python tools/manage_users.py add/list/passwd/role/del` | 网页账号管理 |
| `python tools/get_qq_openid.py [APP_ID] [SECRET]` | 获取 QQ 用户 OpenID（网页端也有「🎯 自动获取」） |
| `python tools/test_models.py` | Gemini 模型连通性诊断 |
| `python tests/test_*.py` | 运行测试（CI 同款） |

### 9.3 目录结构

```
nogizaka-message-push/
├── main.py                      # 入口
├── config/
│   ├── config.json              # 用户配置（非敏感）
│   ├── config.schema.json       # 配置结构定义，保存时自动校验
│   ├── config.py                # 配置 facade：默认值 → JSON → .env 三层加载
│   ├── credentials.py           # 凭证管理：双模式 Token 刷新、Header 构建
│   └── watcher.py               # 配置文件热重载（可选 watchdog）
├── src/
│   ├── app.py                   # 主循环：健康检查、轮询编排、每日摘要
│   ├── fetcher.py               # 抓取：API 轮询、过滤、分发
│   ├── translator.py            # Gemini 翻译（多模型容错、串行限速）
│   ├── notifier.py              # 多通道推送路由 + 系统警报
│   ├── archive.py               # 消息归档：落地、媒体下载、搜索、日历统计
│   ├── auth.py                  # 账号系统：scrypt 哈希、会话、登录限流
│   ├── webui.py                 # 管理端 HTTP 服务（stdlib，零依赖）
│   ├── webui_static/            # 前端：管理端 / 归档 / 登录 / 共享主题
│   ├── health.py                # 运行时健康追踪与状态摘要
│   ├── dedup.py                 # 消息 ID 滑动窗口去重
│   ├── member_directory.py      # 成员目录拉取（工具与网页端共用）
│   ├── logger.py                # 日志（彩色终端 + 滚动文件 + 内存环）
│   ├── utils.py                 # JST 时间、时段判断、限速器
│   └── platforms/               # napcat.py / qq_official.py / tgbot.py
├── tools/                       # 见 9.2
├── tests/                       # 五个测试套件，CI 全跑
├── data/                        # 运行时数据（git-ignored）
│   ├── archive/                 # 消息归档
│   ├── web_credentials/         # 持久化凭证
│   ├── users.json               # 网页账号
│   ├── sent_ids/ time_records/  # 去重与时间戳
└── logs/                        # 运行日志（git-ignored）
```

### 9.4 架构与核心设计

```
Member API  →  fetcher  →  translator(Gemini)  →  notifier  →  napcat / qq_official / tgbot
                  │                                              （QQ 群 / 官方单聊 / TG）
                  ├────────→  archive  →  data/archive/  →  webui /archive（浏览·搜索）
                  └────────→  dedup / credentials  →  data/
```

- **双认证模式** — 每个账号可选 `web`（Cookie + Bearer，Chrome 头仿真）或 `mobile`（refresh_token → JWT，iOS 头仿真），在 URL / Header / 401 处理三处分发
- **Token 生命周期** — 启动时为 mobile 账号初始刷新；每轮巡查前解码 JWT 检查 `exp`，不足 300 秒主动续期；401 时被动刷新后重试
- **原子写入** — 时间戳、去重表、凭证、归档、配置全部走「临时文件 + `os.replace`」，中断不留半截文件
- **串行化限速** — 翻译全局串行 + 最小间隔，无论多少成员并发
- **反爬** — 轮询间隔 ±10% 抖动、发送间隔随机、指数退避重试、每轮打乱成员顺序
- **容错语义** — NapCat 推送失败会阻止时间戳推进（下轮重试）；TG / 官方 Bot 失败仅记日志（避免限频导致重复推送）
- **健康追踪** — 纯内存记录通道成功率、Token 剩余、成员状态，分级错误（TRANSIENT 临时 / PERSISTENT 需人工），供状态页和摘要使用

### 9.5 乃木坂46 现役成员 ID 速查

> 通过手机端 API 拉取的现役成员（`state=open`）。ID 可能随运营调整变化，以实际 API 返回为准；也可在网页端「从账号拉取成员列表」实时查看。

| m_id | 成员 | 期别 | | m_id | 成员 | 期别 |
|---|---|---|---|---|---|---|
| 17 | 伊藤 理々杏 | 3期 | | 51 | 小川 彩 | 5期 |
| 18 | 岩本 蓮加 | 3期 | | 52 | 奥田 いろは | 5期 |
| 19 | 梅澤 美波 | 3期 | | 53 | 川﨑 桜 | 5期 |
| 27 | 吉田 綾乃クリスティー | 3期 | | 54 | 菅原 咲月 | 5期 |
| 29 | 遠藤 さくら | 4期 | | 55 | 冨里 奈央 | 5期 |
| 30 | 賀喜 遥香 | 4期 | | 56 | 中西 アルノ | 5期 |
| 32 | 金川 紗耶 | 4期 | | 60 | 愛宕 心響 | 6期 |
| 34 | 黒見 明香 | 4期 | | 61 | 大越 ひなの | 6期 |
| 36 | 柴田 柚菜 | 4期 | | 62 | 海邉 朱莉 | 6期 |
| 38 | 田村 真佑 | 4期 | | 63 | 川端 晃菜 | 6期 |
| 39 | 筒井 あやめ | 4期 | | 64 | 鈴木 佑捺 | 6期 |
| 41 | 林 瑠奈 | 4期 | | 65 | 瀬戸口 心月 | 6期 |
| 44 | 弓木 奈於 | 4期 | | 66 | 長嶋 凛桜 | 6期 |
| 46 | 五百城 茉央 | 5期 | | 67 | 増田 三莉音 | 6期 |
| 47 | 池田 瑛紗 | 5期 | | 68 | 森平 麗心 | 6期 |
| 48 | 一ノ瀬 美空 | 5期 | | 69 | 矢田 萌華 | 6期 |
| 49 | 井上 和 | 5期 | | 74 | 小津 玲奈 | 6期 |
| 50 | 岡本 姫奈 | 5期 | | | | |

### 9.6 依赖

| 包 | 用途 |
|---|---|
| [httpx](https://www.python-httpx.org/) | 全异步 HTTP 客户端 |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | `.env` 加载 |
| [json5](https://github.com/dpranke/pyjson5) | 解析带注释的 `config.json` |
| [jsonschema](https://github.com/python-jsonschema/jsonschema) | 配置结构校验 |
| [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) | TG 推送（不用可不装，缺失时自动禁用该通道） |
| [websockets](https://websockets.readthedocs.io/) | 仅 `tools/get_qq_openid.py` 需要 |

管理端与归档页是**纯 stdlib + 零依赖前端**（无框架、无构建步骤）。

---

## License

MIT
