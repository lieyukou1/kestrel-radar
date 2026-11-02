"""验证本地 embedding 方案：fastembed + 中文小模型。

用法：
    uv run python scripts/embed_probe.py

产物：
    模型缓存落在 data/models/（不写用户目录），脚本本身只打印指标。
"""

from __future__ import annotations

import os
import time

from ai_radar.config import PROJECT_ROOT, load_config
from ai_radar.pipeline.embedding import prepare_download_env

MODEL = os.getenv("AI_RADAR_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")
CACHE_DIR = PROJECT_ROOT / "data" / "models"

SAMPLES = [
    "用强化学习对齐大语言模型的输出，降低有害内容",
    "通过偏好优化让模型输出更符合人类意图",
    "A new kernel for faster attention computation on GPUs",
    "量化与 KV cache 压缩降低推理显存占用",
]


def main() -> int:
    config = load_config()
    print(f"[probe] network={prepare_download_env(config.network.proxy)}")
    print(f"[probe] hf_endpoint={os.getenv('HF_ENDPOINT', 'https://huggingface.co')}")
    print(f"[probe] cache_dir={CACHE_DIR}")

    from fastembed import TextEmbedding

    supported = [item["model"] for item in TextEmbedding.list_supported_models()]
    print(f"[probe] supported_models={len(supported)}")
    candidates = [name for name in supported if "zh" in name.lower() or "multi" in name.lower()]
    for name in candidates:
        print(f"[probe] candidate: {name}")

    if MODEL not in supported:
        print(f"[probe] ERROR model not supported: {MODEL}")
        print("[probe] 请从上面的 candidate 里挑一个，用 AI_RADAR_EMBED_MODEL 指定后重跑")
        return 2

    started = time.perf_counter()
    model = TextEmbedding(model_name=MODEL, cache_dir=str(CACHE_DIR))
    load_seconds = time.perf_counter() - started

    started = time.perf_counter()
    vectors = list(model.embed(SAMPLES))
    embed_seconds = time.perf_counter() - started

    import numpy as np

    matrix = np.array([vec / (np.linalg.norm(vec) + 1e-9) for vec in vectors])
    similarity = matrix @ matrix.T

    print(f"[probe] model={MODEL} dim={len(vectors[0])}")
    print(f"[probe] load={load_seconds:.2f}s embed={embed_seconds:.3f}s ({len(SAMPLES)} 条)")
    print("[probe] similarity:")
    for i in range(len(SAMPLES)):
        for j in range(i + 1, len(SAMPLES)):
            print(f"  {i}-{j}: {similarity[i][j]:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
