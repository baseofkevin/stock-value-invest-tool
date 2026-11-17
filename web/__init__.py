"""results.db 的本地看板（Flask + Chart.js）。

    ./run.py web                 # 或 python3 -m web；默认 debug，改模板自动重载
    ./run.py web --port 8000
    ./run.py web --no-debug      # 关掉调试模式（不自动重载）

只读：网站不写库，四个工具照旧把结果写进 results.db，刷新页面即最新。
"""
from .app import create_app, main

__all__ = ["create_app", "main"]
