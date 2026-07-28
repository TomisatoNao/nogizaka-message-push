# 图片自动打标签 — 设计方案

## 概述

对归档图片自动生成中文标签（场景/物品/人物状态），使其在归档页可以通过关键词搜索图片。

## 实验验证

2026-07-27 用 Gemini 3.5 Flash Lite 对 5 张不同类型的冨里奈央归档图片进行了测试：

| 图片 | 原文 | 生成的标签 | 耗时 |
|------|------|-----------|------|
| 最新自拍（无文字） | — | 食物 烤肉 | 3.0s |
| 广岛旅拍 | 広島のなおだよ | 室内 玩偶 看镜头 | 2.7s |
| MV 公开海报 | フォルテシモ MV… | 舞台 笑容 挥手 麦克风 | 3.0s |
| 生日周边海报 | 20歳になるぞよ | 海报 舞台 演出服装 | 3.0s |
| 被拍日常 | %%%いたー！ | 室内 玩偶 看镜头 | 3.6s |

**实验结果：**
- 成功率 100%（5/5）
- 平均耗时 ~3 秒（冷启动后）
- 平均 3 个标签，场景/物品/人物状态均有覆盖
- 未出现冗余的成员名/团体名

**结论：Gemini 3.5 Flash Lite（免费层）足以胜任。**

## 架构

### 数据模型

在 `messages.json` 的图片消息上新增字段：

```json
{
  "type": "picture",
  "_local_file": "2026/07/images/20260718_011417_168739.jpg",
  "_tags": "舞台 笑容 挥手 麦克风"
}
```

`_tags` 用空格分隔的中文标签字符串。只在 type=picture/image 的消息上生成。

### 新模块：`src/tagger.py`

```
tagger.py
├── initialize(client)        # 注入共享 HTTP 客户端 + 创建 RateLimiter
├── tag_image(member, path)   # 核心：读取本地图片 → base64 → Gemini → 返回标签串
└── schedule_tag(member,msg)  # 后台任务：非阻塞打标签 + 写回归档
```

- 使用 `gemini-3.5-flash-lite` 为主要模型，`gemini-3.1-flash-lite` 为 fallback
- 复用项目的 `RateLimiter`，标签配额的间隔与翻译的间隔独立
- 共享同一个 `GEMINI_API_KEY`

### Gemini API 调用

端点：
```
POST /v1beta/models/gemini-3.5-flash-lite:generateContent?key={API_KEY}
```

请求体：
```json
{
  "contents": [{
    "parts": [
      {"inlineData": {"mimeType": "image/jpeg", "data": "<base64>"}},
      {"text": "分析这张偶像照片，输出3-5个中文标签，用空格分隔。\n要求：\n- 标签覆盖：场景（舞台/室内/外景/街拍）、物品（花/食物/玩偶/麦克风/手机）、人物状态（笑容/自拍/挥手/比心/看镜头）\n- 不要成员名、团体名、品牌名\n- 严格限制在5个以内\n- 截图或海报：描述画面内容，不要提取图中文字\n只输出标签，不要多余文字。"}
    ]
  }],
  "generationConfig": {"temperature": 0.2, "maxOutputTokens": 64}
}
```

### 归档管道集成

```
archive_message()
  ├── _merge_write(record, 带 _translation)
  ├── if 有媒体: _download_media() → _merge_write(带 _local_file)
  └── if 是图片: schedule_tag()            ← 新增
                    ├── tag_image() → 得 _tags
                    └── _merge_write(带 _tags)
```

- 幂等：`archive_message()` 检查 `_tags` 已存在则跳过
- 非阻塞：`schedule_tag()` 用 `asyncio.create_task` 后台执行，不拖推送主流程
- 失败容忍：Gemini 调用失败写日志，不重试，不丢消息

### 搜索增强

修改 `archive.py search()` 的匹配逻辑：

```python
haystack = (
    (msg.get("text") or "") + "\n" +
    (msg.get("_translation") or "") + "\n" +
    (msg.get("_tags") or "")
)
```

### WebUI 变更

**API 响应（`webui.py`）：** `_handle_archive()` 的消息转换中加入 `tags` 字段。

**前端（`archive.html`）：** 图片气泡下方显示标签小按钮：

```
┌─────────────────────┐
│ 07:54 · picture     │
│ [      图片      ]  │
│                     │
│ 🔍 舞台 笑容 挥手  │  ← 标签行
└─────────────────────┘
```

点击标签 → 自动填入搜索框 → 搜索该标签。

### 回填脚本：`tools/tag_images.py`

```
usage: python tools/tag_images.py [--member 冨里奈央] [--year 2026] [--month 07] [--dry-run]

扫描归档 → 找出 type=picture/image 且 _tags 为空的条目
→ 逐张调 Gemini 打标签 → 合并写回 messages.json
```

- 支持 `--member`、`--year`、`--month` 限定范围
- 支持 `--dry-run` 预览
- 遵守速率限制（每 3 秒一次）

### 配置变更

`config/config.json` 新增：

```json
// ── 图片标签（Gemini Flash Lite）──
"image_tagging": false,
"gemini_tag_models": [
  {"name": "gemini-3.5-flash-lite", "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent"},
  {"name": "gemini-3.1-flash-lite", "url": "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent"}
],
"gemini_tag_min_interval": 5
```

`config/config.schema.json` 同步新增对应 schema 定义。

### 配额管理

| 维度 | 值 | 说明 |
|------|-----|------|
| 免费 RPD | ~500 (Gemini 3.5 Flash Lite) | 足够覆盖日常新增 |
| 新图片/天 | 约 2-10 张 | 远在限额内 |
| RPM | 30 | 5 秒间隔 = 12 RPM，安全 |
| 回填已有图片 | 约 100 张（冨里奈央全部） | 一次回填不超 RPD |

超过 RPD 时 Gemini 返回 429，脚本捕获后等待次日。

## 文件变更清单

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `src/tagger.py` | **新建** | 打标签核心模块 |
| `src/archive.py` | 修改 | 归档管道集成 + search() 改匹配逻辑 |
| `src/webui.py` | 修改 | API 响应加 tags 字段 |
| `config/config.py` | 修改 | 新增 3 个配置项 |
| `config/config.json` | 修改 | 新增 image_tagging 段 |
| `config/config.schema.json` | 修改 | 新增 schema |
| `src/webui_static/archive.html` | 修改 | 前端渲染标签 + 点击搜索 |
| `tools/tag_images.py` | **新建** | 回填脚本 |

## 未纳入范围

- 视频打标签（Gemini 也支持视频分析，但 token 消耗大，以后按需加）
- 标签管理界面（编辑/删除标签）
- 多语言标签
