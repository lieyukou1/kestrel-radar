"""本地文本向量化：fastembed + 中文小模型。

设计原则：
- 懒加载：只有真正需要时才初始化模型并下载权重。
- 静默降级：未安装、模型缺失或推理失败时返回 None，调用方退回词面匹配。
- 缓存落项目内：模型权重写 data/models/，不污染用户目录。
"""

from __future__ import annotations

import json
import math
import os
import socket
import threading
import urllib.parse

from ..config import PROJECT_ROOT

DEFAULT_MODEL = os.getenv("AI_RADAR_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")
MODEL_CACHE_DIR = PROJECT_ROOT / "data" / "models"
HF_MIRROR = os.getenv("AI_RADAR_HF_MIRROR", "https://hf-mirror.com")

_lock = threading.Lock()
_model = None
_model_name: str | None = None
_unavailable = False
_network_prepared = False


def _log(message: str) -> None:
    print(f"[embedding] {message}", flush=True)


def _tcp_alive(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def prepare_download_env(proxy: str | None = None) -> str:
    """权重只在首次下载时联网：代理可用走代理，否则退回国内镜像。

    必须在 import fastembed 之前调用（huggingface_hub 在导入时读取 HF_ENDPOINT）。
    """
    proxy = proxy or os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
    if proxy:
        parsed = urllib.parse.urlparse(proxy)
        if parsed.hostname and _tcp_alive(parsed.hostname, parsed.port or 80):
            os.environ["HTTPS_PROXY"] = proxy
            os.environ["HTTP_PROXY"] = proxy
            return f"proxy={proxy}"
        os.environ.pop("HTTPS_PROXY", None)
        os.environ.pop("HTTP_PROXY", None)
    if not _tcp_alive("huggingface.co", 443):
        os.environ.setdefault("HF_ENDPOINT", HF_MIRROR)
        return f"mirror={os.environ['HF_ENDPOINT']}"
    return "direct"


def model_name() -> str:
    return _model_name or DEFAULT_MODEL


def get_model(model: str | None = None):
    """返回已加载的模型；任何异常都转成 None，并记住不再重试。"""
    global _model, _model_name, _unavailable, _network_prepared
    name = model or DEFAULT_MODEL
    with _lock:
        if _unavailable:
            return None
        if _model is not None and _model_name == name:
            return _model
        if not _network_prepared:
            _network_prepared = True
            proxy = None
            try:
                from ..config import load_config

                proxy = load_config().network.proxy
            except Exception:
                pass
            _log(f"network {prepare_download_env(proxy)}")
        try:
            from fastembed import TextEmbedding
        except Exception as exc:  # 未安装或依赖损坏
            _unavailable = True
            _log(f"fastembed unavailable: {type(exc).__name__}: {exc}")
            return None
        try:
            _model = TextEmbedding(model_name=name, cache_dir=str(MODEL_CACHE_DIR))
            _model_name = name
            _log(f"loaded {name}")
            return _model
        except Exception as exc:
            _unavailable = True
            _log(f"model load failed: {type(exc).__name__}: {exc}")
            return None


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """批量编码；模型不可用或被显式关闭时返回 None。"""
    if not texts:
        return []
    if os.getenv("AI_RADAR_EMBED", "").strip().lower() in ("0", "off", "false", "none"):
        return None
    model = get_model()
    if model is None:
        return None
    try:
        return [vector.tolist() for vector in model.embed(texts)]
    except Exception as exc:
        _log(f"embed failed: {type(exc).__name__}: {exc}")
        return None


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left == 0 or norm_right == 0:
        return 0.0
    return dot / (norm_left * norm_right)


def _plain(value) -> str:
    """kb_articles 的列表字段存的是 JSON 字符串，直接拼进向量文本会引入噪声。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
    if isinstance(parsed, list):
        return "；".join(str(item) for item in parsed if str(item).strip())
    return text


def index_text(document, kb) -> str:
    """知识卡片是中文输出，标题与摘要补足上下文；截断避免超长输入。"""
    parts = [document["title"], document["abstract"]]
    if kb is not None:
        parts.extend([_plain(kb["problem"]), _plain(kb["method"]), _plain(kb["conclusions"])])
    return "\n".join(
        str(part).strip() for part in parts if part and str(part).strip()
    )[:3000]


def ensure_vectors(
    conn, document_ids: list[int], *, force: bool = False
) -> dict[int, list[float]]:
    """读取或补算指定文章的向量；模型不可用时返回已存在的部分。"""
    from ..store import repo

    name = model_name()
    result: dict[int, list[float]] = {}
    missing: list[int] = []
    for document_id in dict.fromkeys(document_ids):
        vector = None if force else repo.get_vector(conn, document_id, name)
        if vector is None:
            missing.append(document_id)
        else:
            result[document_id] = vector
    if not missing:
        return result

    kept: list[int] = []
    texts: list[str] = []
    for document_id in missing:
        document = repo.get_document(conn, document_id)
        kb = repo.get_kb_article(conn, document_id)
        # 没精读过的候选也要向量：话题聚类只用「标题 + 摘要」，不等 kb_ready。
        if document is None:
            continue
        text = index_text(document, kb)
        if not text:
            continue
        kept.append(document_id)
        texts.append(text)
    vectors = embed_texts(texts)
    if vectors is None:
        return result
    for document_id, vector in zip(kept, vectors):
        repo.save_vector(conn, document_id=document_id, model=name, vector=vector)
        result[document_id] = vector
    return result


def backfill_vectors(conn, *, batch_size: int = 8) -> dict:
    """为所有已精读但缺少向量的文章补算，供首次启用或换模型时使用。"""
    from ..store import repo

    name = model_name()
    pending = repo.documents_missing_vector(conn, name)
    indexed = 0
    for start in range(0, len(pending), batch_size):
        ids = [
            int(row["id"]) for row in pending[start : start + batch_size]
        ]
        vectors = ensure_vectors(conn, ids)
        indexed += sum(1 for document_id in ids if document_id in vectors)
    return {"model": name, "pending": len(pending), "indexed": indexed}

