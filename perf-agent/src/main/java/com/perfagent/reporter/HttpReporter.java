package com.perfagent.reporter;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

/**
 * HTTP 上报客户端：向平台服务器批量 POST JSON 数据。
 *
 * <p>使用 JDK 内置 java.net.http.HttpClient，无第三方依赖；
 * 发送失败返回 false 由 {@link MetricsReporter} 进行退避重试。
 */
public class HttpReporter {

    private final HttpClient client;

    public HttpReporter() {
        this.client = HttpClient.newBuilder()
                .connectTimeout(Duration.ofSeconds(3))
                .build();
    }

    /**
     * POST JSON 数据到平台上报接口。
     *
     * @param url  上报地址（如 http://platform:8080/api/agent/report）
     * @param body JSON body
     * @return true 表示 2xx 成功
     */
    public boolean postJson(String url, String body) {
        try {
            HttpRequest request = HttpRequest.newBuilder()
                    .uri(URI.create(url))
                    .timeout(Duration.ofSeconds(10))
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString(body))
                    .build();
            HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
            return response.statusCode() >= 200 && response.statusCode() < 300;
        } catch (Throwable t) {
            // 网络异常（平台不可达/超时）：返回 false 由上层退避重试
            return false;
        }
    }
}
