package com.perfserver.config;

import com.perfserver.source.SourceFileProvider;
import com.perfserver.source.SourceFileService;
import com.perftestor.datagen.DefaultDataGenEngine;
import com.perftestor.datagen.dialect.mysql.MySQLDialect;
import com.perftestor.datagen.executor.JdbcBatchExecutor;
import com.perftestor.datagen.orm.mybatis.MyBatisSourceParser;
import com.perftestor.datagen.spi.BatchExecutor;
import com.perftestor.datagen.spi.DatabaseDialect;
import com.perftestor.datagen.spi.OrmSourceParser;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import java.util.List;

/**
 * 引擎装配：MySQL 方言 + JDBC 批量执行器 + MyBatis 解析器（本期唯一实现组合）。
 * 未来扩展 PostgreSQL/JPA 时在此追加实现类并按请求声明路由。
 */
@Configuration
public class DataGenEngineConfig {

    @Bean
    public DatabaseDialect dataGenDialect() {
        return new MySQLDialect();
    }

    @Bean
    public BatchExecutor dataGenBatchExecutor() {
        return new JdbcBatchExecutor();
    }

    @Bean
    public OrmSourceParser dataGenOrmParser() {
        return new MyBatisSourceParser();
    }

    @Bean
    public DefaultDataGenEngine dataGenEngine(DatabaseDialect dialect, BatchExecutor executor,
                                              OrmSourceParser parser, SourceFileProvider provider,
                                              SourceFileService sourceFileService) {
        return new DefaultDataGenEngine(dialect, executor, parser);
    }
}

