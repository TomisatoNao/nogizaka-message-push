"""
src/platforms/napcat_commands.py — NapCat QQ 群本地互动指令分发器
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime
import re
import threading
import time
from typing import Callable

import config.config as cfg
from src import archive as _archive
from src.logger import log_all

_CMD_REGEX = re.compile(
    r"^(?:/|#)(?:抽张美图|抽美图|随机美图|美图|pic|photo)(?:\s+(.*))?$",
    re.IGNORECASE,
)


class NapCatCommandHandler:
    """处理 NapCat QQ 群内本地快捷指令（零 Token 消耗、毫秒级响应）。"""

    def __init__(
        self,
        *,
        cooldown_user_seconds: float = 8.0,
        cooldown_group_seconds: float = 4.0,
        sender: Callable[[int, list[dict]], object] | None = None,
        logger: Callable[..., object] | None = None,
    ) -> None:
        self._cd_user_sec = max(1.0, float(cooldown_user_seconds))
        self._cd_group_sec = max(1.0, float(cooldown_group_seconds))
        self._sender = sender
        self._log = logger or log_all
        self._last_user_at: dict[tuple[str, str], float] = {}
        self._last_group_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_command(self, text: str) -> bool:
        """快速判断文本是否匹配支持的指令。"""
        if not text:
            return False
        clean = text.strip()
        return bool(_CMD_REGEX.match(clean))

    def try_handle_command(
        self,
        group_id: str,
        user_id: str,
        raw_text: str,
        *,
        self_id: str = "",
    ) -> tuple[bool, str]:
        """尝试匹配并处理指令。

        :return: (is_handled, status_code)
        """
        if not raw_text:
            return False, "not_command"

        clean = raw_text.strip()
        m = _CMD_REGEX.match(clean)
        if not m:
            return False, "not_command"

        arg = (m.group(1) or "").strip()
        now = time.monotonic()

        # 冷却与防刷检查
        with self._lock:
            grp_last = self._last_group_at.get(group_id, 0.0)
            if now - grp_last < self._cd_group_sec:
                return True, "rate_limited_group"

            user_key = (group_id, user_id)
            usr_last = self._last_user_at.get(user_key, 0.0)
            if now - usr_last < self._cd_user_sec:
                return True, "rate_limited_user"

            self._last_group_at[group_id] = now
            if user_id:
                self._last_user_at[user_key] = now

            # 清理过期缓存
            if len(self._last_user_at) > 1024:
                for k, ts in list(self._last_user_at.items()):
                    if now - ts > 3600.0:
                        self._last_user_at.pop(k, None)

        # 异步启动发图协程任务
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            loop.create_task(self._execute_photo_draw(group_id, user_id, arg))
        else:
            asyncio.run(self._execute_photo_draw(group_id, user_id, arg))

        return True, "command_executed"

    async def _execute_photo_draw(self, group_id: str, user_id: str, arg: str) -> None:
        """执行美图抽取并投递到 QQ 群。"""
        target_member_dir = self._resolve_target_member(group_id, arg)

        photo = _archive.get_random_photo(member_dir=target_member_dir)
        if not photo:
            # 如果指定了成员但没搜到，尝试在全员库兜底抽一张
            if target_member_dir:
                photo = _archive.get_random_photo(member_dir=None)

        if not photo:
            hint = "むむ？本地归档库里暂未找到美图呢～请确认已有消息归档(ˊᵕˋ˶ )"
            await self._send(group_id, [{"type": "text", "data": {"text": hint}}])
            return

        # 格式化日期与展示文案
        pub_str = ""
        pub_raw = photo.get("published_at") or ""
        if pub_raw:
            try:
                dt = datetime.fromisoformat(pub_raw.replace("Z", "+00:00"))
                pub_str = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                pub_str = str(pub_raw)[:16]

        m_name = photo.get("member_name") or target_member_dir or "小偶像"
        caption = f"✨ 随机抽到的【{m_name}】美图来啦～"
        if pub_str:
            caption += f"\n📅 发送时间: {pub_str}"

        caption_text = (photo.get("text") or "").replace("\n", " ").strip()
        if caption_text:
            if len(caption_text) > 40:
                caption_text = caption_text[:40] + "…"
            caption += f"\n💬 {caption_text}"

        file_path = photo.get("abs_path")
        file_uri = ""
        if file_path and file_path.is_file():
            try:
                raw_bytes = file_path.read_bytes()
                # 优先采用 base64:// 协议，彻底规避 NapCat 运行于 Docker / 容器 / 跨机器时的 ENOENT 本地文件不存在报错
                if len(raw_bytes) <= 15 * 1024 * 1024:
                    b64_str = base64.b64encode(raw_bytes).decode("ascii")
                    file_uri = f"base64://{b64_str}"
            except Exception as e:
                self._log(f"⚠️ 读取美图文件转 base64 失败: {e}", is_warning=True)

        if not file_uri and file_path:
            file_uri = f"file:///{file_path.as_posix()}"

        chain = [
            {"type": "text", "data": {"text": caption}},
            {"type": "image", "data": {"file": file_uri}},
        ]

        ok = await self._send(group_id, chain)
        if ok:
            self._log(f"🎲 [NapCat指令] 成功为群 {group_id} 抽取并发送【{m_name}】美图")
        else:
            self._log(f"⚠️ [NapCat指令] 为群 {group_id} 发送美图失败", is_warning=True)

    async def _send(self, group_id: str, chain: list[dict]) -> bool:
        """调用 QQ 发送。"""
        try:
            if self._sender:
                res = await self._sender(int(group_id), chain)
                return bool(res is not False)
            from src.platforms.napcat import send_qq_message
            return await send_qq_message(int(group_id), chain, max_retries=1)
        except Exception as ex:
            self._log(f"⚠️ [NapCat指令] 发送消息异常: {ex}", is_error=True)
            return False

    def _resolve_target_member(self, group_id: str, arg: str) -> str | None:
        """根据输入参数或当前群的推送配置推导目标成员名。"""
        all_archived = _archive.list_members()

        # 1. 优先使用用户输入的成员参数
        if arg:
            clean_arg = re.sub(r"[^\w\u4e00-\u9fa5\u3040-\u30ff]", "", arg).strip().lower()
            if clean_arg:
                # 精确/模糊匹配已归档目录
                for m in all_archived:
                    norm = m.replace(" ", "").replace("　", "").replace("_", "").lower()
                    if clean_arg in norm or norm in clean_arg:
                        return m

                # 匹配监控列表 display
                for m in getattr(cfg, "MONITOR_LIST", []) or []:
                    m_name = m.get("m_name") or m.get("name") or ""
                    norm = m_name.replace(" ", "").replace("　", "").replace("_", "").lower()
                    if clean_arg in norm or norm in clean_arg:
                        return _archive.member_dir_name(m_name)

        # 2. 如果无参数，检查群对应的推送路由配置（napcat_routes）
        routes = getattr(cfg, "NAPCAT_ROUTES", []) or []
        for r in routes:
            if str(r.get("group_id")) == str(group_id):
                filters = r.get("member_filter") or []
                if filters and len(filters) == 1:
                    return _archive.member_dir_name(filters[0])
                remark = str(r.get("remark") or "")
                for m in all_archived:
                    if m in remark:
                        return m

        # 3. 若均无法唯一定位，返回 None（触发全员随机抽选）
        return None
