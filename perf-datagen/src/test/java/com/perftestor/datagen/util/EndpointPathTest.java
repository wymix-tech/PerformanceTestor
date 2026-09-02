package com.perftestor.datagen.util;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;

/** 路径归一化单点工具单测：两条解析链路共用，行为必须唯一确定。 */
class EndpointPathTest {

    @Test
    void normalizeAddsLeadingSlash() {
        assertEquals("/order", EndpointPath.normalize("order"));
    }

    @Test
    void normalizeStripsTrailingSlash() {
        assertEquals("/order", EndpointPath.normalize("/order/"));
        assertEquals("/order", EndpointPath.normalize("/order///"));
    }

    @Test
    void normalizeKeepsRoot() {
        assertEquals("/", EndpointPath.normalize("/"));
        assertEquals("/", EndpointPath.normalize("//"));
    }

    @Test
    void normalizeBlankToEmpty() {
        assertEquals("", EndpointPath.normalize(null));
        assertEquals("", EndpointPath.normalize("   "));
    }

    @Test
    void joinConcatenatesPrefixAndMethodPath() {
        assertEquals("/api/order/list", EndpointPath.join("/api/order", "/list"));
        assertEquals("/api/order/list", EndpointPath.join("api/order", "list"));
    }

    @Test
    void joinReturnsRootWhenBothBlank() {
        assertEquals("/", EndpointPath.join("", ""));
        assertEquals("/", EndpointPath.join(null, null));
    }

    @Test
    void joinKeepsPathVariableAsIs() {
        assertEquals("/api/order/{id}", EndpointPath.join("/api/order", "/{id}"));
    }

    @Test
    void joinStripsTrailingSlashOfResult() {
        assertEquals("/api/order", EndpointPath.join("/api/order/", "/"));
    }

    @Test
    void simpleNameOfJavaFile() {
        assertEquals("OrderController", EndpointPath.simpleNameOf("src/main/java/com/x/OrderController.java"));
        assertEquals("OrderController", EndpointPath.simpleNameOf("OrderController.java"));
        assertEquals("OrderController", EndpointPath.simpleNameOf("C:\\repo\\OrderController.java"));
    }
}
