package com.perfserver.stress;

import org.junit.jupiter.api.Test;
import org.springframework.test.util.ReflectionTestUtils;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/**
 * JMeter 引擎纯逻辑单元测试（不依赖 JMeter 安装）：JMX 生成与 JTL CSV 解析。
 */
class JmeterStressEngineTest {

    private JmeterStressEngine newEngine() {
        JmeterStressEngine engine = new JmeterStressEngine();
        // 注入可用性缓存，绕过安装检测（纯逻辑测试）
        ReflectionTestUtils.setField(engine, "availableCache", Boolean.TRUE);
        return engine;
    }

    @Test
    void buildJmxContainsThreadGroupAndSampler() {
        JmeterStressEngine engine = newEngine();
        StressTaskRequest req = StressTaskRequest.single("http://127.0.0.1:8081/api/user/list?limit=200", 20, 30);
        req.setMethod("GET");
        req.setQpsLimit(300);
        String jmx = engine.buildJmx(req, stage(req));

        assertTrue(jmx.contains("<stringProp name=\"ThreadGroup.num_threads\">20</stringProp>"));
        assertTrue(jmx.contains("<stringProp name=\"ThreadGroup.duration\">30</stringProp>"));
        assertTrue(jmx.contains("<stringProp name=\"HTTPSampler.domain\">127.0.0.1</stringProp>"));
        assertTrue(jmx.contains("<stringProp name=\"HTTPSampler.path\">/api/user/list?limit=200</stringProp>"));
        assertTrue(jmx.contains("<stringProp name=\"HTTPSampler.method\">GET</stringProp>"));
        // QPS 300 → 每分钟吞吐 18000
        assertTrue(jmx.contains("<stringProp name=\"throughput\">18000</stringProp>"));
        assertTrue(jmx.contains("ConstantThroughputTimer"));
    }

    @Test
    void buildJmxEscapesXmlSpecials() {
        JmeterStressEngine engine = newEngine();
        StressTaskRequest req = StressTaskRequest.single("http://h/api?a=1&b=2", 1, 1);
        String jmx = engine.buildJmx(req, stage(req));
        // & 必须转义，否则 JMX 非法
        assertTrue(jmx.contains("/api?a=1&amp;b=2"));
        assertFalse(jmx.contains("/api?a=1&b=2"));
    }

    @Test
    void buildJmxRawBodyForPost() {
        JmeterStressEngine engine = newEngine();
        StressTaskRequest req = StressTaskRequest.single("http://h/api", 2, 5);
        req.setMethod("POST");
        req.setBody("{\"k\": \"v\"}");
        String jmx = engine.buildJmx(req, stage(req));
        assertTrue(jmx.contains("HTTPSampler.postBodyRaw\">true"));
        assertTrue(jmx.contains("{&quot;k&quot;: &quot;v&quot;}"));
    }

    @Test
    void splitCsvHandlesQuotedFields() {
        List<String> cols = JmeterStressEngine.splitCsv("a,\"b,c\",,\"d\"\"e\",f");
        assertEquals(List.of("a", "b,c", "", "d\"e", "f"), cols);
    }

    @Test
    void parseJtlComputesStatsAndSkipsWarmup() throws Exception {
        JmeterStressEngine engine = newEngine();
        Path jtl = Files.createTempFile("result", ".jtl");
        long formalStart = 1_000_000;
        try {
            String csv = "timeStamp,elapsed,label,responseCode,responseMessage,threadName,dataType,success,failureMessage,bytes,sentBytes,grpThreads,allThreads,URL,Latency,IdleTime,Connect\n"
                    // warmup 期（不计入统计）
                    + (formalStart - 1000) + ",50,req,200,OK,t1,text,true,,100,50,1,1,url,40,0,5\n"
                    + (formalStart - 500) + ",60,req,200,OK,t1,text,true,,100,50,1,1,url,50,0,5\n"
                    // 正式期：3 成功 1 失败
                    + formalStart + ",10,req,200,OK,t1,text,true,,100,50,1,1,url,8,0,2\n"
                    + (formalStart + 100) + ",20,req,200,OK,t1,text,true,,100,50,1,1,url,15,0,2\n"
                    + (formalStart + 200) + ",30,req,500,Server Error,t1,text,false,boom,100,50,1,1,url,25,0,2\n"
                    + (formalStart + 1200) + ",40,req,200,OK,t1,text,true,,100,50,1,1,url,35,0,2\n";
            Files.writeString(jtl, csv);
            StressTaskResult result = new StressTaskResult();
            // 直接调用内部解析（经 runSingle 太重，通过反射调用 parseJtl）
            Object outcome = ReflectionTestUtils.invokeMethod(engine, "parseJtl",
                    jtl, formalStart, 10, (StressSampleListener) null, formalStart + 3000);

            // 总量 = 4（warmup 2 条被过滤）；错误 1；P50 nearest-rank = 第 2 值 20、P95 = 第 4 值 40
            assertEquals(4L, ReflectionTestUtils.getField(outcome, "total"));
            assertEquals(1L, ReflectionTestUtils.getField(outcome, "errors"));
            assertEquals(20.0, (double) ReflectionTestUtils.getField(outcome, "p50"), 0.0);
            assertEquals(40.0, (double) ReflectionTestUtils.getField(outcome, "p95"), 0.0);
            // 秒级样本：2 个秒桶（formalStart 所在秒 3 条 + 下一秒 1 条）
            @SuppressWarnings("unchecked")
            List<StressTaskResult.Sample> samples = (List<StressTaskResult.Sample>) ReflectionTestUtils.getField(outcome, "samples");
            assertEquals(2, samples.size());
            assertEquals(3.0, samples.get(0).getQps(), 0.0);
        } finally {
            Files.deleteIfExists(jtl);
        }
    }

    private static StressTaskRequest.Stage stage(StressTaskRequest req) {
        StressTaskRequest.Stage s = new StressTaskRequest.Stage();
        s.setConcurrency(req.getConcurrency());
        s.setDurationSec(req.getDurationSec());
        return s;
    }
}
