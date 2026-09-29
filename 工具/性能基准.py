"""性能基准：固定题、跑多次、给出 p50/p90 与分阶段耗时。

    python 工具/性能基准.py                # 默认 6 题 × 1 轮
    python 工具/性能基准.py --repeat 3

为什么要单独一个脚本，而不是看评测的延迟列：
  评测的延迟混合了不同题型（规则路由 vs 大模型路由、RAG vs 建单），
  p50 会随题目构成漂移，**不能用来判断"这次优化有没有效"**。
  基准用固定的题、固定的顺序，只比同一个东西。
"""

import argparse
import json
import os
import statistics
import sys
import time
import uuid

import httpx

BASE = "http://127.0.0.1:5000"
STORE = "H_SH_001"
ROOM = "1208"

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

# 固定基准题：覆盖四条不同成本的主链路
CASES = [
    ("规则路由 + 门店事实", "早餐是几点到几点？", False),
    ("大模型路由 + 政策 RAG", "退改政策是怎么规定的？", False),
    ("大模型路由 + 工单工具", "能送两瓶矿泉水吗？", True),
    ("大模型路由 + 故障建单", "空调不制冷", True),
    ("规则路由 + 安全例外", "卫生间漏水了", False),
    ("多轮追问（无建单）", "刚才那件事办到哪了？", True),
]


def one_turn(session: str, query: str) -> dict:
    started = time.perf_counter()
    first_ms = None
    answer = ""
    try:
        with httpx.stream(
            "POST",
            f"{BASE}/api/chat",
            json={"query": query, "user_id": "user_1001", "session_id": session, "store_id": STORE},
            timeout=300,
        ) as resp:
            for line in resp.iter_lines():
                if not line.startswith("data: "):
                    continue
                payload = json.loads(line[6:])
                if "content" in payload:
                    if first_ms is None:
                        first_ms = int((time.perf_counter() - started) * 1000)
                    answer += payload["content"]
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}", "total_ms": None, "first_ms": None}
    return {
        "total_ms": int((time.perf_counter() - started) * 1000),
        "first_ms": first_ms,
        "answer_len": len(answer),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="随行管家性能基准")
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args()

    try:
        httpx.get(f"{BASE}/", timeout=5)
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 后端未运行（{exc}）")
        return 1

    session = f"perf-{uuid.uuid4().hex[:8]}"
    # 先建会话并完成房间级验证，让基准专注于"回答问题"而不是"验证流程"
    httpx.post(f"{BASE}/api/session", json={"store_id": STORE, "room_no": ROOM, "session_id": session}, timeout=30)
    httpx.post(
        f"{BASE}/api/auth/verify",
        json={"session_id": session, "store_id": STORE, "room_no": ROOM,
              "last_name": "陈", "check_out_date": "2026-09-18"},
        timeout=30,
    )

    print(f"基准开始（{len(CASES)} 题 × {args.repeat} 轮）")
    print()
    totals: list[int] = []
    firsts: list[int] = []
    for run in range(args.repeat):
        for label, query, _needs_order in CASES:
            result = one_turn(session, query)
            if result.get("error"):
                print(f"  ❌ {label:22} {result['error']}")
                continue
            totals.append(result["total_ms"])
            if result["first_ms"]:
                firsts.append(result["first_ms"])
            print(f"  {label:22} 首字 {result['first_ms']:>6} ms · 总 {result['total_ms']:>6} ms"
                  f" · 回答 {result['answer_len']} 字")

    print()
    if totals:
        print(f"总耗时  p50 = {int(statistics.median(totals))} ms   "
              f"p90 = {sorted(totals)[int(len(totals) * 0.9) - 1]} ms   "
              f"min = {min(totals)} ms")
    if firsts:
        print(f"首字    p50 = {int(statistics.median(firsts))} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
