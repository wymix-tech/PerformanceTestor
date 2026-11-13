"""engine 包：压测执行引擎（Locust 主引擎 / JMeter 兜底）+ 统计层 + 引擎注册表。

统计语义逐位对齐 perf-server 的 StressStatistician（30000 桶 × 1ms 平坦直方图），
详见 stats.py 模块注释。
"""
