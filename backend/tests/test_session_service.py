"""会话与鉴权自测（可直接运行）。

    python backend/tests/test_session_service.py

覆盖：
    1. 阶段识别（上下文信号 > 数据信号，且不被语言信号改写）
    2. 逐槽位凭据收集：房间号 → 姓氏 → 离店日
    3. 三要素错误时的**统一话术**（不区分"房号不存在"与"姓氏不匹配"）
    4. 连续失败 3 次 → 锁定并建议转前台（防房号枚举）
    5. 验证过程中可以直接要求转人工（不做鉴权拦截）
    6. 退房即失效（隐私红线）

测试数据用 `sttest-` 前缀标记，运行前后清理。
"""

import os
import sys
from datetime import date

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(BACKEND, "app") not in sys.path:
    sys.path.insert(0, os.path.join(BACKEND, "app"))

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

import psycopg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(BACKEND, "agent", ".env"), override=False)

from service import session_service as ss  # noqa: E402

PREFIX = "sttest-"
STORE = "H_SH_001"
ROOM = "1208"
LAST_NAME = "陈"
CHECKOUT = "2026-09-18"

passed = 0
failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {label}")
    else:
        failed += 1
        print(f"  ❌ {label} {detail}")


def _cleanup() -> None:
    with psycopg.connect(os.getenv("POSTGRES_DSN"), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM auth_attempts WHERE session_id LIKE %s", (f"{PREFIX}%",))
        cur.execute(
            "DELETE FROM stay_context WHERE stay_id IN "
            "(SELECT stay_id FROM guest_sessions WHERE session_id LIKE %s)",
            (f"{PREFIX}%",),
        )
        cur.execute("DELETE FROM guest_sessions WHERE session_id LIKE %s", (f"{PREFIX}%",))
        cur.execute("DELETE FROM stay_context WHERE room_no = %s AND store_id = %s", (ROOM, STORE))


def main() -> int:
    _cleanup()

    print("【1】阶段识别：上下文信号优先")
    check("在住（今天=9/16）→ IN",
          ss.detect_stage(store_id=STORE, room_no=ROOM, today=date(2026, 9, 16)) == "IN")
    check("还没到店（9/10）→ PRE",
          ss.detect_stage(store_id=STORE, room_no=ROOM, today=date(2026, 9, 10)) == "PRE")
    check("已离店（9/25）→ POST",
          ss.detect_stage(store_id=STORE, room_no=ROOM, today=date(2026, 9, 25)) == "POST")
    check("房号查不到 → UNKNOWN（不猜）",
          ss.detect_stage(store_id=STORE, room_no="9999", today=date(2026, 9, 16)) == "UNKNOWN")

    print("\n【2】会话创建与逐槽位收集")
    session = f"{PREFIX}{os.getpid()}"
    info = ss.get_or_create_session(session, store_id=STORE, room_no=ROOM)
    check("会话创建时阶段判定为 IN", info["stage"] == "IN", info["stage"])
    check("初始鉴权等级 0（扫码 ≠ 已验证）", info["auth_level"] == 0, str(info["auth_level"]))

    step = ss.begin_auth(session, required_level=1, original_query="能送两瓶水吗")
    check("第 1 槽位问房间号", step["slot"] == "room_no", step["slot"])
    step = ss.advance_auth(session, "1208", store_id=STORE)
    check("第 2 槽位问姓氏", step["slot"] == "last_name", step["slot"])
    step = ss.advance_auth(session, "我姓陈", store_id=STORE)
    check("第 3 槽位问离店日", step["slot"] == "check_out_date", step["slot"])
    check("未答完不通过", ss.current_auth_level(session) == 0)

    print("\n【3】错误凭据 → 统一话术（不泄露哪个字段错了）")
    step = ss.advance_auth(session, "2026-09-20", store_id=STORE)
    check("结果 FAIL", step["status"] == "FAIL", step.get("status"))
    check("话术统一（不提'房号不存在'/'姓氏不符'）",
          "没能匹配到" in step["message"] and "姓氏不符" not in step["message"], step["message"][:40])

    print("\n【4】连续失败 → 锁定")
    ss.begin_auth(session, required_level=1, original_query="能送两瓶水吗")
    ss.advance_auth(session, "1208", store_id=STORE)
    ss.advance_auth(session, "王", store_id=STORE)          # 错姓氏
    ss.advance_auth(session, "2026-09-18", store_id=STORE)  # 第 2 次失败
    ss.begin_auth(session, required_level=1, original_query="能送两瓶水吗")
    ss.advance_auth(session, "1208", store_id=STORE)
    ss.advance_auth(session, "李", store_id=STORE)
    locked = ss.advance_auth(session, "2026-09-18", store_id=STORE)  # 第 3 次失败
    check("锁定并建议转前台", locked["status"] == "LOCKED", locked.get("status"))

    print("\n【5】验证过程中可以直接要求转人工")
    session2 = f"{PREFIX}esc-{os.getpid()}"
    ss.get_or_create_session(session2, store_id=STORE, room_no=ROOM)
    ss.begin_auth(session2, required_level=1, original_query="空调不制冷")
    escape = ss.advance_auth(session2, "太麻烦了，帮我转人工", store_id=STORE)
    check("可以中途求助（不被鉴权拦住）", escape["status"] == "ESCAPE", escape.get("status"))

    print("\n【6】正确凭据 → 通过并升级会话")
    session3 = f"{PREFIX}ok-{os.getpid()}"
    ss.get_or_create_session(session3, store_id=STORE, room_no=ROOM)
    ss.begin_auth(session3, required_level=1, original_query="能送两瓶水吗")
    ss.advance_auth(session3, "1208", store_id=STORE)
    ss.advance_auth(session3, "陈", store_id=STORE)
    ok = ss.advance_auth(session3, "9月18日", store_id=STORE)
    check("结果 OK", ok["status"] == "OK", ok.get("status"))
    check("鉴权等级升到 1", ss.current_auth_level(session3) == 1, str(ss.current_auth_level(session3)))
    check("原始问题被记住了（验证完继续答）", ok.get("original_query") == "能送两瓶水吗")
    check("生成了入住上下文 stay_id", bool(ok.get("stay_id")))

    print("\n【7】退房即失效（隐私红线）")
    with psycopg.connect(os.getenv("POSTGRES_DSN"), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE stay_context SET expires_at = now() - interval '1 hour' WHERE stay_id = %s",
            (ok["stay_id"],),
        )
    expired = ss.expire_checkouts()
    check("有过期入住上下文被清理", expired >= 1, str(expired))
    with psycopg.connect(os.getenv("POSTGRES_DSN"), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, guest_last_name FROM stay_context WHERE stay_id = %s", (ok["stay_id"],))
        status, name = cur.fetchone()
    check("状态置为 EXPIRED 且姓名已清空", status == "EXPIRED" and name == "", f"{status}/{name}")

    print("\n【鉴权口径】")
    print(f"  {ss.auth_stats(store_id=STORE)}")

    _cleanup()
    print(f"\n结果：通过 {passed} 项，失败 {failed} 项（测试数据已清理）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
