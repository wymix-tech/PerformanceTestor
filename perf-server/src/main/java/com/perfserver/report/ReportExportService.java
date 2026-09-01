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
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.Collections;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.Semaphore;
import java.util.regex.Pattern;

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
 * 当前实现探测系统中文字体候选路径，全部缺失时中文会渲染为空白方块，
 * 此时通过日志与 {@link ExportResult#getWarning()} 明确告警（接口不报错，用户难以自查）。
 *
 * <p>PDF 渲染是同步 CPU 密集操作（HTML 解析 + 版面计算），故限制并发并缓存产物：
 * 同一任务重复导出直接复用，避免多人重复导出把请求线程占满。
 */
@Service
public class ReportExportService {

    private static final Logger log = LoggerFactory.getLogger(ReportExportService.class);

    /** 导出格式常量 */
    public static final String FORMAT_HTML = "html";
    public static final String FORMAT_PDF = "pdf";
    public static final String FORMAT_DOC = "doc";

    /** PDF 渲染并发上限（单次渲染占用一核数百毫秒～数秒，放宽只会拖垮整体响应） */
    private static final int PDF_MAX_CONCURRENT = 2;
    /** PDF 缓存条目上限（每份 PDF 通常数百 KB，8 条约几 MB 量级） */
    private static final int PDF_CACHE_MAX_ENTRIES = 8;

    private final StressTaskService stressTaskService;
    /** PDF 渲染并发闸门：超限请求排队等待，而非并发抢占 CPU */
    private final Semaphore pdfPermit = new Semaphore(PDF_MAX_CONCURRENT);
    /**
     * PDF 产物缓存（LRU，按渲染 HTML 的 SHA-256 摘要为键）。
     * 用 HTML 而非 taskId 作键：任务结果更新后 HTML 随之变化，天然不会命中旧产物。
     */
    private final Map<String, byte[]> pdfCache = Collections.synchronizedMap(
            new LinkedHashMap<>(16, 0.75f, true) {
                @Override
                protected boolean removeEldestEntry(Map.Entry<String, byte[]> eldest) {
                    return size() > PDF_CACHE_MAX_ENTRIES;
                }
            });

    public ReportExportService(StressTaskService stressTaskService) {
        this.stressTaskService = stressTaskService;
    }

    /** 导出结果：字节内容 + 文件名 + Content-Type（+ 可选的降级告警） */
    public static class ExportResult {
        private final byte[] content;
        private final String filename;
        private final String contentType;
        private final String warning;

        public ExportResult(byte[] content, String filename, String contentType) {
            this(content, filename, contentType, null);
        }

        public ExportResult(byte[] content, String filename, String contentType, String warning) {
            this.content = content;
            this.filename = filename;
            this.contentType = contentType;
            this.warning = warning;
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

        /** 产物可用性告警（如缺少中文字体）；无问题返回 null */
        public String getWarning() {
            return warning;
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
            case FORMAT_PDF -> buildPdfCached(html, filename(task, "pdf"));
            case FORMAT_DOC -> new ExportResult(buildDoc(html), filename(task, "doc"),
                    "application/msword");
            default -> new ExportResult(html.getBytes(StandardCharsets.UTF_8), filename(task, "html"),
                    "text/html; charset=UTF-8");
        };
    }

    /**
     * HTML → PDF（先查缓存，未命中才渲染）。
     *
     * <p>同一任务的报告内容不变时直接复用上次产物：PDF 渲染是 CPU 密集操作，
     * 前端「预览/导出/重试」很容易在短时间内对同一任务发起多次请求。
     *
     * <p>缓存键剔除「生成时间」这一易变时间戳：报告正文顶部会渲染当前时间，
     * 若纳入键则跨秒后必然缓存未命中，缓存形同虚设（复用产物时保留首次渲染的时间戳，
     * 与「内容未变则复用」的语义一致）。
     */
    private ExportResult buildPdfCached(String html, String filename) {
        String key = sha256Hex(withoutGeneratedAt(html));
        byte[] cached = pdfCache.get(key);
        if (cached != null) {
            log.debug("report pdf cache hit, key={}, {} bytes", key, cached.length);
            return new ExportResult(cached, filename, "application/pdf", fontWarning());
        }
        byte[] pdf = renderPdf(html);
        pdfCache.put(key, pdf);
        return new ExportResult(pdf, filename, "application/pdf", fontWarning());
    }

    /**
     * HTML 内容的 SHA-256 摘要（缓存键）。
     *
     * <p>不用 {@code String.hashCode}：仅 32 位，长报告在导出量累积后碰撞概率不可忽略，
     * 且碰撞时会静默返回内容错误的 PDF，难以排查。SHA-256 为 256 位摘要，碰撞概率可忽略。
     */
    private static String sha256Hex(String html) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            return HexFormat.of().formatHex(md.digest(html.getBytes(StandardCharsets.UTF_8)));
        } catch (NoSuchAlgorithmException e) {
            // SHA-256 是 JCA 规范必备算法，理论不可达；退回 hashCode 仅损失防碰撞性，不影响可用性
            return "h-" + Integer.toHexString(html.hashCode());
        }
    }

    /** 缓存键用的易变时间戳：报告正文顶部的「生成时间：...」 */
    private static final Pattern GENERATED_AT =
            Pattern.compile("生成时间：\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2}");

    /**
     * 剔除 HTML 中的「生成时间」时间戳，用于构造稳定的缓存键。
     *
     * @param html 渲染后的报告 HTML
     * @return 时间戳被占位符替换的 HTML（仅用于摘要计算，不用于渲染）
     */
    private static String withoutGeneratedAt(String html) {
        return GENERATED_AT.matcher(html).replaceAll("生成时间：*");
    }

    /** HTML → PDF（openhtmltopdf + PDFBox，A4），并发受 {@link #pdfPermit} 限制 */
    private byte[] renderPdf(String html) {
        pdfPermit.acquireUninterruptibly();
        try (ByteArrayOutputStream out = new ByteArrayOutputStream()) {
            PdfRendererBuilder builder = new PdfRendererBuilder();
            // 中文处理：内置 Base-14 字体无中文字形，探测并注册系统中文字体
            java.io.File font = resolveFontFile();
            if (font != null) {
                builder.useFont(font, FONT_FAMILY, 400,
                        BaseRendererBuilder.FontStyle.NORMAL, true);
            } else {
                log.warn("未找到中文字体（候选: {}），PDF 中文将显示为空白方块；"
                        + "请在部署环境安装文泉驿/Noto CJK 字体，或调整 FONT_CANDIDATES",
                        String.join(", ", FONT_CANDIDATES));
            }
            builder.withHtmlContent(html, null);
            builder.toStream(out);
            builder.useFastMode();
            builder.run();
            return out.toByteArray();
        } catch (Exception e) {
            log.error("build report pdf failed", e);
            throw new IllegalStateException("PDF 生成失败：" + e.getMessage(), e);
        } finally {
            pdfPermit.release();
        }
    }

    /** 探测系统中文字体；全部候选均不存在时返回 null（中文将渲染为空白方块） */
    static java.io.File resolveFontFile() {
        for (String path : FONT_CANDIDATES) {
            java.io.File f = new java.io.File(path);
            if (f.exists() && f.isFile()) {
                return f;
            }
        }
        return null;
    }

    /** 无中文字体时的用户侧提示文案（随导出结果返回，便于前端/运维感知） */
    private static String fontWarning() {
        return resolveFontFile() == null
                ? "缺少中文字体，PDF 中的中文可能显示为空白方块" : null;
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
