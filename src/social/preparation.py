"""社交动态正文准备服务。

该模块把翻译、图片 Alt 文本和正文格式化拆成可替换的小组件。组件只处理
本地对象，不执行通道投递；同一个 ``MessagePreparationService`` 可被定时
监控、WebUI 预览和 QQ Bot 指令复用。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from src.logger import log_all
from src.social.formatter import build_post_message, collect_alts
from src.social.models import Post, PreparedSocialPost
from src.social.settings import social_settings


class TranslationService:
    """调用现有 AI 翻译引擎并将失败降级为原文。"""

    def __init__(self, config: dict, logger: Callable[..., None] | None = None):
        self._config = config
        self._log = logger or log_all

    @property
    def enabled(self) -> bool:
        return bool(social_settings(self._config).get("translate", True))

    def _run_translation(self, text: str) -> str | None:
        from src import translator

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    asyncio.run,
                    translator.translate_text(text, "社媒", "偶像"),
                )
                return future.result(timeout=40)
        return asyncio.run(translator.translate_text(text, "社媒", "偶像"))

    def translate(self, text: str) -> str | None:
        """翻译单段文本，返回空值表示未产生有效译文。"""
        if not text or not text.strip() or not self.enabled:
            return None
        try:
            out = self._run_translation(text)
            if out and out.strip() and out.strip() != text.strip():
                self._log(
                    f"✅ [社媒翻译] 翻译完成（{len(text)} 字 → {len(out.strip())} 字）",
                    is_debug=True,
                )
                return out.strip()
        except Exception as exc:
            self._log(
                f"⚠️ [社媒翻译] AI 翻译失败，仅发送原文: {exc}",
                is_debug=True,
            )
        return None


class AltTextService:
    """准备图片 Alt 文本译文。"""

    def __init__(self, translator: TranslationService):
        self._translator = translator

    def prepare(self, post: Post, *, translate: bool = True) -> dict[int, str]:
        if not translate:
            return {}

        alt_translations: dict[int, str] = {}
        alts = collect_alts(post)
        for idx, text in alts:
            translated = self._translator.translate(text)
            if translated:
                alt_translations[idx] = translated

        # 保持归档所需的原文/译文元数据，与旧 Forwarder 行为一致。
        if alts:
            post.extra["_alt_texts"] = {str(idx): text for idx, text in alts}
            if alt_translations:
                post.extra["_alt_translated"] = {
                    str(idx): text for idx, text in alt_translations.items()
                }
        return alt_translations


class MessageFormatter:
    """将准备好的 Post 渲染为通道共用的正文。"""

    def format(
        self,
        post: Post,
        translated: str | None = None,
        alt_translations: dict[int, str] | None = None,
    ) -> str:
        if post.extra.get("raw_message"):
            return post.text
        return build_post_message(post, translated, alt_translations or {})


class MessagePreparationService:
    """统一执行翻译、Alt 文本准备和正文格式化。"""

    def __init__(
        self,
        config: dict,
        *,
        translator: TranslationService | None = None,
        alt_text: AltTextService | None = None,
        formatter: MessageFormatter | None = None,
        logger: Callable[..., None] | None = None,
    ):
        self._config = config
        self.translation = translator or TranslationService(config, logger=logger)
        self.alt_text = alt_text or AltTextService(self.translation)
        self.formatter = formatter or MessageFormatter()

    def prepare(self, post: Post, *, translate: bool = True) -> PreparedSocialPost:
        """生成可跨多个通道复用的准备结果。"""
        if not translate:
            post.extra["_skip_translate"] = True

        skip_translate = bool(post.extra.get("_skip_translate", False))
        translated: str | None = None
        if not skip_translate:
            existing = post.extra.get("_translated")
            translated = str(existing).strip() if existing else None
            if translated is None and post.text:
                translated = self.translation.translate(post.text)
                if translated:
                    post.extra["_translated"] = translated

        alt_translations = self.alt_text.prepare(
            post,
            translate=not skip_translate,
        )
        full_text = self.formatter.format(post, translated, alt_translations)
        return PreparedSocialPost(
            translated=translated,
            alt_translations=alt_translations,
            full_text=full_text,
        )


__all__ = [
    "AltTextService",
    "MessageFormatter",
    "MessagePreparationService",
    "TranslationService",
]
