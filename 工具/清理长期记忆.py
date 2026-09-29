"""清理长期记忆：删掉不该跨会话保留的条目。

    python 工具/清理长期记忆.py --list                 # 只列出
    python 工具/清理长期记忆.py --user user_1001       # 删除某用户的全部长期记忆
    python 工具/清理长期记忆.py --user user_1001 --stay-scoped-only   # 只删"入住级"的脏数据

背景（这是一次真实事故的善后工具）：
  长期记忆原本会把"客人这次住哪家店、哪个房间"当成**用户偏好**记下来。
  结果下一个会话（哪怕换了一家店）会把这句注入上下文，
  模型于是认为客人仍然住在上一家店，**覆盖掉系统真正注入的门店**——
  表现为"同一句话在两家店答出同一家店的答案"，而且回答听起来很正常。

修复分三层（本脚本只负责第三层）：
  1. 抽取提示词：明确禁止提取门店/房间/订单/日期（写入时拦截）；
  2. 读取过滤：`is_stay_scoped()` 在注入上下文前再挡一道；
  3. **清理历史脏数据**（本脚本）——因为已经写进去的不会被前两层影响。
"""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
for path in (os.path.join(BACKEND, "agent"), os.path.join(BACKEND, "app")):
    if path not in sys.path:
        sys.path.insert(0, path)

from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(BACKEND, "agent", ".env"), override=False)

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

COLLECTION = "long_term_memory"


def main() -> int:
    parser = argparse.ArgumentParser(description="清理长期记忆")
    parser.add_argument("--user", default=None, help="只处理某个 user_id")
    parser.add_argument("--list", action="store_true", help="只列出，不删除")
    parser.add_argument("--stay-scoped-only", action="store_true",
                        help="只删被判定为'入住级'的脏数据，保留正常偏好")
    args = parser.parse_args()

    from pymilvus import MilvusClient
    from core.memory.preference_extractor import is_stay_scoped

    client = MilvusClient(uri=os.getenv("MILVUS_URI"), token=os.getenv("MILVUS_API_KEY"))
    # 注意：Milvus 的按表达式删除对非主键并不总是生效（实测：delete(filter='user_id == ...') 返回成功
    # 但数据还在）。可靠做法是**先查出主键，再按主键删**。
    fields = client.describe_collection(collection_name=COLLECTION)["fields"]
    pk_field = next(f["name"] for f in fields if f.get("is_primary"))

    rows = client.query(
        collection_name=COLLECTION, filter="",
        output_fields=[pk_field, "user_id", "content"], limit=500,
    )
    print(f"集合 {COLLECTION} 现有 {len(rows)} 条")

    targets: list[dict] = []
    for row in rows:
        user_id = row.get("user_id") or ""
        content = row.get("content") or ""
        if args.user and user_id != args.user:
            continue
        dirty = is_stay_scoped(content)
        flag = "❌ 入住级脏数据" if dirty else "✅ 正常偏好"
        print(f"  {user_id} | {content[:52]} | {flag}")
        if args.stay_scoped_only and not dirty:
            continue
        targets.append(row)

    print(f"\n待删除 {len(targets)} 条")
    if args.list or not targets:
        return 0

    # 按主键分批删除（可靠且可核对条数）
    ids = [row[pk_field] for row in targets]
    removed = 0
    for start in range(0, len(ids), 50):
        batch = ids[start:start + 50]
        client.delete(collection_name=COLLECTION, ids=batch)
        removed += len(batch)
    print(f"已按主键清理 {removed} 条")

    rest = client.query(collection_name=COLLECTION, filter="", output_fields=["user_id", "content"], limit=500)
    print(f"剩余 {len(rest)} 条")
    for row in rest:
        print(f"  {row.get('user_id')} | {(row.get('content') or '')[:48]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
