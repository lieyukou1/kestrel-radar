from __future__ import annotations

from ..adapters.sources.base import RawItem

TAGS = [
    "1 模型与架构",
    "2 训练与对齐",
    "3 推理与加速",
    "4 Agent 与应用",
    "5 多模态与生成",
    "6 系统与基础设施",
    "7 理论与可解释性",
    "8 数据与评测",
]

# M1 使用关键词启发式；后续由 LLM 分类替换。
# 中英文都要覆盖：中文媒体标题里几乎不含英文术语，只有英文词表时会全部落到默认维度。
KEYWORDS: dict[str, tuple[str, ...]] = {
    "1 模型与架构": (
        "transformer", "architecture", "mixture of experts", "moe", "mamba", "ssm",
        "foundation model", "language model", "attention", "pretraining", "model",
        "大模型", "基础模型", "语言模型", "架构", "注意力", "混合专家", "神经网络", "深度学习",
    ),
    "2 训练与对齐": (
        "training", "fine-tune", "finetune", "rlhf", "dpo", "grpo", "alignment",
        "reward model", "synthetic data", "distillation", "instruction tuning",
        "训练", "微调", "对齐", "强化学习", "蒸馏", "指令微调", "预训练", "合成数据",
    ),
    "3 推理与加速": (
        "inference", "quantization", "kv cache", "speculative decoding", "serving",
        "latency", "throughput", "compiler", "kernel", "acceleration", "efficient",
        "推理", "量化", "加速", "延迟", "吞吐", "显存", "部署", "编译", "算子", "提速",
    ),
    "4 Agent 与应用": (
        "agent", "tool use", "tool calling", "rag", "retrieval", "workflow", "mcp",
        "computer use", "multi-agent", "assistant", "coding agent",
        "智能体", "工作流", "工具调用", "检索增强", "编程助手", "插件",
    ),
    "5 多模态与生成": (
        "multimodal", "vision", "image", "video", "speech", "audio", "diffusion",
        "generation", "vlm", "text-to-image", "text-to-video",
        "多模态", "视觉", "图像", "视频", "语音", "扩散", "文生图", "文生视频", "生成式",
    ),
    "6 系统与基础设施": (
        "distributed", "cluster", "gpu", "scheduler", "storage", "infrastructure",
        "fault tolerance", "checkpoint", "system", "hardware",
        "集群", "分布式", "算力", "芯片", "存储", "调度", "基础设施", "服务器", "数据中心",
    ),
    "7 理论与可解释性": (
        "theory", "scaling law", "interpretability", "explainability", "probing",
        "analysis", "theorem", "proof", "emergent",
        "理论", "可解释", "机理", "缩放律", "涌现", "证明",
    ),
    "8 数据与评测": (
        "dataset", "benchmark", "evaluation", "eval harness", "leaderboard",
        "corpus", "annotation", "metric",
        "数据集", "基准", "评测", "榜单", "标注", "指标", "测试集",
    ),
}

DEFAULT_TAG = {
    "paper": "1 模型与架构",
    "forum": "4 Agent 与应用",
    "blog": "4 Agent 与应用",
    "repo": "6 系统与基础设施",
}


def classify(item: RawItem) -> tuple[str, list[str]]:
    text = f"{item.title} {item.summary or ''}".lower()
    scores: dict[str, int] = {}
    for tag, words in KEYWORDS.items():
        scores[tag] = sum(text.count(word) for word in words)

    ranked = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
    primary = ranked[0][0] if ranked and ranked[0][1] > 0 else DEFAULT_TAG.get(item.kind, TAGS[0])
    tags = [tag for tag, score in ranked if score > 0][:3]
    if primary not in tags:
        tags.insert(0, primary)
    return primary, tags[:3]
