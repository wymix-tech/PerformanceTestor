"""Seed 语义单测（原 QW1 遗留收编平移）。

B7 续批次 11（T7.9 退役）：legacy 引擎逐行哈希复现段随 datagen 包退役
（PTDE 侧 seed → per-entity rng 注入与 definition.seed 落库见
test_perf_cli_ptde_datagen_engine / R4-R6 批次验收归档）。
保留：spec.seed 契约形状、CLI --seed 透传 seam、primitive/id_pool
rng 注入确定性。
"""

import json
import random

import pytest
from typer.testing import CliRunner

from perf_cli.config import EXIT_OK
from perf_cli.datagen_runner import spec_from_request
from perf_cli.main import app
from ptde.generator.primitive import (
    apply_rule,
    generate_default,
    random_text,
    weighted_index,
)
from ptde.models.wire import ColumnMeta, FieldRule
from ptde_common.id_pool import IdPool

runner = CliRunner()

SEED_A = 42


def test_seed_none_is_default_and_spec_shape_unchanged():
    """spec.json 新增可选字段 seed：缺省不出现 → seed=None（契约不破坏）。"""
    spec = spec_from_request({"dbUrl": "jdbc:mysql://h/d",
                              "tables": [{"tableName": "t", "rowCount": 1}]})
    assert spec.seed is None
    spec2 = spec_from_request({"dbUrl": "jdbc:mysql://h/d", "seed": 42,
                               "tables": [{"tableName": "t", "rowCount": 1}]})
    assert spec2.seed == 42
    with pytest.raises(ValueError, match="seed 必须是整数"):
        spec_from_request({"dbUrl": "jdbc:mysql://h/d", "seed": "abc",
                           "tables": [{"tableName": "t", "rowCount": 1}]})


def test_cli_seed_override(tmp_path, monkeypatch):
    """CLI --seed 透传：覆盖 spec 内 seed 字段。"""
    import perf_cli.datagen_runner as dr

    captured = {}

    class FakeEngine:
        def execute(self, spec, listener=None):
            captured["seed"] = spec.seed
            r = dr.EngineResult()
            r.success = True
            r.dry_run = spec.dry_run
            r.total_rows = 1
            r.inserted_rows = 0
            return r

    monkeypatch.setattr(dr, "build_default_engine", lambda: FakeEngine())
    p = tmp_path / "spec.json"
    p.write_text(json.dumps({"dbUrl": "jdbc:mysql://127.0.0.1:1/db", "dryRun": True,
                             "tables": [{"tableName": "perf_user", "rowCount": 1}]}),
                 encoding="utf-8")
    result = runner.invoke(app, ["datagen", "run", str(p), "--seed", str(SEED_A)])
    assert result.exit_code == EXIT_OK
    assert captured["seed"] == SEED_A


# ---------------------------------------------------------------- 单元层


def test_apply_rule_rng_injection_deterministic():
    col = ColumnMeta(name="age", type_name="INT")
    rule = FieldRule(type=FieldRule.TYPE_RANGE, min=0, max=100)
    a = [apply_rule(rule, col, 0, random.Random(7)) for _ in range(50)]
    b = [apply_rule(rule, col, 0, random.Random(7)) for _ in range(50)]
    assert a == b


def test_apply_rule_none_rng_uses_module_random():
    """rng 缺省 → 模块级 random（现行为）：固定种子下可复现。"""
    col = ColumnMeta(name="age", type_name="INT")
    rule = FieldRule(type=FieldRule.TYPE_RANGE, min=0, max=100)
    random.seed(11)
    a = [apply_rule(rule, col, 0) for _ in range(50)]
    random.seed(11)
    b = [apply_rule(rule, col, 0) for _ in range(50)]
    assert a == b


def test_enum_weighted_and_text_rng():
    rule = FieldRule(type=FieldRule.TYPE_ENUM, values=["A", "B"], weights=[1, 9])
    ra, rb = random.Random(3), random.Random(3)
    a = [apply_rule(rule, ColumnMeta(name="s", type_name="VARCHAR"), 0, ra)
         for _ in range(100)]
    b = [apply_rule(rule, ColumnMeta(name="s", type_name="VARCHAR"), 0, rb)
         for _ in range(100)]
    assert a == b and set(a) == {"A", "B"}
    assert random_text(16, random.Random(5)) == random_text(16, random.Random(5))
    assert weighted_index([1, 1], random.Random(9)) == weighted_index([1, 1], random.Random(9))


def test_generate_default_rng_none_fallback():
    col = ColumnMeta(name="n", type_name="VARCHAR", size=8)
    random.seed(1)
    a = [generate_default(col) for _ in range(10)]
    random.seed(1)
    b = [generate_default(col) for _ in range(10)]
    assert a == b


def test_id_pool_rng_injection_deterministic():
    a = IdPool(random.Random(1))
    b = IdPool(random.Random(1))
    for i in range(100):
        a.add(i)
        b.add(i)
    assert [a.sample_uniform() for _ in range(50)] == [b.sample_uniform() for _ in range(50)]
    a.build_skew_weights(20, 80)
    b.build_skew_weights(20, 80)
    assert [a.sample_skewed() for _ in range(50)] == [b.sample_skewed() for _ in range(50)]
    assert [a.sample_invalid_id() for _ in range(10)] == [b.sample_invalid_id() for _ in range(10)]


def test_id_pool_default_none_rng_module_random():
    """rng=None 的池：模块级 random（现行为），固定种子可复现。"""
    random.seed(3)
    p = IdPool()
    p.add_all(list(range(100)))
    a = [p.sample_uniform() for _ in range(50)]
    random.seed(3)
    p2 = IdPool()
    p2.add_all(list(range(100)))
    b = [p2.sample_uniform() for _ in range(50)]
    assert a == b
