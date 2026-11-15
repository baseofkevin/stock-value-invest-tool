"""results.db 的本地看板（Flask + Chart.js）。

    ./run.py web                 # 或 python3 -m web
    ./run.py web --port 8000 --debug

只读：网站不写库，四个工具照旧把结果写进 results.db，刷新页面即最新。
"""
from .app import create_app, main

__all__ = ["create_app", "main"]
