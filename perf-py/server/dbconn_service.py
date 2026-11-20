"""数据库连接管理服务（dbconn 域）：统一维护 db_conn 表的连接配置。

背景：连接此前只有两个入口——agent 上报慢 SQL 时自动注册（空凭据，
见 collector_service.ensure_auto_db_conn）与优化页手填（无编辑/无独立页面）。
本服务补齐管理模块的完整写路径：

- list_conns   列表（id ASC；密码不回显明文）
- save         新建/编辑（id 提供 = 编辑；密码留空 = 沿用已存凭据；加密入库）
- delete       删除
- test         连通性测试（可携带 SELECT 试跑 EXPLAIN）

host 留空时自动从 JDBC URL 推导 host:port/db，保证与 agent 上报的
慢 SQL dbHost 匹配（collector_repo.find_conns_for 的双向包含语义）。
连接匹配只看主机标识与项目绑定，不做「默认连接」兜底
（不同项目用不同数据库，绑定关系已足够；is_default 列仅为存量表结构
兼容保留，保存时统一归零）。
URL 校验沿用 db_conn 设计约束：仅允许 jdbc:mysql:// / jdbc:mariadb://。

connect_fn 可注入（测试替身）；生产用 pymysql 直连，超时 3s/5s。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from server.collector_repo import DbConnModel
from server.crypto import encrypt
from server.db_explain import _parse_jdbc_url
from server.db_explain import validate as validate_explain

ALLOWED_URL_PREFIXES = ("jdbc:mysql://", "jdbc:mariadb://")
CONNECT_TIMEOUT_S = 3
QUERY_TIMEOUT_S = 5


class DbConnService:
    """db_conn 管理写路径 + 连通性测试。"""

    def __init__(self, session_factory: sessionmaker, connect_fn=None):
        self._sessions = session_factory
        # connect_fn(url, user, password) -> DB-API connection（测试注入；生产 pymysql）
        self._connect_fn = connect_fn

    # ------------------------------------------------------------ 查询

    def list_conns(self) -> list[dict]:
        stmt = select(DbConnModel).order_by(DbConnModel.id.asc())
        with self._sessions() as s:
            return [_wire(c, masked=True) for c in s.scalars(stmt)]

    # ------------------------------------------------------------ 新建 / 编辑

    def save(self, body: dict) -> dict:
        conn_name = str(body.get("connName") or "").strip()
        if not conn_name:
            raise ValueError("连接名称不能为空")
        db_url = str(body.get("dbUrl") or "").strip()
        if not db_url:
            raise ValueError("JDBC URL 不能为空（如 jdbc:mysql://127.0.0.1:3306/demo）")
        if not db_url.startswith(ALLOWED_URL_PREFIXES):
            raise ValueError("JDBC URL 仅支持 jdbc:mysql:// 或 jdbc:mariadb://")
        username = str(body.get("username") or "").strip()
        if not username:
            raise ValueError("用户名不能为空")
        host = str(body.get("host") or "").strip() or self._host_from_url(db_url)
        app_name = str(body.get("appName") or "").strip()
        conn_id = _parse_id(body.get("id"))
        password = str(body.get("password") or "").strip()

        with self._sessions() as s:
            if conn_id is None:
                row = DbConnModel(conn_name=conn_name, host=host, app_name=app_name,
                                  db_url=db_url, username=username,
                                  password=encrypt(password), is_default=0)
                s.add(row)
                s.flush()
            else:
                row = s.get(DbConnModel, conn_id)
                if row is None:
                    raise ValueError(f"连接不存在: {conn_id}")
                row.conn_name = conn_name
                row.host = host
                row.app_name = app_name
                row.db_url = db_url
                row.username = username
                # agent 自动注册的连接凭据为空，编辑后补；密码留空 = 沿用已存凭据
                if password:
                    row.password = encrypt(password)
                # 默认连接概念已移除：编辑存量带默认标记的连接时一并归零
                row.is_default = 0
            s.commit()
            return _wire(row, masked=True)

    # ------------------------------------------------------------ 删除

    def delete(self, conn_id: int) -> None:
        with self._sessions() as s:
            row = s.get(DbConnModel, conn_id)
            if row is None:
                raise ValueError(f"连接不存在: {conn_id}")
            s.delete(row)
            s.commit()

    # ------------------------------------------------------------ 连通性测试

    def test(self, body: dict) -> dict:
        db_url = str(body.get("dbUrl") or "").strip()
        username = str(body.get("username") or "").strip()
        password = str(body.get("password") or "")
        sql_text = str(body.get("sqlText") or "").strip()
        if not db_url:
            return {"ok": False, "error": "请填写 JDBC URL"}
        if not username:
            return {"ok": False, "error": "请填写用户名"}
        if not db_url.startswith(ALLOWED_URL_PREFIXES):
            return {"ok": False,
                    "error": "测试连接仅支持 jdbc:mysql:// 或 jdbc:mariadb://"}
        sql = None
        if sql_text:
            err = validate_explain(db_url, sql_text)
            if err is not None:
                return {"ok": False, "error": err}
            sql = sql_text
        conn = None
        try:
            conn = self._open(db_url, username, password)
            with conn.cursor() as cur:
                cur.execute(f"EXPLAIN {sql}" if sql else "SELECT 1")
                cur.fetchall()
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001 — 测试失败原样回传原因
            return {"ok": False, "error": str(exc)}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------ 内部实现

    def _open(self, db_url: str, username: str, password: str):
        if self._connect_fn is not None:
            return self._connect_fn(db_url, username, password)
        import pymysql

        host, port, database = _parse_jdbc_url(db_url)
        return pymysql.connect(host=host, port=port, database=database,
                               user=username, password=password,
                               connect_timeout=CONNECT_TIMEOUT_S,
                               read_timeout=QUERY_TIMEOUT_S,
                               write_timeout=QUERY_TIMEOUT_S)

    @staticmethod
    def _host_from_url(db_url: str) -> str:
        """host 留空时从 JDBC URL 推导 host:port/db（与慢 SQL dbHost 匹配）。"""
        rest = db_url.split("://", 1)[1]
        q = rest.find("?")
        if q >= 0:
            rest = rest[:q]
        return rest


def _parse_id(value) -> int | None:
    """Long.parseLong 语义：空白/非数字返回 None。"""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _wire(row: DbConnModel, masked: bool = False) -> dict:
    """camelCase wire 形态；masked=True 时密码不回显（列表/保存响应）。"""
    return {
        "id": row.id,
        "connName": row.conn_name,
        "host": row.host,
        "appName": row.app_name,
        "dbUrl": row.db_url,
        "username": row.username,
        "password": "" if masked else row.password,
        "isDefault": row.is_default,
        "createTime": (row.create_time.strftime("%Y-%m-%d %H:%M:%S")
                       if row.create_time else None),
    }
