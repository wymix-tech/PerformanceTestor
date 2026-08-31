package com.perfserver.report;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.perfserver.entity.StressTask;
import com.perfserver.service.StressTaskService;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.mockito.Mock;
import org.mockito.junit.jupiter.MockitoExtension;
import org.mockito.junit.jupiter.MockitoSettings;
import org.mockito.quality.Strictness;

import java.nio.charset.StandardCharsets;
import java.time.LocalDateTime;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotSame;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.Mockito.when;

/**
 * 报告导出单测：HTML 渲染（字段转义/空结果兜底/容量与置信区间）、
 * PDF 生成、Word 兼容封装、文件名清洗，以及任务不存在的错误处理。
 */
@ExtendWith(MockitoExtension.class)
@MockitoSettings(strictness = Strictness.LENIENT)
class ReportExportServiceTest {

    @Mock
    private StressTaskService stressTaskService;

    private ReportExportService service;

    /** 含分档、容量评估（含拟合模型 + 置信区间）与扩容建议的完整结果 JSON */
    private static final String RESULT_JSON = """
            {
              "taskId": 1, "engineType": "http", "targetUrl": "http://127.0.0.1:8081/api/user/list",
              "concurrency": 20, "durationSec": 60, "warmupSec": 10, "rampUpSec": 5,
              "totalRequests": 18432, "errorCount": 12, "errorRate": 0.065,
              "qps": 307.2, "p50Ms": 45, "p95Ms": 135, "p99Ms": 210, "maxMs": 890, "avgMs": 62,
              "stagePoints": [
                {"concurrency":5,"qps":120.5,"p50Ms":20,"p95Ms":55,"p99Ms":80,"errorRate":0.01,"totalRequests":3000},
                {"concurrency":20,"qps":307.2,"p50Ms":45,"p95Ms":135,"p99Ms":210,"errorRate":0.065,"totalRequests":8000}
              ],
              "capacity": {
                "targetP95Ms": 200, "maxConcurrency": 26, "maxQps": 173, "extrapolated": false,
                "r2": 0.9987, "rtKneeConcurrency": 22.5,
                "model": {"type":"quadratic","a":-0.5,"b":20.0,"k":0.0,"r2":0.9987,
                          "vertexConcurrency":20.0,"vertexQps":200.0,"points":6,
                          "formula":"qps = -0.5000·c² + 20.0000·c + 0.0000"},
                "confidenceInterval": [
                  {"concurrency":5,"qps":120.5,"qpsLower":113.2,"qpsUpper":127.8,
                   "p95Ms":55,"p95Lower":52.1,"p95Upper":57.9}
                ],
                "advices": [
                  {"appName":"perf-demo","type":"scale","advice":"扩容至 2 实例","reason":"P95 已超阈值"}
                ]
              }
            }
            """;

    @BeforeEach
    void setUp() {
        service = new ReportExportService(stressTaskService);
    }

    private static StressTask task(String name, String status) {
        StressTask t = new StressTask();
        t.setId(1L);
        t.setTaskName(name);
        t.setEngineType("http");
        t.setTargetUrl("http://127.0.0.1:8081/api/user/list");
        t.setMethod("GET");
        t.setAppName("perf-demo");
        t.setAppVersion("v1.2.0");
        t.setConcurrency(20);
        t.setDurationSec(60);
        t.setQpsLimit(0);
        t.setStatus(status);
        t.setCreateTime(LocalDateTime.of(2026, 8, 31, 10, 0, 0));
        t.setFinishTime(LocalDateTime.of(2026, 8, 31, 10, 1, 5));
        return t;
    }

    private void stubTask(String name, String status, String resultJson) {
        StressTask t = task(name, status);
        t.setResultJson(resultJson);
        when(stressTaskService.detail(anyLong())).thenReturn(t);
        when(stressTaskService.parseResultJson(t)).thenReturn(
                resultJson == null ? null : readTree(resultJson));
    }

