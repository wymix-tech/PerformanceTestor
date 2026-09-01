package com.perfserver.ai;

/**
 * SSE 流式事件类型（对应前端 EventSource/fetch 读取的 event 名）。
 */
public enum AiStreamEventType {

    /** 模型正文增量（逐 token） */
    TOKEN("token"),
    /** 推理过程/轮次心跳：工具调用轮次之间推送，防止代理层因长时间无数据断开连接 */
    THOUGHT("thought"),
    /** 工具调用（名称 + 入参） */
    TOOL("tool"),
    /** 工具返回观察值（已截断） */
    OBSERVATION("observation"),
    /** 最终结论与会话元信息 */
    DONE("done"),
    /** 异常 */
    ERROR("error");

    private final String eventName;

    AiStreamEventType(String eventName) {
        this.eventName = eventName;
    }

    /** SSE event 名 */
    public String eventName() {
        return eventName;
    }
}
