"""工单巡检：把超时未完成的工单升级，并打印结果。

    python 工具/工单巡检.py                # 扫一次
    python 工具/工单巡检.py --store H_SH_001
    python 工具/工单巡检.py --interval 60  # 每 60 秒扫一次（Ctrl+C 退出）

为什么做成手动脚本而不是后台定时任务：
  评测与演示需要**确定性地复现"超时"**——把工单的 due_at 拨到过去，再跑一次巡检，
  就能验证升级链路。如果它是个藏在后台的定时器，反而不好复现、也不好演示。
  真实上线时把 `--interval` 交给任务计划程序即可。
"""

import argparse
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT, "backend", "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

from service import work_order_service as wos  # noqa: E402


def sweep_once(store_id: str | None) -> int:
    escalated = wos.sweep_overdue(store_id=store_id)
    stamp = time.strftime("%H:%M:%S")
    if not escalated:
        print(f"[{stamp}] 没有超时工单")
        return 0
    print(f"[{stamp}] 升级 {len(escalated)} 张超时工单：")
    for item in escalated:
        print(
            f"    {item['work_order_id']}  {item['assignee_dept']:<12} "
            f"超时 {item['overdue_minutes']} 分钟  （原状态 {item['previous_status']}）"
        )
    return len(escalated)


def main() -> int:
    parser = argparse.ArgumentParser(description="工单超时巡检")
    parser.add_argument("--store", default=None, help="只巡检某家门店")
    parser.add_argument("--interval", type=int, default=0, help="重复间隔（秒），0 表示只扫一次")
    args = parser.parse_args()

    if args.interval <= 0:
        sweep_once(args.store)
        return 0

    print(f"每 {args.interval} 秒巡检一次（Ctrl+C 退出）")
    try:
        while True:
            sweep_once(args.store)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
