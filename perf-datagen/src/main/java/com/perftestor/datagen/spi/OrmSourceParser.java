package com.perftestor.datagen.spi;

import com.perftestor.datagen.model.MagicHint;
import com.perftestor.datagen.model.RelationHint;
import com.perftestor.datagen.model.SourceFile;

import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * ORM 源码解析 SPI：从业务源码中提取魔法值候选与表间关联语义。
 * 本期实现 MyBatisSourceParser（XML resultMap association/collection 为主，
 * 注解 SQL JOIN 正则为辅，匹配失败静默跳过）；未来 JPA/Spring Data JDBC 新增实现类即可。
 */
public interface OrmSourceParser {

    /**
     * 特征检测：源码集合中是否出现该 ORM 的特征（如 org.apache.ibatis 导入、
     * mybatis mapper DTD、@Select 注解等）。
     */
    boolean supports(List<SourceFile> files);

    /** 多 ORM 共存时的解析优先级（小者优先） */
    int order();

    /**
     * 扫描源码产出语义提示。实现必须容错：单文件解析失败/超时静默跳过，
     * 绝不因 SQL 方言碎片化中断整个灌数流程。
     *
     * @param files       源码文件集合（.java/.xml）
     * @param knownTables 目标表组表名（小写，用于 JOIN 关系归属过滤）
     */
    ScanResult scan(List<SourceFile> files, Collection<String> knownTables);

    /** 扫描产物：魔法值候选 + 关联语义 + Java 枚举候选（枚举反查第四级来源） */
    class ScanResult {
        private List<MagicHint> magicHints = new ArrayList<>();
        private List<RelationHint> relationHints = new ArrayList<>();
        /** Java 枚举类名（如 OrderStatus）→ 常量候选值列表 */
        private Map<String, List<String>> javaEnums = new LinkedHashMap<>();

        public List<MagicHint> getMagicHints() {
            return magicHints;
        }

        public List<RelationHint> getRelationHints() {
            return relationHints;
        }

        public Map<String, List<String>> getJavaEnums() {
            return javaEnums;
        }
    }
}
