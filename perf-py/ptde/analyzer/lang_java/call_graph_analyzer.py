"""调用链分析器：移植自 endpoint/CallGraphAnalyzer.java。

从 Controller 入口方法出发按需展开（BFS），终止于 Mapper 调用。
不做全仓库建图——只解析「被调目标的声明类型」所在文件，逐层展开，并受
深度 / 文件数 / 节点数 / 总超时四重熔断保护。

无状态、线程安全：AST 缓存为单次分析请求的局部对象，分析结束即可回收。
"""

from __future__ import annotations

import time
from collections import deque

from ptde.analyzer.lang_java import java_ast as ja
from ptde.analyzer.lang_java._legacy_model import EndpointRef, SourceFile
from ptde.analyzer.lang_java.endpoint_path import simple_name_of

# 展开深度上限（Controller → Service → 子 Service → Mapper 足够）
MAX_DEPTH = 4
# 参与分析的文件数上限
MAX_FILES = 300
# BFS 节点数上限
MAX_NODES = 200_000
# 分析总超时（ms）
TIMEOUT_MS = 15_000
# 触发熔断后已得结论的置信度
TRIPPED_CONFIDENCE = 0.4
# 接口存在多个实现时结论的置信度
MULTI_IMPL_CONFIDENCE = 0.6

MAPPER_SUFFIX = ("Mapper", "Dao", "Repository")

# 可能包含业务逻辑、值得继续展开的类型后缀（过滤容器/JDK 类型的噪声告警）
BUSINESS_SUFFIX = (
    "Service", "ServiceImpl", "Manager", "ManagerImpl", "Mapper", "Dao", "Repository",
    "Client", "Helper", "Handler", "Provider", "Processor", "BO", "Biz", "Facade",
)


class MapperInvocation:
    """Mapper 调用点：一个接口方法对 Mapper 的一次调用。"""

    def __init__(self, mapper_type: str, method_name: str, call_site: str,
                 mapper_file: str | None) -> None:
        self.mapper_type = mapper_type
        self.method_name = method_name
        # 调用点证据：文件:行号
        self.call_site = call_site
        # Mapper 源码文件相对路径（未定位到文件时为空串）
        self.mapper_file = mapper_file if mapper_file is not None else ""

    def __str__(self) -> str:
        return f"{self.mapper_type}#{self.method_name} @ {self.call_site}"


class CallGraphResult:
    """分析结果。"""

    def __init__(self) -> None:
        self.invocations: list[MapperInvocation] = []
        # 未解析原因集合（去重，保持出现顺序，直接展示给用户）
        self.unresolved: list[str] = []
        self._unresolved_seen: set[str] = set()
        # 是否触发熔断（深度/文件/节点/超时任一）
        self.tripped = False
        # 是否命中「接口多实现」（并集纳入，结论置信度下降）
        self.multi_impl = False

    def add_unresolved(self, reason: str) -> None:
        if reason not in self._unresolved_seen:
            self._unresolved_seen.add(reason)
            self.unresolved.append(reason)

    def trip(self, reason: str) -> None:
        self.tripped = True
        self.add_unresolved(reason)


class _Node:
    __slots__ = ("file", "type", "method", "depth")

    def __init__(self, file: str, type_: str, method: str, depth: int) -> None:
        self.file = file
        self.type = type_
        self.method = method
        self.depth = depth

    def key(self) -> str:
        return f"{self.file}#{self.type}#{self.method}"


