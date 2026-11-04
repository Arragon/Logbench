# R6 旧链路性能基线（实测）

> 票：`R6-006`　｜　被测版本：v3.0.3　｜　采集时间：2026-11-02
> 原始数据：`qa/evidence/R6-006/search-baseline.json`
> 采集脚本：`qa/r6/search_baseline.py`（真实 Chrome + 真实页面，未改服务端代码）

## 1. 方法与边界

测量在**真实浏览器**里对 v3.0.3 页面进行，不替换解析器、不放大超时。脚本用四个包装观察生产代码已经在调用的原语：

| 包装点 | 反映的耗时 |
|---|---|
| `Worker.prototype.postMessage` | 主线程为把 `records` 交给 Worker 所做的**同步结构化克隆** |
| `new Worker` | 一次搜索创建几个 Worker |
| `JSON.stringify` | 消息/状态序列化字节量 |
| `TextDecoder.prototype.decode` | 解码耗时（本链路未走该 API，实测 0） |

外加 `PerformanceObserver({entryTypes:['longtask']})` 记录主线程长任务。**Node 侧的同名实验不计入本报告**（见 `qa/r6/baseline/search-load-probe.txt` 的 651,658 条 / 2398 ms 克隆，那是 Node 结果，仅作交叉参考）。

环境：HeadlessChrome 156、`hardwareConcurrency=12`、`deviceMemory=16`、安全上下文、IndexedDB 与 OPFS 均可用、**每次运行使用全新浏览器档案（冷缓存）**。

语料：`tests/fixtures/r6/scale/` 5 × 20 MiB（合计 100.0 MiB）。Node 分帧结果为每文件 ≈130,331 条、合计 651,658 条。

## 2. 导入实测

| 指标 | 实测 |
|---|---|
| 5 × 20 MiB 导入墙钟 | **10,374 ms**（9.64 MiB/s） |
| 创建的 Worker | **10**（每文件 2 个） |
| 主线程长任务 | **13 次**，最长 **288 ms** |
| 单文件记录数 | **130,295** |
| 虚拟列表挂载行 | 25 行（滚动高度 34,030 px） |

关键事实：**导入后视图只呈现其中一个文件**。`.page-foot` 显示的是当前打开文件的记录数（130,295），不是 5 个文件的合计。因此下节的搜索测量实际上是"单文件 130k 条"的搜索，而不是"5 文件 651k 条"的搜索。

## 3. 搜索实测（三类查询）

| 查询 | 从最后一次击键到结果稳定 | 其中 `postMessage` 克隆 | 克隆记录数 | 结果条数 | Worker | 是否在 1800 ms 内 |
|---|---|---|---|---|---|---|
| 零命中（`zzz_no_such_token_…`） | **1,757 ms** | 273.4 ms | 130,295 | 0 | 1 | 是（余量 43 ms） |
| 多命中（`energy`） | **1,645 ms** | 247.8 ms | 130,295 | 18,484 | 1 | 是（余量 155 ms） |
| 少命中（`RuntimeError: periodic`） | **1,643 ms** | 247.6 ms | 130,295 | 26 | 1 | 是（余量 157 ms） |

三次查询的耗时几乎与命中数无关（1.64–1.76 s），说明**成本在"把全部记录搬到 Worker"这一步，而不在匹配**。零命中反而最慢，因为匹配没有提前退出的机会。

## 4. 克隆 vs 计算的分界

以 `energy` 为例，1,645 ms 的构成（按实测可归因部分）：

```text
1,645 ms  总墙钟（击键 → 结果稳定）
  248 ms  postMessage 同步结构化克隆（主线程，占 15%）
  ~0 ms   JSON.stringify（本链路仅 21 KB 状态，可忽略）
  ~1,397 ms  Worker 内 framing 复用 + 匹配 + 回传 + 主线程重渲染（未细分到更小粒度）
```

**能确定的是**：克隆是稳定可复现的固定开销（约 250 ms / 130k 条，即 ≈1.9 µs/条），随记录数线性增长。**不能确定的是**：剩余 1.4 s 里匹配与渲染各占多少——R5 没有分阶段埋点，这正是 R6 要在 `src/shared/diagnostics.js` 里补的 11 个阶段（`read/decode/framing/rules/postmessage/index/match/highlight/render/storage/queue`）。

按线性外推（仅用于判断量级）：5 文件全量 651,658 条的克隆约 **1.24 s**，加上匹配与渲染，**必然冲破 1800 ms**。这解释了为什么"把 5 个文件都打开后搜索会误杀"，也说明 R6 的方向必须是"不全量克隆"，而不是"调大超时"。

## 5. 目标与实测分列

R6 开发计划没有冻结数值型搜索目标，所以本表**不宣称任何达标**：

| 项 | 目标 | 实测 | 结论 |
|---|---|---|---|
| 普通查询不被 1800 ms 预算中断 | 冻结于 R5：`AbortSignal.timeout(1800)` | 单文件 130k：1,643–1,757 ms（余量 43–157 ms） | 单文件已贴边；多文件外推必超 |
| 全库（5×20 MiB）搜索覆盖 | R6 要求 `coverage.complete=true` 或显式 `partial` | R5 **只能搜当前打开文件**，多文件无合并搜索 | 能力缺口，不是性能问题 |
| 克隆开销 | 不随全量记录数线性增长 | 1.9 µs/条，与命中数无关 | 需在 M1 改为增量索引 |
| 导入吞吐 | 未冻结 | 9.64 MiB/s，长任务最长 288 ms | 仅记录 |

**待 M1 冻结的数值目标（建议，非承诺）**：单次普通查询 ≤ 400 ms（100 MiB 语料、`relation=exact`）、导入期间输入延迟 p95 ≤ 100 ms、主线程单次长任务 ≤ 100 ms。这三项需要 `R6-009`–`R6-018` 的实现数据来校准，现在写死会变成自证。

## 6. 复跑方式

```bash
# 1) 启动一个隔离实例（不要复用生产 data/）
env -u PYTHONPATH python3.9 -B run.py --no-reload --port 50902 \
  --admin-ip 127.0.0.1 --data-dir /tmp/r6e/data --state-dir /tmp/r6e/var

# 2) 采集（需 Playwright + 系统 Chrome）
env -u PYTHONPATH python3.9 qa/r6/search_baseline.py \
  --base-url http://127.0.0.1:50902 \
  --files tests/fixtures/r6/scale/*.log \
  --out qa/evidence/R6-006
```

报告里**不含文件名、日志原文或完整搜索词**，只有长度、词数与关联哈希（`src/shared/diagnostics.js` 的脱敏规则，11 项单测覆盖）。
