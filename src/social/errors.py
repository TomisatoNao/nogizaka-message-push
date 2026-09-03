"""社交业务层的统一异常类型。

平台实现可以保留自己的原始异常，但在跨平台业务边界必须转换为这里的
类型。这样 WebUI、QQ Bot 和定时任务不需要通过异常文本猜测失败原因。
"""

from __future__ import annotations


class SocialError(Exception):
    """所有社交业务异常的基类。"""

    def __init__(
        self,
        message: str = "",
        *,
        request_id: str = "",
        post_id: str = "",
    ) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.post_id = post_id


class SocialParseError(SocialError):
    """URL 或平台响应无法解析。"""


class SocialAuthRequired(SocialParseError):
    """请求内容需要有效的登录态。"""


class SocialDownloadError(SocialError):
    """媒体下载失败。"""


class SocialTranslationError(SocialError):
    """翻译服务失败或返回不可用内容。"""


class SocialDeliveryError(SocialError):
    """投递服务或全部目标投递失败。"""


__all__ = [
    "SocialAuthRequired",
    "SocialDeliveryError",
    "SocialDownloadError",
    "SocialError",
    "SocialParseError",
    "SocialTranslationError",
]
