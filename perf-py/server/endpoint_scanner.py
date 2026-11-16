"""源码 HTTP 接口扫描器（阶段 5 · 步骤 10）：Java EndpointScanner 的逐语义移植。

解析源码仓库中的 Spring MVC Controller，提取接口清单（HTTP 方法 + 完整路径 +
所在类/方法/行号），供压测中心"选项目 → 自动出接口"流程使用。

支持注解（Spring MVC / Spring Boot 通用）：类级 @RequestMapping 前缀 +
方法级 @GetMapping/@PostMapping/@PutMapping/@DeleteMapping/@PatchMapping/
@RequestMapping；兼容含 consumes 等多属性的写法。

路径归一化复用 datagen.endpoint_path（EndpointPath 移植单点，双实现不漂移）。
"""

from __future__ import annotations

import re

from datagen.endpoint_path import join as ep_join
from datagen.endpoint_path import normalize as ep_normalize
from server.source_index import SourceFileIndexer
from server.source_repo import SourceFileModel, SourceRepository
from server.source_write_service import SourceWriteService

# 类级 @RequestMapping：必须紧邻 class 声明（中间仅允许其他注解），避免误吸方法级注解
CLASS_MAPPING = re.compile(
    r"@RequestMapping\s*\(([^)]*)\)\s*(?:@\w+(?:\([^)]*\))?\s*)*"
    r"(?:public\s+)?(?:final\s+|abstract\s+)*class\s+\w+",
    re.IGNORECASE)
# 方法级注解：@GetMapping("/x") / @PostMapping(...) / @RequestMapping(value="/x", method=...)
METHOD_MAPPING = re.compile(r"@(Get|Post|Put|Delete|Patch|Request)Mapping\b([^\n]*)")
# 注解属性中的路径字符串
PATH_ATTR = re.compile(r'(?:value|path)\s*=\s*\{?\s*"([^"]*)"|\(\s*"([^"]*)"')
# 方法名（用于展示来源）
METHOD_DEF = re.compile(r"(?:public|protected|private)\s+[\w.<>\[\],\s]+?\s+(\w+)\s*\(")
# 是否 Controller 类
CONTROLLER_ANNO = re.compile(r"@(Rest)?Controller\b")
# @RequestMapping(method = RequestMethod.POST) 显式指定
REQUEST_METHOD = re.compile(r"RequestMethod\.(GET|POST|PUT|DELETE|PATCH)", re.IGNORECASE)

SCAN_FILE_LIMIT = 5000
METHOD_LOOKUP_LINES = 12


def extract_path(anno_args: str | None) -> str:
    """从注解参数串提取路径：@GetMapping("/a")、@RequestMapping(value="/a", ...)、无参为空。"""
    if not anno_args or not anno_args.strip():
        return ""
    m = PATH_ATTR.search(anno_args)
    if m:
        return m.group(1) if m.group(1) is not None else m.group(2)
    # 形如 ("/a") 已被第二分支覆盖；纯字符串参数
    m = re.search(r'"([^"]*)"', anno_args)
    return m.group(1) if m else ""


class EndpointScanner:
    """扫描仓库全部 HTTP 接口（对齐 EndpointScanner.scan / scanFile）。"""

    def __init__(self, repo: SourceRepository, write_service: SourceWriteService,
                 indexer: SourceFileIndexer):
        self._repo = repo
        self._write = write_service
        self._indexer = indexer

    def scan(self, repo_id: int) -> list[dict]:
        repo = self._write.get_repo(repo_id)
        endpoints: list[dict] = []
        files = self._repo.files_by_repo_contains(repo.id, ".java", SCAN_FILE_LIMIT)
        root = self._write.repo_root_dir(repo.id)
        for sf in files:
            if not sf.relative_path.endswith(".java"):
                continue
            content = self._read_safely(root, sf)
            if content is None or not CONTROLLER_ANNO.search(content):
                continue
            scan_file(sf, content, endpoints)
        return endpoints

    def _read_safely(self, root: str, sf: SourceFileModel) -> str | None:
        try:
            return self._indexer.read_content(root, sf)
        except (OSError, ValueError):
            # 单文件读取失败跳过（不影响其余文件扫描；非法路径同 Java 返回 null）
            return None


def scan_file(sf: SourceFileModel, content: str, out: list[dict]) -> None:
    """单文件解析：类前缀 + 方法级注解逐个匹配（对齐 EndpointScanner.scanFile）。"""
    class_name = sf.file_name.replace(".java", "")
    class_prefix = ""
    class_anno_end_line = -1
    class_m = CLASS_MAPPING.search(content)
    if class_m:
        class_prefix = ep_normalize(extract_path(class_m.group(1)))
        # 类级注解匹配段的行范围（含中间注解），方法级扫描时跳过，避免类级注解被重复解析
        class_anno_end_line = content[:class_m.start()].count("\n") + 1

    lines = content.split("\n")
    for i, raw_line in enumerate(lines):
        line = raw_line.strip()
        if i < class_anno_end_line:
            continue  # 类声明前的类级注解段
        mm = METHOD_MAPPING.search(line)
        if not mm:
            continue
        anno = mm.group(1)  # Get/Post/.../Request
        if anno == "Request":
            # @RequestMapping(method = RequestMethod.POST) 显式指定时优先
            rm = REQUEST_METHOD.search(mm.group(2))
            http_method = rm.group(1).upper() if rm else "GET"
        else:
            http_method = anno.upper()
        path = extract_path(mm.group(2))

        # 向下找最近的方法定义行（最多 12 行），确定 methodName
        # 注意：只跳过"以 @ 开头"的注解行；方法定义行可能含 @RequestParam 等参数注解
        method_name = ""
        for j in range(i + 1, min(i + 1 + METHOD_LOOKUP_LINES, len(lines))):
            candidate = lines[j].strip()
            if not candidate:
                continue
            if candidate.startswith("@") or candidate.startswith("*") \
                    or candidate.startswith("/*"):
                continue
            md = METHOD_DEF.search(candidate)
            if md:
                method_name = md.group(1)
                break
            # 已到下一个方法级注解或类结束仍未命中，停止
            if METHOD_MAPPING.search(candidate) or candidate.startswith("}"):
                break

        full_path = ep_join(class_prefix, path)
        out.append({
            "httpMethod": http_method,
            "path": full_path,
            "className": class_name,
            "classFile": sf.relative_path,
            "methodName": method_name,
            "line": i + 1,
        })
