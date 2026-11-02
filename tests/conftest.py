"""测试默认关闭真实 embedding，避免加载模型拖慢用例或触发下载。"""

from __future__ import annotations

import os

os.environ["AI_RADAR_EMBED"] = "off"
