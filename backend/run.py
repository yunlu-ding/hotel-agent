"""随行管家 · 本地启动入口。

    python run.py

启动后：
    http://127.0.0.1:5000/            服务自述
    http://127.0.0.1:5000/api/health  依赖自检
    http://127.0.0.1:5000/api/docs    接口文档
"""

import os
import sys

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(BACKEND_DIR, "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

# Windows 终端中文输出
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "5000"))
    print(f"🚀 随行管家后端启动中：http://{host}:{port}")
    uvicorn.run("app_main:app", host=host, port=port, reload=False)
