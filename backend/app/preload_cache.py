import asyncio
import json
import os
import sys

# 确保能正确导入 app 模块
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.append(PROJECT_DIR)

from infra.cache import semantic_cache

# 预置问答不再硬编码在代码里，改从场景数据文件读取：
# 这样换业务场景时只改数据，不动代码，也方便对缓存内容做版本管理与评审。
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_QA_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(PROJECT_ROOT)))),
    "CloudAgent-酒店客服助手",
    "数据",
    "缓存预置问答.json",
)


def load_preset_qa(path: str | None = None) -> list[dict]:
    qa_path = path or os.getenv("CACHE_QA_FILE") or DEFAULT_QA_FILE
    with open(qa_path, encoding="utf-8") as fh:
        items = json.load(fh)
    # 兼容两种写法：response / answer
    return [
        {"query": item["query"], "response": item.get("response") or item.get("answer")}
        for item in items
    ]

async def preload_cache():
    print("🔄 开始预热 L1 语义缓存...")
    await semantic_cache.initialize()
    
    for item in load_preset_qa():
        query = item["query"]
        response = item["response"]
        print(f"注入缓存 -> Query: '{query}'")
        
        # 调用 set_cache 将问题向量化并写入 Milvus 语义缓存集合
        await semantic_cache.set_cache(query, response)
        
    print("✅ 缓存预热完成！")

if __name__ == "__main__":
    asyncio.run(preload_cache())
