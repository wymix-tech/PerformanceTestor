"""T1.2 收编对照测试：lang_java 副本与 legacy 解析器同输入同输出（实施方案 §4.6）。

legacy 解析器本体不动；两套并存期间以对照单测保证迁移副本行为一致。
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys
from pathlib import Path

from datagen.call_graph_analyzer import CallGraphAnalyzer as LegacyCallGraphAnalyzer
from datagen.endpoint_index_parser import EndpointIndexParser as LegacyEndpointIndexParser
from datagen.jpa_source_parser import JpaSourceParser as LegacyJpaSourceParser
from datagen.model import SourceFile as LegacySourceFile
from datagen.mybatis_source_parser import MyBatisSourceParser as LegacyMyBatisSourceParser
from datagen.mybatis_sql_resolver import MyBatisSqlResolver as LegacyMyBatisSqlResolver
from ptde.analyzer.lang_java import (
    CallGraphAnalyzer,
    EndpointIndexParser,
    JpaSourceParser,
    MyBatisSourceParser,
    MyBatisSqlResolver,
)
from ptde.analyzer.lang_java._legacy_model import SourceFile

_PERF_DEMO = Path("/workspace/perf-demo")

XML = """
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE mapper PUBLIC "-//mybatis.org//DTD Mapper 3.0//EN" "http://mybatis.org/dtd/mybatis-3-mapper.dtd">
<mapper namespace="com.x.TOrderMapper">
  <resultMap id="rm" type="com.x.T_Order">
    <id column="id" property="id"/>
    <association property="user" column="user_id" javaType="com.x.T_User"/>
    <collection property="items" column="order_id" ofType="com.x.T_Item"/>
  </resultMap>
  <select id="sel" resultType="com.x.T_Order">
    SELECT o.* FROM t_order o WHERE o.status = 'SHIPPED' AND o.user_id = 7
  </select>
</mapper>
"""

JAVA = """
package com.x;
import org.apache.ibatis.annotations.Select;
import java.util.List;
enum PayState { NEW, PAID, DONE }
class OrderConst { public static final String STATUS_SHIPPED = "SHIPPED"; }
interface TUserMapper {
  @Select("SELECT u.* FROM t_user u WHERE u.age = 18")
  List<Object> sel();
}
"""

CONTROLLER_JAVA = """
package com.x;
import org.springframework.web.bind.annotation.*;
@RestController
@RequestMapping("/api/order")
public class OrderController {
  @GetMapping("/list")
  public Object list() { return null; }
  @PostMapping("/create")
  public Object create() { return null; }
}
"""


def _demo_files() -> list[tuple[str, str]]:
    """读取 perf-demo 真实项目源码（存在则用；CI 缺目录时退化为内嵌样例）。"""
    files: list[tuple[str, str]] = []
    if _PERF_DEMO.exists():
        for p in sorted(_PERF_DEMO.rglob("*.java")) + sorted(_PERF_DEMO.rglob("*.xml")):
            try:
                files.append((str(p.relative_to(_PERF_DEMO)), p.read_text(encoding="utf-8")))
            except OSError:  # pragma: no cover
                continue
    if not files:
        files = [("m/TOrderMapper.xml", XML), ("m/TUserMapper.java", JAVA)]
    return files


def _as_legacy(sf: SourceFile) -> LegacySourceFile:
    return LegacySourceFile(path=sf.path, content=sf.content)


def _compare(scan_new, scan_old) -> None:
    assert dataclasses.asdict(scan_new) == dataclasses.asdict(scan_old)


def test_mybatis_parser_same_output() -> None:
    files = [SourceFile("m/TOrderMapper.xml", XML), SourceFile("m/TUserMapper.java", JAVA)]
    known = {"t_order", "t_user", "t_item"}
    _compare(
        MyBatisSourceParser().scan(files, known),
        LegacyMyBatisSourceParser().scan([_as_legacy(f) for f in files], known),
    )


def test_jpa_parser_same_output() -> None:
    files = [SourceFile("m/TUserMapper.java", JAVA)]
    known = {"t_user"}
    _compare(
        JpaSourceParser().scan(files, known),
        LegacyJpaSourceParser().scan([_as_legacy(f) for f in files], known),
    )


def test_endpoint_parser_same_output() -> None:
    files = [SourceFile("c/OrderController.java", CONTROLLER_JAVA)]
    new_eps = EndpointIndexParser().parse(files)
    old_eps = LegacyEndpointIndexParser().parse([_as_legacy(f) for f in files])
    assert [dataclasses.asdict(e) for e in new_eps] == [dataclasses.asdict(e) for e in old_eps]
    assert any(e.path == "/api/order/list" and e.http_method == "GET" for e in new_eps)


def test_call_graph_and_sql_resolver_same_output() -> None:
    files = _demo_files()
    src = [SourceFile(p, c) for p, c in files]
    legacy_src = [_as_legacy(s) for s in src]
    java_files = [SourceFile(p, c) for p, c in files if p.endswith(".java")]
    eps = EndpointIndexParser().parse(java_files)
    if not eps:
        return
    entry = eps[0]
    new_cg = CallGraphAnalyzer(deadline_ms=5000).analyze(src, entry)
    old_cg = LegacyCallGraphAnalyzer(deadline_ms=5000).analyze(legacy_src, entry)
    assert [dataclasses.asdict(i) for i in new_cg.invocations] == [
        dataclasses.asdict(i) for i in old_cg.invocations
    ]
    assert new_cg.unresolved == old_cg.unresolved
    xml_files = [SourceFile(p, c) for p, c in files if p.endswith(".xml")]
    new_res = MyBatisSqlResolver(xml_files)
    old_res = LegacyMyBatisSqlResolver([_as_legacy(s) for s in xml_files])
    assert new_res.resolve("com.x.TOrderMapper", "sel") == old_res.resolve(
        "com.x.TOrderMapper", "sel"
    )


def test_no_reverse_dependency_on_datagen() -> None:
    """A4 反向依赖禁令：ptde/analyzer 与 ptde/adapters 不得 import datagen。

    （ptde/models/compat.py 是 B0 确立的旧→新适配层，允许引用 legacy 模型。）
    """
    offenders: list[str] = []
    for pkg in (Path("/workspace/perf-py/ptde/analyzer"), Path("/workspace/perf-py/ptde/adapters")):
        for py in pkg.rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            for i, line in enumerate(text.splitlines(), 1):
                s = line.strip()
                if s.startswith("#"):
                    continue
                if s.startswith(("from datagen", "import datagen")) or "from datagen." in s:
                    offenders.append(f"{py}:{i}: {s}")
    assert offenders == []
    # 额外用子进程导入验证（sys.modules 层面）
    r = subprocess.run(
        [sys.executable, "-c",
         "import ptde.analyzer, ptde.analyzer.lang_java, ptde.adapters.mysql, "
         "ptde.adapters.postgres; "
         "assert not any(m == 'datagen' or m.startswith('datagen.') "
         "for m in __import__('sys').modules)"],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
