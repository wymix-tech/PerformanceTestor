package com.perfdemo.controller;

import jakarta.servlet.http.HttpServletResponse;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;

/**
 * 场景编排演示接口：登录 → 下单 → 订单详情三步串联。
 *
 * <p>供压测引擎的「步骤上下文引用」端到端自验使用（scripts/verify-scenario-context.sh）：
 * 下游步骤不声明 extract，直接引用上游响应体与响应头——
 * <ul>
 *   <li>{@code {{p1.result.data.token}}} → login 响应体的 token；</li>
 *   <li>{@code {{p1.result.data.userId}}} → login 响应体的 userId；</li>
 *   <li>{@code {{p1.header.X-Request-Id}}} → login 下发的响应头。</li>
 * </ul>
 * 下单与详情校验 {@code Authorization: Bearer tk-}，未携带则 401——便于用错误率
 * 直观判断上下文引用是否真的生效（引用失败会表现为 100% 401）。
 *
 * <p>刻意保持无状态、无数据库访问：自验只关心步骤间的取数链路，不引入慢 SQL 干扰指标。
 */
@RestController
@RequestMapping("/api/scenario")
public class DemoScenarioController {

    /** 登录：下发 token 与 userId，并带一个可被引用的响应头 */
    @PostMapping("/login")
    public Map<String, Object> login(HttpServletResponse response) {
        response.addHeader("X-Request-Id", "trace-" + UUID.randomUUID().toString().substring(0, 8));
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("token", "tk-" + UUID.randomUUID().toString().substring(0, 12));
        data.put("userId", "u-" + (1000 + (int) (Math.random() * 9000)));
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("code", 0);
        body.put("data", data);
        return body;
    }

    /**
     * 下单：校验上游 token，回传 orderId 供详情步骤引用。
     *
     * @param auth 由上游步骤上下文注入（{@code Bearer {{p1.result.data.token}}}）
     */
    @PostMapping("/order")
    public Map<String, Object> createOrder(@RequestHeader(value = "Authorization", required = false) String auth,
                                           HttpServletResponse response) {
        if (auth == null || !auth.startsWith("Bearer tk-")) {
            response.setStatus(HttpServletResponse.SC_UNAUTHORIZED);
            Map<String, Object> err = new LinkedHashMap<>();
            err.put("code", 401);
            err.put("message", "missing or invalid Authorization header: " + auth);
            return err;
        }
        response.addHeader("X-Request-Id", "trace-" + UUID.randomUUID().toString().substring(0, 8));
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("orderId", "o-" + UUID.randomUUID().toString().substring(0, 12));
        data.put("amount", 100 + (int) (Math.random() * 900));
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("code", 0);
        body.put("data", data);
        return body;
    }

    /**
     * 订单详情：路径中的 orderId 由下游引用上游响应体得到
     * （{@code /api/scenario/order/{{order.result.data.orderId}}}）。
     */
    @GetMapping("/order/{orderId}")
    public Map<String, Object> orderDetail(@PathVariable String orderId,
                                           @RequestHeader(value = "Authorization", required = false) String auth,
                                           HttpServletResponse response) {
        if (auth == null || !auth.startsWith("Bearer tk-")) {
            response.setStatus(HttpServletResponse.SC_UNAUTHORIZED);
            Map<String, Object> err = new LinkedHashMap<>();
            err.put("code", 401);
            err.put("message", "missing or invalid Authorization header: " + auth);
            return err;
        }
        Map<String, Object> data = new LinkedHashMap<>();
        data.put("orderId", orderId);
        data.put("status", "PAID");
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("code", 0);
        body.put("data", data);
        return body;
    }
}
