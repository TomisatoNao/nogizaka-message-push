"""测试 nogizaka-message-push 的 Gemini 模型序列是否可用。

与生产链路对齐：使用 translator 的同一套 prompt 模板和 generationConfig，
并打印 finishReason / usageMetadata / parts 结构 —— 这些是判断
「思考 token 是否撞上 maxOutputTokens」「parts[0] 是否为思考段」的依据。

运行: python tools/test_models.py
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

import config.config as cfg
from src.translator import _PROMPT_TEMPLATE

# 取自乃木坂46 官博的一段示例
SAMPLE = """こんにちは！与田祐希です😊

今日は久しぶりにメンバーと一緒にランチに行きました〜
ずっと楽しみにしてたお店で、パスタがとっても美味しかったです🍝

明日はライブのリハーサルがあるので早めに寝ます！
皆さんも良い一日を過ごしてくださいね〜"""

BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
DIM = "\033[90m"
RESET = "\033[0m"


def _build_payload() -> dict:
    """与 translator.translate_text 完全一致的请求体。"""
    prompt = _PROMPT_TEMPLATE.format(
        group_name="乃木坂46",
        member_name="与田祐希",
        text=SAMPLE,
    )
    return {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 4096},
    }


async def test_one(model: dict) -> dict:
    """返回单个模型的测试结果（含响应结构诊断信息）。"""
    name = model["name"]
    url = f"{model['url']}?key={cfg.GEMINI_API_KEY}"
    result: dict = {"name": name, "text": "", "elapsed": 0.0, "error": None,
                    "finish": "", "parts": [], "usage": {}}
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, json=_build_payload())
        result["elapsed"] = time.time() - t0

        if resp.status_code != 200:
            reason = {
                503: "503 已下线", 404: "404 端点不存在", 429: "429 限流",
            }.get(resp.status_code, f"HTTP {resp.status_code}")
            result["error"] = reason
            return result

        data = resp.json()
        if "error" in data:
            result["error"] = data["error"].get("message", str(data["error"]))[:100]
            return result

        candidate = (data.get("candidates") or [{}])[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        result["finish"] = candidate.get("finishReason", "?")
        result["usage"] = data.get("usageMetadata") or {}
        result["parts"] = [
            {"thought": bool(p.get("thought")), "chars": len(p.get("text", ""))}
            for p in parts
        ]

        # 生产逻辑应取第一个非思考段 —— 这里同样这么取，以便发现 parts[0] 是思考段的情况
        text = next(
            (p["text"] for p in parts if p.get("text") and not p.get("thought")),
            "",
        )
        if not text:
            result["error"] = f"无可用文本段 (finishReason={result['finish']}, parts={len(parts)})"
            return result

        result["text"] = text.strip()
        return result
    except Exception as e:
        result["elapsed"] = time.time() - t0
        result["error"] = f"{type(e).__name__}: {e}"[:120]
        return result


async def main() -> None:
    print(f"\n{BOLD}═══ nogizaka-message-push 模型序列测试 ═══{RESET}")
    print(f"  模型数: {len(cfg.GEMINI_MODELS)}")
    print(f"  API Key: {'已配置' if cfg.GEMINI_API_KEY else '❌ 未配置'}")
    print(f"  测试文本: {len(SAMPLE)} 字 | maxOutputTokens: 4096 | temperature: 0.3\n")

    if not cfg.GEMINI_API_KEY:
        print(f"{YELLOW}  GEMINI_API_KEY 未配置，无法测试{RESET}\n")
        return

    total = len(cfg.GEMINI_MODELS)
    for i, model in enumerate(cfg.GEMINI_MODELS, start=1):
        print(f"  [{i}/{total}] {CYAN}⏳ {model['name']}{RESET} ...", end="", flush=True)
        r = await test_one(model)

        if r["error"]:
            print(f"\r  [{i}/{total}] {YELLOW}✗ {r['name']}{RESET} ({r['elapsed']:.1f}s): {r['error']}")
        else:
            print(f"\r  [{i}/{total}] {GREEN}✓ {r['name']}{RESET} "
                  f"({r['elapsed']:.1f}s, {len(r['text'])}字)")
            preview = r["text"][:200].replace("\n", "\\n")
            print(f"      {preview}{'...' if len(r['text']) > 200 else ''}")

        # 结构诊断（无论成功失败都打印，用于判断 thinking 行为）
        usage = r["usage"]
        if r["finish"] or usage:
            thought_parts = sum(1 for p in r["parts"] if p["thought"])
            print(f"      {DIM}finishReason={r['finish'] or '-'} | "
                  f"parts={len(r['parts'])} (思考段 {thought_parts}) | "
                  f"prompt={usage.get('promptTokenCount', '-')} "
                  f"candidates={usage.get('candidatesTokenCount', '-')} "
                  f"thoughts={usage.get('thoughtsTokenCount', '-')} "
                  f"total={usage.get('totalTokenCount', '-')}{RESET}")

        await asyncio.sleep(1.0)   # 避免限流

    print(f"\n{BOLD}═══ 完成 ═══{RESET}\n")


if __name__ == "__main__":
    asyncio.run(main())
