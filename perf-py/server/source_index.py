"""源码文件索引与读取（阶段 5 · 步骤 9）：Java SourceFileService 的逐语义移植。

对齐点：
- 语言识别 LANGUAGE_MAP / TEXT_EXTS 白名单：无扩展名 → "text"，白名单外 → "binary"；
- index_directory：深度 ≤20 遍历 → 过滤忽略项（.git/.idea/*.class/target/node_modules/dist）
  → 超大文件跳过 → binary 跳过 → findByRepoAndPath 幂等跳过 → SHA-256 content_hash
  （失败置空串）→ 500 条一批入库 → 事后校验文件数/总大小上限（中文文案逐字对齐）；
- read_content：路径越界检查 + 单文件上限，超限返回占位串；
- read_lines：1-based 闭区间行切片，带 "{n}: " 行号前缀。

Java 侧 Files.walk(root, 20) 深度上限对应 os.walk 的目录层数控制（walk 内部手动限深）；
配置项对齐 @Value：perf.source.max-file-size-mb=1 / max-total-size-mb=200 / max-file-count=5000。
"""

from __future__ import annotations

import hashlib
import os

from server.source_repo import SourceFileModel, SourceRepository

LANGUAGE_MAP = {
    "java": "java", "xml": "xml", "yml": "yaml", "yaml": "yaml", "json": "json",
    "properties": "properties", "sql": "sql", "md": "markdown", "html": "html",
    "js": "javascript", "ts": "typescript", "vue": "vue", "css": "css",
    "kt": "kotlin", "gradle": "groovy", "sh": "shell",
}
TEXT_EXTS = {
    "java", "xml", "yml", "yaml", "json", "properties", "sql", "md", "txt",
    "html", "js", "ts", "vue", "css", "scss", "less", "kt", "gradle", "conf", "ini", "sh", "bat",
}
WALK_DEPTH_LIMIT = 20
FILE_BATCH_SIZE = 500
TOO_LARGE_PLACEHOLDER = "// 文件过大，未加载"


def detect_language(file_name: str) -> str:
    dot = file_name.rfind(".")
    if dot < 0:
        return "text"
    ext = file_name[dot + 1:].lower()
    return LANGUAGE_MAP.get(ext, ext if ext in TEXT_EXTS else "binary")


def is_ignored(rel: str) -> bool:
    """忽略 git 元数据与构建产物（对齐 SourceFileService.isIgnored）。"""
    return (rel.startswith(".git/") or rel.startswith(".idea/")
            or rel.endswith(".class") or "/target/" in rel
            or "/node_modules/" in rel or "/dist/" in rel)


def walk_files(root: str) -> list[str]:
    """递归列出全部文件（目录深度限制 20，避免符号链接死循环；对齐 Files.walk(root, 20)）。"""
    result: list[str] = []
    root_depth = os.path.abspath(root).rstrip(os.sep).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(os.path.abspath(root)):
        if dirpath.count(os.sep) - root_depth >= WALK_DEPTH_LIMIT:
            dirnames[:] = []
        for name in filenames:
            result.append(os.path.join(dirpath, name))
    return result


def _hash_file(file: str) -> str:
    """SHA-256（对齐 hashFile：哈希失败返回空串，文件索引仍可写入）。"""
    try:
        with open(file, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except Exception:  # noqa: BLE001
        return ""


class SourceFileIndexer:
    """SourceFileService 移植：扫描磁盘目录建立 source_file 索引，懒加载文件内容。"""

    def __init__(self, repo: SourceRepository,
                 max_file_size_mb: int | None = None,
                 max_total_size_mb: int | None = None,
                 max_file_count: int | None = None):
        self._repo = repo
        self.max_file_size_mb = (max_file_size_mb if max_file_size_mb is not None
                                 else int(os.environ.get("PERF_SOURCE_MAX_FILE_SIZE_MB", "1")))
        self.max_total_size_mb = (max_total_size_mb if max_total_size_mb is not None
                                  else int(os.environ.get("PERF_SOURCE_MAX_TOTAL_SIZE_MB", "200")))
        self.max_file_count = (max_file_count if max_file_count is not None
                               else int(os.environ.get("PERF_SOURCE_MAX_FILE_COUNT", "5000")))

    def index_directory(self, repo_id: int, root_dir: str) -> int:
        """扫描磁盘目录并批量建索引（幂等：已存在的相对路径跳过）。返回新索引文件数。"""
        if not os.path.isdir(root_dir):
            raise ValueError(f"源码目录不存在: {root_dir}")
        batch: list[SourceFileModel] = []
        indexed = 0
        file_count = 0
        total_size = 0
        for file in walk_files(root_dir):
            rel = os.path.relpath(file, os.path.abspath(root_dir)).replace("\\", "/")
            if is_ignored(rel):
                continue
            size = os.path.getsize(file)
            file_count += 1
            total_size += size
            if size > self.max_file_size_mb * 1024 * 1024:
                # 超大文件跳过（Java log.warn 后 continue，索引不含该文件）
                continue
            file_name = os.path.basename(file)
            language = detect_language(file_name)
            if language == "binary":
                continue
            if self._repo.file_by_repo_and_path(repo_id, rel) is not None:
                continue  # 幂等跳过
            batch.append(SourceFileModel(
                repo_id=repo_id, relative_path=rel, file_name=file_name,
                language=language, size=size, content_hash=_hash_file(file)))
            if len(batch) >= FILE_BATCH_SIZE:
                indexed += self._repo.batch_insert_files(batch)
                batch = []
        if batch:
            indexed += self._repo.batch_insert_files(batch)
        if file_count > self.max_file_count:
            raise ValueError(f"文件数超过上限 {self.max_file_count}")
        if total_size > self.max_total_size_mb * 1024 * 1024:
            raise ValueError(f"源码总大小超过上限 {self.max_total_size_mb}MB")
        return indexed

    def read_content(self, root_dir: str, sf: SourceFileModel) -> str:
        """读取文件内容（懒加载，单文件上限内；路径越界 → 非法文件路径文案）。"""
        root_abs = os.path.abspath(root_dir)
        file = os.path.abspath(os.path.join(root_abs, sf.relative_path))
        if not (file == root_abs or file.startswith(root_abs + os.sep)):
            raise ValueError(f"非法文件路径: {sf.relative_path}")
        if os.path.getsize(file) > self.max_file_size_mb * 1024 * 1024:
            return TOO_LARGE_PLACEHOLDER
        with open(file, encoding="utf-8", errors="replace") as fh:
            return fh.read()

    def read_lines(self, root_dir: str, sf: SourceFileModel,
                   line_start: int, line_end: int) -> str:
        """读取指定行区间（1-based，含两端，带 "{n}: " 行号前缀）。"""
        content = self.read_content(root_dir, sf)
        lines = content.split("\n")
        start = max(0, line_start - 1)
        end = min(len(lines), line_end)
        if start >= end:
            return ""
        return "".join(f"{i + 1}: {lines[i]}\n" for i in range(start, end))
