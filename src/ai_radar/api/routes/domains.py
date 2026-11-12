from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from ...config import ConfigError, load_config
from ...domain.profile import DomainError, load_profile
from ..serialize import profile_dict

router = APIRouter(tags=["domains"])


def _domain_ids(root: Path) -> list[str]:
    base = root / "config" / "domains"
    if not base.exists():
        return []
    return sorted(
        path.name for path in base.iterdir() if (path / "domain.yaml").exists()
    )


def _domain_entry(root: Path, domain_id: str) -> dict:
    profile = load_profile(root, domain_id)
    entry = profile_dict(profile)
    try:
        config = load_config(root, domain=domain_id)
        entry["sources"] = [
            {
                "id": source.id,
                "name": source.name,
                "kind": source.kind,
                "authority": source.authority,
                "enabled": source.enabled,
            }
            for source in config.sources
        ]
        entry["weights"] = {
            "scoring": config.weights.get("scoring"),
            "maturity_hours": config.weights.get("maturity_hours"),
            "event_window_hours": (config.weights.get("topics") or {}).get(
                "event_window_hours"
            ),
        }
    except ConfigError:
        entry["sources"] = []
        entry["weights"] = {}
    entry["db"] = f"data/domains/{profile.id}/ai_radar.db"
    return entry


@router.get("/domains")
def list_domains(request: Request) -> list[dict]:
    root = Path(request.app.state.root)
    entries: list[dict] = []
    for domain_id in _domain_ids(root):
        try:
            entries.append(_domain_entry(root, domain_id))
        except DomainError:
            # 坏掉的领域包不拖垮整个列表，网页端仍能看到其他领域。
            continue
    return entries


@router.get("/domains/{domain_id}")
def get_domain(domain_id: str, request: Request) -> dict:
    root = Path(request.app.state.root)
    if domain_id not in _domain_ids(root):
        raise HTTPException(status_code=404, detail=f"领域包不存在：{domain_id}")
    return _domain_entry(root, domain_id)
