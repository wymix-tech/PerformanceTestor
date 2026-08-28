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
 * 发送结果由 {@link MetricsReporter} 处理：成功/失败退避重试/项目不存在则停报。
 */
public class HttpReporter {

    /** 上报结果：sendResult 仅为 true/false 判断兼容，body 供识别"项目不存在" */
    public record PostResult(boolean ok, int statusCode, String body) {
    }

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
     * @return 结果（ok = 2xx；body 用于识别业务拒绝原因）
     */
    public PostResult postJson(String url, String body) {
        try {
            HttpRequest request = HttpRequest.newBuilder()
                    .uri(URI.create(url))
                    .timeout(Duration.ofSeconds(10))
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString(body))
                    .build();
            HttpResponse<String> response = client.send(request, HttpResponse.BodyHandlers.ofString());
            boolean ok = response.statusCode() >= 200 && response.statusCode() < 300;
            return new PostResult(ok, response.statusCode(), response.body() == null ? "" : response.body());
        } catch (Throwable t) {
            // 网络异常（平台不可达/超时）：ok=false 由上层退避重试
            return new PostResult(false, 0, "");
        }
    }
}
