"""datagen 写路径单测（阶段 5 · 步骤 7）：请求解析 / 校验文案 / upsert 幂等 / 凭据解密。

文案断言取自 Java DataGenService 源码字符串，逐字对齐。
"""

import base64
import hashlib
import json
import os

import pytest

from server.crypto import decrypt
from server.datagen_repo import DatagenRepository
from server.datagen_request import parse_datagen_request
from server.datagen_service import MAX_ROWS, DatagenWriteService, _nvl, validate_db_url
from server.db import make_engine

# ---------------------------------------------------------------- 解析语义

def test_parse_defaults_match_java_field_init():
    r = parse_datagen_request({"dbUrl": "jdbc:mysql://x/demo", "tableName": "t"})
    assert r["rowCount"] == 1000 and r["dryRun"] is False
    assert r["taskName"] is None and r["connId"] is None and r["repoId"] is None
    assert r["orphanRate"] is None and r["tables"] is None


def test_parse_explicit_null_row_count_becomes_zero():
    """显式 null rowCount → Jackson 基本类型 0（单表校验拒绝）。"""
    r = parse_datagen_request({"tableName": "t", "rowCount": None})
    assert r["rowCount"] == 0


def test_parse_tables_row_count_default_per_table():
    r = parse_datagen_request({"tables": [{"tableName": "a"}, {"tableName": "b"}]})
    assert [t["rowCount"] for t in r["tables"]] == [1000, 1000]


# ---------------------------------------------------------------- 校验文案

def test_validate_db_url_message_exact():
    with pytest.raises(ValueError) as ei:
        validate_db_url("jdbc:oracle://x")
    assert str(ei.value) == "JDBC URL 仅支持 jdbc:mysql:// 或 jdbc:postgresql:// 前缀"
    with pytest.raises(ValueError):
        validate_db_url(None)


def test_service_validate_messages_exact():
    svc = DatagenWriteService(DatagenRepository(make_engine("sqlite:///:memory:")))
    with pytest.raises(ValueError, match="表名仅允许字母数字下划线"):
        svc._validate(parse_datagen_request(
            {"dbUrl": "jdbc:mysql://x/d", "tableName": "bad-name"}))
    with pytest.raises(ValueError) as ei:
        svc._validate(parse_datagen_request(
            {"dbUrl": "jdbc:mysql://x/d", "tableName": "t", "rowCount": MAX_ROWS + 1}))
    assert str(ei.value) == f"行数必须在 1-{MAX_ROWS} 之间"
    with pytest.raises(ValueError) as ei:
        svc._validate(parse_datagen_request(
            {"dbUrl": "jdbc:mysql://x/d",
             "tables": [{"tableName": "t", "rowCount": -1}]}))
    assert str(ei.value) == "行数非法: t"
    with pytest.raises(ValueError) as ei:
        svc._validate(parse_datagen_request(
            {"dbUrl": "jdbc:mysql://x/d",
             "tables": [{"tableName": "t", "rowCount": MAX_ROWS},
                        {"tableName": "t2", "rowCount": 1}]}))
    assert str(ei.value) == f"表组总行数必须在 1-{MAX_ROWS} 之间"


def test_nvl_lowercases_for_idempotent_key():
    assert _nvl(None) == "" and _nvl("User_ID") == "user_id"


# ---------------------------------------------------------------- tablesJson 形态

def _svc_with_repo():
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=make_engine("sqlite:///:memory:"), expire_on_commit=False)
    import server.collector_repo  # noqa: F401  注册模型
    import server.datagen_repo  # noqa: F401
    import server.source_repo  # noqa: F401
    from server.datagen_repo import Base

    Base.metadata.create_all(factory.kw["bind"])
    return DatagenWriteService(DatagenRepository(factory)), factory


