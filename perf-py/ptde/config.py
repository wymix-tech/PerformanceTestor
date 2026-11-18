"""PTDE 配置框架（实施方案 T0.6）。

- `strict_mode`（A3 预留）：默认关闭；开启后推断类发现也阻断（R1 finding_policy 生效）。
- 双 DSN（A5 预留，R2 生效）：`profile_dsn`（只读采集）/ `target_dsn`（测试库写入）；
  未配置 profile_dsn 时回退同值并产生显式 warning（记入执行清单）。

配置项均有默认值与校验；支持显式传参与环境变量加载（PTDE_STRICT_MODE /
PTDE_PROFILE_DSN / PTDE_TARGET_DSN）。
"""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = ["DsnResolution", "PtdeConfig"]


class DsnResolution(BaseModel):
    """双 DSN 解析结果：采集/装载实际使用的连接串 + 告警（A5）。"""

    profile_dsn: str
    target_dsn: str
    warnings: list[str] = Field(default_factory=list)
    fallback_same_value: bool = False  # profile_dsn 回退 target_dsn 同值时为 True


class PtdeConfig(BaseModel):
    """PTDE 运行配置。"""

    model_config = ConfigDict(extra="forbid")

    # A3：严格模式开关（默认关闭；R1 finding_policy 消费）
    strict_mode: bool = False

    # A5：双 DSN 占位（R2 采集器开始消费；占位阶段仅校验形态）
    profile_dsn: str | None = None
    target_dsn: str | None = None

    # 采集/装载资源占位（R2/R5 细化；本阶段仅承载默认值与校验）
    sampling_rate: float = 1.0  # 0 < rate <= 1
    collect_timeout_seconds: int = 30  # > 0
    collect_max_rows: int = 100_000  # 单次采集行数上限（> 0）

    @field_validator("profile_dsn", "target_dsn")
    @classmethod
    def _dsn_not_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("dsn 不能为空白串（未配置请传 None）")
        return v

    @field_validator("sampling_rate")
    @classmethod
    def _sampling_rate_range(cls, v: float) -> float:
        if not 0 < v <= 1:
            raise ValueError("sampling_rate 必须在 (0, 1] 区间")
        return v

    @field_validator("collect_timeout_seconds", "collect_max_rows")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("超时/行数上限必须为正整数")
        return v

    def resolve_dsns(self) -> DsnResolution:
        """解析双 DSN：未配置 profile_dsn 时回退 target_dsn 同值 + 显式 warning（A5）。"""
        warnings: list[str] = []
        if self.target_dsn is None:
            raise ValueError("target_dsn 未配置：写入类操作不可用")
        profile_dsn = self.profile_dsn
        fallback = False
        if profile_dsn is None:
            profile_dsn = self.target_dsn
            fallback = True
            warnings.append(
                "A5: 未配置 profile_dsn（只读采集凭据），已回退 target_dsn 同值；"
                "生产环境必须分离统计采集与写入凭据，本次回退已记入执行清单"
            )
        return DsnResolution(
            profile_dsn=profile_dsn,
            target_dsn=self.target_dsn,
            warnings=warnings,
            fallback_same_value=fallback,
        )

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> PtdeConfig:
        """从环境变量加载（缺省回退 os.environ）；未设置项取默认值。"""
        env = environ if environ is not None else os.environ
        strict_raw = env.get("PTDE_STRICT_MODE", "").strip().lower()
        strict = strict_raw in {"1", "true", "yes", "on"}
        return cls(
            strict_mode=strict,
            profile_dsn=env.get("PTDE_PROFILE_DSN") or None,
            target_dsn=env.get("PTDE_TARGET_DSN") or None,
        )
