package com.perftestor.datagen.orm.mybatis;

import com.perftestor.datagen.model.RelationHint;
import com.perftestor.datagen.model.SourceFile;
import com.perftestor.datagen.spi.OrmSourceParser;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** MyBatis 解析器单测：XML resultMap/SQL 与 Java 注解/常量/枚举（样例内嵌）。 */
class MyBatisSourceParserTest {

    private static final String XML = """
            <?xml version="1.0" encoding="UTF-8"?>
            <!DOCTYPE mapper PUBLIC "-//mybatis.org//DTD Mapper 3.0//EN" "http://mybatis.org/dtd/mybatis-3-mapper.dtd">
            <mapper namespace="com.x.TOrderMapper">
              <resultMap id="rm" type="com.x.T_Order">
                <id column="id" property="id"/>
                <association property="user" column="user_id" javaType="com.x.T_User"/>
                <collection property="items" column="order_id" ofType="com.x.T_Item"/>
              </resultMap>
              <select id="sel" resultType="com.x.T_Order">
                SELECT o.* FROM t_order o WHERE o.status = 'SHIPPED' AND o.user_id = 7
              </select>
            </mapper>
            """;

    private static final String JAVA = """
            package com.x;
            import org.apache.ibatis.annotations.Select;
            import java.util.List;
            enum PayState { NEW, PAID, DONE }
            class OrderConst { public static final String STATUS_SHIPPED = "SHIPPED"; }
            interface TUserMapper {
              @Select("SELECT u.* FROM t_user u WHERE u.age = 18")
              List<Object> sel();
            }
            """;

    private final MyBatisSourceParser parser = new MyBatisSourceParser();

    @Test
    void supportsDetected() {
        Set<String> tables = Set.of("t_order", "t_user", "t_item");
        List<SourceFile> files = List.of(new SourceFile("m/TOrderMapper.xml", XML),
                new SourceFile("m/TUserMapper.java", JAVA));
        assertTrue(parser.supports(files));
        assertFalse(parser.supports(List.of(new SourceFile("a.txt", "hello"))));
        assertFalse(parser.supports(List.of()));
    }

    @Test
    void xmlAssociationAndCollection() {
        OrmSourceParser.ScanResult r = parser.scan(
                List.of(new SourceFile("m/TOrderMapper.xml", XML)), Set.of("t_order", "t_user", "t_item"));
        assertTrue(r.getRelationHints().stream().anyMatch(h ->
                "t_order".equals(h.getChildTable()) && "user_id".equals(h.getChildColumn())
                        && "t_user".equals(h.getParentTable()) && RelationHint.SRC_ORM_JOIN.equals(h.getSource())));
        assertTrue(r.getRelationHints().stream().anyMatch(h ->
                "order_id".equals(h.getChildColumn()) && "t_item".equals(h.getParentTable())));
    }

    @Test
    void xmlLiteralsAndJoin() {
        OrmSourceParser.ScanResult r = parser.scan(
                List.of(new SourceFile("m/TOrderMapper.xml", XML)), Set.of("t_order", "t_user", "t_item"));
        assertTrue(r.getMagicHints().stream().anyMatch(h -> "status".equals(h.getColumn())
                && h.getValues().contains("SHIPPED")));
        assertTrue(r.getMagicHints().stream().anyMatch(h -> "user_id".equals(h.getColumn())
                && h.getValues().contains(7L)));
    }

    @Test
    void javaEnumConstantAndAnnotation() {
        OrmSourceParser.ScanResult r = parser.scan(
                List.of(new SourceFile("m/TUserMapper.java", JAVA)), Set.of("t_user", "t_order"));
        assertEquals(List.of("NEW", "PAID", "DONE"), r.getJavaEnums().get("PayState"));
        assertTrue(r.getMagicHints().stream().anyMatch(h -> "age".equals(h.getColumn())
                && h.getValues().contains(18L)));
        assertTrue(r.getMagicHints().stream().anyMatch(h ->
                "constant".equals(h.getKind()) && h.getValues().contains("SHIPPED")));
    }

    @Test
    void tableNameConversion() {
        assertEquals("t_order", MyBatisSourceParser.toTableName("com.x.T_Order"));
        assertEquals("user", MyBatisSourceParser.toTableName("com.x.UserDO"));
        assertEquals(null, MyBatisSourceParser.toTableName(null));
        assertEquals(null, MyBatisSourceParser.toTableName("  "));
    }

    @Test
    void malformedContentSilentlySkipped() {
        OrmSourceParser.ScanResult r = parser.scan(List.of(
                new SourceFile("bad.xml", "<mapper><broken"),
                new SourceFile("bad.java", "class {{{")), Set.of("t"));
        assertTrue(r.getMagicHints().isEmpty());
        assertTrue(r.getRelationHints().isEmpty());
    }
}
