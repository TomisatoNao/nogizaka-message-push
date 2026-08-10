"""博客数据源：日向坂46 / 乃木坂46 / 樱坂46。

每个 source 模块提供统一的接口：
    fetch_posts(client, limit=30) -> list[dict]    # 最新博客列表
    fetch_images(client, url)    -> list[str]       # 详情页图片（可选）
    fetch_body(client, url)      -> str             # 正文纯文本（可选）

返回的 post dict 格式：
    {url, title, author, date, images?, body?}
"""

import httpx

# 共享的超时配置
TIMEOUT = httpx.Timeout(15.0, connect=10.0)
