package com.perftestor.datagen.endpoint;

import com.perftestor.datagen.model.SourceFile;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Optional;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** Mapper → SQL 双通道解析单测：XML namespace+id 与注解（含字符串拼接）。 */
class MyBatisSqlResolverTest {

    private static final String XML = "src/main/resources/mapper/OrderMapper.xml";
    private static final String MAPPER_JAVA = "src/main/java/com/demo/mapper/OrderMapper.java";

    private static final String XML_CONTENT = """
            <?xml version="1.0" encoding="UTF-8" ?>
            <!DOCTYPE mapper PUBLIC "-//mybatis.org//DTD Mapper 3.0//EN"
                    "http://mybatis.org/dtd/mybatis-3-mapper.dtd">
            <mapper namespace="com.demo.mapper.OrderMapper">
                <select id="selectByStatus" resultType="com.demo.entity.Order">
                    SELECT * FROM perf_order o
                    <where>
                        <if test="status != null">AND o.status = #{status}</if>
                    </where>
                    ORDER BY o.id DESC LIMIT #{limit}
                </select>
                <insert id="insert">
                    INSERT INTO perf_order (user_id, amount, status) VALUES (#{userId}, #{amount}, #{status})
                </insert>
            </mapper>
            """;

    /** perf-demo 形态：注解 SQL 以 "A" + "B" 拼接 */
    private static final String ANNOTATION_CONTENT = """
            package com.demo.mapper;

            import org.apache.ibatis.annotations.Select;

            public interface OrderMapper {
                @Select("SELECT id, user_id, amount, status FROM perf_order WHERE status = #{status} "
                        + "ORDER BY id DESC LIMIT #{limit}")
                List<Order> selectByStatus(String status, int limit);
            }
            """;

    @Test
    void xmlStatementIsResolvedByNamespaceAndId() {
        MyBatisSqlResolver resolver = new MyBatisSqlResolver(List.of(new SourceFile(XML, XML_CONTENT)));
        Optional<String> sql = resolver.resolve("OrderMapper", "selectByStatus", null);
        assertTrue(sql.isPresent());
        assertTrue(sql.get().contains("perf_order"));
    }

    @Test
    void xmlInsertIsResolved() {
        MyBatisSqlResolver resolver = new MyBatisSqlResolver(List.of(new SourceFile(XML, XML_CONTENT)));
        assertTrue(resolver.resolve("OrderMapper", "insert", null).orElse("").contains("INSERT INTO perf_order"));
    }

    @Test
    void unknownStatementReturnsEmpty() {
        MyBatisSqlResolver resolver = new MyBatisSqlResolver(List.of(new SourceFile(XML, XML_CONTENT)));
        assertTrue(resolver.resolve("OrderMapper", "notExist", null).isEmpty());
    }

    @Test
    void annotationSqlWithStringConcatenationIsResolved() {
        MyBatisSqlResolver resolver = new MyBatisSqlResolver(List.of());
        resolver.indexAnnotations(List.of(new SourceFile(MAPPER_JAVA, ANNOTATION_CONTENT)));
        Optional<String> sql = resolver.resolve("OrderMapper", "selectByStatus", MAPPER_JAVA);
        assertTrue(sql.isPresent(), "字符串拼接的注解 SQL 必须能解析，否则整条 SQL 丢失");
        assertTrue(sql.get().contains("FROM perf_order"));
    }

    @Test
    void brokenXmlDoesNotThrow() {
        MyBatisSqlResolver resolver = new MyBatisSqlResolver(
                List.of(new SourceFile(XML, "<mapper><select id=\"x\">")));
        assertFalse(resolver.resolve("OrderMapper", "x", null).isPresent());
    }

    @Test
    void mapperInvocationOverloadResolves() {
        MyBatisSqlResolver resolver = new MyBatisSqlResolver(List.of(new SourceFile(XML, XML_CONTENT)));
        CallGraphAnalyzer.MapperInvocation inv =
                new CallGraphAnalyzer.MapperInvocation("OrderMapper", "insert", "X.java:10", "");
        assertTrue(resolver.resolve(inv).orElse("").contains("INSERT INTO perf_order"));
    }
}