class CallGraphAnalyzer:

    def __init__(self, deadline_ms: float | None = None) -> None:
        # 测试可见：允许注入固定 deadline 以验证熔断
        self._deadline = deadline_ms if deadline_ms is not None else time.monotonic() * 1000 + TIMEOUT_MS

    def analyze(self, java_files: list[SourceFile] | None, entry: EndpointRef | None) -> CallGraphResult:
        """从入口接口方法出发展开调用链；返回 Mapper 调用点与未解析原因；不会抛异常。"""
        result = CallGraphResult()
        if java_files is None or entry is None or entry.declaring_file is None:
            result.add_unresolved("入口接口信息不完整，无法分析调用链")
            return result

        content_by_path: dict[str, str] = {}
        files_by_type: dict[str, list[str]] = {}
        for f in java_files:
            path = f.path
            if path is None or not path.lower().endswith(".java") or f.content is None:
                continue
            content_by_path[path] = f.content
            files_by_type.setdefault(simple_name_of(path), []).append(path)

        ast_cache: dict[str, object] = {}
        parsed_files = [0]  # 计数器（熔断用）

        def ast(file: str):
            """惰性解析 + 缓存；解析失败记为 None，避免重复尝试。"""
            if file in ast_cache:
                return ast_cache[file]
            parsed_files[0] += 1
            content = content_by_path.get(file)
            node = ja.parse_java(content) if content is not None else None
            ast_cache[file] = node
            return node

        queue: deque[_Node] = deque()
        visited: set[str] = set()
        root = _Node(entry.declaring_file,
                     simple_name_of(entry.declaring_file), entry.method_name or "", 0)
        queue.append(root)
        visited.add(root.key())

        nodes = 0
        while queue:
            if time.monotonic() * 1000 > self._deadline:
                result.trip(f"调用链分析超时（>{TIMEOUT_MS}ms），结论可能不完整")
                break
            nodes += 1
            if nodes > MAX_NODES or parsed_files[0] > MAX_FILES:
                result.trip(f"调用链规模超上限（节点 {MAX_NODES} / 文件 {MAX_FILES}），结论可能不完整")
                break
            node = queue.popleft()
            if node.depth > MAX_DEPTH:
                result.trip(f"调用链深度超上限（>{MAX_DEPTH} 层），深层调用未展开")
                continue
            cu = ast(node.file)
            if cu is None:
                result.add_unresolved(f"源码解析失败或未索引: {node.file}")
                continue
            type_node = _find_type(cu, node.type)
            if type_node is None:
                result.add_unresolved(f"未找到类型声明: {node.type} ({node.file})")
                continue
            methods = ja.body_children(type_node, "method_declaration")
            matched = None
            for m in methods:
                name = ja.field(m, "name")
                if name is not None and ja.text(name) == node.method:
                    matched = m
                    break
            if matched is None:
                # 同名重载/继承方法：退化为整类型扫描，仍可能命中 Mapper 调用
                result.add_unresolved(f"未定位到方法体，按类型整体扫描: {node.type}#{node.method}")
                for m in methods:
                    self._walk(cu, type_node, m, node, queue, visited, result,
                               content_by_path, files_by_type, ast)
                continue
            self._walk(cu, type_node, matched, node, queue, visited, result,
                       content_by_path, files_by_type, ast)
        return result

    # ===== 遍历 =====

    def _walk(self, cu, type_node, method, node: _Node, queue, visited,
              result: CallGraphResult, content_by_path, files_by_type, ast) -> None:
        for call in ja.walk(method, "method_invocation"):
            callee_node = ja.field(call, "name")
            callee = ja.text(callee_node) if callee_node is not None else ""
            scope = ja.field(call, "object")
            if scope is None:
                target_type = node.type                                    # this.xxx() / 本类方法
            elif scope.type == "this":
                target_type = node.type
            elif scope.type == "field_access":
                inner = ja.field(scope, "object")
                if inner is not None and inner.type == "this":
                    target_type = node.type
                else:
                    fa_name = ja.field(scope, "field")
                    target_type = (self._resolve_identifier_type(
                        cu, type_node, method, ja.text(fa_name), ast)
                        if fa_name is not None else None)
            elif scope.type == "identifier":
                target_type = self._resolve_identifier_type(cu, type_node, method,
                                                            ja.text(scope), ast)
            else:
                target_type = None

            if target_type is None:
                # 无法静态确定的调用：JDK 静态工具（Math/UUID）、容器方法（List#size）、
                # var 推导等。与数据库访问无关，静默跳过——unresolved 通道要保持干净，
                # 否则真正的「未覆盖」会被噪声淹没
                continue
            if _is_mapper_type(target_type):
                result.invocations.append(MapperInvocation(
                    target_type, callee, _source_ref(node.file, call),
                    _first_file_of_type(files_by_type, target_type)))
                continue
            if node.depth + 1 > MAX_DEPTH:
                result.trip(f"调用链深度超上限（>{MAX_DEPTH} 层），深层调用未展开")
                continue
            impl_files = self._resolve_implementation_files(
                target_type, files_by_type, content_by_path, ast)
            if not impl_files:
                # 只对「可能含业务逻辑」的类型报未覆盖；List/Map/String/Optional 等
                # 容器与 JDK 类型没有源码是常态，报出来只会淹没有效信息
                if _looks_like_business_type(target_type):
                    result.add_unresolved(
                        f"未找到类型源码，无法继续展开: {target_type}（{node.file}）")
                continue
            if len(impl_files) > 1:
                result.multi_impl = True
            for impl_file in impl_files:
                nxt = _Node(impl_file, target_type, callee, node.depth + 1)
                if nxt.key() not in visited:
                    visited.add(nxt.key())
                    queue.append(nxt)

    def _resolve_identifier_type(self, cu, type_node, method, name: str, ast) -> str | None:
        """解析被调用者的声明类型：字段注入类型 → 方法参数类型 → 局部变量初始化类型。

        返回简单类型名；无法解析返回 None。
        """
        # 1. 字段注入（@Autowired/@Resource 字段，含构造器注入的 final 字段）
        for fd in ja.body_children(type_node, "field_declaration"):
            for vd in fd.named_children:
                if vd.type != "variable_declarator":
                    continue
                vd_name = ja.field(vd, "name")
                if vd_name is not None and ja.text(vd_name) == name:
                    return _simple_type_name(ja.text(ja.field(fd, "type")))
        # 2. 方法参数
        params = ja.field(method, "parameters")
        if params is not None:
            for p in params.named_children:
                if p.type != "formal_parameter":
                    continue
                p_name = ja.field(p, "name")
                if p_name is not None and ja.text(p_name) == name:
                    return _simple_type_name(ja.text(ja.field(p, "type")))
        # 3. 局部变量：有显式声明类型时直接用（List<User> → List，无对应源码时会安静跳过）
        for vd in ja.walk(method, "variable_declarator"):
            vd_name = ja.field(vd, "name")
            if vd_name is None or ja.text(vd_name) != name:
                continue
            init = ja.field(vd, "value")
            if init is not None and init.type == "object_creation_expression":
                return _simple_type_name(ja.text(ja.field(init, "type")))
            type_node2 = ja.field(vd, "type")
            if type_node2 is None:
                continue
            type_text = ja.text(type_node2)
            return None if type_text == "var" else _simple_type_name(type_text)
        # 4. 同文件其他类型（内部类/父类字段）兜底
        for fd in ja.walk(cu, "field_declaration"):
            for vd in fd.named_children:
                if vd.type != "variable_declarator":
                    continue
                vd_name = ja.field(vd, "name")
                if vd_name is not None and ja.text(vd_name) == name:
                    return _simple_type_name(ja.text(ja.field(fd, "type")))
        return None

    def _resolve_implementation_files(self, target_type: str, files_by_type,
                                      content_by_path, ast) -> list[str]:
        """目标类型对应的实现文件：接口则纳入全部实现类（并集，避免漏表）；
        否则按「文件名 = 类型名」的 Java 惯例定位。"""
        direct = files_by_type.get(target_type)
        if not direct:
            return []
        is_interface = False
        for f in direct:
            c = ast(f)
            if c is not None:
                t = _find_type(c, target_type)
                if t is not None and t.type == "interface_declaration":
                    is_interface = True
                    break
        if not is_interface:
            return list(direct)
        impls = self._find_implementations(target_type, content_by_path, ast)
        return impls if impls else list(direct)

    def _find_implementations(self, target_type: str, content_by_path, ast) -> list[str]:
        """查找实现了指定接口的文件：文本粗筛 + AST 精判。

        纯文本扫描会把注释/字符串里的 implements 误判为声明，且 implements A, B
        中目标不在首位时会漏判；纯 AST 扫描则要对全仓库建 AST，会直接触发文件数
        熔断。故先按文本快速筛出候选，再用声明语法做权威确认——两级结合既准又可控。
        """
        impls: list[str] = []
        for path, content in content_by_path.items():
            if "implements" not in content or target_type not in content:
                continue
            cu = ast(path)
            if cu is None:
                continue
            for t in ja.walk(cu, "class_declaration"):
                hit = False
                sup = next((c for c in t.named_children if c.type == "super_interfaces"), None)
                if sup is not None:
                    for type_node in ja.walk(sup, "type_identifier"):
                        if _simple_type_name(ja.text(type_node)) == target_type:
                            hit = True
                            break
                if hit:
                    impls.append(path)
                    break
        return impls


