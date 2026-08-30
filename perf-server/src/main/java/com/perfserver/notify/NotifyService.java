package com.perfserver.notify;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 告警通知服务：支持自定义 webhook 与钉钉/飞书机器人消息格式（超阈值回归告警）。
 *
 * <p>格式自适应：URL 含 feishu/open.feishu 用飞书文本格式、含 dingtalk/oapi.dingtalk
 * 用钉钉文本格式，其余按自定义 POST（title/content 字段）发送。发送失败仅记日志不重试
 * （告警尽力而为，不阻断压测流程）。
 */
@Service
public class NotifyService {

    private static final Logger log = LoggerFactory.getLogger(NotifyService.class);

    private final ObjectMapper objectMapper = new ObjectMapper();
    private final HttpClient http = HttpClient.newBuilder()
            .connectTimeout(Duration.ofSeconds(5))
            .build();

    /** 发送文本告警（webhook 为空时静默跳过） */
    public void send(String webhookUrl, String title, String text) {
        if (webhookUrl == null || webhookUrl.isBlank()) {
            return;
        }
        try {
            Map<String, Object> body = buildBody(webhookUrl, title, text);
            HttpRequest request = HttpRequest.newBuilder(URI.create(webhookUrl.trim()))
                    .header("Content-Type", "application/json;charset=UTF-8")
                    .timeout(Duration.ofSeconds(10))
                    .POST(HttpRequest.BodyPublishers.ofString(objectMapper.writeValueAsString(body)))
                    .build();
            HttpResponse<String> resp = http.send(request, HttpResponse.BodyHandlers.ofString());
            if (resp.statusCode() / 100 != 2) {
                log.warn("notify webhook non-2xx: url={}, status={}, body={}",
                        maskUrl(webhookUrl), resp.statusCode(), resp.body());
            }
        } catch (Exception e) {
            log.warn("notify webhook failed: url={}, error={}", maskUrl(webhookUrl), e.getMessage());
        }
    }

    /** 按 webhook 平台构造消息 body */
    private Map<String, Object> buildBody(String url, String title, String text) {
        String lower = url.toLowerCase();
        Map<String, Object> body = new LinkedHashMap<>();
        if (lower.contains("feishu") || lower.contains("open.feishu")) {
            body.put("msg_type", "text");
            body.put("content", Map.of("text", title + "\n" + text));
        } else if (lower.contains("dingtalk") || lower.contains("oapi.dingtalk")) {
            body.put("msgtype", "text");
            body.put("text", Map.of("content", title + "\n" + text));
        } else {
            // 自定义 webhook：title + content 平铺字段
            body.put("title", title);
            body.put("content", text);
        }
        return body;
    }

    /** 日志脱敏：隐藏 token 参数 */
    private static String maskUrl(String url) {
        int q = url.indexOf('?');
        return q > 0 ? url.substring(0, q) + "?***" : url;
    }
}
