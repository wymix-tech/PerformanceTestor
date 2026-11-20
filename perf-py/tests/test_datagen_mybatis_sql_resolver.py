"""Mapper → SQL 双通道解析等价单测：移植 MyBatisSqlResolverTest 全部 6 个用例。

XML namespace+id 与注解（含字符串拼接）。
"""

from ptde.analyzer.lang_java.call_graph_analyzer import MapperInvocation
from ptde.analyzer.lang_java.mybatis_sql_resolver import MyBatisSqlResolver
from ptde.models.wire import SourceFile

XML = "src/main/resources/mapper/OrderMapper.xml"
MAPPER_JAVA = "src/main/java/com/demo/mapper/OrderMapper.java"

XML_CONTENT = """
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
"""

# perf-demo 形态：注解 SQL 以 "A" + "B" 拼接
ANNOTATION_CONTENT = """
package com.demo.mapper;

import org.apache.ibatis.annotations.Select;

public interface OrderMapper {
    @Select("SELECT id, user_id, amount, status FROM perf_order WHERE status = #{status} "
            + "ORDER BY id DESC LIMIT #{limit}")
    List<Order> selectByStatus(String status, int limit);
}
"""


def test_xml_statement_is_resolved_by_namespace_and_id():
    resolver = MyBatisSqlResolver([SourceFile(XML, XML_CONTENT)])
    sql = resolver.resolve("OrderMapper", "selectByStatus", None)
    assert sql is not None
    assert "perf_order" in sql


def test_xml_insert_is_resolved():
    resolver = MyBatisSqlResolver([SourceFile(XML, XML_CONTENT)])
    sql = resolver.resolve("OrderMapper", "insert", None) or ""
    assert "INSERT INTO perf_order" in sql


def test_unknown_statement_returns_empty():
    resolver = MyBatisSqlResolver([SourceFile(XML, XML_CONTENT)])
    assert resolver.resolve("OrderMapper", "notExist", None) is None


def test_annotation_sql_with_string_concatenation_is_resolved():
    resolver = MyBatisSqlResolver([])
    resolver.index_annotations([SourceFile(MAPPER_JAVA, ANNOTATION_CONTENT)])
    sql = resolver.resolve("OrderMapper", "selectByStatus", MAPPER_JAVA)
    assert sql is not None, "字符串拼接的注解 SQL 必须能解析，否则整条 SQL 丢失"
    assert "FROM perf_order" in sql


def test_broken_xml_does_not_throw():
    resolver = MyBatisSqlResolver([SourceFile(XML, '<mapper><select id="x">')])
    assert resolver.resolve("OrderMapper", "x", None) is None


def test_mapper_invocation_overload_resolves():
    resolver = MyBatisSqlResolver([SourceFile(XML, XML_CONTENT)])
    inv = MapperInvocation("OrderMapper", "insert", "X.java:10", "")
    sql = resolver.resolve_invocation(inv) or ""
    assert "INSERT INTO perf_order" in sql
