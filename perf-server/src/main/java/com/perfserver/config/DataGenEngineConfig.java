package com.perfserver.config;

import com.perfserver.source.SourceFileProvider;
import com.perfserver.source.SourceFileService;
import com.perftestor.datagen.DefaultDataGenEngine;
import com.perftestor.datagen.dialect.mysql.MySQLDialect;
import com.perftestor.datagen.dialect.postgres.PostgreSQLDialect;
import com.perftestor.datagen.executor.JdbcBatchExecutor;
import com.perftestor.datagen.orm.jpa.JpaSourceParser;
import com.perftestor.datagen.orm.mybatis.MyBatisSourceParser;
import com.perftestor.datagen.spi.BatchExecutor;
import com.perftestor.datagen.spi.DatabaseDialect;
import com.perftestor.datagen.spi.OrmSourceParser;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import java.util.List;

/**
 * 引擎装配：多方言 + 多 ORM 解析器注册，由引擎按 {@code supports(jdbcUrl)}
 * 与 {@code supports(files)+order()} 运行时路由（见 {@link DefaultDataGenEngine}）。
 * 新增库种/ORM 仅需在此追加实现类，无需改动引擎与调用方。
 */
@Configuration
public class DataGenEngineConfig {

    /**
     * 已注册方言：MySQL + PostgreSQL（按 JDBC URL 前缀判别）。
     */
    @Bean
    public List<DatabaseDialect> dataGenDialects() {
        return List.of(new MySQLDialect(), new PostgreSQLDialect());
    }

    @Bean
    public BatchExecutor dataGenBatchExecutor() {
        return new JdbcBatchExecutor();
    }

    /**
     * 已注册 ORM 解析器：MyBatis（order 10）+ JPA（order 20）。
     * 两者特征互斥（Mapper XML 注解 vs jakarta/javax.persistence），多 ORM 共存时按源码特征全部命中并合并。
     */
    @Bean
    public List<OrmSourceParser> dataGenOrmParsers() {
        return List.of(new MyBatisSourceParser(), new JpaSourceParser());
    }

    @Bean
    public DefaultDataGenEngine dataGenEngine(List<DatabaseDialect> dialects, BatchExecutor executor,
                                              List<OrmSourceParser> parsers, SourceFileProvider provider,
                                              SourceFileService sourceFileService) {
        return new DefaultDataGenEngine(dialects, executor, parsers);
    }
}