def _find_type(cu, simple_name: str):
    """按简单名找类型声明；未命中回退文件内第一个类型声明。"""
    nodes = ja.class_or_interface_nodes(cu)
    for t in nodes:
        name = ja.field(t, "name")
        if name is not None and ja.text(name) == simple_name:
            return t
    return nodes[0] if nodes else None


def _is_mapper_type(type_: str | None) -> bool:
    if type_ is None:
        return False
    return any(type_.endswith(s) for s in MAPPER_SUFFIX)


def _looks_like_business_type(type_: str | None) -> bool:
    """是否为疑似业务类型（无源码时值得提示用户）。"""
    if type_ is None:
        return False
    return any(type_.endswith(s) for s in BUSINESS_SUFFIX)


def _first_file_of_type(files_by_type: dict[str, list[str]], type_: str) -> str:
    files = files_by_type.get(type_)
    return files[0] if files else ""


def _simple_type_name(raw_type: str | None) -> str | None:
    if raw_type is None:
        return None
    s = raw_type
    generic = s.find("<")
    if generic >= 0:
        s = s[:generic]  # List<Order> → List（容器类型无需展开）
    dot = s.rfind(".")
    if dot >= 0:
        s = s[dot + 1:]
    s = s.replace("[]", "").strip()
    return s or None


def _source_ref(file: str, node) -> str:
    return f"{file}:{node.start_point[0] + 1}"
