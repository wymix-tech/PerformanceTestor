"""宿主装配层等价单测（阶段 3 步骤 5）：spec 加载 / 规则解析 / 结果序列化 /
默认装配注册，对齐 DataGenSpecAdapter 与 DataGenEngineConfig 行为。
"""

import json

import pytest

from datagen.model import DataGenSpec, EngineResult, TableStat
from perf_cli.datagen_runner import (
    build_default_engine,
    collect_source_files,
    parse_fields,
    parse_fields_json,
    result_to_camel_json,
    rule_from_json,
    spec_from_request,
)

# ===== rule_from_json（对齐 DataGenSpecAdapter#toRule） =====


def test_rule_full_mapping():
    r = rule_from_json({
        "type": "refSkew", "value": 42, "min": 1.5, "max": 9.5,
        "values": ["PENDING", 2, None], "weights": [10, 90],
        "table": "perf_user", "column": "ID",
        "hotPercent": 20, "hotShare": 80,
        "overThreshold": {"op": ">=", "value": 1000, "share": 25},
        "seqPrefix": "U", "seqStart": 7,
    })
    assert r is not None
    assert r.type == "refSkew"
    assert r.value == 42
    assert r.min == 1.5 and r.max == 9.5
    assert r.values == ["PENDING", 2, "null"], "数字保留原值、null→'null'（对齐 asText）"
    assert r.weights == [10.0, 90.0]
    assert r.table == "perf_user" and r.column == "ID"
    assert r.hot_percent == 20.0 and r.hot_share == 80.0
    assert r.over_threshold is not None
    assert r.over_threshold.op == ">="
    assert r.over_threshold.value == 1000.0
    assert r.over_threshold.share == 25.0
    assert r.seq_prefix == "U" and r.seq_start == 7


def test_rule_value_number_vs_text():
    assert rule_from_json({"type": "fixed", "value": 100}).value == 100
    assert rule_from_json({"type": "fixed", "value": "PAID"}).value == "PAID"
    assert rule_from_json({"type": "fixed", "value": True}).value == "true", "布尔走 asText 文本化"
    # value 缺失时不设置（Java asText 仅在非缺失非 null 时赋值）
    assert rule_from_json({"type": "range"}).value is None


def test_rule_over_threshold_defaults():
    r = rule_from_json({"type": "range", "overThreshold": {"value": 500}})
    assert r.over_threshold.op == ">", "op 缺省 >"
    assert r.over_threshold.value == 500.0
    assert r.over_threshold.share == 30.0, "share 缺省 30（对齐 Java asDouble(30)）"


def test_rule_non_dict_returns_none():
    assert rule_from_json("x") is None
    assert rule_from_json(None) is None
    assert rule_from_json([1]) is None


# ===== parse_fields / parse_fields_json =====


def test_parse_fields_lowercases_column_keys():
    out = parse_fields({"UserID": {"type": "seq", "seqPrefix": "X"},
                        "status": {"type": "fixed", "value": "PAID"}})
    assert set(out) == {"userid", "status"}


def test_parse_fields_accepts_json_string_and_blank():
    assert parse_fields('{"a": {"type": "fixed", "value": 1}}')["a"].value == 1
    assert parse_fields("") == {}
    assert parse_fields(None) == {}


def test_parse_fields_json_legacy_shape():
    out = parse_fields_json('{"fields": {"user_id": {"type": "ref", "table": "t", "column": "id"}}}')
    assert out["user_id"].table == "t"
    assert parse_fields_json(None) == {}
    assert parse_fields_json("   ") == {}


# ===== spec_from_request（对齐 DataGenSpecAdapter#toSpec） =====


def test_spec_from_request_tables_form():
    spec = spec_from_request({
        "dbUrl": "jdbc:mysql://127.0.0.1:3306/demo", "dbUser": "root",
        "dbPassword": "pwd", "dryRun": True, "orphanRate": 1.5,
        "tables": [{"tableName": "perf_user", "rowCount": 100},
                   {"tableName": "perf_order", "rowCount": 500, "orphanRate": 2.0,
                    "fields": '{"status": {"type": "fixed", "value": "PAID"}}'}],
    })
    assert isinstance(spec, DataGenSpec)
    assert spec.jdbc_url.endswith("/demo")
    assert spec.dry_run and spec.orphan_rate == 1.5
    assert [t.table_name for t in spec.tables] == ["perf_user", "perf_order"]
    assert spec.tables[0].row_count == 100
    assert spec.tables[1].orphan_rate == 2.0
    assert spec.tables[1].fields["status"].value == "PAID"


def test_spec_from_request_legacy_single_table():
    spec = spec_from_request({
        "tableName": "perf_order", "rowCount": 10,
        "ruleJson": json.dumps({"fields": {"amount": {"type": "range", "min": 1, "max": 9}}}),
    })
    assert len(spec.tables) == 1
    assert spec.tables[0].row_count == 10
    assert spec.tables[0].fields["amount"].max == 9.0


# ===== collect_source_files =====


def test_collect_source_files_selects_java_xml_sorted(tmp_path):
    (tmp_path / "b" / "sub").mkdir(parents=True)
    (tmp_path / "a.java").write_text("class A {}")
    (tmp_path / "b" / "sub" / "c.xml").write_text("<mapper/>")
    (tmp_path / "skip.txt").write_text("nope")
    (tmp_path / "d.JAVA").write_text("class D {}")
    files = collect_source_files(tmp_path)
    names = [f.path.split("/")[-1] for f in files]
    assert names == ["a.java", "c.xml", "d.JAVA"], "排序确定性 + 后缀大小写不敏感 + 过滤无关文件"
    assert files[0].content == "class A {}"


# ===== build_default_engine（对齐 DataGenEngineConfig） =====


def test_build_default_engine_registration():
    engine = build_default_engine()
    kinds = [type(d).__name__ for d in engine.get_dialects()]
    assert kinds == ["MysqlDialect", "PostgresDialect"]
    assert [type(p).__name__ for p in engine.orm_parsers] == \
        ["MyBatisSourceParser", "JpaSourceParser"]
    assert engine.executor.get_batch_size() >= 1


def test_engine_routes_registered_dialects():
    engine = build_default_engine()
    assert engine.resolve_dialect("jdbc:mysql://h/db").__class__.__name__ == "MysqlDialect"
    assert engine.resolve_dialect("jdbc:postgresql://h/db").__class__.__name__ == "PostgresDialect"
    with pytest.raises(ValueError):
        engine.resolve_dialect("jdbc:oracle://h/db")


# ===== result_to_camel_json（对齐 Java Jackson 序列化字段名） =====


def test_result_to_camel_json_success_shape():
    r = EngineResult()
    r.success = True
    r.dry_run = True
    r.total_rows = 6000
    r.inserted_rows = 0
    r.estimated_max_txn_ms = 120
    r.warnings = ["w1"]
    r.table_stats = [TableStat("perf_user", 1000), TableStat("perf_order", 0)]
    obj = json.loads(result_to_camel_json(r))
    assert obj == {
        "success": True, "dryRun": True, "totalRows": 6000, "insertedRows": 0,
        "estimatedMaxTxnMs": 120, "warnings": ["w1"],
        "tableStats": [{"table": "perf_user", "rows": 1000},
                       {"table": "perf_order", "rows": 0}],
        "errorMsg": None,
    }


def test_result_to_camel_json_failure_shape():
    obj = json.loads(result_to_camel_json(EngineResult.failure("boom")))
    assert obj["success"] is False
    assert obj["errorMsg"] == "boom"
    assert obj["tableStats"] == []
