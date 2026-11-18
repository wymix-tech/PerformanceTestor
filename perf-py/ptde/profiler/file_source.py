"""文件来源画像导入（R2 T2.4）：无生产库访问时的替代来源。

- 支持 YAML/JSON 统计文件导入，结构同 StatisticsProfile（A6 双读统一入口）；
- 导入后强制 source.type="file_upload"，与库只读采集/测试库自画像可区分标记；
- 导入即 seal()：指纹由文件内容决定（不含 collected_at 易变字段），
  同一文件两次导入指纹一致（§5.5 验收门禁 5）。
"""

from __future__ import annotations

import json
from pathlib import Path

from ptde.models.profile import StatisticsProfile
from ptde.profiler.collector import SOURCE_TYPE_FILE_UPLOAD

__all__ = ["load_statistics_profile"]

_YAML_SUFFIXES = {".yaml", ".yml"}


def load_statistics_profile(
    path: str | Path, *, source_type: str = SOURCE_TYPE_FILE_UPLOAD
) -> StatisticsProfile:
    """导入统计文件 → 已 seal 的 StatisticsProfile（指纹稳定）。"""
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    suffix = p.suffix.lower()
    if suffix == ".json":
        profile = StatisticsProfile.from_json(text)
    elif suffix in _YAML_SUFFIXES:
        profile = StatisticsProfile.from_yaml(text)
    else:
        # 未识别后缀：先按 JSON 解析，失败回退 YAML（内容形态探测）
        try:
            json.loads(text)
            profile = StatisticsProfile.from_json(text)
        except (json.JSONDecodeError, ValueError):
            profile = StatisticsProfile.from_yaml(text)
    profile.source.type = source_type  # 来源可区分标记
    return profile.seal()
