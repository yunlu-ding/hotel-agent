"""随行管家 · 冒烟测试：跑通一次真实问答（阶段一的验收动作）。

用法（后端已在 :5000 运行时执行）：
    python 工具/冒烟测试.py
    python 工具/冒烟测试.py --query "我最近一笔订单能退吗" --user user_1001

为什么单独做这一步：参考项目留下过"通了但没跑过"的坑
（语义缓存把相似度当距离，语义路径永不触发）。所以基线验收不看代码，
只看**一次真实请求有没有走完、走了哪条路、耗时多少**。
"""

import argparse
import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:5000"

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(description="随行管家冒烟测试")
    parser.add_argument("--query", default="早餐是几点到几点？")
    parser.add_argument("--user", default="user_1001")
    parser.add_argument("--session", default=f"smoke-{int(time.time())}")
    parser.add_argument("--store", default=None, help="指定门店（store_id），默认用服务端配置")
    args = parser.parse_args()

    print(f"【请求】session={args.session} user={args.user}")
    print(f"         门店={args.store or '（服务端默认）'}")
    print(f"         query={args.query}")
    print()

    started = time.perf_counter()
    first_token_ms = None
    content_parts: list[str] = []
    meta: dict = {}
    work_order: dict = {}
    done: dict = {}

    try:
        with httpx.stream(
            "POST",
            f"{BASE}/api/chat",
            json={
                "query": args.query,
                "user_id": args.user,
                "session_id": args.session,
                "store_id": args.store,
            },
            timeout=180,
        ) as resp:
            if resp.status_code != 200:
                print(f"❌ HTTP {resp.status_code}")
                return 1
            for line in resp.iter_lines():
                if not line.startswith("data: "):
                    continue
                payload = json.loads(line[6:])
                if "meta" in payload:
                    meta = payload["meta"]
                elif "content" in payload:
                    if first_token_ms is None:
                        first_token_ms = int((time.perf_counter() - started) * 1000)
                    content_parts.append(payload["content"])
                elif "work_order" in payload:
                    work_order = payload["work_order"]
                elif "done" in payload:
                    # 服务端发的是 {"done": true, "elapsed_ms": N}，这里取整个事件
                    done = payload
                elif "error" in payload:
                    print(f"❌ 服务端错误：{payload['error']}")
                    return 1
    except Exception as exc:  # noqa: BLE001
        print(f"❌ 请求失败（{type(exc).__name__}: {exc}）")
        print("   后端起了吗？用 工具/启动本地.ps1")
        return 1

    answer = "".join(content_parts)
    if not answer.strip():
        print("❌ 未收到任何内容")
        return 1

    print("【路由与状态】")
    agent = meta.get("agent")
    # 被鉴权门拦下时，agent 显示的是 auth_guard——但"本来该由谁处理"同样重要，
    # 所以把 auth_target_agent 也打出来（对应 05-TDD §5.5：鉴权门只拦一下，不该改写路由）。
    target = meta.get("auth_target_agent")
    if target:
        print(f"  意图/Agent : {agent}（被鉴权门拦下，目标 Agent = {target}）")
    else:
        print(f"  意图/Agent : {agent}")
    print(f"  是否命中缓存: {meta.get('cached')}")
    if work_order:
        print(f"  工单        : {work_order}")
    print()
    print("【回答】")
    print(answer.strip()[:600])
    print()
    print("【耗时】")
    total_ms = done.get("elapsed_ms") if isinstance(done, dict) else None
    print(f"  首字 {first_token_ms} ms · 总 {total_ms} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
