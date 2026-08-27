# PerformanceTestor — 中间产出物归档

> 本目录统一归档 PerformanceTestor 项目开发过程中产生的**全部中间产出物**，并纳入 git 版本管理（不入 .gitignore），作为项目交付基线的一部分。

## 项目简介

PerformanceTestor 是一个面向 Java 应用的**性能检测智能体平台**：

- 平台（perf-server、MySQL、源码存储）与被测应用**分离部署**在不同服务器，避免压测与监控互相干扰；
- 通过自研 Java Agent（ByteBuddy 字节码增强 + JMX）**无侵入**采集 JVM 指标、业务方法调用链耗时与慢 SQL；
- 提供自研压测引擎与自动灌数据能力；
- 支持 AI 自然语言对话自动执行检测任务；
- 采集项目源代码，将检测结果关联到源码位置，由大模型分析根因并给出多种修改方案，用户选定方案后生成**修复提示词**供 AI 辅助编码工具直接修复；
- 基于检测结果提供**容量评估**（可承受并发）、**扩容时机**与 **JVM 配置**优化建议。

## 目录结构

```
docs/
├── README.md               # 本文件：归档总览与索引
├── design/                 # 设计文档、架构图、技术方案、数据库设计说明
│   ├── requirements.md     # 需求文档（功能需求与边界说明）
│   ├── architecture.md     # 整体架构（分离部署）与网络拓扑
│   ├── tech-solution.md    # 技术方案与关键设计决策
│   └── database-design.md  # 数据库设计说明（表结构与分区策略）
├── reports/                # 性能检测报告（压测报告 / JVM 分析 / 容量评估）
├── issues/                 # 性能问题分析结果与修复提示词归档
└── artifacts/              # 其他中间产物（SQL 脚本导出、数据字典、联调记录等）
```

## 归档约定

1. 开发过程中产生的**全部中间产出物**统一归档至本目录，并按 `design / reports / issues / artifacts` 子目录归类；
2. `docs/` 全目录纳入 git 版本管理，不写入 `.gitignore`；
3. 各子目录需在文件头部注明：生成日期、对应模块、状态（草稿/评审中/已定稿）。

## 文档索引

| 文档 | 说明 | 状态 |
| ---- | ---- | ---- |
| [design/requirements.md](design/requirements.md) | 需求文档：7 大功能模块与边界说明 | 已定稿 |
| [design/architecture.md](design/architecture.md) | 整体架构：平台与被测应用分离部署拓扑 | 已定稿 |
| [design/tech-solution.md](design/tech-solution.md) | 技术方案：技术栈、关键设计决策、代码结构 | 已定稿 |
| [design/database-design.md](design/database-design.md) | 数据库设计说明：核心表、四张新表、分区策略 | 已定稿 |

> 状态说明：`已定稿` 表示该产出物已评审通过并作为后续开发基线；`进行中` 表示仍在迭代。
