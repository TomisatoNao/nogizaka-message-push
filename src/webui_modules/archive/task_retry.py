"""Pure retry-selection helpers for the archive task workers."""

from __future__ import annotations


def filter_message_targets(targets: list[dict], retry_items: list[dict], key_for_target) -> list[dict]:
    """Select only failed member/media scopes; a task-only failure may rerun its untouched scope."""
    if not retry_items:
        return list(targets)
    allowed_keys: set[str] = set()
    for item in retry_items:
        payload = item.get("payload", {})
        if item.get("type") == "member":
            key = payload.get("member_key") or item.get("key")
        elif item.get("type") == "media":
            key = payload.get("member_key")
        else:
            continue
        if key:
            allowed_keys.add(str(key))
    if not allowed_keys:
        return list(targets)
    return [target for target in targets if key_for_target(target) in allowed_keys]


def blog_page_ids(retry_items: list[dict], group_key: str) -> set[int]:
    """Return only pages represented by failed page or article snapshots."""
    pages = set()
    for item in retry_items:
        payload = item.get("payload", {})
        if payload.get("group") == group_key and payload.get("page") is not None:
            pages.add(int(payload["page"]))
    return pages


def blog_page_requested(retry_items: list[dict], group_key: str, page: int) -> bool:
    """A page-level failure permits retrying the page listing itself."""
    if not retry_items:
        return True
    return any(
        item.get("type") == "page"
        and item.get("payload", {}).get("group") == group_key
        and int(item.get("payload", {}).get("page", -1)) == page
        for item in retry_items
    )


def blog_article_requested(
    retry_items: list[dict],
    group_key: str,
    page: int,
    post_url: str,
    index: int,
    *,
    completed_items: list[dict] | None = None,
) -> bool:
    """Select failed articles and avoid re-fetching successes on a failed page."""
    fallback_key = f"{group_key}:{page}:{index}"
    candidate_keys = {post_url, fallback_key} - {""}
    for item in completed_items or []:
        if item.get("type") != "article":
            continue
        payload = item.get("payload", {})
        if payload.get("group") == group_key and int(payload.get("page", -1)) == page:
            if str(item.get("key") or "") in candidate_keys or str(payload.get("url") or "") == post_url:
                return False

    if not retry_items or blog_page_requested(retry_items, group_key, page):
        return True
    return any(
        item.get("type") == "article"
        and item.get("payload", {}).get("group") == group_key
        and int(item.get("payload", {}).get("page", -1)) == page
        and (
            item.get("payload", {}).get("url") == post_url
            or item.get("key") in candidate_keys
        )
        for item in retry_items
    )
