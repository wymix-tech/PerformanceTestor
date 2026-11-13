"""灌数工具：DataGenSpec → 引擎执行（宿主装配与 spec 加载复用 datagen_runner）。"""

from __future__ import annotations

import json
from pathlib import Path

from perf_cli.datagen_runner import (
    build_default_engine,
    load_spec,
    result_to_camel_json,
)
from tools.registry import Tool

DATAGEN_SCHEMA = {
    "type": "object",
    "properties": {
        "spec_file": {"type": "string", "description": "灌数规格 JSON 文件路径"
                      "（DataGenRequest 形状：dbUrl/dbUser/dbPassword/dryRun/tables）"},
        "source_dir": {"type": "string",
                       "description": "可选源码目录（.java/.xml，供 ORM 关联与魔法值扫描）"},
    },
    "required": ["spec_file"],
}


def _run(args: dict, dry_run: bool) -> dict:
    spec_file = args.get("spec_file")
    if not spec_file:
        return {"error": "缺少 spec_file 参数"}
    p = Path(spec_file)
    if not p.is_file():
        return {"error": f"spec 文件不存在: {spec_file}"}
    source_dir = args.get("source_dir")
    spec = load_spec(p, Path(source_dir) if source_dir else None)
    if dry_run:
        spec.dry_run = True
    result = build_default_engine().execute(spec)
    out = json.loads(result_to_camel_json(result))
    if result.success and not dry_run:
        out["advice"] = (f"灌入 {result.inserted_rows}/{result.total_rows} 行；"
                         "若 hotShare 较高，压测时关注行锁冲突与 BATCH_SIZE 调整建议")
    return out


def datagen_dry_run(args: dict) -> dict:
    """灌数试运行：元数据/规则/关联/拓扑/SQL 预检 + 行数与事务时长估算，不写入。"""
    return _run(args, dry_run=True)


def datagen_run(args: dict) -> dict:
    """灌数正式执行：拓扑排序写入（FK 全部有效 / refSkew / orphanRate 后置替换）。"""
    return _run(args, dry_run=False)


DATAGEN_TOOLS = [
    Tool(
        name="datagen_dry_run",
        description="灌数试运行（不写库）：校验表/列/规则/关联，输出预计行数与"
                    "最大单表事务时长估算，供压测前造数决策。",
        parameters=DATAGEN_SCHEMA,
        handler=datagen_dry_run,
    ),
    Tool(
        name="datagen_run",
        description="灌数正式执行（写库）：按拓扑序灌入数据，保证外键有效，"
                    "支持 refSkew 热点倾斜与 enum 权重分布。",
        parameters=DATAGEN_SCHEMA,
        handler=datagen_run,
        read_only=False,
    ),
]
