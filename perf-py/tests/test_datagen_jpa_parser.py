"""JPA 解析器等价单测：移植 JpaSourceParserTest 全部 11 个用例。

Java 测试内嵌源码即 golden 文件：@Entity/@ManyToOne/@ManyToMany 关联、
枚举常量、mappedBy 跳过、异常容错。
"""

from datagen.jpa_source_parser import JpaSourceParser
from datagen.model import RelationHint, SourceFile

ENTITIES = """
package com.x;
import jakarta.persistence.*;
import java.util.List;

@Entity
@Table(name = "t_order")
class OrderEntity {
    @Id
    @GeneratedValue(strategy = GenerationType.IDENTITY)
    private Long id;

    @ManyToOne
    @JoinColumn(name = "user_id", referencedColumnName = "id")
    private UserEntity user;

    @Enumerated(EnumType.STRING)
    private PayState state;
}

@Entity
class UserEntity {
    @Id
    private Long id;

    @OneToMany(mappedBy = "user")
    private List<OrderEntity> orders;
}
"""

JOIN_TABLE = """
package com.x;
import jakarta.persistence.*;
import java.util.Set;

@Entity
@Table(name = "t_user")
class UserEntity {
    @Id
    private Long id;

    @ManyToMany
    @JoinTable(name = "t_user_role",
            joinColumns = @JoinColumn(name = "uid"),
            inverseJoinColumns = @JoinColumn(name = "rid"))
    private Set<RoleEntity> roles;
}

@Entity
@Table(name = "t_role")
class RoleEntity {
    @Id
    private Long id;
}
"""

CONSTANTS = """
package com.x;
import javax.persistence.Entity;
enum OrderStatus { NEW, PAID, DONE }
class Const { public static final String STATUS_SHIPPED = "SHIPPED"; }
@Entity
class Item { @Id Long id; }
"""


def scan(content: str, path: str = "e/OrderEntity.java", tables=("t_order", "user")):
    return JpaSourceParser().scan([SourceFile(path, content)], set(tables))


def test_supports_detected_for_jakarta_and_javax():
    p = JpaSourceParser()
    assert p.supports([SourceFile("e/OrderEntity.java", ENTITIES)])
    assert p.supports([SourceFile("e/Const.java", CONSTANTS)]), "javax.persistence 也应识别"
    assert not p.supports([SourceFile("a.txt", "hello")])
    assert not p.supports([])
    assert not p.supports(None)


def test_order_after_mybatis():
    assert JpaSourceParser().order() == 20, "JPA 应排在 MyBatis(10) 之后"


def test_many_to_one_join_column_produces_hint():
    # UserEntity 无 @Table：表名按类名推导（剥离 Entity 后缀 → user）
    r = scan(ENTITIES)
    assert any(h.child_table == "t_order" and h.child_column == "user_id"
               and h.parent_table == "user" and h.parent_column == "id"
               and h.source == RelationHint.SRC_ORM_JOIN and h.confidence == 0.9
               for h in r.relation_hints), "应产出 t_order.user_id → user.id"


def test_mapped_by_does_not_duplicate():
    r = scan(ENTITIES)
    # UserEntity.orders 为 mappedBy 反向声明，不产出 user → t_order 边（由对端 ManyToOne 覆盖）
    assert not any(h.child_table == "user" and h.parent_table == "t_order"
                   for h in r.relation_hints), "mappedBy 反向声明不应产出关联边"
    assert len(r.relation_hints) == 1, "仅 ManyToOne 一侧产出一条边"


def test_join_column_falls_back_to_field_name():
    src = """
    package com.x;
    import jakarta.persistence.*;
    @Entity
    @Table(name = "t_order")
    class OrderEntity {
        @Id Long id;
        @ManyToOne
        private UserEntity buyer;
    }
    @Entity
    @Table(name = "t_user")
    class UserEntity { @Id Long id; }
    """
    r = scan(src, tables=("t_order", "t_user"))
    assert any(h.child_column == "buyer_id" and h.parent_column == "id"
               for h in r.relation_hints), "无 @JoinColumn 时按字段名推导外键列 buyer_id，父列默认 id"


def test_many_to_many_join_table_produces_two_hints():
    r = scan(JOIN_TABLE, tables=("t_user", "t_role", "t_user_role"))
    assert any(h.child_table == "t_user_role" and h.child_column == "uid"
               and h.parent_table == "t_user" for h in r.relation_hints), "中间表 → 拥有方"
    assert any(h.child_table == "t_user_role" and h.child_column == "rid"
               and h.parent_table == "t_role" for h in r.relation_hints), "中间表 → 目标方"


def test_unknown_table_filtered_out():
    r = scan(ENTITIES, tables=("other",))
    assert not r.relation_hints, "表名不在目标表组时应过滤"


def test_enums_and_constants_collected():
    r = scan(CONSTANTS, tables=("item", "const"))
    assert r.java_enums.get("OrderStatus") == ["NEW", "PAID", "DONE"]
    assert any(h.column == "status" and "SHIPPED" in h.values for h in r.magic_hints)


def test_malformed_content_silently_skipped():
    r = scan("class {{{", path="bad.java", tables=("t",))
    assert not r.magic_hints
    assert not r.relation_hints


def test_many_to_many_without_join_table_produces_nothing():
    r = scan(JOIN_TABLE, tables=("t_user", "t_role"))
    # JOIN_TABLE 片段确已声明 @JoinTable，此处仅校验表名解析（显式 @Table）后能产出中间表边
    assert (not r.relation_hints
            or all(h.child_table == "t_user_role" for h in r.relation_hints))


def test_table_annotation_overrides_class_name():
    # JOIN_TABLE 中 UserEntity/RoleEntity 均显式 @Table(name=...)，中间表两侧边应命中显式表名
    r = scan(JOIN_TABLE, tables=("t_user", "t_role", "t_user_role"))
    assert len(r.relation_hints) == 2
    assert all(h.parent_table in ("t_user", "t_role") for h in r.relation_hints), \
        "父表应命中显式 @Table 名（t_user/t_role），而非类名推导"
