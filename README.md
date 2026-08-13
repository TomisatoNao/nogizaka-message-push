# 🌸 nogizaka-message-push

> **坂道联合（乃木坂46 / 日向坂46 / 櫻坂46）Message & 官方博客全自动监控、Gemini AI 智能双语翻译、多通道格式化推送与全量本地永久归档系统。**

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg?style=flat-square)](LICENSE)
[![Platform: Cross-Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux-lightgrey.svg?style=flat-square)]()
[![AI: Google Gemini](https://img.shields.io/badge/AI-Google%20Gemini-orange.svg?style=flat-square&logo=google)]()

自动轮询成员 Mobile Message 私密消息及官方博客，利用 Google Gemini AI 进行地道上下文双语翻译，格式化推送到 **QQ 群（NapCat/Lagrange） / Telegram 频道 / QQ 官方机器人**，并将每条消息与博客连同高清原图、视频、语音**永久归档到本地数据库与硬盘**。日常运维、凭证热更、用户管理与归档查阅全部在自带的现代 Web UI 中极简完成。

```
┌────────────────────────────────┐
│  成员发消息 / 官方博客更新更新  │
└───────────────┬────────────────┘
                ▼
      ┌──────────────────┐
      │   异步轮询与抓取  │
      └─────────┬────────┘
                ├─────────────────────────────────────────────────┐
                ▼                                                 ▼
     ┌─────────────────────┐                          ┌───────────────────────┐
     │  Gemini AI 智能翻译  │                          │  本地持久化与媒体归档  │
     │  (多模型容错/段落对照)│                          │  (SQLite/JSON/原图/音视频)│
     └──────────┬──────────┘                          └───────────┬───────────┘
                ▼                                                 ▼
┌───────────────────────────────┐             ┌────────────────────────────────────────┐
│      多通道格式化消息推送      │             │         现代浏览器 Web 管理端          │
│ · QQ 群 (NapCat/Lagrange)     │             │ · 4 列响应式自适应博客网格矩阵         │
│ · Telegram 频道 / 私聊        │             │ · 吸顶式热力日历与毫秒级全文检索       │
│ · QQ 官方 Bot (个人/群聊推送) │             │ · 日中对照/日文/中文三态解耦阅读器     │
└───────────────────────────────┘             │ · 账号池管理 / 凭证热更 / 实时脱敏日志 │
                                              └────────────────────────────────────────┘
```

---

## 📑 目录

- [✨ 核心特性 / Features](#-核心特性--features)
- [🚀 快速开始 / Quick Start](#-快速开始--quick-start)
  - [1.1 环境准备与安装](#11-环境准备与安装)
  - [1.2 准备推送通道](#12-准备推送通道)
  - [1.3 基础配置填写](#13-基础配置填写)
  - [1.4 账号凭证获取与配置](#14-账号凭证获取与配置)
  - [1.5 启动系统](#15-启动系统)
- [🧩 核心概念 / Concepts](#-核心概念--concepts)
  - [2.1 账号、成员与通道映射](#21-账号成员与通道映射)
  - [2.2 双模式认证与凭证优先级](#22-双模式认证与凭证优先级)
- [🛠️ 博客归档与双语阅读器](#️-博客归档与双语阅读器)
  - [3.1 4 列自适应网格与日期日历](#31-4-列自适应网格与日期日历)
  - [3.2 智能分段解析与图片原位保护](#32-智能分段解析与图片原位保护)
  - [3.3 三态多语言视图与解耦架构](#33-三态多语言视图与解耦架构)
  - [3.4 全渠道排版规范与格式化推送](#34-全渠道排版规范与格式化推送)
- [💬 消息归档与全文检索](#-消息归档与全文检索)
  - [4.1 聚合动态首页与最新写真画廊](#41-聚合动态首页与最新写真画廊)
  - [4.2 时间线浏览、FTS5 全文搜索与图片 AI 识图打标](#42-时间线浏览fts5-全文搜索与图片-ai-识图打标)
- [🖥️ 网页管理端与日常运维](#️-网页管理端与日常运维)
  - [5.1 六大管理页签功能](#51-六大管理页签功能)
  - [5.2 账号权限体系与访客隔离](#52-账号权限体系与访客隔离)
  - [5.3 QQ 官方机器人私聊交互指令](#53-qq-官方机器人私聊交互指令)
- [⚙️ 服务化部署与运维](#️-服务化部署与运维)
  - [6.1 Windows 计划任务后台守护](#61-windows-计划任务后台守护)
  - [6.2 Linux Systemd 服务化托管](#62-linux-systemd-服务化托管)
  - [6.3 每日健康摘要与数据备份](#63-每日健康摘要与数据备份)
- [🔍 故障排查 / FAQ](#-故障排查--faq)
- [📖 附录与配置参考](#-附录与配置参考)
  - [8.1 全量配置项手册 (config.json)](#81-全量配置项手册-configjson)
  - [8.2 环境变量速查 (.env)](#82-环境变量速查-env)
  - [8.3 命令行工具矩阵 (tools/)](#83-命令行工具矩阵-tools)
  - [8.4 现役成员 ID 速查表](#84-现役成员-id-速查表)

---

## ✨ 核心特性 / Features

### 1. 智能博客解析与媒体保护
- **DOM 智能分段与大段落合并**：全面兼容乃木坂46、日向坂46、樱坂46三团官网差异化 DOM。以空 `<p>` 标签作为视效分段标识，智能合并连续 `<p>` / `<div>` 节点为完整大段落并保留段内换行（`\n`），1:1 精准还原官方博客的排版留白与阅读节奏。
- **图片节点抽离与位置保护**：在送交 AI 模型翻译前，预先抽离、标记并保护正文中的 `<img>` 节点，补全绝对 URL 并携带 `referrerpolicy="no-referrer"`，杜绝长篇博客翻译截断与畸形标签导致的图片丢失或位移。
- **破图优雅降级机制**：卡片列表与封面图片遇 404、防盗链拦截或资源损坏时，自动隐藏图片容器并转为优雅占位样式，彻底杜绝浏览器破图图标。

### 2. 多语言视图与解耦架构
- **三档语言视图随心切换**：双语阅读器提供「日中对照」、「日文」、「中文」三种视图模式（未生成译文时自动隐藏切换器，避免误操作）。
- **智能生命周期状态跳转**：
  - 进入博客详情页若已包含译文，默认激活并展示「日中对照」视图；
  - 在当前页面发起即时翻译并收到就绪信号后，自动平滑切换至「日中对照」视图。
- **数据独立持久化存储**：日文原文（原始 HTML）与中文译文解耦独立存储在 SQLite `blogs.db` 中，切换语言直接取独立字段，杜绝单语模式切换导致的内容篡改与丢段问题。

### 3. 视觉排版与全渠道对齐
- **专属中日字体区分规范**：日中对照模式下，**日文原文统一渲染为斜体（Italic）**，**中文译文统一渲染为粗体（Bold）**，主次分明、视觉层次清晰。
- **全渠道排版严格一致**：Web 网页端阅读器与各推送通道（Telegram、QQ 群、QQ 官方 Bot 等卡片/长图）统一执行该排版与字体规范，并自动压缩连续照片占位符为 `[写真1-3]` 形式。

### 4. 翻译来源透明化
- **AI 模型实时动态标记**：详情页右上角动态显示当前译文生成的 AI 模型名称（如 `Gemini 2.5 Flash` / `Gemini 1.5 Pro` 等），仅在存在译文且处于「日中对照」或「中文」视图时优雅展示。

### 5. 极致的稳定性与性能
- **多模型自动 Failover 降级**：内置模型熔断轮询机制，遭遇 HTTP 429 或限额时自动毫秒级切换备用模型，保障翻译不中断。
- **SQLite WAL 模式与零依赖 WebUI**：后端采用标准库 asyncio HTTP 服务，零外部前端框架依赖；归档采用 SQLite WAL 模式 + FTS5 全文索引，百万级消息秒级响应。

---

## 🚀 快速开始 / Quick Start

### 1.1 环境准备与安装

需要 **Python 3.10+**（支持 Windows / Linux / macOS）。

```bash
# 1. 克隆代码仓库
git clone https://github.com/TomisatoNao/nogizaka-message-push.git
cd nogizaka-message-push

# 2. 安装核心依赖
pip install -r requirements.txt

# 3. 复制环境变量配置文件
cp .env.example .env        # Windows CMD/PowerShell: copy .env.example .env
```

### 1.2 准备推送通道

至少准备以下任意一个推送通道：

| 通道类型 | 推荐指数 | 快速准备步骤 |
|---|:---:|---|
| **Telegram** | ⭐⭐⭐⭐⭐ | 找 [@BotFather](https://t.me/BotFather) 发送 `/newbot` 获取 `TG_BOT_TOKEN`；将机器人加入频道并设为**管理员**。 |
| **QQ 群 (OneBot v11)** | ⭐⭐⭐⭐ | 启动 [NapCatQQ](https://github.com/NapNeko/NapCatQQ) 或 [Lagrange.Core](https://github.com/LagrangeDev/Lagrange.Core)，开启 HTTP API（默认 `http://127.0.0.1:3000`）。 |
| **QQ 官方机器人** | ⭐⭐⭐⭐ | 在 [QQ 开放平台](https://q.qq.com/#/apps) 创建机器人，获取 `AppID` 和 `ClientSecret`（支持个人单聊与群聊推送）。 |

### 1.3 基础配置填写

1. 编辑 `.env` 文件（密钥凭证）：
   ```bash
   TG_BOT_TOKEN=123456:ABC-DEF                 # 使用 Telegram 推送时填写
   GEMINI_API_KEY=AIzaSy...                    # Google AI Studio 免费申请 (https://aistudio.google.com/apikey)
   ```

2. 编辑 `config/config.json`，配置通道与要监控的成员：
   ```json5
   {
     "channels": {
       "napcat": false,
       "tg": true,
       "qq_official": false
     },
     "accounts": {
       "nogizaka_main": { "group": "nogizaka46" }    // 账号 ID 自定义（小写下划线）
     },
     "monitor": [
       {
         "id": "55",
         "name": "冨里奈央",
         "account": "nogizaka_main",
         "tg": "-1004219007326"                      // 填入接收消息的 TG 频道 ID
       }
     ],
     "blog_monitor": {
       "enabled": true,
       "nogizaka": true,
       "hinatazaka": true,
       "sakurazaka": true
     }
   }
   ```

> 💡 **成员 ID 怎么获取？** 先运行程序，在网页管理端「监控成员 → 📋 从账号拉取成员列表」中直接点选添加，或参考文末 [8.4 现役成员 ID 速查表](#84-现役成员-id-速查表)。

### 1.4 账号凭证获取与配置

Message 账号凭证从电脑浏览器或手机端抓包获取：

| 凭证类型 | 对应登录方式 | 获取途径 |
|---|---|---|
| `TOKEN` (JWT) | Web 网页版 (默认) | 电脑登录 Message 官网 ➔ F12 开发者工具 ➔ Network ➔ 任意 API ➔ 请求头 `authorization: Bearer eyJ...` |
| `COOKIE` | Web 网页版 (默认) | 同上请求的 Request Headers 中 `cookie:` 完整字符串 |
| `REFRESH_TOKEN` | Mobile 移动端 | 手机抓包（Charles / Proxyman / HTTP Catcher）拦截 `update_token` 请求体中的 `refresh_token` |

配置写入 `.env`（变量名规则：`账号ID大写_TOKEN` / `_COOKIE` / `_REFRESH_TOKEN`）：
```bash
NOGIZAKA_MAIN_TOKEN=eyJhbGciOi...
NOGIZAKA_MAIN_COOKIE=session=xxx; other=yyy
```

> 💡 **零手动极简配置**：可先留空直接启动，打开网页管理端「👥 账号与成员 ➔ 填凭证」直接粘贴，系统将自动安全写入 `.env` 并即时热重载生效！

### 1.5 启动系统

```bash
python main.py
```

终端将打印初始化自检日志并启动轮询：
```text
22:00:00 [INFO] 🟢 TG Bot 连通正常 (@sakurapush_bot)
22:00:01 [INFO] 🟢 账号凭证完整（1 个账号已就绪）
22:00:02 [INFO] 🟢 博客监控引擎已启动 [乃木坂46 / 日向坂46 / 櫻坂46]
22:00:03 [INFO] 🔍 巡查完毕 [冨里奈央] - 无新消息
```

打开浏览器访问 **`http://127.0.0.1:8787/`**，即可进入全功能 Web 管理控制台与归档中心。

---

## 🧩 核心概念 / Concepts

### 2.1 账号、成员与通道映射

```text
┌────────────────────────┐      ┌────────────────────────┐      ┌────────────────────────┐
│    账号池 (Accounts)    │ 1:N  │   监控成员 (Monitor)    │ 1:N  │   推送通道 (Channels)  │
│  你持有的各团体订阅凭证  ├─────►│  用指定账号监控谁的消息 ├─────►│  消息分发到哪些群或频道 │
│  · "nogizaka_main"     │      │  · 冨里奈央 (id=55)    │      │  · TG 频道 -100xxx     │
│  · "hinata_sub"        │      │  · 金村美玖 (id=12)    │      │  · QQ 群 533072575     │
└────────────────────────┘      └────────────────────────┘      └────────────────────────┘
```

- **多成员复用账号**：一个账号只要订阅了多位成员，即可同时监控抓取这些成员。
- **通道灵活绑定**：每个成员可独立配置推送目标（QQ 群 `groups`、Telegram `tg` 等），无配置的通道自动跳过。

### 2.2 双模式认证与凭证优先级

1. **认证模式分发**：
   - `web` 模式（默认）：模拟 Chrome 桌面端，依赖 Token 与 Cookie；
   - `mobile` 模式：模拟 iOS 客户端，使用 `refresh_token` 自动向鉴权中心换取短期 JWT，免去 Cookie 过期困扰。
2. **凭证加载优先级机制**：
   ```
   磁盘动态凭证 (data/web_credentials/*.json)  >  环境变量 (.env)  >  默认配置
   ```
   > ⚠️ **重要提示**：Token 自动续期后会落地到 `data/web_credentials/`。更换凭证时强烈推荐在**网页端「账号与成员」卡片中点击「填凭证」**修改，系统会自动清理旧磁盘凭证并同步更新 `.env`。

---

## 🛠️ 博客归档与双语阅读器

本系统深度集成坂道三团官方博客爬虫，提供**抓取 ➔ 结构化解耦 ➔ AI 翻译 ➔ 高清下载 ➔ 优雅阅读**全链路方案：

### 3.1 4 列自适应网格与日期日历

- **4 列自适应响应式矩阵**：每页 24 条博客卡片排版，搭载 1:1 精致封面、作者头像标签、发布时间与搜索关键词高亮。
- **破图优雅降级**：封面遇 404、防盗链拦截或原图下架时，前端自动隐藏破损图片节点并以 📝 极简图标占位，视觉整洁一致。
- **日期跳转与日历热力**：左侧吸顶日历按日统计博客发文量，点击任意日期自动重置筛选并精准定位高亮目标博客。

### 3.2 智能分段解析与图片原位保护

```
[原始 HTML] ──────────────────────────┐
  │                                    ▼
  │  1. 空 <p>/空行切分 ───►  [视效大段落 1 (JP)] ──► Gemini AI ──► [中文译文 1 (ZH)]
  │  2. 抽离 <img> 节点 ───►  [图片节点原位 (IMG)] ──► 绝对路径化 ──► [原图保留 (IMG)]
  │  3. 连续非空行合并 ───►  [视效大段落 2 (JP)] ──► Gemini AI ──► [中文译文 2 (ZH)]
  │                                    │
  └────────────────────────────────────┴───────────────────────────┐
                                                                   ▼
                                                [结构化解耦存储 content_json]
```

- **5 类 DOM 统一归一化**：自动消除 `<br>` 堆叠、逐行 `<p>` 碎片化与 `<div>` 容器差异，确保段落粒度与人类视效完全一致。
- **原位节点保留**：图片不参与纯文本翻译，预先抽离并在拼接时无损插回原位，彻底根除漏图、跳段与长文截断现象。

### 3.3 三态多语言视图与解耦架构

双语阅读器内置自由切换机制：

| 视图模式 | 渲染逻辑 | 适用场景 |
|---|---|---|
| **📖 日中对照** (默认) | 日文段落（*斜体*）与中文译文（**粗体**）交替插值渲染，图片原位嵌入 | 最佳双语精读体验 |
| **🇯🇵 仅日文** | 100% 渲染官网原始 `body_html`，保留原汁原味排版与样式 | 原文核对、生词学习 |
| **🇨🇳 仅中文** | 提取纯中文段落并原位保留图片；若某段无译文自动优雅降级回退至日文 | 快速通读流览 |

- **翻译模型动态溯源**：详情页右上角次级灰字动态标示 `翻译模型：gemini-2.5-flash`，让翻译来源清晰透明。
- **管理员一键管理**：登录 Admin 后可在阅读器内随时「🗑️ 删除翻译」或「🌐 即时重译」。

### 3.4 全渠道排版规范与格式化推送

博客推送全渠道（Telegram / QQ 群 / 官方 Bot）严格统一按照 **zakablog 标准排版规范** 发送：
1. **Header 信息头**：团体 Emoji、成员名、标题、发布时间、原图统计与原文链接；
2. **全量高清原图**：并发下载并无损发送所有博客配图；
3. **双语正文**：日文 *斜体*、中文 **粗体**；连续图片占位符智能归并为 `[写真1-3]`，自动转义特殊 Markdown 符号。

---

## 💬 消息归档与全文检索

### 4.1 聚合动态首页与最新写真画廊

访问 `/archive` 即可进入跨成员聚合首页：
- **Hero 卡片**：直观展示全站归档总数、时间跨度、本周收发环比及「今日 X 条」一键准确定位跳转。
- **最新写真横向画廊**：轮播展示最新抓取的高清生写与日常随拍，支持键盘方向键翻页与全屏灯箱。
- **最近动态流**：聚合展示各成员最新 Message 气泡与双语译文。

### 4.2 时间线浏览、FTS5 全文搜索与图片 AI 识图打标

- **聊天气泡时间线**：按天精准分隔（JST 时间），支持日中双语对照、音频在线播放、视频流媒体拖拽进度。
- **SQLite FTS5 全文检索**：毫秒级秒搜数万条历史消息，支持空格多词分词与中日双语联合匹配。
- **Gemini Vision 智能打标**：自动对收到的图片进行 10 种类目识别（自拍/合照/舞台/外出/美食/玩偶/动物/花草/风景/截图），点击标签即可一键聚合检索。

---

## 🖥️ 网页管理端与日常运维

管理后台监听于 **`http://127.0.0.1:8787/`**，包含六大核心模块：

### 5.1 六大管理页签功能

```
┌────────┬─────────────┬──────────┬──────────┬─────────┬──────────┐
│ 📊状态 │ 👥账号与成员 │ 📢推送通道│ ⚙️系统设置│ 🔑用户   │ 🛠️高级   │
└────────┴─────────────┴──────────┴──────────┴─────────┴──────────┘
```

| 页签 | 功能覆盖与亮点 |
|---|---|
| **📊 状态** | 实时查看巡查轮次、倒计时、账号 Token 剩余有效期、通道健康度；提供「立即巡查」与「测试推送」快捷入口。 |
| **👥 账号与成员** | 账号池增删改、凭证即时粘贴更新；从官方 API 一键拉取现役成员目录，点击直接添加监控。 |
| **📢 推送通道** | 集中管控 NapCat QQ、Telegram、QQ 官方 Bot 等通道开关、API 接口、推送路由与独立成员/博客过滤。 |
| **⚙️ 系统设置** | 日间/深夜/休眠时段轮询间隔配置、博客爬虫抓取开关、Gemini API 参数及图片打标设置。 |
| **🔑 用户** | 采用 scrypt 加盐哈希的用户鉴权系统，在线增删用户、分配角色、生成随机高强度密码（防锁死保护）。 |
| **🛠️ 高级** | 实时脱敏运行日志控制台（支持过滤与清空）、全局 JSON 配置可视化编辑及 **10 份历史快照一键回滚**。 |

### 5.2 账号权限体系与访客隔离

通过 `config.json` 中的 `auth` 模块轻松启用多用户访问控制：
- **`admin` (管理员)**：拥有管理后台全部权限、配置热更、系统重启与归档管理能力。
- **`viewer` (访客/朋友)**：仅允许访问 `/archive` 归档查阅界面，完全隔离敏感配置、日志与凭证信息。
- **安全防误锁机制**：系统内置底层约束，严禁删除或降级当前登录账号及最后一个管理员账号。

### 5.3 QQ 官方机器人私聊交互指令

启用 QQ 官方 Bot 并开启指令后，授权用户在 QQ 私聊机器人即可发送查询指令（回复走被动消息，不消耗主动额度）：

| 指令 | 说明 |
|---|---|
| `/help` | 查看支持的指令帮助菜单 |
| `/status` | 实时查看系统运行时间、巡查轮次、各账号 Token 剩余与异常告警 |
| `/members` | 查看当前监控的所有成员及其绑定的推送通道 |
| `/latest [成员名] [条数]` | 调取指定成员最新的 Message 消息（默认 5 条） |
| `/search <关键词>` | 全归档双语检索并返回最近 5 条匹配结果 |
| `/stats` | 统计各成员的历史归档总量与月份跨度 |

---

## ⚙️ 服务化部署与运维

### 6.1 Windows 计划任务后台守护

针对 Windows 生产环境，项目内置了具备崩溃自拉起、防孤儿进程的 PowerShell 守护脚本：

```powershell
# 以管理员权限打开 PowerShell，运行以下命令：
powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1 -Start     # 安装自启计划任务并立即拉起
powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1 -Status    # 查看守护进程运行状态
powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1 -Stop      # 优雅停机（不留孤儿进程）
powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1 -Uninstall # 卸载开机自启
```

### 6.2 Linux Systemd 服务化托管

针对 Linux 服务器环境，提供一键注册为 systemd 守护服务的脚本：

```bash
# 用户级守护服务（推荐，无需 root，自动开启 linger 开机启动）
bash tools/install_systemd.sh
bash tools/install_systemd.sh --status
bash tools/install_systemd.sh --logs       # 查看实时 journal 日志
bash tools/install_systemd.sh --stop       # 停止服务
```

### 6.3 每日健康摘要与数据备份

- **死人开关 (Daily Summary)**：每日设定时间（默认 JST 23:00）向指定通道推送运行日报。如果某天未收到日报，即说明系统出现异常。
- **核心数据备份清单**：
  - `data/archive/`：历史 Message 归档与 `blogs.db` 数据库（**最重要数据，建议定期冷备份**）；
  - `data/blog_images/`：本地下载的博客原图；
  - `config/config.json` 与 `.env`：系统配置与密钥凭证。

---

## 🔍 故障排查 / FAQ

| 故障现象 | 潜在排查原因 | 解决方案 |
|---|---|---|
| **启动提示「没有任何可用推送目标」** | 监控成员未配置 `groups` 或 `tg`，且未启用官方 Bot。 | 在「👥 账号与成员」中为监控成员勾选或填入推送目标。 |
| **Telegram 报错 `Chat not found`** | Bot 未加入目标频道，或未被赋予「发布消息」管理员权限。 | 将 Bot 添加至频道 Admin；使用 [@getidsbot](https://t.me/getidsbot) 获取准确的频道 Chat ID。 |
| **NapCat 提示连接失败** | OneBot 框架未启动，或 `napcat_api` 地址配置有误。 | 确认 NapCat 运行中，且 HTTP API 地址（如 `http://127.0.0.1:3000`）可正常访问。 |
| **Token 频繁报错过期或 401** | Web 凭证 Cookie 失效或账号在多处登录发生冲突。 | 重新抓包获取最新凭证，在 WebUI「账号与成员」中点击「填凭证」更新。 |
| **消息/博客有原文但无译文** | `GEMINI_API_KEY` 未配置或触发了 Google 限频 (429)。 | 在「⚙️ 系统设置」中检查 API Key 是否有效，或增加备用 Gemini 模型。 |
| **改了 `.env` 但未生效** | 磁盘动态凭证 `data/web_credentials/` 优先级高于 `.env`。 | 推荐在 WebUI 页面直接粘贴凭证，或删除对应磁盘 JSON 文件后重启。 |
| **博客列表封面出现破图** | 官方 CDN 开启了防盗链或原图链接失效。 | 新版已集成自动过滤与占位降级机制，更新代码后自动恢复整洁样式。 |

---

## 📖 附录与配置参考

### 8.1 全量配置项手册 (config.json)

| 配置节点 | 类型 | 默认值 | 详细说明 |
|---|:---:|:---:|---|
| `channels.napcat` | `bool` | `true` | 是否启用 NapCat / Lagrange QQ 群消息推送 |
| `channels.tg` | `bool` | `false` | 是否启用 Telegram 频道 / 机器人消息推送 |
| `channels.qq_official` | `bool` | `false` | 是否启用 QQ 官方开放平台机器人推送 |
| `napcat_api` | `string` | `"http://127.0.0.1:3000"` | NapCat HTTP API 服务监听地址 |
| `qq_official_bots` | `array` | `[]` | 官方 Bot 列表（支持配置 `name`, `app_id`, `target_openid`, `group_openid`, `member_filter`, `blog_filter`） |
| `web_admin.enabled` | `bool` | `true` | 是否启动 Web 管理端后台 |
| `web_admin.host` | `string` | `"127.0.0.1"` | Web 管理端监听地址（若需要局域网访问可设为 `"0.0.0.0"` 并启用 auth） |
| `web_admin.port` | `int` | `8787` | Web 管理端监听端口 |
| `archive.enabled` | `bool` | `true` | 是否开启 Message 本地归档与持久化 |
| `archive.media` | `bool` | `true` | 是否下载 Message 配套图片、语音与视频媒体至本地 |
| `blog_monitor.enabled` | `bool` | `true` | 是否全局开启官方博客抓取与监控 |
| `blog_monitor.nogizaka` | `bool` | `true` | 乃木坂46 官方博客监控开关 |
| `blog_monitor.hinatazaka` | `bool` | `true` | 日向坂46 官方博客监控开关 |
| `blog_monitor.sakurazaka` | `bool` | `true` | 櫻坂46 官方博客监控开关 |
| `day_interval` | `[int, int]` | `[120, 180]` | 白天轮询随机间隔范围（单位：秒） |
| `night_interval` | `[int, int]` | `[1500, 1800]` | 深夜轮询随机间隔范围（单位：秒） |
| `sleep_hours` | `[int, int]` | `[2, 7]` | 休眠时段范围（JST 日本标准时间小时） |
| `translate` | `bool` | `true` | 是否开启 Gemini AI 智能翻译 |
| `gemini_models` | `array` | `[...]` | 翻译模型池与 Fallback 备选降级序列 |
| `gemini_min_interval` | `float` | `1.0` | 翻译请求并发最小间隔保护（秒） |
| `image_tagging` | `bool` | `true` | 是否启用 Gemini Vision 消息图片自动打标签 |
| `daily_summary.enabled` | `bool` | `true` | 每日运行健康报告日报开关 |
| `daily_summary.hour` | `int` | `23` | 日报推送时间（JST 小时） |
| `auth.enabled` | `bool` | `false` | Web 管理端多用户登录鉴权开关 |
| `auth.archive_public` | `bool` | `false` | 是否允许免登录公开查阅 `/archive` 归档 |
| `auth.session_hours` | `int` | `24` | 登录会话 Token 有效期（小时） |

### 8.2 环境变量速查 (.env)

```bash
# AI 翻译与多模态识图
GEMINI_API_KEY=AIzaSy...

# Telegram Bot Token
TG_BOT_TOKEN=123456:ABC-DEF

# 网页管理端 API Token (可选，外部脚本调用凭证)
WEB_ADMIN_TOKEN=your_secure_token

# 账号凭证格式：{账号ID大写}_TOKEN / _COOKIE / _REFRESH_TOKEN
NOGIZAKA_MAIN_TOKEN=eyJ...
NOGIZAKA_MAIN_COOKIE=session=xxx
NOGIZAKA_MAIN_REFRESH_TOKEN=

# QQ 官方 Bot 密钥：{Bot名称大写}_CLIENT_SECRET
QQ_OFFICIAL_BOT1_CLIENT_SECRET=your_secret_here
```

### 8.3 命令行工具矩阵 (tools/)

| 脚本文件 | 命令示例 | 功能说明 |
|---|---|---|
| `archive_member.py` | `python tools/archive_member.py <博客列表/详情URL> --translate` | 归档指定成员全量历史博客与原图（支持断点与 AI 翻译） |
| `backfill_archive.py` | `python tools/backfill_archive.py 冨里奈央 --from 2023-01-01` | 回填指定成员或全员的历史 Message 消息与多媒体 |
| `sync_archive_db.py` | `python tools/sync_archive_db.py` | 扫描磁盘 JSON 归档并全量同步重构 SQLite 数据库索引 |
| `tag_images.py` | `python tools/tag_images.py --member 冨里奈央` | 批量对归档图片调用 Gemini Vision 进行补全打标 |
| `manage_users.py` | `python tools/manage_users.py add <用户名> --viewer` | 命令行增删网页端用户、重置密码或变更权限角色 |
| `list_members.py` | `python tools/list_members.py nogizaka_main` | 查询指定账号已订阅/可见的成员列表与成员 ID |
| `get_qq_openid.py` | `python tools/get_qq_openid.py [APP_ID] [SECRET]` | 自动捕获私聊用户的 `target_openid` |
| `get_qq_group_openid.py`| `python tools/get_qq_group_openid.py [APP_ID] [SECRET]` | 自动捕获机器人在目标群中的 `group_openid` |
| `test_models.py` | `python tools/test_models.py` | 诊断检测 `.env` 中 Gemini 各模型连通性与响应延时 |
| `install_autostart.ps1` | `powershell -File tools\install_autostart.ps1 -Start` | Windows 计划任务开机自启安装与管理脚本 |
| `install_systemd.sh` | `bash tools/install_systemd.sh` | Linux systemd 服务化守护一键安装配置脚本 |

### 8.4 现役成员 ID 速查表

<details>
<summary><b>乃木坂46 现役成员 ID 速查（点击展开）</b></summary>

> 注：以下为 API 常规 ID。实际配置亦可在 Web 管理端「从账号拉取成员列表」中点选。

| ID | 成员姓名 | 期别 | | ID | 成员姓名 | 期别 |
|:---:|:---|:---:|:---:|:---:|:---|:---:|
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

</details>

---

## 📄 开源许可证 / License

本项目采用 [MIT License](LICENSE) 许可证开源。仅供粉丝个人学习、技术研究与偶像应援交流使用，请勿用于商业用途。
