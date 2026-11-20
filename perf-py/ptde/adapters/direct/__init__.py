"""直连内省与 DbApi 批量执行套件（自 datagen/spi·executor·dialects 原样收编）。

- ``spi``：直连方言协议（DatabaseDialect / BatchExecutor）；
- ``executor``：DbApi 批量 INSERT 执行器（pilot_run 降级通道）；
- ``dialects``：MySQL / PostgreSQL 直连方言（supports/connect/enrich_jdbc_url/
  fetch_metadata/quote_identifier/param）——服务层表列探测（/tables、/columns）、
  水位捕获、verifyMin、perf_cli 采集工具继续消费（B7 续批次 11 收编归位）。

与 ``ptde/adapters`` 分层基座（R5 装载适配器）职责不同：本包面向「已有
JDBC 形态入口的直连内省与逐批写入」，``ptde/adapters`` 面向 OfflinePipeline
装载链；两者并存，语义各自冻结。
"""
