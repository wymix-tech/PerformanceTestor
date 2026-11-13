"""知识库服务等价测试（阶段 4 步骤 4）：沉淀阈值 / bigram Jaccard 去重 / 检索语义
（无命中不回落、异常才降级）/ SQLite 存储契约。
"""

from server.knowledge import (
    DEDUP_SIMILARITY,
    MIN_CONTENT_CHARS,
    KnowledgeService,
    SqliteKnowledgeStore,
    similarity,
)

_A = "结论 A：容量拐点出现在并发 40 附近，QPS 上限约 6000，超过后 P95 快速恶化，建议在 35 并发处设置限流保护并持续观测。"
_B = "结论 B：慢 SQL 集中在订单列表联表查询，单次扫描行数达百万级，perf_order 表 user_id 缺少索引，需要补充复合索引。"
_DEDUP_BASE = ("结论：perf_order 表 user_id 列缺少索引导致慢查询，联表扫描行数达百万级，"
               "P95 随并发上升非线性恶化；建议建立复合索引后重新压测验证收益并回归容量评估。")
_GLOBAL = "全局结论：FullGC 频繁时优先检查大对象缓存与元空间配置，堆外内存泄漏同样会诱发 GC 抖动，压测中表现为吞吐突降。"


def _svc() -> KnowledgeService:
    return KnowledgeService(SqliteKnowledgeStore())


def _seed(svc: KnowledgeService, app: str, content: str) -> bool:
    return svc.save(app, None, content, "analysis")


# ===== 相似度（对齐 Java bigram Jaccard） =====


def test_similarity_identical():
    assert similarity(_DEDUP_BASE, _DEDUP_BASE) == 1.0


def test_similarity_minor_edits_high():
    dup = _DEDUP_BASE.replace("验证收益", "验证收益！") + "……"
    assert similarity(_DEDUP_BASE, dup) >= DEDUP_SIMILARITY, "长文本小幅增删不敏感"


def test_similarity_unrelated_low():
    assert similarity(_A, _GLOBAL) < 0.3


def test_similarity_empty():
    assert similarity("", "x") == 0.0


# ===== 沉淀 =====


def test_save_too_short_rejected():
    svc = _svc()
    assert svc.save("demo", "t", "太短", None) is False
    assert svc.save("demo", "t", "x" * (MIN_CONTENT_CHARS - 1), None) is False


def test_save_ok_and_recent():
    svc = _svc()
    assert _seed(svc, "demo", _B) is True
    recent = svc.search(None, None, 10)
    assert len(recent) == 1
    assert recent[0].app_name == "demo"
    assert recent[0].source == "ai"
    assert "结论" in recent[0].title, "标题兜底取首行"


def test_save_dedup_skips_near_duplicate():
    svc = _svc()
    assert _seed(svc, "demo", _DEDUP_BASE) is True
    dup = _DEDUP_BASE.replace("验证收益", "验证收益！") + "……"
    assert _seed(svc, "demo", dup) is False, "近似重复被去重跳过"
    assert len(svc.search(None, None, 10)) == 1


def test_save_dedup_scoped_by_app():
    svc = _svc()
    assert _seed(svc, "demo", _DEDUP_BASE) is True
    assert _seed(svc, "other", _DEDUP_BASE + "（other 应用视角）") is True, "不同应用不去重"


# ===== 检索语义 =====


def test_search_blank_query_returns_recent():
    svc = _svc()
    _seed(svc, "demo", _A)
    _seed(svc, "demo", _B)
    hits = svc.search("", "demo", 10)
    assert len(hits) == 2, "空白 query = 列最近（语义不同于检索无命中）"


def test_search_no_hit_returns_empty_not_recent():
    svc = _svc()
    _seed(svc, "demo", _A)
    assert svc.search("FullGC", "demo", 10) == [], "无命中如实返回空，不做 recent 回落"


def test_search_hit_ranked():
    svc = _svc()
    _seed(svc, "demo", _A)
    _seed(svc, "demo", _GLOBAL + "本条结论针对 demo 应用压测期间 FullGC 观测得出。")
    hits = svc.search("FullGC", "demo", 10)
    assert len(hits) == 1
    assert hits[0].score >= 2.0, "按命中次数评分"


def test_search_app_filter_includes_global():
    svc = _svc()
    _seed(svc, "demo", _B)
    _seed(svc, "", _GLOBAL)
    hits = svc.search("FullGC", "demo", 10)
    assert len(hits) == 1 and hits[0].app_name == "", "非空 app 含全局结论"


def test_search_exception_falls_back_to_recent():
    class BrokenStore(SqliteKnowledgeStore):
        def search(self, query, app_name, limit):
            raise RuntimeError("fulltext unavailable")

    svc = KnowledgeService(BrokenStore())
    _seed(svc, "demo", _B)
    assert len(svc.search("anything", "demo", 10)) == 1, "检索异常才降级到最近结论"


def test_delete():
    svc = _svc()
    _seed(svc, "demo", _B)
    item = svc.search(None, None, 10)[0]
    assert svc.delete(item.id) is True
