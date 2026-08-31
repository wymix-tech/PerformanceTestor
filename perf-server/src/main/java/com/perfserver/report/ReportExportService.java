package com.perfserver.report;

import com.fasterxml.jackson.databind.JsonNode;
import com.openhtmltopdf.outputdevice.helper.BaseRendererBuilder;
import com.openhtmltopdf.pdfboxout.PdfRendererBuilder;
import com.perfserver.entity.StressTask;
import com.perfserver.service.StressTaskService;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Service;

import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.Locale;

/**
 * 压测报告导出服务：将压测结果渲染为自包含 HTML，再按需转为 PDF 或 Word。
 *
 * <p>格式策略：
 * <ul>
 *   <li><b>html</b>：直接返回内联样式的 HTML（打印页预览 / 浏览器另存）。</li>
 *   <li><b>pdf</b>：openhtmltopdf + PDFBox 后端渲染（A4，与打印页同套 CSS）。</li>
 *   <li><b>doc</b>：Word 兼容 HTML——加 Office XML 命名空间与 MHTML 头，
 *       以 {@code .doc} 扩展名下载，Word 可正确解析表格排版；选择此方案是为了
 *       <b>零额外依赖</b>（不引入 Apache POI 及其庞大的 OOXML 依赖树）。</li>
 * </ul>
 *
 * <p>字体说明：PDF 渲染使用 Base-14 内置字体（Helvetica），无需外挂字体文件，
 * 但内置字体不含中文字形——生产部署需通过 {@code useFont()} 注册中文字体；
 * 当前实现显式设置 {@code defaultTextDirection} 并保留字体扩展点，见 {@link #buildPdf}。
 */
@Service
public class ReportExportService {

    private static final Logger log = LoggerFactory.getLogger(ReportExportService.class);

    /** 导出格式常量 */
    public static final String FORMAT_HTML = "html";
    public static final String FORMAT_PDF = "pdf";
    public static final String FORMAT_DOC = "doc";

    private final StressTaskService stressTaskService;

    public ReportExportService(StressTaskService stressTaskService) {
        this.stressTaskService = stressTaskService;
    }

    /** 导出结果：字节内容 + 文件名 + Content-Type */
    public static class ExportResult {
        private final byte[] content;
        private final String filename;
        private final String contentType;

        public ExportResult(byte[] content, String filename, String contentType) {
            this.content = content;
            this.filename = filename;
            this.contentType = contentType;
        }

        public byte[] getContent() {
            return content;
        }

        public String getFilename() {
            return filename;
        }

        public String getContentType() {
            return contentType;
        }
    }

    /**
     * 导出压测报告。
     *
     * @param taskId 压测任务 ID
     * @param format html / pdf / doc（大小写不敏感，未知按 html 处理）
     */
    public ExportResult export(long taskId, String format, boolean printMode) {
        StressTask task = stressTaskService.detail(taskId);
        if (task == null) {
            throw new IllegalArgumentException("压测任务不存在: " + taskId);
        }
        JsonNode result = stressTaskService.parseResultJson(task);
        String html = ReportHtmlRenderer.render(task, result, printMode);

        String fmt = format == null || format.isBlank()
                ? FORMAT_HTML : format.toLowerCase(Locale.ROOT);
        return switch (fmt) {
            case FORMAT_PDF -> new ExportResult(buildPdf(html), filename(task, "pdf"),
                    "application/pdf");
            case FORMAT_DOC -> new ExportResult(buildDoc(html), filename(task, "doc"),
                    "application/msword");
            default -> new ExportResult(html.getBytes(StandardCharsets.UTF_8), filename(task, "html"),
                    "text/html; charset=UTF-8");
        };
    }

    /** HTML → PDF（openhtmltopdf + PDFBox，A4） */
    private static byte[] buildPdf(String html) {
        try (ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            PdfRendererBuilder builder = new PdfRendererBuilder();
            // 中文处理：内置 Base-14 字体无中文字形，探测并注册系统中文字体
            for (String path : FONT_CANDIDATES) {
                java.io.File f = new java.io.File(path);
                if (f.exists()) {
                    builder.useFont(f, FONT_FAMILY, 400,
                            BaseRendererBuilder.FontStyle.NORMAL, true);
                    break;
                }
            }
            builder.withHtmlContent(html, null);
            builder.toStream(out);
            builder.useFastMode();
            builder.run();
            return out.toByteArray();
        } catch (Exception e) {
            log.error("build report pdf failed", e);
            throw new IllegalStateException("PDF 生成失败：" + e.getMessage(), e);
        }
    }

    /**
     * HTML → Word 兼容格式：加 MHTML 头 + Office XML 命名空间，以 .doc 下载。
     * Word 能正确解析其中的表格与内联样式，避免引入 Apache POI。
     */
    static byte[] buildDoc(String html) {
        String doc = "<html xmlns:o=\"urn:schemas-microsoft-com:office:office\" "
                + "xmlns:w=\"urn:schemas-microsoft-com:office:word\" "
                + "xmlns=\"http://www.w3.org/TR/REC-html40\">"
                + "<head><meta charset=\"UTF-8\">"
                + "<!--[if gte mso 9]><xml><w:WordDocument><w:View>Print</w:View>"
                + "<w:Zoom>100</w:Zoom></w:WordDocument></xml><![endif]-->"
                + "</head><body>" + stripOuterHtml(html) + "</body></html>";
        return doc.getBytes(StandardCharsets.UTF_8);
    }

    /** 剥离源 HTML 的 html/head/body 外层标签，仅保留内容以便嵌入 Office 头 */
    static String stripOuterHtml(String html) {
        String s = html;
        int bodyStart = s.indexOf("<body>");
        int bodyEnd = s.lastIndexOf("</body>");
        if (bodyStart >= 0 && bodyEnd > bodyStart) {
            s = s.substring(bodyStart + "<body>".length(), bodyEnd);
        }
        // 保留 <style>：Word 依赖内联样式还原排版
        int styleStart = s.indexOf("<style>");
        if (styleStart < 0 && html.contains("<style>")) {
            s = html.substring(html.indexOf("<style>"), html.indexOf("</style>") + 8) + s;
        }
        return s;
    }

    /** 下载文件名（任务名 + 任务 ID + 扩展名，去除文件名非法字符） */
    static String filename(StressTask task, String ext) {
        String name = task.getTaskName() == null || task.getTaskName().isBlank()
                ? "stress-report" : task.getTaskName();
        name = name.replaceAll("[\\\\/:*?\"<>|\\s]+", "_");
        return "压测报告_" + name + "_" + task.getId() + "." + ext;
    }

    /** 注册的字体族名（须与 CSS font-family 首选一致，见 ReportHtmlRenderer.css） */
    private static final String FONT_FAMILY = "Microsoft YaHei";

    /** 中文字体候选路径（PDF 渲染探测用；全不存在时退回内置字体，中文显示为空白方块） */
    private static final String[] FONT_CANDIDATES = {
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/arphic/uming.ttc",
            "/System/Library/Fonts/PingFang.ttc",
            "C:\\Windows\\Fonts\\msyh.ttc"
    };
}
