"""AI 服务与 SSE 契约测试（阶段 4 步骤 3）：帧格式 / FastAPI 端点 / 流式事件序 /
角色检测 / 轮次上限收敛 / 知识沉淀联动。全部脚本化假 LLM，无网络。
"""

import json

from fastapi.testclient import TestClient

from agents.llm import ChatResult, LlmClient, ToolCall
from server.ai_service import (
    GENERAL,
    ROOT_CAUSE_ANALYST,
    STRESS_EXECUTOR,
    detect_role,
)
from server.app import create_app
from server.knowledge import KnowledgeService, SqliteKnowledgeStore
from tools.knowledge_tools import build_knowledge_tool
from tools.registry import build_registry


class FakeLlm(LlmClient):
    def __init__(self, script: list[ChatResult]) -> None:
        self.script = list(script)

    def chat(self, messages, tools):
        return self.script.pop(0)


class StreamingFakeLlm(FakeLlm):
    """带 chat_stream 的假客户端：正文按 delta 产出，StopIteration.value 回传 ChatResult。"""

    def chat_stream(self, messages, tools):
        result = self.script.pop(0)
        text = result.content or ""
        for i in range(0, len(text), 4):
            yield text[i:i + 4]
        return result


def _final(text) -> ChatResult:
    return ChatResult(content=text)


def _tool(name, args, i="1") -> ChatResult:
    return ChatResult(tool_calls=[ToolCall(id=f"call-{i}", name=name, arguments=args)])


def _client(llm, knowledge: KnowledgeService | None = None) -> TestClient:
    app = create_app(llm=llm, knowledge=knowledge or KnowledgeService(SqliteKnowledgeStore()))
    return TestClient(app)


def _parse_sse(raw: str) -> list[tuple[str, str]]:
    """解析 SSE 原始文本 → [(event, data)，data 为 JSON 解码后的 payload 字符串]。"""
    events = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        event, data = None, None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])  # data 侧 JSON 编码 → 解码得 payload 字符串
        assert event and data is not None
        events.append((event, data))
    return events


# ===== 角色检测（对齐 PromptBuilder.detectRole 分档顺序） =====


def test_detect_role():
    assert detect_role("一键压测并分析整个链路") == STRESS_EXECUTOR, "显式闭环最高优先"
    assert detect_role("给我生成修复提示词") == "FIX_ADVISOR", "强修复优先于归因"
    assert detect_role("P95 为什么这么高") == ROOT_CAUSE_ANALYST
    assert detect_role("压测 100 并发看 qps") == STRESS_EXECUTOR
    assert detect_role("帮我优化这个接口") == "FIX_ADVISOR", "弱修复意图"
    assert detect_role("你好") == GENERAL
    assert detect_role("") == GENERAL


# ===== SSE 帧格式 =====


def test_sse_frame_format():
    from server import sse

    frame = sse.token("你好\n第二行")
    assert frame == 'event: token\ndata: "你好\\n第二行"\n\n', "data 为 JSON 编码字符串（转义换行）"
    t, d = _parse_sse(frame)[0]
    assert t == "token" and d == "你好\n第二行"


def test_sse_done_payload_is_json_object():
    from server import sse

    _, data = _parse_sse(sse.done({"mode": "llm", "role": "GENERAL", "rounds": 2}))[0]
    assert json.loads(data) == {"mode": "llm", "role": "GENERAL", "rounds": 2}


# ===== 同步对话 /api/ai/chat =====


