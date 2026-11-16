"""SQLAlchemy 引擎接入（阶段 5）。

数据库 URL：环境变量 PERF_PY_DATABASE_URL（默认 sqlite 内存库，供测试/本地冒烟；
生产指向 Java perf-server 同款 MySQL，如 mysql+pymysql://user:pwd@host/perf）。
- sqlite 内存库需 StaticPool 共享单连接，否则每个连接各自拿到空库；
- 分区表（stress_sample 等）的分区 DDL 由 sql/init_empty.sql 维护，ORM 不重建分区。
"""

from __future__ import annotations

import os

from sqlalchemy import BigInteger, Integer, create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

DATABASE_URL_ENV = "PERF_PY_DATABASE_URL"


class Base(DeclarativeBase):
    """全库共享 ORM 基类：stress / collector 域模型统一 metadata，create_all 一次建齐。"""


# sqlite 的 INTEGER PRIMARY KEY 才走 rowid 自增；MySQL 用 BIGINT（方言 variant 统一两侧）
BigIntPk = BigInteger().with_variant(Integer, "sqlite")


def database_url() -> str:
    return os.environ.get(DATABASE_URL_ENV, "sqlite:///:memory:")


def make_engine(url: str | None = None):
    url = url or database_url()
    if url.startswith("sqlite"):
        kwargs: dict = {"connect_args": {"check_same_thread": False}}
        if ":memory:" in url or url == "sqlite://":
            from sqlalchemy.pool import StaticPool

            kwargs["poolclass"] = StaticPool
        return create_engine(url, future=True, **kwargs)
    return create_engine(url, future=True)


def make_session_factory(url: str | None = None) -> sessionmaker:
    return sessionmaker(bind=make_engine(url), expire_on_commit=False, future=True)
