"""source 写路径单元测试（阶段 5 · 步骤 9）：纯函数语义逐条对齐 Java SourceManager。

契约来源：SourceManager.deriveRepoName / sanitizeDirName / buildAuthUrl、
SourceFileService.detectLanguage / isIgnored、DataCleanupService 空 appName 拒绝。
"""

import pytest

from server.knowledge import KnowledgeItem, SqliteKnowledgeStore
from server.source_index import detect_language, is_ignored
from server.source_write_service import (
    DataCleanupService,
    build_auth_url,
    derive_repo_name,
    sanitize_dir_name,
)

# ------------------------------------------------------------ 名称派生

@pytest.mark.parametrize("type_,payload,expected", [
    # git 取 URL 末段去 .git
    ("git", "https://git.example.com/group/perf-server.git/", "perf-server"),
    ("git", "https://git.example.com/group/demo.git", "demo"),
    ("git", "https://git.example.com/group/repo", "repo"),
    # local 取路径末段
    ("local", "/data/sources/my-app/", "my-app"),
    ("local", "/data/sources/my-app", "my-app"),
    # zip 取文件名去 .zip
    ("zip", "/tmp/uploads/uuid-demo.zip", "uuid-demo"),
    ("zip", "/tmp/uploads/uuid-bundle", "uuid-bundle"),
])
def test_derive_repo_name(type_, payload, expected):
    assert derive_repo_name(type_, payload) == expected


@pytest.mark.parametrize("name,expected", [
    ("perf-server", "perf-server"),
    ("我的 项目.v2", "我的_项目.v2"),      # 空格 → 下划线，中文保留
    ("a/b\\c:d", "a_b_c_d"),
    ("   ", "___"),   # 先替换后判空：空格 → "_"，仅空串才回退 repo
    ("", "repo"),
])
def test_sanitize_dir_name(name, expected):
    assert sanitize_dir_name(name) == expected


# ------------------------------------------------------------ git 凭据注入

def test_build_auth_url_combinations():
    # user+token → user:token@；仅 token → ":token@"（Java 代码语义：空 user 保留冒号，
    # git http 对空用户名+密码可用）；仅 user → user@；无凭据原样
    assert build_auth_url("http://g.io/a.git", "u", "t") == "http://u:t@g.io/a.git"
    assert build_auth_url("http://g.io/a.git", "", "t") == "http://:t@g.io/a.git"
    assert build_auth_url("http://g.io/a.git", "u", "") == "http://u@g.io/a.git"
    assert build_auth_url("http://g.io/a.git", " ", " ") == "http://g.io/a.git"
    assert build_auth_url("file:///repo.git", "u", "t") == "file:///repo.git"
    # user 中的特殊字符 URL 编码（对齐 URLEncoder）
    assert build_auth_url("http://g.io/a.git", "a b", "t") == "http://a+b:t@g.io/a.git"


# ------------------------------------------------------------ 语言识别与忽略规则

def test_detect_language_map_and_fallbacks():
    assert detect_language("A.java") == "java"
    assert detect_language("a.yml") == "yaml"        # yml → yaml 归一
    assert detect_language("a.gradle") == "groovy"
    assert detect_language("README.md") == "markdown"
    assert detect_language("Dockerfile") == "text"   # 无扩展名
    assert detect_language("a.txt") == "txt"         # 白名单内、映射表外 → 原样
    assert detect_language("a.py") == "binary"       # 白名单外 → binary（不索引）
    assert detect_language("A.JAVA") == "java"       # 大小写不敏感


@pytest.mark.parametrize("rel,expected", [
    (".git/config", True),
    (".idea/workspace.xml", True),
    ("A.class", True),
    ("target/A.class", True),
    ("src/main/target/x.java", True),
    ("web/node_modules/lib.js", True),
    ("web/dist/bundle.js", True),   # contains("/dist/") 需前导斜杠，顶层 dist/ 不忽略
    ("src/Main.java", False),
    ("src/.gitignore-tips.md", False),  # 仅前缀 ".git/" 命中，".gitignore" 不算
])
def test_is_ignored(rel, expected):
    assert is_ignored(rel) is expected


# ------------------------------------------------------------ 清理服务守卫与全局结论

def test_cleanup_rejects_blank_app_name():
    svc = DataCleanupService(None, None)
    with pytest.raises(ValueError, match="应用名（appName）不能为空，拒绝执行清理"):
        svc.cleanup_by_app_name("  ")


def test_knowledge_store_delete_by_app_keeps_global():
    store = SqliteKnowledgeStore()
    app_id = store.save(KnowledgeItem(app_name="demo", title="t", content="c" * 50,
                                      create_time="2026-11-16T10:00:00"))
    store.save(KnowledgeItem(app_name="", title="全局", content="g" * 50,
                             create_time="2026-11-16T10:00:01"))
    assert store.delete_by_app("demo") == 1      # 仅本应用
    assert store.delete_by_app("demo") == 0      # 幂等
    ids = [i.id for i in store.recent("", 10)]
    assert ids and all(i != app_id for i in ids), "本应用结论已删"
    assert len(store.recent("", 10)) == 1, "全局结论保留"
    assert store.delete_global() == 1
    assert store.recent("", 10) == []
