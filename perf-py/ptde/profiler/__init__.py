"""PTDE 统计画像包（R2 批次 B2）。

- collector：只读统计采集器（A5 双 DSN + SELECT 白名单 + 采样安全 + 元数据）
- file_source：无生产库时的文件来源画像导入（T2.4）
- merger：按总方案 §4.4 优先级表融合统一画像（T2.6）
"""

from ptde.profiler.collector import (
    SOURCE_TYPE_DB_READONLY,
    SOURCE_TYPE_FILE_UPLOAD,
    SOURCE_TYPE_TEST_DB_SELF,
    CollectionManifest,
    CollectorSettings,
    StatisticsCollectionError,
    StatisticsCollector,
    adapter_for_dsn,
    assert_readonly_sql,
    credential_label,
    fit_zipf,
    is_sensitive_column,
)
from ptde.profiler.file_source import load_statistics_profile
from ptde.profiler.merger import ProfileMerger

__all__ = [
    "CollectionManifest",
    "CollectorSettings",
    "ProfileMerger",
    "SOURCE_TYPE_DB_READONLY",
    "SOURCE_TYPE_FILE_UPLOAD",
    "SOURCE_TYPE_TEST_DB_SELF",
    "StatisticsCollectionError",
    "StatisticsCollector",
    "adapter_for_dsn",
    "assert_readonly_sql",
    "credential_label",
    "fit_zipf",
    "is_sensitive_column",
    "load_statistics_profile",
]
