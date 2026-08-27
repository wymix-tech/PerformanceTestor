package com.perfserver.stress;

import org.springframework.stereotype.Component;

import java.util.List;
import java.util.Map;
import java.util.function.Function;
import java.util.stream.Collectors;

/**
 * 压测引擎注册表：按 type 选择引擎，未匹配时回退 http。
 */
@Component
public class StressEngineRegistry {

    private final Map<String, StressEngine> engines;

    public StressEngineRegistry(List<StressEngine> engineList) {
        this.engines = engineList.stream()
                .collect(Collectors.toMap(StressEngine::type, Function.identity(), (a, b) -> a));
    }

    public StressEngine get(String type) {
        return engines.getOrDefault(type == null ? "http" : type, engines.get("http"));
    }
}