def test_create_and_run_tables_json_shape():
    """tablesJson 序列化对齐 DataGenSpecAdapter.toTablesJson：仅 DTO 四字段、null 透传。"""
    svc, factory = _svc_with_repo()
    data = svc.create_and_run({
        "dbUrl": "jdbc:mysql://127.0.0.1:3306/demo",
        "tables": [{"tableName": "user", "rowCount": 10,
                    "fields": json.dumps({"age": {"type": "range", "min": 1, "max": 2}})}],
    })
    assert set(data) == {"id", "taskName", "dbUrl", "tableName", "rowCount", "ruleJson",
                         "status", "inserted", "totalRows", "tablesJson", "detailJson",
                         "costMs", "errorMsg", "createTime"}
    assert data["taskName"] == "灌数据-user" and data["createTime"] is None
    assert data["rowCount"] == 0 and data["totalRows"] == 10
    tj = json.loads(data["tablesJson"])
    assert set(tj[0]) == {"tableName", "rowCount", "fields", "orphanRate"}
    assert tj[0]["orphanRate"] is None and tj[0]["rowCount"] == 10


# ---------------------------------------------------------------- hint upsert 幂等

def test_hint_upsert_idempotent_by_uk():
    """uk_hint 命中更新值字段、status 保留原值（ON DUPLICATE KEY UPDATE 语义）。"""
    svc, factory = _svc_with_repo()
    repo = svc._repo
    row = {"repo_id": 1, "hint_type": "value", "table_name": "user",
           "column_name": "status", "values_json": "[\"A\"]", "parent_table": "",
           "parent_column": "", "source_refs": "[]", "source": "literal",
           "confidence": None}
    repo.hint_upsert(**row)
    row2 = {**row, "values_json": "[\"A\",\"B\"]", "source": "enum"}
    repo.hint_upsert(**row2)
    hints = repo.suggestions(1, None, None, None)
    assert len(hints) == 1, "同幂等键不重复插入"
    assert hints[0].values_json == "[\"A\",\"B\"]" and hints[0].source == "enum"
    assert hints[0].status == "pending"
    # 不同 parent_table → 新行（uk_hint 含 parent_table）
    repo.hint_upsert(**{**row, "parent_table": "dept"})
    assert len(repo.suggestions(1, None, None, None)) == 2


# ---------------------------------------------------------------- 建议采纳

def test_adopt_status_transition_and_missing():
    svc, factory = _svc_with_repo()
    svc._repo.hint_upsert(repo_id=None, hint_type="relation", table_name="o",
                          column_name="user_id", values_json=None, parent_table="u",
                          parent_column="id", source_refs="[]", source="naming",
                          confidence=0.5)
    hid = svc._repo.suggestions(None, None, None, None)[0].id
    svc.adopt(hid, True)
    assert svc._repo.hint_by_id(hid).status == "adopted"
    svc.adopt(hid, False)
    assert svc._repo.hint_by_id(hid).status == "dismissed"
    with pytest.raises(ValueError, match="建议不存在: 999"):
        svc.adopt(999, True)


# ---------------------------------------------------------------- 凭据解密

def _enc(plain: str, secret: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    iv = b"0123456789ab"
    ct = AESGCM(hashlib.sha256(secret.encode()).digest()).encrypt(iv, plain.encode(), None)
    return "ENC:" + base64.b64encode(iv + ct).decode()


def test_decrypt_roundtrip_with_secret(monkeypatch):
    monkeypatch.setenv("PERF_CRYPTO_SECRET", "s3cret")
    assert decrypt(_enc("p@ss", "s3cret")) == "p@ss"


def test_decrypt_graceful_degradations(monkeypatch):
    monkeypatch.setenv("PERF_CRYPTO_SECRET", "s3cret")
    assert decrypt(None) is None
    assert decrypt("plain") == "plain", "无 ENC: 前缀（存量明文）原样返回"
    assert decrypt("ENC:!!!not-base64!!!") == "ENC:!!!not-base64!!!"
    assert decrypt("ENC:" + base64.b64encode(b"short").decode()) \
        == "ENC:" + base64.b64encode(b"short").decode(), "IV 长度不足按原值"
    wrong_key = _enc("p@ss", "other-secret")
    assert decrypt(wrong_key) == wrong_key, "GCM 校验失败（密钥轮换）降级原值"


def test_decrypt_without_secret_passthrough(monkeypatch):
    monkeypatch.delenv("PERF_CRYPTO_SECRET", raising=False)
    assert decrypt(os.environ.get("X", "ENC:abcd")) == "ENC:abcd"
