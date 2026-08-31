package com.perfserver.controller;

import com.perfserver.report.ReportExportService;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;

/**
 * 压测报告导出 API：按任务导出 HTML / PDF / Word。
 *
 * <p>文件下载接口直接返回 {@link ResponseEntity}，不走 {@code ApiResponse} 统一封装
 * （JSON 包壳会破坏文件字节流）。鉴权沿用 JWT 拦截器（/api/** 默认需登录）。
 */
@RestController
@RequestMapping("/api/reports")
public class ReportExportController {

    /** 产物可用性告警响应头（如缺少中文字体）；值做 URL 编码以保证 HTTP 头安全 */
    private static final String WARNING_HEADER = "X-Report-Warning";

    private final ReportExportService reportExportService;

    public ReportExportController(ReportExportService reportExportService) {
        this.reportExportService = reportExportService;
    }

    /**
     * 导出压测报告。
     *
     * @param id       压测任务 ID
     * @param format   html / pdf / doc
     * @param printMode true = 追加浏览器打印脚本（打印页预览用）
     */
    @GetMapping("/export")
    public ResponseEntity<byte[]> export(@RequestParam("stressTaskId") long id,
                                         @RequestParam(defaultValue = "pdf") String format,
                                         @RequestParam(defaultValue = "false") boolean printMode) {
        ReportExportService.ExportResult result = reportExportService.export(id, format, printMode);
        // RFC 5987：中文文件名需 encoding 转义，同时保留 filename* 供非 ASCII 场景
        String encoded = URLEncoder.encode(result.getFilename(), StandardCharsets.UTF_8)
                .replace("+", "%20");
        ResponseEntity.BodyBuilder ok = ResponseEntity.ok()
                .contentType(MediaType.parseMediaType(result.getContentType()))
                .header(HttpHeaders.CONTENT_DISPOSITION,
                        "attachment; filename=\"" + encoded + "\"; filename*=UTF-8''" + encoded);
        // 导出本身不报错但产物有缺陷时（如缺中文字体导致中文空白）在此告警，便于运维自查
        if (result.getWarning() != null) {
            ok = ok.header(WARNING_HEADER, URLEncoder.encode(result.getWarning(), StandardCharsets.UTF_8));
        }
        return ok.body(result.getContent());
    }
}
