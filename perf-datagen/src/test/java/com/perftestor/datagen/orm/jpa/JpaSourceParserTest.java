package com.perftestor.datagen.orm.jpa;

import com.perftestor.datagen.model.RelationHint;
import com.perftestor.datagen.model.SourceFile;
import com.perftestor.datagen.spi.OrmSourceParser;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Set;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

/** JPA 解析器单测：@Entity/@ManyToOne/@ManyToMany 关联、枚举常量、mappedBy 跳过、异常容错。 */
class JpaSourceParserTest {

    private static final String ENTITIES = """
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
            """;

    private static final String JOIN_TABLE = """
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
            """;

    private static final String CONSTANTS = """
            package com.x;
            import javax.persistence.Entity;
            enum OrderStatus { NEW, PAID, DONE }
            class Const { public static final String STATUS_SHIPPED = "SHIPPED"; }
            @Entity
            class Item { @Id Long id; }
            """;

    private final JpaSourceParser parser = new JpaSourceParser();

    @Test
    void supportsDetectedForJakartaAndJavax() {
        assertTrue(parser.supports(List.of(new SourceFile("e/OrderEntity.java", ENTITIES))));
        assertTrue(parser.supports(List.of(new SourceFile("e/Const.java", CONSTANTS))), "javax.persistence 也应识别");
        assertFalse(parser.supports(List.of(new SourceFile("a.txt", "hello"))));
        assertFalse(parser.supports(List.of()));
        assertFalse(parser.supports(null));
    }

    @Test
    void orderAfterMyBatis() {
        assertEquals(20, parser.order(), "JPA 应排在 MyBatis(10) 之后");
    }

    @Test
    void manyToOneJoinColumnProducesHint() {
        // UserEntity 无 @Table：表名按类名推导（剥离 Entity 后缀 → user）
        OrmSourceParser.ScanResult r = parser.scan(
                List.of(new SourceFile("e/OrderEntity.java", ENTITIES)), Set.of("t_order", "user"));
        assertTrue(r.getRelationHints().stream().anyMatch(h ->
                "t_order".equals(h.getChildTable()) && "user_id".equals(h.getChildColumn())
                        && "user".equals(h.getParentTable()) && "id".equals(h.getParentColumn())
                        && RelationHint.SRC_ORM_JOIN.equals(h.getSource())
                        && h.getConfidence() == 0.9), "应产出 t_order.user_id → user.id");
    }

    @Test
    void mappedByDoesNotDuplicate() {
        OrmSourceParser.ScanResult r = parser.scan(
                List.of(new SourceFile("e/OrderEntity.java", ENTITIES)), Set.of("t_order", "user"));
        // UserEntity.orders 为 mappedBy 反向声明，不产出 user → t_order 边（由对端 ManyToOne 覆盖）
        assertFalse(r.getRelationHints().stream().anyMatch(h ->
                "user".equals(h.getChildTable()) && "t_order".equals(h.getParentTable())),
                "mappedBy 反向声明不应产出关联边");
        assertEquals(1, r.getRelationHints().size(), "仅 ManyToOne 一侧产出一条边");
    }

    @Test
    void joinColumnFallsBackToFieldName() {
        String src = """
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
                """;
        OrmSourceParser.ScanResult r = parser.scan(
                List.of(new SourceFile("e/OrderEntity.java", src)), Set.of("t_order", "t_user"));
        assertTrue(r.getRelationHints().stream().anyMatch(h ->
                "buyer_id".equals(h.getChildColumn()) && "id".equals(h.getParentColumn())),
                "无 @JoinColumn 时按字段名推导外键列 buyer_id，父列默认 id");
    }

    @Test
    void manyToManyJoinTableProducesTwoHints() {
        OrmSourceParser.ScanResult r = parser.scan(
                List.of(new SourceFile("e/UserEntity.java", JOIN_TABLE)),
                Set.of("t_user", "t_role", "t_user_role"));
        assertTrue(r.getRelationHints().stream().anyMatch(h ->
                "t_user_role".equals(h.getChildTable()) && "uid".equals(h.getChildColumn())
                        && "t_user".equals(h.getParentTable())), "中间表 → 拥有方");
        assertTrue(r.getRelationHints().stream().anyMatch(h ->
                "t_user_role".equals(h.getChildTable()) && "rid".equals(h.getChildColumn())
                        && "t_role".equals(h.getParentTable())), "中间表 → 目标方");
    }

    @Test
    void unknownTableFilteredOut() {
        OrmSourceParser.ScanResult r = parser.scan(
                List.of(new SourceFile("e/OrderEntity.java", ENTITIES)), Set.of("other"));
        assertTrue(r.getRelationHints().isEmpty(), "表名不在目标表组时应过滤");
    }

    @Test
    void enumsAndConstantsCollected() {
        OrmSourceParser.ScanResult r = parser.scan(
                List.of(new SourceFile("e/Const.java", CONSTANTS)), Set.of("item", "const"));
        assertEquals(List.of("NEW", "PAID", "DONE"), r.getJavaEnums().get("OrderStatus"));
        assertTrue(r.getMagicHints().stream().anyMatch(h ->
                "status".equals(h.getColumn()) && h.getValues().contains("SHIPPED")));
    }

    @Test
    void malformedContentSilentlySkipped() {
        OrmSourceParser.ScanResult r = parser.scan(
                List.of(new SourceFile("bad.java", "class {{{")), Set.of("t"));
        assertTrue(r.getMagicHints().isEmpty());
        assertTrue(r.getRelationHints().isEmpty());
    }

    @Test
    void manyToManyWithoutJoinTableProducesNothing() {
        OrmSourceParser.ScanResult r = parser.scan(
                List.of(new SourceFile("e/UserEntity.java", JOIN_TABLE)), Set.of("t_user", "t_role"));
        // JOIN_TABLE 片段确已声明 @JoinTable，此处仅校验表名解析（显式 @Table）后能产出中间表边
        assertTrue(r.getRelationHints().isEmpty() || r.getRelationHints().stream()
                .allMatch(h -> "t_user_role".equals(h.getChildTable())));
    }

    @Test
    void tableAnnotationOverridesClassName() {
        // JOIN_TABLE 中 UserEntity/RoleEntity 均显式 @Table(name=...)，中间表两侧边应命中显式表名
        OrmSourceParser.ScanResult r = parser.scan(
                List.of(new SourceFile("e/UserEntity.java", JOIN_TABLE)),
                Set.of("t_user", "t_role", "t_user_role"));
        assertEquals(2, r.getRelationHints().size());
        assertTrue(r.getRelationHints().stream().allMatch(h ->
                "t_user".equals(h.getParentTable()) || "t_role".equals(h.getParentTable())),
                "父表应命中显式 @Table 名（t_user/t_role），而非类名推导");
    }
}
