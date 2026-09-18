<div align="center">

<img src="src/webui_static/archive_icon.svg" width="84" height="84" alt="Sakamichi Message Push Logo" />

# Sakamichi Message Push (坂道消息抓取、归档与推送系统)

> **专为乃木坂46 / 櫻坂46 / 日向坂46 / yodel 打造的私密消息、官方博客与社交媒体全自动智能抓取、永久本地归档、AI 双语翻译与多渠道通知系统。**

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg?style=flat-square)](LICENSE)
[![Platform: Cross-Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS%20%7C%20Docker-lightgrey.svg?style=flat-square)]()
[![AI: Gemini & Zhipu](https://img.shields.io/badge/AI-Gemini%20%2F%20Zhipu%20GLM-orange.svg?style=flat-square&logo=google)]()
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg?style=flat-square)](https://github.com/astral-sh/ruff)
[![Tests: Pytest](https://img.shields.io/badge/tests-pytest%20passing-success.svg?style=flat-square)](tests/)

[🚀 快速开始](#-快速开始--quick-start) • [✨ 核心功能](#-核心功能--features) • [📖 使用指南](#-具体怎么用--使用指南) • [🖥️ Web 管理端](#️-web-管理端一览) • [🛠️ 运维与工具](#️-常用运维工具) • [❓ 常见问题](#-常见问题--faq)

</div>

---

## ✨ 核心功能 / Features

本项目核心聚焦于**偶像消息的全自动采集与本地高保真归档**：

- 💬 **全渠道 Message 智能抓取**：完整支持**乃木坂46、櫻坂46、日向坂46**及 **yodel** 平台，实时抓取文本、语音原声、高清原图、视频以及**粉丝信件（Fan Letters）原图**；
- 💾 **本地永久归档与全文检索**：全量多媒体本地持久化落盘；内置 **SQLite WAL + FTS5 全文索引**，万级历史记录毫秒级中日双语搜索，配合 **Gemini Vision** 智能识别图片场景分类（自拍/合影/舞台/美食等）；
- 📝 **官方博客智能解析与归档**：独立定时轮询三大团体官方博客，精准保留段落排版与原位配图，支持全量历史博文回填；
- 🌐 **全平台社交动态监控**：支持 𝕏 (Twitter)、Instagram (Feed/Story/Reels)、TikTok 视频，以及 **TikTok Live 直播开播秒级探测与 ffmpeg 无损录制**；
- 🖼️ **纯享美图画廊 (Gallery)**：聚合 Message 私密照片与博客配图，预生成极速 WebP 缩略图（秒开省流），提供原图全屏灯箱查看与年份时间轴筛选；
- 🤖 **AI 双引擎智能翻译**：Google Gemini 与智谱清言（GLM 系列）双引擎交替轮询与自动容灾切流，呈现自然流畅的偶像口吻双语对照；
- 📢 **多通道通知分发（可选扩展）**：可按需将抓取到的内容广播至 **Telegram 频道**、**QQ 官方机器人** 或 **QQ 群（OneBot11/NapCat）**，支持多群路由分发与成员白名单过滤。

---

## 🚀 快速开始 / Quick Start

### 方式 A：Docker Compose 部署 (强烈推荐)

适用于群晖 Synology NAS、威联通 QNAP、Unraid、1Panel、Portainer、各类云服务器及本地 Docker 环境。

1. **创建 `docker-compose.yml`**：
   在任意工作目录下创建 `docker-compose.yml` 文件：
   ```yaml
   services:
     sakamichi-push:
       image: ghcr.io/tomisatonao/nogizaka-message-push:${APP_IMAGE_TAG:-latest}
       container_name: sakamichi-push
       restart: unless-stopped
       ports:
         - "46046:46046"  # Web 管理端与归档浏览端口
       environment:
         - TZ=Asia/Tokyo
       volumes:
         - ./config:/app/config
         - ./data:/app/data
         - ./logs:/app/logs
         - ./.env:/app/.env
   ```

2. **一键拉起容器**：
   ```bash
   docker compose up -d
   ```

3. **查看初始管理员账号与密码**：
   ```bash
   docker logs sakamichi-push
   ```
   控制台将高亮输出自动生成的初始管理员密码。在浏览器打开 **`http://<服务器IP>:46046/`** 即可进入系统。

> 💡 **后续升级**：个人试用可继续使用 `latest`。生产环境建议在 `.env` 中把
> `APP_IMAGE_TAG` 固定为 CI 发布的 `sha-xxxxxxx`，避免同一配置在不同时间拉到不同镜像。
> 修改版本后执行 `docker compose pull && docker compose up -d` 即可升级。

---

### 方式 B：原生 Python 环境运行

需要 **Python 3.10+** 及系统已安装 `ffmpeg`（音视频提取）。

1. **克隆代码与安装依赖**：
   ```bash
   git clone https://github.com/TomisatoNao/nogizaka-message-push.git
   cd nogizaka-message-push

   pip install -r requirements.txt
   ```

2. **启动程序**：
   ```bash
   python main.py
   ```
   初次启动时，终端会打印出初始 `admin` 密码及访问链接，浏览器访问 **`http://127.0.0.1:46046/`** 即可登录。

---

## 📖 具体怎么用 / 使用指南

> [!TIP]
> **全流程可视化操作**：系统内置了现代化 Web 管理面板，所有的账号凭证录入、成员勾选、翻译配置和通知渠道设置，**均可在网页端直接点选完成并实时热重载**，完全无需手动编辑任何复杂配置文件！

### 第一步：登录管理后台
在浏览器访问 `http://<IP>:46046/`，使用初始密码登录（建议首次登录后在「🔑 用户」页面修改密码）。

### 第二步：录入 Message 凭证（核心抓取配置）
项目支持 **浏览器 F12 一键复制 cURL 智能解析**，1 分钟即可完成全套凭证配置：

1. 打开浏览器**无痕窗口（Incognito）**，访问对应团体 Message 网页版（如 `https://message.nogizaka46.com/welcome`）；
2. 按 **`F12`** 打开开发者工具，切换到 **「网络 (Network)」** 标签页，勾选顶部的 **「保留日志 (Preserve log)」**；
3. 完成账号授权登录，在网络请求列表中找到名为 **`signin`** 的请求；
4. **右键该请求 ➔ 复制 ➔ 以 cURL 格式复制**；
5. 回到本系统后台「👥 账号与成员」页面，点击对应团体的「填写凭证」，展开「📋 智能一键解析」粘贴并点击「解析并填充」；
6. 保存后系统将自动完成握手鉴权，随后点击「从账号拉取成员列表」，勾选你需要监控归档的偶像成员即可！

### 第三步：配置 AI 翻译（可选）
进入「⚙️ 系统设置」页面，填入你的 **Google Gemini API Key**（[Google AI Studio 免费申请](https://aistudio.google.com/apikey)）或**智谱开放平台 API Key**（[免费申请](https://open.bigmodel.cn/)，国内直连免代）。系统将在抓取到新消息或博客时自动完成高质量双语翻译。

### 第四步：配置通知渠道（可选）
如果希望在手机或群聊中实时接收最新消息推送，可进入「📢 推送通道」按需开启对应渠道：
- **Telegram**：填入 Bot Token 与频道 Target Chat ID；
- **QQ 官方 Bot**：填入腾讯开放平台 AppID 与 Client Secret；
- **NapCat / OneBot**：填入 OneBot HTTP API 地址与 QQ 群号。

### 第五步：随时查阅归档与画廊
- **消息归档**：点击后台顶部的「消息归档」，即可按成员、时间、类型检索所有历史私密消息与信件；
- **美图画廊**：点击「相册」，聚合浏览成员所有 Message 照片与博客原图，支持按年份筛选及全屏高清大图翻页；
- **双语博客**：点击「官方博客」，查阅带格式还原与中日对照的官方博客。
- 若希望免登录分享归档给同好浏览，可在「用户」设置中开启「归档公开访问（游客免登录）」。

---

## 🖥️ Web 管理端一览

管理后台全面适配 PC 与手机移动端浏览：

| 页签 | 功能介绍 |
|---|---|
| **📊 状态** | 实时巡查轮次、下轮倒计时、各账号 Token 剩余寿命、即时手动触发巡查与测试。 |
| **👥 账号与成员** | Message 账号凭证填报、握手状态监测、成员名册拉取与订阅勾选。 |
| **📢 推送通道** | Telegram、QQ 官方机器人、NapCat 等通知通道的开关、路由分发与白名单过滤。 |
| **🌐 动态监控** | 官方博客、𝕏 (Twitter)、Instagram、TikTok 短视频与 TikTok Live 直播录制总控。 |
| **⚙️ 系统设置** | AI 翻译密钥、网络代理、轮询频率、静音休眠时段及每日健康报表配置。 |
| **🔑 用户** | 用户鉴权、密码重置、多角色权限（管理员/只读访客）及游客公开浏览开关。 |
| **🛠️ 高级** | 实时脱敏运行日志、全局配置文件在线可视化编辑及**历史配置快照一键回滚**。 |

---

## 🛠️ 常用运维工具

日常运行完全由 Web 后台接管。仓库 `tools/` 目录下额外提供了一些用于自动化运维、数据回填的辅助脚本：

| 脚本命令 | 作用说明 |
|---|---|
| `python tools/manage_users.py passwd admin <新密码>` | 重设管理员密码（忘记密码时使用） |
| `python tools/backfill_archive.py <成员名> --from 2024-01-01` | 回填指定成员的历史 Message 消息与媒体 |
| `python tools/backfill_blogs.py --group nogizaka --download-images` | 批量回填指定团体的全量历史官方博客与配图 |
| `python tools/archive_letters.py [成员名]` | 批量归档粉丝信件（Fan Letters）原图入库 |
| `python tools/sync_archive_db.py` | 扫描磁盘静态归档并全量重构 SQLite 索引与全文检索库 |

### 使用固定镜像版本部署到 NAS

推送到 `main` 后，GitHub CI 会先完成 Python 3.10/3.12 全量测试。只有全部通过，才会发布
`sha-<提交号前 7 位>` 镜像；`latest` 只是最新版指针，不建议作为生产版本依据。

NAS 项目目录的 `.env` 应保留所有现有凭证，并增加一行：

```dotenv
APP_IMAGE_TAG=sha-1234abc
```

可以在 NAS 上手动执行 `docker compose pull && docker compose up -d`，也可以从开发机运行安全部署器：

```powershell
$env:NAS_HOST = "你的 NAS 地址"
$env:NAS_USER = "仅拥有该项目部署权限的 SSH 用户"
$env:NAS_SSH_KEY = "C:\path\to\deploy_key"
$env:NAS_DOCKER_BIN = "/var/packages/ContainerManager/target/usr/bin/docker"
python tools/deploy_release.py --tag sha-1234abc
```

部署器只支持不可变的 `sha-xxxxxxx` 或 `v1.2.3` 标签，不接受 `latest`/`main`。它会：

1. 保存当前容器镜像作为本地回滚版本；
2. 拉取并启动目标镜像，不重建 NapCat 等依赖服务；
3. 校验容器状态、`/api/health/status` 和运行版本；
4. 验证失败时自动恢复部署前镜像；
5. 将结果写入 NAS 项目目录的 `.deploy-state`，其中不包含凭证。

部署脚本不接受密码参数，也不会保存 SSH 密码。部署用户需要能够运行 Docker Compose；建议使用
SSH Key，并仅授予该项目容器所需的最小权限。`config`、`data`、`logs` 和 `.env` 均继续通过卷挂载，
容器替换不会删除归档或运行配置。

---

## ❓ 常见问题 / FAQ

<details>
<summary><b>Q1: 首次启动没看清 admin 密码怎么办？</b></summary>

在项目目录下执行 `python tools/manage_users.py passwd admin <你的新密码>`（密码需 ≥ 8 位）即可直接重置密码。
</details>

<details>
<summary><b>Q2: 复制了 cURL 粘贴后提示「未包含有效凭证」或找不到 signin 请求？</b></summary>

1. **务必勾选「保留日志 (Preserve log)」**：登录跳转会经过重定向，若未勾选保留日志，关键的 `signin` 请求会被浏览器自动清空；
2. **推荐使用无痕窗口 (Incognito)**：避免历史登录 Cookie 导致跳过登录接口；
3. **必须复制 `signin` 请求**：`profile` 等其他接口不包含完整的登录授权会话。
</details>

<details>
<summary><b>Q3: 如何将消息归档免登录公开分享给朋友或粉丝群？</b></summary>

在 Web 管理后台「🔑 用户」页签中，开启「归档公开访问（游客免登录）」并保存。开启后，管理配置界面依然受到密码保护，而 `/archive` 归档与相册页面允许任何人直接访问浏览。
</details>

<details>
<summary><b>Q4: 为什么抓取到了消息但没有中文翻译？</b></summary>

请在 Web 管理端「⚙️ 系统设置」中检查是否已配置有效的 Google Gemini 或 智谱 API Key。推荐录入两个 API Key，系统会在一家超频或故障时自动无缝容灾切换。
</details>

---

## 📄 开源协议与免责声明 / License

本项目采用 [MIT License](LICENSE) 开源协议。

> [!IMPORTANT]
> **免责声明**：本项目仅供粉丝个人技术研究、学习交流与偶像应援使用。所涉及的偶像消息、官方博客及媒体资源版权均归各运营方（乃木坂46合同会社、Seed & Flower合同会社等）及原作者所有。请勿将本项目用于任何商业营利用途，使用本项目所产生的一切后果由使用者自行承担。
