"""MyBatis 解析器等价单测：移植 MyBatisSourceParserTest 全部 6 个用例。

XML resultMap/SQL 与 Java 注解/常量/枚举（样例内嵌即 golden 文件）。
"""

from datagen.model import RelationHint, SourceFile
from datagen.mybatis_source_parser import MyBatisSourceParser, snake, to_table_name

XML = """
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
"""

JAVA = """
package com.x;
import org.apache.ibatis.annotations.Select;
import java.util.List;
enum PayState { NEW, PAID, DONE }
class OrderConst { public static final String STATUS_SHIPPED = "SHIPPED"; }
interface TUserMapper {
  @Select("SELECT u.* FROM t_user u WHERE u.age = 18")
  List<Object> sel();
}
"""


def test_supports_detected():
    p = MyBatisSourceParser()
    files = [SourceFile("m/TOrderMapper.xml", XML), SourceFile("m/TUserMapper.java", JAVA)]
    assert p.supports(files)
    assert not p.supports([SourceFile("a.txt", "hello")])
    assert not p.supports([])


def test_xml_association_and_collection():
    r = MyBatisSourceParser().scan([SourceFile("m/TOrderMapper.xml", XML)],
                                   {"t_order", "t_user", "t_item"})
    assert any(h.child_table == "t_order" and h.child_column == "user_id"
               and h.parent_table == "t_user" and h.source == RelationHint.SRC_ORM_JOIN
               for h in r.relation_hints)
    assert any(h.child_column == "order_id" and h.parent_table == "t_item"
               for h in r.relation_hints)


def test_xml_literals_and_join():
    r = MyBatisSourceParser().scan([SourceFile("m/TOrderMapper.xml", XML)],
                                   {"t_order", "t_user", "t_item"})
    assert any(h.column == "status" and "SHIPPED" in h.values for h in r.magic_hints)
    assert any(h.column == "user_id" and 7 in h.values for h in r.magic_hints)


def test_java_enum_constant_and_annotation():
    r = MyBatisSourceParser().scan([SourceFile("m/TUserMapper.java", JAVA)],
                                   {"t_user", "t_order"})
    assert r.java_enums.get("PayState") == ["NEW", "PAID", "DONE"]
    assert any(h.column == "age" and 18 in h.values for h in r.magic_hints)
    assert any(h.kind == "constant" and "SHIPPED" in h.values for h in r.magic_hints)


def test_table_name_conversion():
    assert to_table_name("com.x.T_Order") == "t_order"
    assert to_table_name("com.x.UserDO") == "user"
    assert to_table_name(None) is None
    assert to_table_name("  ") is None


def test_snake():
    assert snake("STATUS_SHIPPED") == "status"
    assert snake("USER_TYPE_A") == "type"


def test_malformed_content_silently_skipped():
    r = MyBatisSourceParser().scan(
        [SourceFile("bad.xml", "<mapper><broken"), SourceFile("bad.java", "class {{{")],
        {"t"})
    assert not r.magic_hints
    assert not r.relation_hints
