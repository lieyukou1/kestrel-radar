"""本地 Web API：把现有的只读查询与动作包装成 HTTP 接口。

只依赖 FastAPI/uvicorn，不改变 pipeline 的任何行为；网页端（P4）通过
`/api/v1/*` 读取日报、事件、归档与知识库。
"""

from __future__ import annotations

from .app import create_app, run_api

__all__ = ["create_app", "run_api"]
