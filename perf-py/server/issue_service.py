"""性能问题编排服务（阶段 5 · 步骤 10）：Java SourceIssueService / IssueAssociator /
PerformanceAnalyzer / FixPromptGenerator 四类的逐语义移植。

职责链：从检测数据（慢 SQL / 热点方法 / 压测任务）创建问题 → 关联定位源码位置 →
驱动大模型分析（JSON Schema 约束输出 2-3 方案）→ 选定方案生成修复提示词。

- IssueAssociator：ORM 无关多级定位——方法链（首选）→ MyBatis XML → JPA @Table/@Entity
  驼峰推断；热点方法类名/方法名精确匹配；压测 URL → Controller RequestMapping。
- PerformanceAnalyzer：组装上下文（证据 + EXPLAIN 执行计划 + 源码片段）调用 LLM；
  无 LLM / 调用失败降级规则模板（文案逐字对齐）。慢 SQL 先按 db_conn 三级匹配
  （host 双向包含 → 应用绑定 → 默认兜底）执行只读 EXPLAIN 并回写证据。
- FixPromptGenerator：选定方案组装结构化 Markdown（fix_suggestion.prompt_md 落库，
  同一问题单选）。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from agents.llm import LlmClient
from server.collector_repo import CollectorRepository
from server.crypto import decrypt
from server.db_explain import DbExplainService, format_summary
from server.source_index import SourceFileIndexer
from server.source_repo import (
    FixSuggestionModel,
    PerformanceIssueModel,
    SourceFileModel,
    SourceRepository,
)
from server.source_write_service import SourceWriteService

log = logging.getLogger(__name__)


# ============================================================ IssueAssociator

XML_METHOD_ID = re.compile(r'<(select|insert|update|delete)\s+id\s*=\s*"([\w.]+)"')
METHOD_DEF = re.compile(r"(public|private|protected)?\s*[\w<>\[\],\s.]+\s+([a-zA-Z_$][\w$]*)\s*\(")
REQUEST_MAPPING = re.compile(
    r"@(?:RequestMapping|GetMapping|PostMapping|PutMapping|DeleteMapping)\s*\(\s*[\"']?([^\"'\)]*)")
FROM_JOIN_INTO_UPDATE = re.compile(r"(?:from|into|update|join)\s+([`\w.]+)", re.IGNORECASE)
SQL_KEYWORD = re.compile(r"(select|insert|update|delete|with)\s", re.IGNORECASE)
MAIN_TABLE = re.compile(r"(?:from|join|into|update)\s+`?([A-Za-z0-9_.]+)`?", re.IGNORECASE)
SCAN_FILE_LIMIT = 5000
DAO_LIST_LIMIT = 10


@dataclass
class IssueLocation:
    """关联结果：源码文件与行区间（对齐 IssueAssociator.IssueLocation）。"""

    repo_id: int
    file_id: int
    line_start: int
    line_end: int


def _camel_to_snake(name: str) -> str:
    """驼峰转下划线（JPA 默认命名策略）：OrderInfo → order_info。"""
    return re.sub(r"([a-z\d])([A-Z])", r"\1_\2", name).lower()


def _line_count(content: str | None) -> int:
    return 1 if content is None else len(content.split("\n"))


def _extract_table_name(sql: str | None) -> str | None:
    """从 SQL 提取主表名（FROM/JOIN/INTO/UPDATE 后），无则返回 None。"""
    if not sql or not sql.strip():
        return None
    s = sql.strip()
    m = SQL_KEYWORD.search(s)
    if m and m.start() > 0:
        s = s[m.start():]  # 复用同款清理：剥掉驱动 toString 前缀噪音
    m = MAIN_TABLE.search(s.strip())
    return m.group(1) if m else None


def _extract_sql_fragment(sql: str) -> str | None:
    """从 SQL 中提取用于模糊匹配的关键片段（表名 或 前 40 字符）。"""
    s = sql.strip()
    m = FROM_JOIN_INTO_UPDATE.search(s)
    if m:
        table = m.group(1).replace("`", "")
        if table.strip():
            return table
    return s[:40] if len(s) > 40 else s


def _find_nearest_method_id(xml: str, keyword_index: int) -> str | None:
    last = None
    for m in XML_METHOD_ID.finditer(xml):
        if m.start() > keyword_index:
            break
        last = m.group(2)
    return last


def _find_method_line(content: str | None, method_name: str | None) -> tuple[int, int] | None:
    """在 Java 内容中找到方法定义行区间（跳过方法调用与注解行）。"""
    if not content or not method_name or not method_name.strip():
        return None
    lines = content.split("\n")
    p = re.compile(r"\b" + re.escape(method_name) + r"\s*\(")
    for i, line in enumerate(lines):
        if line.strip().startswith("//") or f".{method_name}(" in line:
            continue
        if p.search(line) and ")" in line:
            end = min(len(lines), i + 8)
            return i + 1, end
    return None


def _find_class_mapping_prefix(content: str) -> str:
    for line in content.split("\n"):
        m = REQUEST_MAPPING.search(line)
        if m:
            path = m.group(1).strip().replace('"', "")
            if path.startswith("/") and "@RequestMapping" in line:
                return path
    return ""


def _normalize_url(url: str) -> str:
    s = url.strip()
    # 去掉域名与 query
    if "://" in s:
        s = s[s.find("://") + 3:]
        s = s[s.find("/"):] if "/" in s else "/"
    q = s.find("?")
    if q >= 0:
        s = s[:q]
    # 去掉路径参数占位符差异（/user/1 vs /user/{id}）
    return re.sub(r"/\{[^}]+\}", "/{id}", s)


def _find_mapping_method_line(content: str, normalized_url: str,
                              class_prefix: str) -> tuple[int, int] | None:
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if not line.strip().startswith("@"):
            continue
        m = REQUEST_MAPPING.search(line)
        path = m.group(1).strip().replace('"', "") if m else None
        if not path:
            continue
        full = ("" if path.startswith("/") else class_prefix) + path
        if _normalize_url(full) == normalized_url or normalized_url in _normalize_url(full):
            # 方法定义在注解下一行或下几行
            for j in range(i + 1, min(len(lines), i + 10)):
                if "(" in lines[j] and not lines[j].strip().startswith("@"):
                    return j + 1, min(len(lines), j + 8)
    return None


class IssueAssociator:
    """检测结果与源码关联定位（ORM 无关，多级策略，对齐 IssueAssociator）。"""

    def __init__(self, repo: SourceRepository, indexer: SourceFileIndexer,
                 write_service: SourceWriteService):
        self._repo = repo
        self._indexer = indexer
        self._write = write_service

    def associate_slow_sql_smart(self, repo, sql_text: str | None,
                                 method_chain_json: str | None) -> IssueLocation | None:
        """慢 SQL 关联统一入口（方法链 → MyBatis XML → JPA 实体，三级均未命中返回 None）。"""
        loc = self.associate_by_method_chain(repo, method_chain_json)
        if loc is not None:
            return loc
        loc = self.associate_mybatis_xml(repo, sql_text)
        if loc is not None:
            return loc
        return self.associate_by_table_entity(repo, sql_text)

    def associate_by_table_entity(self, repo, sql_text: str | None) -> IssueLocation | None:
        """JPA / Hibernate / Spring Data JDBC 实体定位：@Table 精确匹配，
        未命中按 @Entity + 类名驼峰转下划线 == 表名 推断。"""
        table = _extract_table_name(sql_text)
        if not table or not table.strip():
            return None
        short_table = table[table.rfind(".") + 1:] if "." in table else table
        java_files = [f for f in self._repo.files_by_repo_contains(
            repo.id, ".java", SCAN_FILE_LIMIT) if f.relative_path.endswith(".java")]
        entity_fallback: SourceFileModel | None = None
        entity_content: str | None = None
        for sf in java_files:
            content = self._read_safely(repo, sf)
            if content is None:
                continue
            # 1) 显式 @Table(name = "t_order")（容忍反引号/单双引号/空白）
            if re.search(r"(?s).*@Table\s*\(\s*name\s*=\s*[\"`']"
                         + re.escape(short_table) + "[\"`'].*", content):
                return IssueLocation(repo.id, sf.id, 1, min(30, _line_count(content)))
            # 2) @Entity + 类名推断（JPA 默认命名策略）
            if entity_fallback is None and "@Entity" in content:
                class_name = sf.file_name.replace(".java", "")
                if _camel_to_snake(class_name) == short_table:
                    entity_fallback = sf
                    entity_content = content
        if entity_fallback is not None:
            return IssueLocation(repo.id, entity_fallback.id, 1,
                                 min(30, _line_count(entity_content)))
        return None

    def associate_mybatis_xml(self, repo, sql_text: str | None) -> IssueLocation | None:
        """MyBatis 专属关联：SQL 关键片段在 Mapper XML 匹配 → statement id → DAO 接口方法。"""
        if not sql_text or not sql_text.strip():
            return None
        key_fragment = _extract_sql_fragment(sql_text)
        if key_fragment is None:
            return None
        xml_files = [f for f in self._repo.files_by_repo_contains(
            repo.id, ".xml", SCAN_FILE_LIMIT) if f.relative_path.endswith(".xml")]
        for xml in xml_files:
            content = self._read_safely(repo, xml)
            if content is None or key_fragment not in content:
                continue
            # 2. 匹配最近的 statement id（方法名）
            method_id = _find_nearest_method_id(content, content.index(key_fragment))
            if method_id is None:
                continue
            # 3. 同目录下找对应 DAO 接口（XML 名去掉 .xml 后缀）
            base_name = xml.file_name.replace(".xml", "")
            daos = self._repo.files_by_name(repo.id, base_name + ".java", DAO_LIST_LIMIT)
            if daos:
                dao_lines = _find_method_line(self._read_safely(repo, daos[0]), method_id)
                if dao_lines is not None:
                    return IssueLocation(repo.id, daos[0].id, dao_lines[0], dao_lines[1])
            # 4. 兜底：直接定位 XML 中该 statement 所在行
            return IssueLocation(repo.id, xml.id, 1, 1)
        return None

    def associate_hot_method(self, repo, class_name: str | None,
                             method_name: str | None) -> IssueLocation | None:
        """热点方法关联：按类名/方法名精确匹配。"""
        simple_name = class_name[class_name.rfind(".") + 1:] if class_name else None
        candidates: list[SourceFileModel] = []
        if simple_name and simple_name.strip():
            candidates.extend(self._repo.files_by_name(repo.id, simple_name + ".java", 20))
        if not candidates and class_name and class_name.strip():
            # 按类路径包含找（如 com/foo/Bar）
            path_prefix = class_name.replace(".", "/")
            candidates.extend(self._repo.files_by_repo_contains(repo.id, path_prefix, 20))
        for sf in candidates:
            lines = _find_method_line(self._read_safely(repo, sf), method_name)
            if lines is not None:
                return IssueLocation(repo.id, sf.id, lines[0], lines[1])
        return None

    def associate_by_method_chain(self, repo, method_chain_json: str | None) -> IssueLocation | None:
        """方法链关联：从链尾（最靠近 DB 的方法）向链首逐个精确定位，命中即返回。"""
        if not method_chain_json or not method_chain_json.strip():
            return None
        chain: list[tuple[str, str]] = []
        try:
            root = json.loads(method_chain_json)
            if not isinstance(root, list):
                return None
            for item in root:
                m = str(item or "")
                # 兼容两种格式：agent 上报 "包.类.方法"（最后一个 . 前为类）与 "包.类#方法"
                hash_idx = m.rfind("#")
                dot_idx = m.rfind(".")
                if hash_idx > 0:
                    chain.append((m[:hash_idx], m[hash_idx + 1:]))
                elif dot_idx > 0:
                    chain.append((m[:dot_idx], m[dot_idx + 1:]))
        except Exception:  # noqa: BLE001
            return None
        # 链尾优先：SQL 定义在 DAO/Mapper；接口方法声明行同样可被匹配
        for class_name, method_name in reversed(chain):
            loc = self.associate_hot_method(repo, class_name, method_name)
            if loc is not None:
                return loc
        return None

    def associate_url(self, repo, url: str | None) -> IssueLocation | None:
        """压测瓶颈 URL 关联：URL 匹配 Controller RequestMapping，定位处理方法。"""
        if not url or not url.strip():
            return None
        normalized = _normalize_url(url)
        controllers = [f for f in self._repo.files_by_repo_contains(
            repo.id, "controller", SCAN_FILE_LIMIT) if f.file_name.endswith(".java")]
        for ctl in controllers:
            content = self._read_safely(repo, ctl)
            if content is None:
                continue
            class_prefix = _find_class_mapping_prefix(content)
            lines = _find_mapping_method_line(content, normalized, class_prefix)
            if lines is not None:
                return IssueLocation(repo.id, ctl.id, lines[0], lines[1])
        return None

    def _read_safely(self, repo, sf: SourceFileModel) -> str | None:
        try:
            return self._indexer.read_content(self._write.repo_root_dir(repo.id), sf)
        except (OSError, ValueError):
            return None


# ============================================================ PerformanceAnalyzer

SYSTEM_PROMPT = """你是一个资深 Java 性能优化专家。根据检测证据、数据库执行计划与源码上下文，分析性能问题根因，\
并给出 2-3 个可落地的修复方案。方案必须具体到文件与改动点。\
若提供了 EXPLAIN 执行计划，请基于 type/key/rows/Extra 等字段给出确诊结论（如全表扫描、未命中索引、filesort）。\
只输出 JSON，不要输出任何其他文字。"""
# JSON 输出形状（Java 侧经 chatJson 的 schema 参数约束；Python 侧并入指令文本）
JSON_SHAPE = ('输出 JSON 形状：{"root_cause": string, "solutions": [{"title": string, '
              '"files": array, "changes": array, "code_changes": array, '
              '"benefit": string, "risk": string}]}')


class PerformanceAnalyzer:
    """性能问题分析器：LLM 分析 + 规则降级（对齐 PerformanceAnalyzer）。"""

    def __init__(self, repo: SourceRepository, llm: LlmClient | None,
                 indexer: SourceFileIndexer, write_service: SourceWriteService,
                 collector_repo: CollectorRepository, db_explain: DbExplainService):
        self._repo = repo
        self._llm = llm
        self._indexer = indexer
        self._write = write_service
        self._collector = collector_repo
        self._db_explain = db_explain

    def analyze(self, issue_id: int) -> int:
        """对问题执行根因分析并生成 2-3 个修复方案；返回方案数（可能为 0）。"""
        issue = self._repo.issue_by_id(issue_id)
        if issue is None:
            raise ValueError(f"问题不存在: {issue_id}")
        # 慢 SQL：尝试数据库只读检测（EXPLAIN 等），结果并入证据与上下文
        db_explain_text = self._collect_db_explain(issue)
        context = self._build_context(issue, db_explain_text)
        json_text = self._chat_json(SYSTEM_PROMPT + "\n" + JSON_SHAPE,
                                    f"请分析以下性能问题并生成修复方案：\n\n{context}")
        if json_text is None:
            # 降级规则模板
            return self._fallback_analyze(issue)
        return self._parse_and_save(issue, json_text)

    def _chat_json(self, system: str, user: str) -> str | None:
        """经 LlmClient.chat 发起 JSON 约束补全；客户端缺失/异常返回 None（降级）。"""
        if self._llm is None:
            return None
        try:
            result = self._llm.chat([{"role": "system", "content": system},
                                     {"role": "user", "content": user}], [])
            return result.content
        except Exception as exc:  # noqa: BLE001
            log.warning("llm chat failed: %s", exc)
            return None

    def _collect_db_explain(self, issue: PerformanceIssueModel) -> str:
        """慢 SQL 数据库只读检测：按证据 slowSqlId 找 SQL，按三级链匹配连接执行 EXPLAIN。"""
        if issue.issue_type != "slow_sql" or issue.evidence_json is None:
            return ""
        try:
            evidence = json.loads(issue.evidence_json)
            slow_sql_id = int(evidence.get("slowSqlId") or 0)
            if slow_sql_id <= 0:
                return ""
            slow_sql = self._repo.slow_sql_by_id(slow_sql_id)
            if slow_sql is None:
                return ""
            conn = self._match_conn(str(evidence.get("dbHost") or ""), slow_sql.app_name)
            if conn is None:
                return ""
            # 取用点解密：库内凭据为密文，明文不入 AI 上下文与日志
            explain = self._db_explain.analyze_sql(
                conn.db_url, conn.username, decrypt(conn.password), slow_sql.sql_text)
            summary = format_summary(explain)
            if not summary.strip():
                return ""
            actual_db = str(evidence.get("dbHost") or "")
            explain_target = self._db_explain.url_host(conn.db_url)
            # 一致性标注：EXPLAIN 库与慢 SQL 实际执行库不一致时前端警告
            consistent = (not actual_db or not explain_target
                          or explain_target in actual_db or actual_db in explain_target)
            evidence["dbExplain"] = {"summary": summary, "target": explain_target,
                                     "actualDb": actual_db, "consistent": consistent}
            updated = json.dumps(evidence, ensure_ascii=False)
            self._repo.update_issue_evidence(issue.id, updated)
            issue.evidence_json = updated
            return summary
        except Exception as exc:  # noqa: BLE001
            log.warning("collect db explain failed for issue %s: %s", issue.id, exc)
            return ""

    def _match_conn(self, db_host: str, app_name: str | None):
        """连接匹配三级链：host 双向包含 → 应用绑定 → 默认兜底（对齐 matchConn）。"""
        conns = self._collector.match_order()
        if not conns:
            return None
        if db_host and db_host.strip():
            for c in conns:
                if c.host and c.host.strip() and (db_host in c.host or c.host in db_host):
                    return c
        if app_name and app_name.strip():
            for c in conns:
                if c.app_name and c.app_name.strip() and app_name == c.app_name:
                    return c
        return conns[0]

    def _build_context(self, issue: PerformanceIssueModel, db_explain_text: str) -> str:
        """组装上下文：检测证据 + 数据库执行计划 + 源码片段。"""
        sb = [f"【问题类型】{issue.issue_type}\n", f"【标题】{issue.title}\n",
              f"【检测证据】{issue.evidence_json if issue.evidence_json is not None else '无'}\n"]
        if db_explain_text:
            sb.append(f"\n【数据库执行计划（真实 EXPLAIN）】\n{db_explain_text}\n")
        if issue.source_repo_id is not None and issue.source_file_id is not None:
            try:
                repo = self._write.get_repo(issue.source_repo_id)
                sf = self._repo.file_by_id(issue.source_file_id)
                if sf is not None:
                    root = self._write.repo_root_dir(repo.id)
                    line_start = issue.line_start if issue.line_start is not None else 1
                    line_end = issue.line_end if issue.line_end is not None else line_start + 20
                    snippet = self._indexer.read_lines(root, sf, line_start, line_end)
                    sb.append(f"\n【关联源码】{sf.relative_path} (第 {line_start}-{line_end} 行)\n"
                              f"{snippet}")
            except (OSError, ValueError) as exc:
                log.warning("read source snippet failed for issue %s: %s", issue.id, exc)
        return "".join(sb)

    def _parse_and_save(self, issue: PerformanceIssueModel, json_text: str) -> int:
        try:
            # 剥离可能的 markdown 代码围栏
            cleaned = json_text.strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
                cleaned = re.sub(r"\n?```$", "", cleaned)
            root = json.loads(cleaned)
            root_cause = root.get("root_cause") or ""
            self._repo.update_issue_analyzed(issue.id, root_cause)

            solutions = root.get("solutions")
            if not isinstance(solutions, list):
                solutions = []
            batch: list[FixSuggestionModel] = []
            for s in solutions[:3]:
                batch.append(FixSuggestionModel(
                    issue_id=issue.id,
                    title=str(s.get("title") or "修复方案"),
                    files_json=_node_to_json(s.get("files")),
                    changes_json=_node_to_json(s.get("changes")),
                    code_changes_json=_node_to_json(s.get("code_changes")),
                    benefit=str(s.get("benefit") or ""),
                    risk=str(s.get("risk") or ""),
                    selected=0))
            if batch:
                self._repo.insert_suggestions(batch)
            return len(batch)
        except Exception as exc:  # noqa: BLE001
            log.warning("parse llm analysis failed: %s", exc)
            return 0

    def _fallback_analyze(self, issue: PerformanceIssueModel) -> int:
        """规则降级：无 API Key 时按问题类型生成模板方案（文案逐字对齐）。"""
        root_cause = "（规则模式）" + _template_root_cause(issue.issue_type)
        self._repo.update_issue_analyzed(issue.id, root_cause)
        self._repo.insert_suggestions([
            FixSuggestionModel(
                issue_id=issue.id, title=_template_title(issue.issue_type),
                files_json='[{"file":"待确认","action":"按检测证据定位"}]',
                changes_json='[{"point":"优化热点执行路径，减少重复计算与无效调用"},'
                             '{"point":"增加缓存或索引，降低耗时"}]',
                code_changes_json="[]",
                benefit="显著降低该路径耗时，提升整体吞吐",
                risk="需回归验证，防止引入数据一致性问题",
                selected=0),
            FixSuggestionModel(
                issue_id=issue.id, title="异步化与削峰（规则模式）",
                files_json='[{"file":"待确认","action":"定位热点方法"}]',
                changes_json='[{"point":"将非关键路径改为异步执行，削峰填谷"},'
                             '{"point":"必要时引入消息队列解耦"}]',
                code_changes_json="[]",
                benefit="提升系统弹性与稳定性",
                risk="异步化增加复杂度，需关注失败补偿",
                selected=0),
        ])
        return 2


def _template_root_cause(issue_type: str | None) -> str:
    return {
        "slow_sql": "SQL 查询未命中索引或扫描行数过大，导致单次查询耗时偏高",
        "hot_method": "热点方法存在重复计算/循环内调用，执行路径耗时占比过高",
        "stress_bottleneck": "压测结果显示吞吐达到上限，存在串行瓶颈或资源争用",
        "jvm_alert": "堆内存或 GC 频率异常，可能存在内存泄漏或配置不当",
    }.get(issue_type or "", "结合检测证据初步判断为执行路径耗时过高")


def _template_title(issue_type: str | None) -> str:
    return {
        "slow_sql": "SQL 优化：索引与查询改写",
        "hot_method": "热点方法优化：消除重复计算",
        "stress_bottleneck": "瓶颈消除：并发与资源调优",
        "jvm_alert": "JVM 参数与内存优化",
    }.get(issue_type or "", "执行路径优化")


def _node_to_json(node) -> str:
    """None/缺失 → "[]"；其余 JSON 序列化（对齐 nodeToJson）。"""
    if node is None:
        return "[]"
    try:
        return json.dumps(node, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return "[]"


# ============================================================ FixPromptGenerator

def _format_list(json_text: str | None, *value_keys: str) -> str:
    """通用 JSON 数组格式化：兼容对象数组（按 valueKeys 优先级提取文本）与字符串数组。"""
    if not json_text or not json_text.strip():
        return "待确认"
    try:
        node = json.loads(json_text)
        if isinstance(node, list):
            parts = [t for t in (_item_text(item, *value_keys) for item in node) if t]
            return "；".join(parts) if parts else "待确认"
        return json_text
    except Exception:  # noqa: BLE001
        return json_text


def _item_text(item, *value_keys: str) -> str:
    """单个 JSON 元素转可读文本：file+action 组合优先，其次 valueKeys，最后拼接短字段。"""
    if item is None:
        return ""
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return str(item)
    file_name = str(item.get("file") or "")
    action = str(item.get("action") or "")
    if file_name:
        path = file_name[file_name.rfind("/") + 1:] if "/" in file_name else file_name
        return f"`{path}`" if not action else f"`{path}`（{action}）"
    for key in value_keys:
        v = item.get(key)
        if isinstance(v, str) and v.strip():
            return v
    # 兜底：拼接所有短文本字段值，避免输出空串
    parts = [v for v in item.values()
             if isinstance(v, str) and len(v) <= 200]
    return "，".join(parts)


class FixPromptGenerator:
    """修复提示词生成器（对齐 FixPromptGenerator）：选定方案组装 Markdown 并落库。"""

    def __init__(self, repo: SourceRepository, indexer: SourceFileIndexer,
                 write_service: SourceWriteService):
        self._repo = repo
        self._indexer = indexer
        self._write = write_service

    def generate(self, issue_id: int, suggestion_id: int) -> str:
        issue = self._repo.issue_by_id(issue_id)
        if issue is None:
            raise ValueError(f"问题不存在: {issue_id}")
        suggestion = self._repo.suggestion_by_id(suggestion_id)
        if suggestion is None or suggestion.issue_id != issue_id:
            raise ValueError(f"方案不存在或不属于该问题: {suggestion_id}")
        md = self._build_markdown(issue, suggestion)
        # 先清除该问题下其他方案的选定状态，保证同一问题只能有一个选定方案（单选）
        self._repo.clear_selected_by_issue(issue_id)
        self._repo.update_selected(suggestion_id, 1)
        self._repo.update_prompt_md(suggestion_id, md)
        return md

    def _build_markdown(self, issue: PerformanceIssueModel,
                        suggestion: FixSuggestionModel) -> str:
        sb = ["# 性能问题修复任务\n\n",
              "## 1. 任务目标\n\n",
              f"修复性能问题：**{issue.title}**（类型：{issue.issue_type}）\n\n",
              "## 2. 检测证据\n\n",
              "无\n\n" if issue.evidence_json is None
              else f"```json\n{issue.evidence_json}\n```\n\n",
              "## 3. 根因分析\n\n",
              "（规则模式）待进一步确认\n\n" if issue.root_cause is None
              else f"{issue.root_cause}\n\n"]

        # 源码引用
        if issue.source_repo_id is not None and issue.source_file_id is not None:
            try:
                repo = self._write.get_repo(issue.source_repo_id)
                sf = self._repo.file_by_id(issue.source_file_id)
                if sf is not None:
                    root = self._write.repo_root_dir(repo.id)
                    line_start = issue.line_start if issue.line_start is not None else 1
                    line_end = issue.line_end if issue.line_end is not None else line_start + 20
                    snippet = self._indexer.read_lines(root, sf, line_start, line_end)
                    sb.append("## 4. 源码引用\n\n")
                    sb.append(f"文件：`{sf.relative_path}` 第 {line_start}-{line_end} 行\n\n"
                              f"```java\n{snippet}```\n\n")
            except (OSError, ValueError) as exc:
                sb.append(f"（源码片段读取失败：仓库路径不存在或文件已被删除，请确认源码仓库仍可访问。"
                          f"原因：{exc}）\n\n")

        sb.append("## 5. 建议修改\n\n")
        sb.append("目标文件：\n\n")
        sb.append(f"- {_format_list(suggestion.files_json, 'file', 'path', 'name')}\n\n")
        sb.append("修改点：\n\n")
        sb.append(f"- {_format_list(suggestion.changes_json, 'point', 'change', 'desc', 'description', 'code')}\n\n")
        if (suggestion.code_changes_json and suggestion.code_changes_json.strip()
                and suggestion.code_changes_json != "[]"):
            sb.append(f"关键代码改动：\n\n```json\n{suggestion.code_changes_json}\n```\n\n")

        sb.append("## 6. 预期收益\n\n")
        sb.append(f"{suggestion.benefit}\n\n")
        sb.append("## 7. 风险与注意事项\n\n")
        sb.append(f"{suggestion.risk}\n\n")
        sb.append("## 8. 验证方式\n\n")
        sb.append("- 重新压测/复跑慢查询，确认耗时下降\n")
        sb.append("- 回归业务功能，确认无数据一致性问题\n")
        sb.append("- 观察 JVM 指标与错误率，确认无新异常\n\n")
        sb.append("> 请按上述方案修改代码，保持现有代码风格，改动最小化，并补充必要注释。")
        return "".join(sb)


# ============================================================ SourceIssueService

def _truncate(s: str | None, max_len: int) -> str:
    """null 安全截断：超长追加省略号（对齐 Texts.truncate）。"""
    if s is None:
        return ""
    return s if len(s) <= max_len else s[:max_len] + "..."


class SourceIssueService:
    """性能问题编排服务（对齐 SourceIssueService）：创建/绑定/分析/提示词。"""

    def __init__(self, repo: SourceRepository, write_service: SourceWriteService,
                 associator: IssueAssociator, analyzer: PerformanceAnalyzer,
                 prompt_generator: FixPromptGenerator, stress_repo):
        self._repo = repo
        self._write = write_service
        self._associator = associator
        self._analyzer = analyzer
        self._prompt_generator = prompt_generator
        self._stress_repo = stress_repo

    def create_from_slow_sql(self, slow_sql_id: int, repo_id: int) -> PerformanceIssueModel:
        """从慢 SQL 创建问题并关联源码（方法链精确定位 → XML → 实体多级兜底）。"""
        sql = self._find_slow_sql(slow_sql_id)
        repo = self._write.get_repo(repo_id)
        loc = self._associator.associate_slow_sql_smart(repo, sql.sql_text, sql.method_chain)

        evidence: dict = {
            "slowSqlId": sql.id,
            "appName": sql.app_name,
            "sqlText": sql.sql_text,
            "costMs": sql.cost_ms,
            "source": sql.source,
            "dbHost": sql.db_host,
        }
        # 触发方法链：让根因分析与提示词都知道"这条 SQL 由哪个业务链路触发"
        if sql.method_chain and sql.method_chain.strip():
            try:
                evidence["methodChain"] = json.loads(sql.method_chain)
            except Exception:  # noqa: BLE001
                evidence["methodChain"] = sql.method_chain
        issue = PerformanceIssueModel(
            issue_type="slow_sql", app_name=sql.app_name,
            title=f"慢 SQL: {_truncate(sql.sql_text, 60)}",
            evidence_json=json.dumps(evidence, ensure_ascii=False), status="pending")
        return self._insert_issue(issue, loc, repo_id)

    def create_from_hot_method(self, app_name: str | None, class_name: str,
                               method_name: str, repo_id: int) -> PerformanceIssueModel:
        """从热点方法创建问题（人工/AI 指定类名方法名）。"""
        repo = self._write.get_repo(repo_id)
        loc = self._associator.associate_hot_method(repo, class_name, method_name)
        issue = PerformanceIssueModel(
            issue_type="hot_method",
            app_name=app_name if app_name is not None else "unknown",
            title=f"热点方法耗时高: {class_name}#{method_name}",
            evidence_json=json.dumps({"className": class_name, "methodName": method_name},
                                     ensure_ascii=False),
            status="pending")
        return self._insert_issue(issue, loc, repo_id)

    def create_from_stress(self, stress_task_id: int, repo_id: int,
                           app_name: str | None) -> PerformanceIssueModel:
        """从压测任务创建问题（URL 关联 Controller）。

        应用继承链：调用方显式指定 → 压测任务自带 appName → 回退 stress。
        """
        task = self._stress_repo.find_by_id(stress_task_id)
        if task is None:
            raise ValueError(f"压测任务不存在: {stress_task_id}")
        repo = self._write.get_repo(repo_id)
        loc = self._associator.associate_url(repo, task.target_url)

        resolved_app = (app_name if app_name and app_name.strip()
                        else (task.app_name if task.app_name and task.app_name.strip()
                              else "stress"))
        issue = PerformanceIssueModel(
            issue_type="stress_bottleneck", app_name=resolved_app,
            title=f"压测瓶颈: {task.target_url} (并发 {task.concurrency})",
            # 场景快照：记录问题发现时的压测任务，作为修复后一键重放验证的基线
            stress_task_id=stress_task_id,
            evidence_json=json.dumps({"taskId": task.id, "targetUrl": task.target_url,
                                      "concurrency": task.concurrency,
                                      "resultJson": task.result_json or "{}"},
                                     ensure_ascii=False),
            status="pending")
        return self._insert_issue(issue, loc, repo_id)

    def _insert_issue(self, issue: PerformanceIssueModel, loc: IssueLocation | None,
                      repo_id: int) -> PerformanceIssueModel:
        """统一收尾：命中定位则写入源码位置，否则仅绑仓库；随后落库返回。"""
        # 未关联压测场景时显式落 0（列 NOT NULL，避免 null 入参）
        if issue.stress_task_id is None:
            issue.stress_task_id = 0
        if loc is not None:
            issue.source_repo_id = loc.repo_id
            issue.source_file_id = loc.file_id
            issue.line_start = loc.line_start
            issue.line_end = loc.line_end
        else:
            issue.source_repo_id = repo_id
        issue_id = self._repo.insert_issue(issue)
        return self._repo.issue_by_id(issue_id) or issue

    def analyze(self, issue_id: int) -> int:
        """执行大模型分析；压测瓶颈问题必须已关联源码仓库，否则拒绝。"""
        issue = self._repo.issue_by_id(issue_id)
        if issue is None:
            raise ValueError(f"问题不存在: {issue_id}")
        if issue.issue_type == "stress_bottleneck" and issue.source_repo_id is None:
            raise ValueError("压测问题必须先关联源码仓库才能分析：请先完成「关联源码仓库」再执行分析")
        return self._analyzer.analyze(issue_id)

    def bind_repo(self, issue_id: int, repo_id: int) -> dict:
        """补绑源码仓库并自动定位（按问题类型选择定位策略）；返回问题详情。"""
        issue = self._repo.issue_by_id(issue_id)
        if issue is None:
            raise ValueError(f"问题不存在: {issue_id}")
        repo = self._write.get_repo(repo_id)
        url = sql_text = chain = class_name = method_name = None
        try:
            ev = json.loads(issue.evidence_json or "{}")
            url = ev.get("targetUrl")
            sql_text = ev.get("sqlText")
            chain = json.dumps(ev["methodChain"], ensure_ascii=False) \
                if "methodChain" in ev else None
            class_name = ev.get("className")
            method_name = ev.get("methodName")
        except Exception:  # noqa: BLE001 证据缺失时仅落仓库级关联
            pass
        loc: IssueLocation | None = None
        if issue.issue_type == "stress_bottleneck" and url:
            loc = self._associator.associate_url(repo, url)
        elif issue.issue_type == "slow_sql":
            loc = self._associator.associate_slow_sql_smart(repo, sql_text, chain)
        elif issue.issue_type == "hot_method" and class_name:
            loc = self._associator.associate_hot_method(repo, class_name, method_name)
        self._repo.update_issue_location(
            issue_id, repo_id,
            loc.file_id if loc else None,
            loc.line_start if loc else None,
            loc.line_end if loc else None)
        return self.get_issue_detail(issue_id)

    def bind_stress_task(self, issue_id: int, stress_task_id: int) -> PerformanceIssueModel:
        """补绑压测场景快照（一键验证的前置条件：选定同场景压测任务作为验证基线）。"""
        issue = self._repo.issue_by_id(issue_id)
        if issue is None:
            raise ValueError(f"问题不存在: {issue_id}")
        if stress_task_id <= 0:
            raise ValueError(f"压测任务 ID 非法: {stress_task_id}")
        if self._stress_repo.find_by_id(stress_task_id) is None:
            raise ValueError(f"压测任务不存在: {stress_task_id}")
        self._repo.update_issue_stress_task(issue_id, stress_task_id)
        issue.stress_task_id = stress_task_id
        return issue

    def select_suggestion(self, issue_id: int, suggestion_id: int) -> str:
        """选定方案并生成修复提示词。"""
        return self._prompt_generator.generate(issue_id, suggestion_id)

    def get_issue_detail(self, issue_id: int) -> dict:
        """问题详情（含方案列表）。"""
        issue = self._repo.issue_by_id(issue_id)
        if issue is None:
            raise ValueError(f"问题不存在: {issue_id}")
        return {"issue": issue, "suggestions": self._repo.suggestions_by_issue(issue_id)}

    def _find_slow_sql(self, slow_sql_id: int):
        sql = self._repo.slow_sql_by_id(slow_sql_id)
        if sql is None:
            raise ValueError(f"慢 SQL 不存在: {slow_sql_id}")
        return sql
