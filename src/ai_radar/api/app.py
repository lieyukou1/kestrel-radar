from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..config import PROJECT_ROOT
from .routes import articles, assistant, digests, domains, jobs, query

# 开发期前端（Vite）与 API 不同端口；生产期同源托管，不受这里影响。
CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8765",
    "http://127.0.0.1:8765",
]


def _version() -> str:
    try:
        return version("kestrel-radar")
    except PackageNotFoundError:
        return "0.1.0"


def create_app(root: Path | None = None) -> FastAPI:
    app = FastAPI(
        title="Kestrel Radar API",
        version=_version(),
        description="本地资讯雷达：日报、事件、归档、检索与知识库问答。",
    )
    app.state.root = Path(root) if root else PROJECT_ROOT
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for module in (domains, digests, articles, query, assistant, jobs):
        app.include_router(module.router, prefix="/api/v1")

    @app.get("/api/v1/healthz", tags=["health"])
    def healthz() -> dict:
        return {"ok": True, "version": _version()}

    return app


def run_api(
    root: Path | None = None, *, host: str = "127.0.0.1", port: int = 8765
) -> None:
    import uvicorn

    uvicorn.run(create_app(root), host=host, port=port, log_level="info")
