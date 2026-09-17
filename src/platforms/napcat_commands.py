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

import src.config.config as cfg
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
        target_member_dir, is_unmatched_arg = self._resolve_target_member(group_id, arg)

        # 如果用户明确指定了小偶像名字，但无法识别该小偶像
        if is_unmatched_arg:
            hint = f"むむ？未能找到小偶像【{arg}】呢～请确认名字是否正确(ˊᵕˋ˶ )"
            await self._send(group_id, [{"type": "text", "data": {"text": hint}}])
            return

        photo = _archive.get_random_photo(member_dir=target_member_dir)

        # 如果明确指定了小偶像，但该小偶像暂无归档照片，明确提示用户，绝不可静默乱发他人照片
        if not photo and target_member_dir:
            hint = f"むむ？本地归档库中暂未找到【{target_member_dir}】的照片呢～（可能暂未归档该成员的消息或博客配图）"
            await self._send(group_id, [{"type": "text", "data": {"text": hint}}])
            return

        # 若未指定小偶像且全员库也为空
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

        # 若本地原图未落盘但有远程官方 CDN 图片 URL，异步下载转为 base64 发送
        if not file_uri and photo.get("remote_url"):
            try:
                import httpx
                async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0"}, timeout=12.0) as client:
                    resp = await client.get(photo["remote_url"])
                    if resp.status_code == 200 and len(resp.content) <= 15 * 1024 * 1024:
                        b64_str = base64.b64encode(resp.content).decode("ascii")
                        file_uri = f"base64://{b64_str}"
            except Exception as e:
                self._log(f"⚠️ 下载远程美图转 base64 失败: {e}", is_warning=True)

        if not file_uri and file_path:
            file_uri = f"file:///{file_path.as_posix()}"

        if not file_uri:
            hint = f"⚠️ 读取【{m_name}】的照片失败，请稍后重试"
            await self._send(group_id, [{"type": "text", "data": {"text": hint}}])
            return

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

    def _resolve_target_member(self, group_id: str, arg: str) -> tuple[str | None, bool]:
        """根据输入参数或当前群的推送配置推导目标成员名。
        
        返回: (target_member_name_or_dir, is_unmatched_arg)
        - 如果用户显式传入参数但未匹配到任何小偶像，返回 (None, True)，严禁静默退回群默认成员；
        - 如果用户未传入参数，尝试使用群默认绑定的成员，无绑定时返回 (None, False)。
        """
        all_archived = _archive.list_members()

        # 1. 优先使用用户输入的成员参数
        if arg:
            clean_arg = re.sub(r"[^\w\u4e00-\u9fa5\u3040-\u30ff]", "", arg).strip().lower()
            # 常见简繁体汉字互通支持
            _sim_map = {
                "纯叶": "純葉", "叶月": "葉月", "远藤": "遠藤", "富里": "冨里",
                "樱": "桜", "贺喜": "賀喜", "遥香": "遥香", "莲加": "蓮加",
                "纱耶": "紗耶", "史绪里": "史緒里", "阳世": "陽世",
                "未来虹": "未来虹", "茉莉": "茉莉", "璃果": "璃果", "绚音": "絢音",
            }
            mapped_arg = clean_arg
            for s_k, t_v in _sim_map.items():
                if s_k in mapped_arg:
                    mapped_arg = mapped_arg.replace(s_k, t_v)

            if clean_arg:
                # 1.1 精确/模糊匹配已归档消息目录
                for m in all_archived:
                    norm = m.replace(" ", "").replace("　", "").replace("_", "").lower()
                    if clean_arg in norm or norm in clean_arg or mapped_arg in norm or norm in mapped_arg:
                        return m, False

                # 1.2 匹配监控列表 display
                for m in getattr(cfg, "MONITOR_LIST", []) or []:
                    m_name = m.get("m_name") or m.get("name") or ""
                    norm = m_name.replace(" ", "").replace("　", "").replace("_", "").lower()
                    if clean_arg in norm or norm in clean_arg or mapped_arg in norm or norm in mapped_arg:
                        return _archive.member_dir_name(m_name), False

                # 1.3 匹配三团官方名册与博客作者
                candidates = []
                try:
                    from src import sakamichi_roster
                    for g_key, r_dict in sakamichi_roster.ALL_ROSTERS.items():
                        for k, (_gen, kana) in r_dict.items():
                            norm_k = k.replace(" ", "").replace("　", "").replace("_", "").lower()
                            norm_kana = kana.replace(" ", "").replace("　", "").lower()
                            if (clean_arg in norm_k or mapped_arg in norm_k or clean_arg in norm_kana or norm_k in clean_arg):
                                candidates.append(k)
                except Exception:
                    pass

                # 1.4 匹配 blog.db 中的博主
                try:
                    from src.webui_modules.archive_handlers import get_blog_db
                    blog_db = get_blog_db()
                    if blog_db:
                        b_authors = [r[0] for r in blog_db.execute("SELECT DISTINCT author FROM blog_posts;").fetchall() if r[0]]
                        for a in b_authors:
                            norm_a = a.replace(" ", "").replace("　", "").replace("_", "").lower()
                            if clean_arg in norm_a or mapped_arg in norm_a or norm_a in clean_arg:
                                candidates.append(a)
                except Exception:
                    pass

                if candidates:
                    # 如果匹配到多个（如“向井”匹配向井純葉与向井葉月），优先选择库中拥有配图的成员
                    try:
                        from src.webui_modules.archive_handlers import get_blog_db
                        blog_db = get_blog_db()
                        if blog_db:
                            for c in candidates:
                                norm_c = c.replace(" ", "").replace("　", "").replace("_", "")
                                cnt = blog_db.execute(
                                    "SELECT COUNT(*) FROM blog_posts WHERE REPLACE(REPLACE(REPLACE(author, ' ', ''), '　', ''), '_', '') = ? AND ((images_json IS NOT NULL AND images_json != '[]' AND images_json != '') OR (image_paths_json IS NOT NULL AND image_paths_json != '[]' AND image_paths_json != ''))",
                                    (norm_c,)
                                ).fetchone()[0]
                                if cnt > 0:
                                    return _archive.member_dir_name(c), False
                    except Exception:
                        pass
                    return _archive.member_dir_name(candidates[0]), False

                # 用户显式指定了成员参数，但无法匹配到任何小偶像，返回未匹配标记，禁止回退群默认
                return None, True

        # 2. 如果用户未输入参数，检查群对应的推送路由配置（napcat_routes）
        routes = getattr(cfg, "NAPCAT_ROUTES", []) or []
        for r in routes:
            if str(r.get("group_id")) == str(group_id):
                filters = r.get("member_filter") or []
                if filters and len(filters) == 1:
                    return _archive.member_dir_name(filters[0]), False
                remark = str(r.get("remark") or "")
                for m in all_archived:
                    if m in remark:
                        return m, False

        # 3. 若均无法唯一定位，返回 None（触发全员随机抽选）
        return None, False