def test_chat_sync_with_tool_round():
    llm = FakeLlm([
        _tool("capacity_analyze",
              {"points": [{"concurrency": 10, "qps": 100, "p95Ms": 120},
                          {"concurrency": 20, "qps": 120, "p95Ms": 300}],
               "target_p95_ms": 150}, i="c1"),
        _final("根因：P95 超阈值，容量上限低"),
    ])
    client = _client(llm)
    resp = client.post("/api/ai/chat", json={"sessionId": "s1", "message": "分析 P95 为什么恶化"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0 and body["message"] == "success"
    data = body["data"]
    assert data["reply"] == "根因：P95 超阈值，容量上限低"
    assert data["mode"] == "llm" and data["rounds"] == 2
    assert data["role"] == ROOT_CAUSE_ANALYST
    assert data["toolCalls"][0]["name"] == "capacity_analyze"


def test_chat_max_rounds_converges():
    llm = FakeLlm([ChatResult(tool_calls=[ToolCall(id="x", name="slowlog_analyze",
                                                   arguments={"content": "y"})])] * 10)
    client = _client(llm)
    data = client.post("/api/ai/chat", json={"message": "排查慢 SQL"}).json()["data"]
    assert data["rounds"] == 10 and "阶段性结论" in data["reply"]


def test_chat_llm_without_tools_registry_empty():
    client = _client(FakeLlm([_final("纯对话回复")]))
    data = client.post("/api/ai/chat", json={"message": "你好"}).json()["data"]
    assert data["reply"] == "纯对话回复" and data["role"] == GENERAL


def test_chat_without_llm_falls_back_to_rule_mode():
    """LLM 未配置 → mode="rule" 规则兜底（对齐 Java，不 500），且不入知识库。"""
    knowledge = KnowledgeService(SqliteKnowledgeStore())
    app = create_app(llm=None, knowledge=knowledge)
    client = TestClient(app)
    data = client.post("/api/ai/chat", json={"message": "分析瓶颈"}).json()["data"]
    assert data["mode"] == "rule"
    assert "LLM 未配置" in data["reply"]
    resp = client.post("/api/ai/chat/stream", json={"message": "分析瓶颈"})
    assert "token" in resp.text and "LLM 未配置" in resp.text
    assert knowledge.search(None, None, 10) == [], "兜底文案不沉淀"


def test_session_clear():
    llm = FakeLlm([_final("回复 A"), _final("回复 B")])
    client = _client(llm)
    client.post("/api/ai/chat", json={"sessionId": "s9", "message": "你好"})
    assert client.delete("/api/ai/session/s9").json()["data"] == "cleared"
    # 清空后为全新会话：历史消息不残留（第一条 user 消息直接是"第二条"）
    data = client.post("/api/ai/chat", json={"sessionId": "s9", "message": "再见"}).json()["data"]
    assert data["reply"] == "回复 B"


# ===== 流式对话 /api/ai/chat/stream =====


def test_stream_event_sequence_and_contract():
    llm = StreamingFakeLlm([
        _tool("slowlog_analyze", {"content": "# Time: 1\n"}, i="t1"),
        _final("慢 SQL 已定位：订单联表缺索引"),
    ])
    client = _client(llm)
    resp = client.post("/api/ai/chat/stream",
                       json={"sessionId": "s2", "message": "排查慢 SQL", "appName": "demo"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)

    names = [e for e, _ in events]
    assert names[0] == "thought", "轮次心跳先行"
    assert names.count("tool") == 1 and names.count("observation") == 1
    assert names[-1] == "done"
    # token 在最终回答阶段且位于 tool/observation 之后
    token_idx = [i for i, e in enumerate(names) if e == "token"]
    assert token_idx and token_idx[0] > names.index("observation")

    # tool/observation 负载为 JSON 文本
    tool_payload = json.loads(dict(events)["tool"])
    assert tool_payload["name"] == "slowlog_analyze"
    # token 增量拼回完整正文
    text = "".join(d for e, d in events if e == "token")
    assert text == "慢 SQL 已定位：订单联表缺索引"
    # done 负载带 mode/role/rounds
    done_payload = json.loads(dict(events)["done"])
    assert done_payload["role"] == ROOT_CAUSE_ANALYST and done_payload["mode"] == "llm"


def test_stream_llm_exception_emits_error_event():
    class BoomLlm(LlmClient):
        def chat(self, messages, tools):
            raise RuntimeError("gateway down")

    client = _client(BoomLlm())
    resp = client.post("/api/ai/chat/stream", json={"message": "hi"})
    events = _parse_sse(resp.text)
    assert events[-1][0] == "error" and "gateway down" in events[-1][1]


# ===== 工具清单 / 知识库端点 =====


def test_tools_endpoint_lists_registry():
    client = _client(FakeLlm([_final("ok")]))
    items = client.get("/api/ai/tools").json()["data"]
    names = {i["name"] for i in items}
    assert "capacity_analyze" in names and "datagen_dry_run" in names
    assert all(i["description"] for i in items)


def test_knowledge_endpoints_roundtrip():
    knowledge = KnowledgeService(SqliteKnowledgeStore())
    client = _client(FakeLlm([_final("ok")]), knowledge)
    body = ("结论：订单接口 P95 恶化的根因是 perf_order 表 user_id 缺少索引，"
            "联表扫描行数达百万级，建议补充复合索引后重新执行容量评估验证收益。")
    knowledge.save("demo", None, body, "analysis")

    hits = client.get("/api/ai/knowledge", params={"query": "索引", "app": "demo"}).json()["data"]
    assert len(hits) == 1 and hits[0]["appName"] == "demo", "camelCase 契约"
    assert len(hits[0]["content"]) > 0 and "snippet" not in hits[0]

    item_id = hits[0]["id"]
    assert client.delete(f"/api/ai/knowledge/{item_id}").json()["data"] is True
    assert client.delete("/api/ai/knowledge/99999").json()["data"] is False


def test_chat_sediments_knowledge():
    knowledge = KnowledgeService(SqliteKnowledgeStore())
    registry = build_registry()
    registry["knowledge_search"] = build_knowledge_tool(knowledge)
    llm = FakeLlm([
        _tool("knowledge_search", {"query": "索引"}, i="k1"),
        _final("结论：该问题与历史经验一致，user_id 列缺少索引导致联表扫描行数过大，"
               "P95 随并发上升非线性恶化；建议建立复合索引后回归验证。"),
    ])
    client = _client(llm, knowledge)
    client.post("/api/ai/chat", json={"sessionId": "s3", "message": "分析订单接口",
                                      "appName": "demo"})
    hits = knowledge.search("user_id", "demo", 10)
    assert len(hits) == 1, "结论自动沉淀（tags 含角色与已用工具）"
    assert "root_cause_analyst" in hits[0].tags
    assert "knowledge_search" in hits[0].tags


def test_chat_short_reply_not_sedimented():
    knowledge = KnowledgeService(SqliteKnowledgeStore())
    client = _client(FakeLlm([_final("太短")]), knowledge)
    client.post("/api/ai/chat", json={"message": "你好"})
    assert knowledge.search(None, None, 10) == []


# ===== 工具注册表内的 knowledge_search =====


def test_knowledge_tool_handler():
    knowledge = KnowledgeService(SqliteKnowledgeStore())
    knowledge.save("demo", None,
                   "结论：订单联表查询的 perf_order 表 user_id 列缺少索引，全表扫描导致 P95 恶化，"
                   "建议补充复合索引并回归压测验证收益。", "analysis")
    tool = build_knowledge_tool(knowledge)
    out = tool.handler({"query": "索引", "app": "demo"})
    assert out["hits"][0]["snippet"], "回填上下文截断为 snippet"
    assert tool.handler({"query": "FullGC"})["hits"] == []
