from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request

from ..config import AppConfig, ConfigError, load_config
from ..domain.profile import DomainError
from ..store.db import connect, init_db


@dataclass(slots=True)
class ApiContext:
    """一次请求的配置与数据库连接。

    SQLite 连接不能跨线程复用，所以每个请求单独开、用完即关；
    WAL 模式下多个读请求不会互相阻塞。
    """

    config: AppConfig
    conn: sqlite3.Connection

    @property
    def domain_id(self) -> str:
        return self.config.domain.id if self.config.domain else "ai"


def get_context(
    request: Request,
    domain: str = Query("ai", description="领域包 id，例如 ai / finance"),
) -> Iterator[ApiContext]:
    root = Path(request.app.state.root)
    try:
        config = load_config(root, domain=domain)
    except (ConfigError, DomainError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    conn = connect(config.db_path)
    init_db(conn)
    try:
        yield ApiContext(config=config, conn=conn)
    finally:
        conn.close()


# 路由统一用 Annotated 注入，避免 ruff B008（Depends 在默认参数里）误报。
Ctx = Annotated[ApiContext, Depends(get_context)]
