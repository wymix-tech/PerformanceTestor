"""数据库只读检测服务（阶段 5 · 步骤 10）：Java DbExplainService 的逐语义移植。

对慢 SQL 执行 EXPLAIN（执行计划）、SHOW INDEX（索引清单）、表行数估算，
作为大模型根因分析的量化证据。

安全约束（逐条对齐）：
- 仅允许 SELECT/WITH 开头的单条语句做 EXPLAIN（EXPLAIN 只编译不执行，无副作用）；
  禁止 ANALYZE 关键字（EXPLAIN ANALYZE 会真实执行）
- 禁止分号/注释，防多语句与注释绕过
- JDBC URL 仅允许 jdbc:mysql / jdbc:mariadb（Python 侧映射为 PyMySQL 直连，
  host:port/db 从 URL 提取），连接/查询超时 3s/5s，失败不影响分析流程

连接器可注入（connect_fn）供测试使用；生产用 pymysql 直连。
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

TABLE_PATTERN = re.compile(r"(?:from|join|into|update)\s+`?([A-Za-z0-9_.]+)`?", re.IGNORECASE)
SQL_KEYWORD = re.compile(r"(select|insert|update|delete|with)\s", re.IGNORECASE)
CONNECT_TIMEOUT_S = 3
QUERY_TIMEOUT_S = 5


class DbExplainService:
    """只读检测：analyze_sql / url_host / format_summary（对齐 DbExplainService）。"""

    def __init__(self, connect_fn=None):
        # connect_fn(url, user, password) -> DB-API connection（测试注入替身；生产 PyMySQL）
        self._connect_fn = connect_fn

    def analyze_sql(self, db_url: str | None, username: str | None,
                    password: str | None, sql_text: str | None) -> dict:
        """对一条 SQL 执行完整只读检测：explainRows/explainJson/indexes/tableRows/error。"""
        result: dict = {}
        sql = sanitize_sql(sql_text or "")
        check = validate(db_url, sql)
        if check is not None:
            result["error"] = check
            return result
        conn = None
        try:
            conn = self._open_connection(db_url, username, password)
            # 1. EXPLAIN FORMAT=JSON（含 cost/rows/possible_keys），失败回退表格模式
            explain_json = self._explain_json(conn, sql)
            if explain_json is not None:
                result["explainJson"] = _trim(explain_json, 4000)
            else:
                result["explainRows"] = self._explain_rows(conn, sql)
            # 2. 涉及表的索引与规模
            tables = extract_tables(sql)
            indexes: dict[str, str] = {}
            table_rows: dict[str, int] = {}
            for table in tables:
                idx = self._show_index(conn, table)
                if idx is not None:
                    indexes[table] = idx
                rows = self._table_row_count(conn, table)
                if rows is not None:
                    table_rows[table] = rows
            result["indexes"] = indexes
            result["tableRows"] = table_rows
            result["explainTables"] = tables
            # 连接成功但执行计划/索引/表规模全部为空：极可能连错了库，必须留痕
            rows_obj = result.get("explainRows")
            if (explain_json is None and (rows_obj is None or not rows_obj)
                    and not indexes and not table_rows):
                result["error"] = ("连接成功但未取得执行计划/索引/表规模——目标库中可能不存在相关表，"
                                   "请核对连接指向的库是否为慢 SQL 实际执行的库")
        except Exception as exc:  # noqa: BLE001 检测失败不阻断分析流程
            log.warning("db explain failed, url=%s: %s", _mask_url(db_url), exc)
            result["error"] = f"数据库检测失败: {exc}"
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
        return result

    def url_host(self, db_url: str | None) -> str:
        """从 JDBC URL 提取 host:port/db 标识（与 agent 端 dbHost 提取逻辑一致）。"""
        if not db_url:
            return ""
        idx = db_url.find("://")
        if idx < 0:
            return ""
        rest = db_url[idx + 3:]
        q = rest.find("?")
        return rest[:q] if q > 0 else rest

    # ------------------------------------------------------------ 内部实现

    def _open_connection(self, db_url: str, username: str | None, password: str | None):
        if self._connect_fn is not None:
            return self._connect_fn(db_url, username, password)
        import pymysql

        host, port, database = _parse_jdbc_url(db_url)
        return pymysql.connect(host=host, port=port, database=database,
                               user=username or "", password=password or "",
                               connect_timeout=CONNECT_TIMEOUT_S,
                               read_timeout=QUERY_TIMEOUT_S, write_timeout=QUERY_TIMEOUT_S)

    def _explain_json(self, conn, sql: str) -> str | None:
        """EXPLAIN FORMAT=JSON，失败返回 None（回退表格模式）。"""
        try:
            with conn.cursor() as cur:
                cur.execute(f"EXPLAIN FORMAT=JSON {sql}")
                row = cur.fetchone()
                if row:
                    return row[0]
        except Exception as exc:  # noqa: BLE001
            log.debug("EXPLAIN FORMAT=JSON fallback: %s", exc)
        return None

    def _explain_rows(self, conn, sql: str) -> list[dict[str, str]]:
        """表格模式 EXPLAIN（兜底）。"""
        rows: list[dict[str, str]] = []
        try:
            with conn.cursor() as cur:
                cur.execute(f"EXPLAIN {sql}")
                cols = [d[0] for d in cur.description]
                for row in cur.fetchall():
                    rows.append({c: ("" if v is None else str(v))
                                 for c, v in zip(cols, row, strict=False)})
        except Exception as exc:  # noqa: BLE001
            log.warning("EXPLAIN failed: %s", exc)
        return rows

    def _show_index(self, conn, table: str) -> str | None:
        """SHOW INDEX 摘要：idx_name(col1,col2) [UNIQUE]，失败返回 None。"""
        if not re.fullmatch(r"[A-Za-z0-9_.]+", table):
            return None
        idx_cols: dict[str, list[tuple[int, str]]] = {}
        idx_unique: dict[str, int] = {}
        try:
            with conn.cursor() as cur:
                cur.execute(f"SHOW INDEX FROM `{table.replace(chr(96), '')}`")
                for row in cur.fetchall():
                    # PyMySQL 元组序：Key_name, Column_name, Seq_in_index, Non_unique
                    # （按 SHOW INDEX 列序取值，兼容 dict cursor 形态）
                    if isinstance(row, dict):
                        key_name, col = row["Key_name"], row["Column_name"]
                        seq, non_unique = row["Seq_in_index"], row["Non_unique"]
                    else:
                        key_name, col, seq, non_unique = row[2], row[4], row[3], row[1]
                    idx_cols.setdefault(key_name, []).append((seq, col))
                    idx_unique[key_name] = non_unique
        except Exception:  # noqa: BLE001
            return None
        parts: list[str] = []
        for key_name, cols in idx_cols.items():
            cols.sort()
            col_str = ",".join(c for _, c in cols)
            flag = " [UNIQUE]" if idx_unique.get(key_name, 1) == 0 else ""
            parts.append(f"{key_name}({col_str}){flag}")
        summary = "; ".join(parts)
        return summary if summary else "（无二级索引）"

    def _table_row_count(self, conn, table: str) -> int | None:
        """表行数估算（information_schema，不扫描真实数据），失败返回 None。"""
        catalog = getattr(conn, "db", None)  # PyMySQL 当前库（bytes）；测试替身可能无此属性
        if isinstance(catalog, bytes):
            catalog = catalog.decode("utf-8", "replace")
        dot = table.rfind(".")
        schema = table[:dot] if dot > 0 else (catalog or None)
        table_name = table[dot + 1:] if dot > 0 else table
        if not re.fullmatch(r"[A-Za-z0-9_]+", table_name or "") \
                or not re.fullmatch(r"[A-Za-z0-9_]+", schema or ""):
            return None
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT TABLE_ROWS FROM information_schema.TABLES "
                            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                            (schema, table_name))
                row = cur.fetchone()
                if row:
                    return int(row[0])
        except Exception:  # noqa: BLE001 估算值查询失败不阻断 EXPLAIN 分析
            log.debug("estimate table rows failed: %s.%s", schema, table_name)
        return None


def validate(db_url: str | None, sql_text: str | None) -> str | None:
    """校验 URL 与 SQL 合法性，非法返回原因（None = 通过）。"""
    if not db_url or not db_url.strip():
        return "未配置数据库连接"
    url = db_url.strip()
    if not url.startswith("jdbc:mysql://") and not url.startswith("jdbc:mariadb://"):
        return "JDBC URL 仅支持 jdbc:mysql:// 或 jdbc:mariadb://"
    if not sql_text or not sql_text.strip():
        return "SQL 为空"
    lower = sql_text.lower()
    if not (lower.startswith("select") or lower.startswith("with")):
        return "仅支持对 SELECT/WITH 查询执行 EXPLAIN"
    if ";" in sql_text or "--" in sql_text or "/*" in sql_text:
        return "SQL 含分号或注释，疑似多语句，已拒绝"
    if "explain analyze" in lower or "into outfile" in lower or "into dumpfile" in lower:
        return "SQL 含受限关键字"
    if len(sql_text) > 4000:
        return "SQL 过长"
    return None


def sanitize_sql(sql_text: str) -> str:
    """剥掉驱动 toString 产生的前缀噪音，定位到 SQL 关键字起始处（对齐 sanitizeSql）。"""
    s = sql_text.strip()
    m = SQL_KEYWORD.search(s)
    if m and m.start() > 0:
        s = s[m.start():]
    return s.strip()


def extract_tables(sql: str) -> list[str]:
    """提取 SQL 涉及表名（去重，最多 5 张）。"""
    tables: list[str] = []
    for m in TABLE_PATTERN.finditer(sql):
        t = m.group(1)
        if t not in tables and len(tables) < 5:
            tables.append(t)
    return tables


def format_summary(explain: dict | None) -> str:
    """供大模型使用的紧凑文本摘要（对齐 formatSummary）。"""
    if not explain:
        return ""
    sb: list[str] = []
    error = explain.get("error")
    if error is not None:
        return f"（数据库检测不可用：{error}）"
    rows = explain.get("explainRows")
    if isinstance(rows, list) and rows:
        sb.append("EXPLAIN 执行计划：\n")
        for r in rows:
            sb.append(f"- {r}\n")
    explain_json = explain.get("explainJson")
    if explain_json:
        sb.append(f"EXPLAIN JSON（含 cost/rows/keys）：\n{explain_json}\n")
    indexes = explain.get("indexes")
    if isinstance(indexes, dict) and indexes:
        sb.append("现有索引：\n")
        for t, v in indexes.items():
            sb.append(f"- {t}: {v}\n")
    table_rows = explain.get("tableRows")
    if isinstance(table_rows, dict) and table_rows:
        sb.append("表规模（估算行数）：")
        for t, v in table_rows.items():
            sb.append(f"{t}={v} ")
        sb.append("\n")
    return "".join(sb)


def _parse_jdbc_url(jdbc_url: str) -> tuple[str, int, str | None]:
    """jdbc:mysql://host:port/db?params → (host, port, db)。"""
    rest = jdbc_url.split("://", 1)[1]
    q = rest.find("?")
    if q >= 0:
        rest = rest[:q]
    slash = rest.find("/")
    host_port = rest if slash < 0 else rest[:slash]
    database = None if slash < 0 else rest[slash + 1:]
    colon = host_port.rfind(":")
    host = host_port if colon < 0 else host_port[:colon]
    port = int(host_port[colon + 1:]) if colon >= 0 and host_port[colon + 1:].isdigit() else 3306
    return host, port, database or None


def _mask_url(url: str | None) -> str:
    return re.sub(r"password=[^&]*", "password=***", url or "")


def _trim(s: str, max_len: int) -> str:
    return s if len(s) <= max_len else s[:max_len] + "...(截断)"
