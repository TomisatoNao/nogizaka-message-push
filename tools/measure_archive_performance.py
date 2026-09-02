"""测量归档首页的网络响应与关键 API 耗时。

该脚本只发起只读 GET 请求，不携带 Cookie、令牌或业务数据，适合在公网入口和
NAS 内网入口分别运行。首次请求可用于观察冷缓存，后续请求可用于观察暖缓存。
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import os
import statistics
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ENDPOINTS = (
    ("archive_html", "/archive"),
    ("auth_me", "/api/auth/me"),
    ("archive_members", "/api/archive/members"),
    ("archive_blog_groups", "/api/archive/blog_groups"),
    ("archive_home", "/api/archive/home"),
    ("archive_css", "/static/archive.css?v=20260902_3"),
    ("archive_js", "/static/archive.js?v=20260902_3"),
)


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * percentile) - 1))
    return ordered[index]


def _measure_once(url: str, timeout: float, accept_gzip: bool) -> dict:
    headers = {
        "Accept-Encoding": "gzip" if accept_gzip else "identity",
        "Cache-Control": "no-cache",
        "User-Agent": "sakamichi-archive-performance/1.0",
    }
    started = time.perf_counter()
    try:
        with urlopen(Request(url, headers=headers), timeout=timeout) as response:
            header_elapsed = time.perf_counter() - started
            body = response.read()
            elapsed = time.perf_counter() - started
            encoding = response.headers.get("Content-Encoding", "")
            decoded_size = len(body)
            if encoding.lower() == "gzip":
                try:
                    decoded_size = len(gzip.decompress(body))
                except (OSError, EOFError):
                    decoded_size = None
            return {
                "status": response.status,
                "header_ms": round(header_elapsed * 1000, 1),
                "total_ms": round(elapsed * 1000, 1),
                "transfer_bytes": len(body),
                "decoded_bytes": decoded_size,
                "encoding": encoding or None,
            }
    except HTTPError as exc:
        try:
            exc.read()
        except OSError:
            pass
        return {
            "status": exc.code,
            "header_ms": None,
            "total_ms": round((time.perf_counter() - started) * 1000, 1),
            "transfer_bytes": 0,
            "decoded_bytes": None,
            "encoding": None,
            "error": f"HTTPError: {exc.reason}",
        }
    except (TimeoutError, URLError, OSError) as exc:
        return {
            "status": None,
            "header_ms": None,
            "total_ms": round((time.perf_counter() - started) * 1000, 1),
            "transfer_bytes": 0,
            "decoded_bytes": None,
            "encoding": None,
            "error": f"{type(exc).__name__}: {exc}",
        }


def measure_endpoint(name: str, path: str, base_url: str, runs: int, timeout: float, accept_gzip: bool) -> dict:
    url = base_url.rstrip("/") + path
    samples = [_measure_once(url, timeout, accept_gzip) for _ in range(runs)]
    successful = [sample for sample in samples if sample.get("status") is not None and sample["status"] < 400]
    elapsed = [float(sample["total_ms"]) for sample in successful]
    transfer = [int(sample["transfer_bytes"]) for sample in successful]
    decoded = [int(sample["decoded_bytes"]) for sample in successful if sample.get("decoded_bytes") is not None]
    return {
        "name": name,
        "url": url,
        "runs": runs,
        "success": len(successful),
        "first": samples[0] if samples else None,
        "p50_ms": round(statistics.median(elapsed), 1) if elapsed else None,
        "p95_ms": round(_percentile(elapsed, 0.95), 1) if elapsed else None,
        "median_transfer_bytes": int(statistics.median(transfer)) if transfer else None,
        "median_decoded_bytes": int(statistics.median(decoded)) if decoded else None,
        "samples": samples,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="测量归档首页和相关 API 的响应耗时")
    parser.add_argument(
        "--base-url",
        default=os.getenv("ARCHIVE_PERF_BASE_URL", "https://zakapush.220206.xyz"),
        help="入口地址，例如 https://zakapush.220206.xyz 或 http://192.168.22.13:46046",
    )
    parser.add_argument("--runs", type=int, default=5, help="每个请求的次数（默认 5）")
    parser.add_argument("--timeout", type=float, default=20.0, help="单次请求超时秒数（默认 20）")
    parser.add_argument("--no-gzip", action="store_true", help="禁用 gzip，用于比较未压缩响应大小")
    parser.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 输出完整采样结果")
    return parser.parse_args()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _parse_args()
    if args.runs < 1 or args.timeout <= 0:
        print("runs 必须大于 0，timeout 必须大于 0", file=sys.stderr)
        return 2

    results = [
        measure_endpoint(name, path, args.base_url, args.runs, args.timeout, not args.no_gzip)
        for name, path in ENDPOINTS
    ]
    if args.as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print(f"入口: {args.base_url} | runs={args.runs} | gzip={'off' if args.no_gzip else 'on'}")
        print("name\tfirst_ms\tp50_ms\tp95_ms\ttransfer\tdecoded\tsuccess")
        for result in results:
            first = result["first"] or {}
            print(
                f"{result['name']}\t{first.get('total_ms', '-')}\t{result['p50_ms'] or '-'}\t"
                f"{result['p95_ms'] or '-'}\t{result['median_transfer_bytes'] or '-'}\t"
                f"{result['median_decoded_bytes'] or '-'}\t{result['success']}/{result['runs']}"
            )
    return 0 if any(result["success"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
