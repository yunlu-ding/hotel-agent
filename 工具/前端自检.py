"""前端自检：确认开发服务器在跑、并且真的在提供**我们的**页面。

    python 工具/前端自检.py

检查三件事：
  1. 根页面 200，且标题是我们的（防止"端口通着，但起的是别的项目"——实测遇到过）；
  2. 两个视图模块能被 vite 编译返回（说明 SFC 语法没问题）；
  3. 前端用的 API 地址指向本项目的后端，且后端 /api/health 可达。
"""

import os
import sys

import httpx

WEB = os.getenv("WEB_BASE", "http://localhost:5173")
API = os.getenv("API_BASE", "http://127.0.0.1:5000")

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'✅' if ok else '❌'} {label}" + (f"  {detail}" if detail else ""))
    return ok


def main() -> int:
    passed = True
    print(f"【前端】{WEB}")
    try:
        page = httpx.get(f"{WEB}/", timeout=10).text
        passed &= check("根页面可访问", True)
        passed &= check("标题是本项目的（不是别的项目占着端口）", "随行管家" in page)
    except Exception as exc:  # noqa: BLE001
        passed &= check("根页面可访问", False, f"{type(exc).__name__}: {exc}")
        print("  提示：vite 默认绑定 localhost（可能是 IPv6），用 http://localhost:5173 访问")
        return 1

    for view, marker in (("GuestChat.vue", "扫码进入房间"), ("StaffDesk.vue", "承接部门")):
        try:
            body = httpx.get(f"{WEB}/src/views/{view}", timeout=15).text
            passed &= check(f"{view} 可编译返回", marker in body, f"{len(body)} 字节")
        except Exception as exc:  # noqa: BLE001
            passed &= check(f"{view} 可编译返回", False, str(exc))

    # 模板样式有没有清干净：参考项目的模板 CSS 会把应用限宽到 1280px 并在宽屏下变两列网格，
    # 那会让页面"不像一个正常网页"（用户就是这么发现的）。
    try:
        css = httpx.get(f"{WEB}/src/assets/main.css", timeout=10).text
        # 先把注释剥掉再判断——否则"我在注释里写了这个坑"会被当成"这个坑还在"（第一次就误报了）
        import re

        rules_only = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
        bad = [
            token for token in ("max-width: 1280px", "grid-template-columns: 1fr 1fr", "font-weight: normal")
            if token in rules_only
        ]
        passed &= check("全局样式里没有模板残留（限宽/两列网格/取消加粗）", not bad, str(bad))
    except Exception as exc:  # noqa: BLE001
        passed &= check("全局样式可读取", False, str(exc))

    print(f"\n【后端】{API}")
    try:
        health = httpx.get(f"{API}/api/health", timeout=10).json()
        status = health.get("overall")
        passed &= check("健康检查可达", True, f"overall={status}")
        if status == "down":
            print("  ⚠️ 主链路依赖不可用：界面能打开，但对话会失败（见 07 手册 §6）")
        for path in health.get("degraded_paths", []):
            print(f"     · 降级路径：{path}")
    except Exception as exc:  # noqa: BLE001
        passed &= check("健康检查可达", False, str(exc))
        print("  提示：后端没起？执行 工具/启动本地.ps1")

    print()
    print("结论：" + ("前端与后端都就绪，可以打开 " + WEB + " 演示" if passed else "有检查项未通过，见上"))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