    private static JsonNode readTree(String json) {
        try {
            return new ObjectMapper().readTree(json);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    // ==================== HTML 渲染 ====================

    @Test
    void htmlContainsAllSections() {
        stubTask("用户列表压测", "finished", RESULT_JSON);
        String html = service.export(1, "html", false).getContent() != null
                ? new String(service.export(1, "html", false).getContent(), StandardCharsets.UTF_8) : "";

        // 头部与元信息
        assertTrue(html.contains("性能压测报告"), "应有报告标题");
        assertTrue(html.contains("用户列表压测"), "应有任务名");
        assertTrue(html.contains("http://127.0.0.1:8081/api/user/list"), "应有目标地址");
        assertTrue(html.contains("内置 HTTP 引擎"), "引擎类型应中文映射");
        assertTrue(html.contains("已完成"), "状态应中文映射");
        assertTrue(html.contains("perf-demo"), "应有归属应用");
        assertTrue(html.contains("v1.2.0"), "应有版本快照");

        // 核心指标与分布
        assertTrue(html.contains("核心指标"));
        assertTrue(html.contains("307.2"), "应含 QPS");
        assertTrue(html.contains("135"), "应含 P95");
        assertTrue(html.contains("响应时间分布"));

        // 分档
        assertTrue(html.contains("分档压测数据"));
        assertTrue(html.contains("120.5"), "应含分档 QPS");

        // 容量（含本轮科学化字段）
        assertTrue(html.contains("容量评估"));
        assertTrue(html.contains("外推估算") == false, "非外推应无该标记");
        assertTrue(html.contains("拟合模型"), "应含拟合模型区块");
        assertTrue(html.contains("qps = -0.5000·c² + 20.0000·c + 0.0000"), "应含拟合公式");
        assertTrue(html.contains("0.9987"), "应含 R²");
        assertTrue(html.contains("20.0"), "应含理论拐点并发");
        assertTrue(html.contains("22.5"), "应含 RT 上翘拐点");
        assertTrue(html.contains("分档 95% 置信区间"), "应含置信区间表");
        assertTrue(html.contains("113.2"), "应含 CI 下界");

        // 建议
        assertTrue(html.contains("扩容与配置建议"));
        assertTrue(html.contains("扩容至 2 实例"));
    }

    @Test
    void htmlEscapesHtmlSpecialCharsInTaskName() {
        stubTask("<script>alert('x')</script>", "finished", RESULT_JSON);
        String html = new String(service.export(1, "html", false).getContent(), StandardCharsets.UTF_8);
        assertFalse(html.contains("<script>alert"), "任务名必须转义，防破坏文档结构");
        assertTrue(html.contains("&lt;script&gt;"));
    }

    @Test
    void htmlHandlesMissingResultGracefully() {
        stubTask("进行中的任务", "running", null);
        String html = new String(service.export(1, "html", false).getContent(), StandardCharsets.UTF_8);
        assertTrue(html.contains("尚未产生结果"), "无结果时应给出占位提示而非 NPE");
        assertTrue(html.contains("执行中"));
    }

    @Test
    void htmlFallsBackToDashForMissingNumericFields() {
        stubTask("缺字段任务", "finished", "{\"qps\":100.0}");
        String html = new String(service.export(1, "html", false).getContent(), StandardCharsets.UTF_8);
        assertTrue(html.contains("-"), "缺失数值字段应渲染为占位符");
    }

    @Test
    void htmlWithoutCapacitySkipsCapacitySection() {
        stubTask("无容量任务", "finished", "{\"qps\":10.0,\"p95Ms\":20}");
        String html = new String(service.export(1, "html", false).getContent(), StandardCharsets.UTF_8);
        assertFalse(html.contains("容量评估"), "无容量结果时不渲染该区块");
    }

    // ==================== PDF ====================

    @Test
    void pdfStartsWithPdfMagicNumber() {
        stubTask("用户列表压测", "finished", RESULT_JSON);
        ReportExportService.ExportResult r = service.export(1, "pdf", false);
        byte[] pdf = r.getContent();
        assertTrue(pdf.length > 1000, "PDF 应有实际内容，实际 " + pdf.length + " 字节");
        assertEquals('%', (char) pdf[0]);
        assertEquals('P', (char) pdf[1]);
        assertEquals('D', (char) pdf[2]);
        assertEquals('F', (char) pdf[3]);
        assertEquals("application/pdf", r.getContentType());
        assertTrue(r.getFilename().endsWith(".pdf"));
    }

    @Test
    void pdfHandlesTaskWithoutResult() {
        stubTask("空结果", "failed", null);
        byte[] pdf = service.export(1, "pdf", false).getContent();
        assertTrue(pdf.length > 1000, "空结果也应能生成 PDF");
        assertEquals('%', (char) pdf[0]);
    }

    /** 同内容重复导出复用缓存：PDF 渲染是 CPU 密集操作，不应每次请求都重渲染 */
    @Test
    void pdfReusesCachedBytesForSameContent() {
        stubTask("用户列表压测", "finished", RESULT_JSON);

        byte[] first = service.export(1, "pdf", false).getContent();
        byte[] second = service.export(1, "pdf", false).getContent();

        assertSame(first, second, "内容未变时应直接复用上次产物");
    }

    /** 缓存以渲染内容为键：任务内容变化后不得命中旧产物 */
    @Test
    void pdfCacheIsKeyedByRenderedContent() {
        stubTask("用户列表压测", "finished", RESULT_JSON);
        byte[] first = service.export(1, "pdf", false).getContent();

        stubTask("订单创建压测", "finished", RESULT_JSON);
        byte[] second = service.export(1, "pdf", false).getContent();

        assertNotSame(first, second, "内容变化后必须重新渲染");
    }

    /**
     * 无中文字体时中文会渲染成空白方块，而接口不报错、用户难以察觉，
     * 因此必须随导出结果给出明确告警。
     */
    @Test
    void pdfWarnsWhenChineseFontMissing() {
        stubTask("用户列表压测", "finished", RESULT_JSON);

        String warning = service.export(1, "pdf", false).getWarning();

        assertEquals(ReportExportService.resolveFontFile() == null, warning != null,
                "告警应与字体探测结果一致（无字体时必须告警），实际 warning=" + warning);
    }

    // ==================== Word ====================

    @Test
    void docHasOfficeNamespaceAndPreservesContent() {
        stubTask("用户列表压测", "finished", RESULT_JSON);
        ReportExportService.ExportResult r = service.export(1, "doc", false);
        String doc = new String(r.getContent(), StandardCharsets.UTF_8);
        assertTrue(doc.contains("urn:schemas-microsoft-com:office:word"), "应含 Office XML 命名空间");
        assertTrue(doc.contains("PerformanceTestor") || doc.contains("性能压测报告"),
                "应保留报告内容");
        assertFalse(doc.contains("<!DOCTYPE html>"), "外层 DOCTYPE 应被剥离");
        assertEquals("application/msword", r.getContentType());
        assertTrue(r.getFilename().endsWith(".doc"));
    }

    @Test
    void stripOuterHtmlKeepsStyleBlock() {
        String src = "<html><head><style>a{color:red}</style></head><body><p>x</p></body></html>";
        String out = ReportExportService.stripOuterHtml(src);
        assertTrue(out.contains("a{color:red}"), "Word 依赖内联样式还原排版，style 必须保留");
        assertTrue(out.contains("<p>x</p>"));
        assertFalse(out.contains("<body>"));
    }

    // ==================== 文件名与错误处理 ====================

    @Test
    void filenameSanitizesUnsafeCharacters() {
        StressTask t = task("a/b:c*d?e\"f<g>h|i j", "finished");
        String name = ReportExportService.filename(t, "pdf");
        assertFalse(name.matches(".*[/\\\\:*?\"<>| ].*"), "文件名非法字符应被替换: " + name);
        assertTrue(name.endsWith("_1.pdf"), "应含任务 ID 与扩展名: " + name);
    }

    @Test
    void filenameFallsBackWhenTaskNameBlank() {
        StressTask t = task("   ", "finished");
        assertTrue(ReportExportService.filename(t, "html").contains("stress-report"));
    }

    @Test
    void unknownFormatFallsBackToHtml() {
        stubTask("任务", "finished", RESULT_JSON);
        ReportExportService.ExportResult r = service.export(1, "xlsx", false);
        assertTrue(new String(r.getContent(), StandardCharsets.UTF_8).contains("性能压测报告"));
        assertTrue(r.getFilename().endsWith(".html"));
    }

    @Test
    void nullFormatFallsBackToHtml() {
        stubTask("任务", "finished", RESULT_JSON);
        assertTrue(service.export(1, null, false).getFilename().endsWith(".html"));
    }

    @Test
    void missingTaskRaisesClearError() {
        when(stressTaskService.detail(anyLong())).thenReturn(null);
        IllegalArgumentException e = assertThrows(IllegalArgumentException.class,
                () -> service.export(999, "pdf", false));
        assertTrue(e.getMessage().contains("压测任务不存在"), e.getMessage());
    }

    @Test
    void numberFormattingUsesThousandsSeparator() {
        stubTask("任务", "finished", "{\"totalRequests\":18432,\"qps\":307.5}");
        String html = new String(service.export(1, "html", false).getContent(), StandardCharsets.UTF_8);
        assertTrue(html.contains("18,432"), "大数字应有千分位: " + html);
        assertTrue(html.contains("307.5"), "QPS 保留一位小数");
    }
}
