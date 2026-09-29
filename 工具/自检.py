"""随行管家 · 环境自检。

用法（在项目根目录执行）：
    python 工具/自检.py             # 浅检查：不产生任何外部调用费用
    python 工具/自检.py --deep      # 深检查：真实调用一次 embedding，验证 key 与网络
    python 工具/自检.py --deep --strict   # 主链路依赖不通时以非零码退出（可用于脚本）

为什么要有这个脚本：参考项目踩过"看起来配好了、实际这条路径从来没跑过"的坑
（语义缓存把相似度当距离，语义路径永不触发）。所以自检要能回答两件事：
    1) 依赖是否可达；2) 当前环境下**哪些降级路径正在生效**。
"""

import argparse
import os
import sys

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOL_DIR)
APP_DIR = os.path.join(ROOT, "backend", "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


def _print_packages() -> None:
    import importlib.metadata as md

    names = [
        "fastapi",
        "uvicorn",
        "langgraph",
        "langchain-core",
        "langchain-openai",
        "langchain-mcp-adapters",
        "mcp",
        "pymilvus",
        "psycopg",
        "redis",
        "httpx",
    ]
    print("【运行环境】")
    print(f"  python {sys.version.split()[0]}")
    for name in names:
        try:
            print(f"  {name:26} {md.version(name)}")
        except Exception:  # noqa: BLE001
            print(f"  {name:26} 缺失")
    print()


def _print_service() -> None:
    import httpx

    print("【后端服务】")
    try:
        resp = httpx.get("http://127.0.0.1:5000/", timeout=3)
        payload = resp.json()
        print(f"  HTTP {resp.status_code} · {payload.get('service', '')}（{payload.get('stage', '')}）")
    except Exception as exc:  # noqa: BLE001
        print(f"  未启动（{type(exc).__name__}）—— 用 工具/启动本地.ps1 启动")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="随行管家环境自检")
    parser.add_argument("--deep", action="store_true", help="真实调用一次 embedding")
    parser.add_argument("--strict", action="store_true", help="主链路依赖不通时非零退出")
    parser.add_argument("--verbose", action="store_true", help="打印 Milvus 集合清单等细节")
    args = parser.parse_args()

    _print_packages()
    _print_service()

    from infra.health import collect_health

    health = collect_health(deep=args.deep)
    icons = {"ok": "✅", "degraded": "⚠️", "down": "❌", "disabled": "⏸"}

    print(f"【依赖自检】overall = {health['overall'].upper()}"
          + ("（深检查）" if args.deep else "（浅检查，加 --deep 做真实调用）"))
    for name, check in health["checks"].items():
        icon = icons.get(check["status"], "?")
        print(f"  {icon} {name:12} [{check['role']:8}] {check['status']:8} {check['detail']}")
        if args.verbose and check.get("collections"):
            for collection in check["collections"]:
                print(f"        - 集合: {collection}")

    print()
    print("【当前生效的降级路径】")
    if health["degraded_paths"]:
        for path in health["degraded_paths"]:
            print(f"  - {path}")
    else:
        print("  （无）")

    if health["primary_down"]:
        print()
        print(f"⛔ 主链路依赖不可用：{', '.join(health['primary_down'])}")
        print("   这两条链上的能力（知识检索 / 语义缓存 / 生成）会失效，评测结论不可用。")

    if args.strict and health["primary_down"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
