package com.perfserver.ai;

/**
 * SSE 流式事件：{@code event: <type>} + {@code data: <payload 的 JSON 字符串>}。
 *
 * <p>payload 统一为字符串：token/thought 为纯文本，tool/observation/done 为 JSON 文本，
 * 由 {@link AiChatStreamController} 统一 JSON 编码后下发（避免正文换行破坏 SSE 帧格式）。
 */
public record AiStreamEvent(AiStreamEventType type, String payload) {

    public static AiStreamEvent token(String text) {
        return new AiStreamEvent(AiStreamEventType.TOKEN, text);
    }

    public static AiStreamEvent thought(String text) {
        return new AiStreamEvent(AiStreamEventType.THOUGHT, text);
    }

    public static AiStreamEvent tool(String json) {
        return new AiStreamEvent(AiStreamEventType.TOOL, json);
    }

    public static AiStreamEvent observation(String json) {
        return new AiStreamEvent(AiStreamEventType.OBSERVATION, json);
    }

    public static AiStreamEvent done(String json) {
        return new AiStreamEvent(AiStreamEventType.DONE, json);
    }

    public static AiStreamEvent error(String message) {
        return new AiStreamEvent(AiStreamEventType.ERROR, message);
    }
}
