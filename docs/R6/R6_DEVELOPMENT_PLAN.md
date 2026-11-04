# LogBench R6 任务开发计划

> 编制依据：Linear 项目 `LogBench R6 · 大文件工作台与远程采集`（93 张工单、10 个里程碑、6 份项目文档）＋ `LogBench_R6_Complete_Design_Rev2`（2026-11-02）＋ `unify-design v1.0.1`。
> 本文件是**编排层**：不重复工单正文的范围/方案/验收，而是解决"谁先做、谁能并行、什么时候算过、证据放哪"四个问题。
> 编制日期：2026-11-02。状态：**计划，未开始实施**。

---

## 1. 基线核对（实测事实）

以下为直接调用 Linear API 读取项目实物后的结果，非引用他人转述。

| 项目 | 实测值 |
|---|---|
| 工单总数 | 93（92 张 `[R6-001]`–`[R6-092]` ＋ 1 张参考资料 `INH-2336`） |
| 实施票构成 | 83 张交付票 ＋ 9 张 Gate（M0–M8） |
| 里程碑 | 10 个：M0 基线 / M1 本机引擎 / M2 本机导入搜索 / M3 服务端调度 / M4 服务端查询与上传 / M5 多存储根 / M6 多协议采集 / M7 归档与实时 / M8 跨来源 / M9 发布验收 |
| 各里程碑票数（均含 Gate） | M0=8（7 交付+1 Gate）、M1=10、M2=13、M3=10、M4=10、M5=7、M6=11、M7=8、M8=7、M9=8（**无 Gate**） |
| 状态 | 92 张 R6 实施票全部 Backlog；另 1 张 Done 为参考资料 `INH-2336`；**无一张 In Progress** |
| 责任人 / 估算 / 截止 | 全部为空（符合"未排具体日期、未分配执行人"的项目约束） |
| 标签分布 | 安全 25、验收·视觉模型 21、数据迁移 12、Contract·Freeze 3、Dispatch·External-Block 5、Gate 9 |
| 外部证据阻塞票 | `R6-041`、`R6-069`、`R6-077`、`R6-091`、`R6-092` |
| 契约冻结票 | `R6-002`（记录/任务/查询/Provider）、`R6-012`（查询 AST）、`R6-060`（传输适配器） |
| 项目文档 | 00 总计划、01 资料清单、02 UI 规范、03 公共契约、05 Rev2 权威设计、06 远程设计参考 |
| 代码侧现状 | 工作区为 v3.0.3 干净工作树，`docs/r6/`、`contracts/r6/`、`qa/r6/`、`src/shared/` 等 R6 目录**尚不存在** |

### 1.1 编排前必须修正的 4 项登记问题

| # | 问题 | 影响 | 建议动作 |
|---|---|---|---|
| 1 | **M9 缺聚合 Gate**。M0–M8 各有 1 张 Gate 票，M9 的 8 张票（`085`–`092`）全为验收型交付票，无汇总准入项 | 项目文档 00 要求"每阶段设独立验收 Gate"；M9 是唯一可以宣布"R6 可发布"的位置，现在没有唯一出口，`089`–`092` 各自 Done 也无法证明发布准入 | 新增 `[R6-093] Gate M9：发布准入与交付证据汇总`，把 `089`–`092` 的 PASS/BLOCKED 汇总成唯一发布判据 |
| 2 | **参考资料票混入 M0 统计**。`INH-2336` 位于 M0 且已 Done，使 M0 进度显示 11.11%（1/9） | 里程碑完成率被非实施项抬高；M0 实际实现进度为 0 | 将 `INH-2336` 移出里程碑，或保留但明确"不计入完成率"；M0 分母固定为 8（7 交付 + 1 Gate） |
| 3 | **两份 unify-design ZIP 未成为 Linear 附件**（文档 01 已如实标注） | `R6-003`、`R6-004` 以及 21 张 UI 票缺少可追溯的规范原件 | 本机已存 `unify-design` v1.0.1 skill 包（`SKILL.md`、`references/design-rules.md`、`assets/ui-kit/DESIGN.md`、`assets/ui-kit/manifest.json`），可作为实际输入；但须在 `docs/r6/design-map.md` 记录"skill 包 ↔ ZIP 哈希"的对应关系，未核验一致前不得宣称"已归档原件" |
| 4 | **依赖关系未在本项目 API 输出中体现**（工单仅有范围/方案/验收，无 blockedBy 字段返回） | 92 张票若无显式阻塞关系，并行开发极易在契约或存储层踩前置 | 本计划第 5 节给出按票面范围推导的依赖链，**须在派发前回填 Linear `blockedBy`**；本文件只作为回填底稿，不作为授权派发依据 |

> 说明：`R6-008` 检查清单第 1 条已要求"hash 一致、资料实际可取得"，因此第 3 项不是新增要求，而是必须在 M0 Gate 关闭前解决的前置。

---

## 2. 目标与完成定义

### 2.1 交付目标（来自 Rev2 §20）

以 v3.0.3 为唯一实现基线，完成五件事，且**按此顺序交付价值**：

1. **本机链路先修**：多文件导入可观察、可暂停/取消/恢复；普通搜索不再被 1.8 秒总超时误杀；已解析文件复用缓存。
2. **服务端链路**：≥1 GiB 单文件流式解析、分片索引、异步分页查询、显式私有上传。
3. **多根与采集**：部署目录外的多个物理根合并为逻辑总库；SFTP/SMB/HTTP(S)/传统 SCP 按真实能力接入。
4. **增量与实时**：归档事务、代次识别、轮转/缺口、断线补采、SSE 跟随。
5. **跨来源与发布**：混合范围筛选、2–4 文件时间对比、规则影响预览、原地升级与最终 ZIP。

### 2.2 十条红线（违反即视为未完成，不接受"下一版修"）

1. 本机导入默认不上传原文、文件名、搜索词或任务详情；服务端加速必须显式同意且落入当前身份私有区。
2. 原文是权威输入：只清理可再生索引，不因配额、暂停或缓存压力删除用户原文。
3. 读取 / 分帧 / 规则 / 可搜索 / 确认落盘分阶段报水位；未知总量不给百分比；部分结果不得显示为"共 N 条"。
4. 取消 ≠ 删除；上传完成 ≠ 索引完成；连接正常 ≠ 归档完成；暂停跟随 ≠ 暂停采集。
5. 记录身份用 `file_id + generation_id + record_id`，不用数组下标、文件名、大小或 mtime。
6. 19 位整数、纳秒、字节偏移不以浮点或 JS Number 传输；原始等级不被派生异常覆盖；`0 / false / null / 空串 / 缺失` 必须可区分。
7. 规则语义保持 JavaScript 单一实现，禁止 Python `re` 替代或双引擎分叉。
8. 保留 R5 已确认交互：六入口紧凑双列导航、toolbar 内阅读视角与时间范围、覆盖式悬浮详情、2–4 文件对比、四套阅读主题。
9. 新 API 一律 `/api/v2`，旧 `/api/v1` 语义不变；读操作逐请求复核 ACL，签名游标不是授权票据。
10. 没有原生 HTTP、真实协议、目标 Python 3.9.11 与双路 E5 证据，不得写"全部通过"；受阻必须写 `BLOCKED`。

---

## 3. 架构落点与模块所有权

| 落点 | 责任 | 拥有票 | 硬约束 |
|---|---|---|---|
| `src/shared/framer.js` | 流式分帧、检查点、跨块状态 | `009` | 浏览器与 Node 唯一实现；不写 Python 解析器 |
| `src/shared/rules.js` | 提取/异常/节点/证据 + 分层语义签名 | `011` | 复用 `LB4EngineFactory`；显示属性不入语义哈希 |
| `src/shared/query.js` | 条件归一化、精确匹配、marks | `012` | 布尔过滤与高亮分离；候选必须是命中集超集 |
| `src/storage/local-store.js` `index-store.js` `leases.js` `migrations.js` `backup.js` | IndexedDB 分块原文、索引块、租约、迁移、分卷备份 | `013`–`017` | IndexedDB 为基线，OPFS 仅增强；租约带 fencing token |
| `src/providers/local.js` `server.js` `combined.js` | ReaderProvider 三实现 | `019`、`045`、`078` | 方法/错误/取消/分页一致；Combined 不上传本机范围 |
| `src/workers/*` | 本机引擎 RPC、导入、查询、正则隔离 | `019`、`020`、`022`、`024` | 常驻；消息带 `request_id/query_version`，旧回包拒收 |
| `src/ui/*` | 任务浮层、搜索、高亮、阅读器、运行时记录 | `021`、`025`–`028`、`083` | 浮层不挤压阅读区；虚拟列表不因进度消息销毁 |
| `server/jobs/` | 持久任务、租约、优先级、公平性 | `033`、`034` | 每代次单一写所有者；恢复只信已提交水位 |
| `server/indexing/` | 分片 SQLite、Node 池、缓存版本 | `035`–`038` | 单写所有者、短事务、本地文件系统 |
| `server/query/` | 计划、候选、快照、分页、归并 | `042`–`044` | 游标绑定 ACL/代次/规则版本 |
| `server/uploads/` | 私有上传会话与分片 | `046`、`047` | 8 MiB 顺序提交，同偏移异内容 409 |
| `server/storage/` | 多根、挂载身份、归档代次 | `052`–`054`、`070`、`071` | 路径不互为祖先；离线不写入 |
| `server/collectors/` | SFTP/SMB/HTTP/SCP 适配 | `059`–`065` | `shell=False` 固定参数；逐跳准入 |
| `server/events/` | 复用事件流 | `074` | 一浏览器一条；超额降级轮询 |
| `src/node/` | 常驻解析/查询执行器 | `035` | 只接受控 IPC，不读任意路径 |

原则：新增能力按模块落在上表位置，**不继续堆进 `server/app.py`**；`server/frontend.py` 的脚本组装清单必须同步登记新文件，否则 `run.py` 的发布快照与预检会漏文件。

---

## 4. 执行编排：五个交付波次

10 个里程碑不按编号线性推进。按 Rev2 §20「本机痛点优先」与真实依赖，合并为 5 个波次：

| 波次 | 里程碑 | 票数（含 Gate） | 波次目标 | 出口 |
|---|---|---|---|---|
| **A 契约与量具** | M0 | 8（7 交付 + 1 Gate） | 冻结基线、公共契约、设计映射、夹具、旧链路计量、能力预检 | `R6-008` |
| **B 本机纵切（最高优先）** | M1 + M2 | 23（21 交付 + 2 Gate） | 本机大文件导入/搜索/阅读全链路可交付 | `R6-018`、`R6-031` |
| **C 服务端链路** | M3 + M4 | 20（18 交付 + 2 Gate） | ≥1 GiB 索引、异步查询、私有分片上传 | `R6-041`、`R6-051` |
| **D 采集与实时** | M5 + M6 + M7 | 26（23 交付 + 3 Gate） | 多根总库、四协议接入、归档与跟随 | `R6-058`、`R6-069`、`R6-077` |
| **E 收口与发布** | M8 + M9 | 15（14 交付 + 1 Gate） | 跨来源视图、规则/缓存管理、升级与最终验收 | `R6-084`、`R6-093`（待建） |

波次 A 是**唯一允许的全局串行段**；B 之后 C 与 D 的启动条件是"被依赖的契约已冻结"，不是"上一个波次全部 Done"。D 内部的 M5/M6/M7 三条支线可并行；E 的 M8 依赖 `019`/`045`/`044`，M9 依赖全部。

### 4.1 波次 A（M0）：三条并行准备线

| 线 | 顺序 | 说明 |
|---|---|---|
| 契约线 | `001` → `002` | `001` 锁源码/哈希/行为清单，`002` 才能冻结 `RecordRef/RecordHeader/Finding/TaskProgress/QuerySpec/QueryState/ReaderProvider`。`002` 是所有下游的唯一 schema 所有者 |
| 设计线 | `003` → `004` | `003` 先建 token 映射与 R5 视觉基线（否则 `004` 补原型没有度量基准）；`004` 只补 Rev2 新增交互（任务浮层、渐进搜索、私有上传），不重画已有页面 |
| 量具线 | `005` → `006` ∥ `007` | `005` 夹具先行，`006` 的旧链路诊断与 `007` 的能力预检都要用同一份语料/环境证据 |

Gate `008` 的关闭条件不是"七张票都 Done"，而是：哈希可复核、契约统一、原型覆盖 Rev2 新增、golden corpus 可复现、诊断区分克隆/计算/渲染、能力预检有可复跑证据。**任一项缺失即 FAIL，不得以"后续补"关闭。**

### 4.2 波次 B（M1 + M2）：首个可交付纵切

M1 内部三条泳道，`018` 收口后再进 M2 的 UI 层：

| 泳道 | 顺序 | 关键不变量 |
|---|---|---|
| 引擎 | `009` → `010`；`011` → `012` | 任意分块（UTF-8 / CRLF / Traceback / 异常链）与 R5 整文件解析结果字节级等价 |
| 存储 | `013` → `014` → `015` → `016` → `017` | 原文先提交再推检查点；旧 R5 库保留副本、逐文件迁移、可回退 |
| 查询 | `022` → `023` ∥ `024` → `025` → `026` | 候选剪枝零假阴性；正则外部可终止且不杀索引所有者 |

M2 的顺序关键点：

- `019`（LocalProvider）是 M2 全部票的前置，必须在 `013`–`015` 完成后开工。
- `020`（导入流水线）→ `021`（任务浮层）：先有真实流水线再接 UI，禁止先做假进度。
- `022`（常驻查询）必须在 `025`（搜索 UI）之前，否则 UI 只能绑旧 `runSearch`。
- `027`（阅读器分页）→ `028`（懒加载详情）保持 `029`（时间线）可并行。
- `030`（状态增量保存）放在最后，因为它依赖 `013` 的存储接口形态稳定。

Gate `031` 是 R6 的**第一个用户可感知交付**：5×20 MiB 与 10×100 MiB 在原生浏览器可完整走通，且普通搜索不再有 1.8 秒误杀。此 Gate 通过后可对外演示，但仍不等于服务端能力就绪。

### 4.3 波次 C（M3 + M4）：服务端

脊柱：`032` → `033` → `034` → `035` → `036` → `037` → `038` → `039` → `040` → Gate `041`。

可并行段：`046`（上传会话）与 `047`（分片落盘）可在 `042`–`045` 推进时并行开工，两者只依赖 `032` 的库模型与 `033` 的幂等动作框架。

`041` 是本项目第一个 `Dispatch·External-Block` Gate：**必须在真实 Python 3.9.11 + ≥1 GiB 语料 + 目标磁盘上产出**，开发机结果只能算辅助证据。

### 4.4 波次 D（M5 + M6 + M7）：三条支线并行

| 支线 | 顺序 | 并行边界 |
|---|---|---|
| 多根 | `052` → `053` → `054` → `055` → `056`；`057` 依赖 `052`/`054` | 与 M6 完全并行；`056` 的 UI 依赖 `054` 的目录 API |
| 协议 | `059` → `060` → {`061`∥`062`∥`063`} → `064`/`065` → `066` → `067`/`068` | `060` 是契约冻结点，四协议适配器在其后才可并行；`067`/`068` 只做 UI，不重定义能力矩阵 |
| 归档实时 | `070` → `071` → `072` → `073` → `074` → `075`；`076` 依赖 `034`/`070` | `070` 依赖 `053`（安全访问）；`073` 依赖 `036`/`037`（服务端解析与索引） |

关键约束（易被误排）：**M7 不能早于 M3 的 `036`/`037`**——增量解析与计数撤旧必须建立在已存在的分片索引与提交水位上；先做 SSE 会出现"事件流没有可发布水位"的返工。

### 4.5 波次 E（M8 + M9）：收口

- M8：`078`（CombinedProvider）依赖 `019` + `045` + `044`；`079`/`080` 依赖 `078`；`081` 依赖 `011` + `038`；`082`/`083` 依赖 `038` + `034`；Gate `084`。
- M9：`085` 依赖 `032`/`052`；`086` 依赖 `033`/`034`/`036`；`087` 依赖 `052`/`057`；`088` 依赖 `007`/`085`；`089`–`092` 是四条独立证据线，可并行，但**必须等前序全部产生可运行成品**。
- 新建 `093` Gate M9：汇总 `089`–`092` 的 PASS/FAIL/BLOCKED，作为唯一发布判据。

---

## 5. 依赖底稿（供回填 blockedBy）

```text
001 ─┬─ 002 ─┬─ 012 ─┬─ 022 ─┬─ 023 ─┐
     │       │       │       ├─ 024 ─┼─ 025 ─ 026
     │       │       │       └────────┘
     │       ├─ 009 ─ 010        │
     │       └─ 011 ─────────────┘
     ├─ 003 ─ 004
     ├─ 005 ─ 006
     └─ 007 ────────────────────────────── 088

013 ─ 014 ─ 015 ─ 016 ─ 017
        └──── 019 ─┬─ 020 ─ 021
                   ├─ 022（见上）
                   ├─ 027 ─ 028
                   ├─ 029
                   └─ 030

M3：032 ─ 033 ─ 034 ─ 035 ─ 036 ─ 037 ─ 038 ─ 039 ─ 040
M4：          033 ─ 042 ─ 043 ─ 044 ─ 045
              033 ─ 046 ─ 047 ─ 048
              044 ─ 049 ； 046~049 ─ 050

M5：052 ─ 053 ─ 054 ─ 055 ─ 056 ； 052/054 ─ 057
M6：059 ─ 060 ─ 061/062/063 ─ 064/065 ─ 066 ─ 067/068
M7：053 ─ 070 ─ 071 ─ 072 ─ 073 ─ 074 ─ 075 ； 034/070 ─ 076
M8：019/044/045 ─ 078 ─ 079/080 ； 011/038 ─ 081 ； 038/034 ─ 082/083
M9：032/052 ─ 085 ； 033/034/036 ─ 086 ； 052/057 ─ 087 ； 007/085 ─ 088
```

---

## 6. Gate 判据总表

统一判定：每项只能填 `PASS` / `FAIL` / `BLOCKED`，`BLOCKED` 必须附原因与解锁条件。**Gate 的 Done 由证据文件决定，不由子票 Done 数量决定。**

| Gate | 票 | 必须成立的最小集合 | 证据文件 | 外部依赖 |
|---|---|---|---|---|
| M0 | `008` | 哈希一致；`002` 契约统一；`004` 覆盖 Rev2 新增；`005` 语料可复现；`006` 区分克隆/计算/渲染；`007` 预检可复跑 | `qa/evidence/M0-gate.md` | 否 |
| M1 | `018` | 任意分块与 R5 差分一致；原文不丢；租约 fencing 有效；旧库迁移可回退；分卷备份可校验 | `qa/evidence/M1-gate.md` | 否（需原生 HTTP） |
| M2 | `031` | 5×20 MiB / 10×100 MiB 可操作；真实水位；暂停取消恢复；无 1.8 秒误杀；局部渲染保持锚点 | `qa/evidence/M2-gate.md` | 否 |
| M3 | `041` | ≥1 GiB 流式索引；重复打开零全量重解析；崩溃只从提交水位恢复；预算背压有效 | `qa/evidence/M3-gate.md` | **是**（Python 3.9.11 + 目标磁盘） |
| M4 | `051` | 私有上传→索引→查询→下载端到端隔离；分片幂等；越权读计数被拒 | `qa/evidence/M4-gate.md` | 否 |
| M5 | `058` | 多物理根登记；离线不写入；路径不逃逸；目录继承与恢复 | `qa/evidence/M5-gate.md` | 需真实挂载 |
| M6 | `069` | SFTP/SMB/HTTP/SCP 实连预检；能力降级可见；配置 revision 绑定 | `qa/evidence/M6-gate.md` | **是**（真实协议端点） |
| M7 | `077` | 崩溃点注入；同代次不混写；轮转/截断/缺口可见；跟随不暂停采集 | `qa/evidence/M7-gate.md` | **是**（可制造故障的源） |
| M8 | `084` | 混合范围筛选不上传本机；对比时间轴对齐；规则影响预览；管理页绑真实 API | `qa/evidence/M8-gate.md` | 否 |
| M9 | 待建 `093` | 升级回退；备份恢复；安全回归；视觉/键盘/响应式回归；原生持久化；性能基准 | `qa/evidence/M9-gate.md` | **是**（4 项） |

---

## 7. 设计规范落点（unify-design v1.0.1 × R6）

### 7.1 令牌映射（`R6-003` 的产出契约）

| 语义 | 取值 | R6 用法 | 禁止 |
|---|---|---|---|
| 页面底 / 主表面 / 次表面 | `#f5f6f8` / `#ffffff` / `#f9fafb` | 工作区、数据面、工具条分区 | 层层白卡嵌套 |
| 边框 普通 / 强分隔 | `#e4e7ec` / `#d0d5dd` | 表格行、分区 | 用浅边框代替控件边界 |
| 文字 主 / 次 / 辅助 | `#101828` / `#344054` / `#5d6676` | 正文、元数据、提示 | 浅灰当正文 |
| 主操作 / hover / 浅选中 | `#2563eb` / `#1d4ed8` / `#eef4ff` | 主按钮、当前选中行 | 装饰性上蓝 |
| 成功 / 危险 / 警告 / 绑定 | `#16a34a` `#dc2626` `#d97706` `#7c3aed` | 状态徽标、差异标记 | 颜色单独承载含义 |
| 间距 | 4 / 8 / 12 / 16 / 20 / 24 / 32 | 全部新增容器 | 任意像素 |
| 圆角 | 6 默认 / 4 紧凑 / 8 面板 | 控件、行、面板 | pill 用于非状态 |
| 字号 | 正文 13–14 / 辅助 12 / 小标题 16 / 页标题 ≤20 | 全部新增 UI | 主体降到 10–11px |
| 控件密度 | 控件 32 / 单行 28 / 触屏 44 | toolbar、表格、任务行 | 固定行高裁掉多行内容 |
| 动效 | 120 / 180 ms，`prefers-reduced-motion` 全关 | 浮层、状态切换 | 与任务无关的入场/漂浮 |

**R6 增量约束**（优先级高于 kit 通用示例）：四套阅读主题与暖纸高对比保留；详情/任务浮层为覆盖式，打开前后主表与时间线的 boundingBox 不变；时间范围不独占一行；六入口双列导航；不引入外部字体、CDN 图标、第二套表单/弹层体系；**kit 数据表不得替换现有虚拟日志表**。

### 7.2 新增组件与状态契约

| 组件 | 归属票 | 复用/变体 | 必交状态 |
|---|---|---|---|
| 任务入口徽标（`处理 2/5`） | `021` | 现有 toolbar 按钮变体 | idle / running / paused / partial_error / done |
| 任务浮层（逐文件阶段水位） | `021` | 悬浮详情抽屉的覆盖层语言 | queued / running / waiting_input / waiting_storage / failed / cancelled |
| 搜索进行中横条 | `025` | toolbar 内联提示 | 新条件进行中 / 旧结果可见 / 预算暂停可续 |
| 覆盖率与计数状态 | `025`、`079` | 纯文本 + 数值 | `exact` / `at_least` / `unknown` 三种文案 |
| 私有上传进度 | `048` | 任务浮层同构 | 网络已发送 / 服务器已确认 / 续传中 / 冲突 |
| 存储位置与源列表 | `056`、`068` | 现有文件库列表 | 在线 / 离线保留登记 / 预检失败 |
| 解析缓存管理 | `082` | 管理页表格 | 各层版本 / 覆盖水位 / 可清理项 |
| 运行记录与预算 | `083` | 管理页 | 真实指标 / 不可用项显式置灰并说明 |

**文案硬规则**：`at_least` 不得写成"共 N 条"；`partial` 不得写成"0 条"；未索引不得写成"无匹配"；上传完成不得写成"索引完成"；关闭浮层不得写成"取消任务"。

### 7.3 21 张 UI 票的统一证据要求

每张 `验收·视觉模型` 票必须附：

1. 同一固定 fixture、同一状态下的实现截图，以及必要时的 R5/原型对照；
2. 至少 `1366×768`、`1920×1080` 桌面与 `375×812`、`768×1024`、`1024×768` 窄屏；
3. 量测数据：核心数据区高度、toolbar 行数、浮层前后 boundingBox、虚拟记录 DOM 数量、原文锚点偏移（≤2px）；
4. 键盘路径：Tab 顺序、Esc 关闭、返回焦点、表单错误可达；
5. 状态矩阵：loading / empty / partial / error / disabled / success / 长内容 / 只读；
6. 四条反例自查（见 7.4）。

### 7.4 反例清单（出现即视为缺陷）

- 侧栏重新出现"阅读视角"，或时间范围按钮独占一行。
- 打开详情/任务浮层导致日志表或时间线位移、宽度变化。
- 用 `innerHTML` 插入日志原文；用颜色单独表达严重度。
- 计时器驱动的假进度；关闭面板即停止任务；取消即删除原文。
- 50,000 条逻辑页被整批挂载为 DOM。
- 管理页展示模拟计数或"全部成功"，而后端未提交。

---

## 8. 证据与工件规范

| 类型 | 位置 | 说明 |
|---|---|---|
| 契约 | `docs/r6/contracts.md`、`contracts/r6/*.schema.json`、`tests/fixtures/r6/contracts/` | `002` 产出，全下游共用 |
| 设计映射 | `docs/r6/design-map.md`、`qa/r6/visual_baseline.py` | `003` 产出 |
| 原型 | `docs/prototypes/r6-rev2.html`、`docs/r6/prototype-coverage.md` | `004` 产出；模拟状态须显式标注 |
| 语料 | `tests/fixtures/r6/golden/`、`tools/generate_log_fixture.py` | 固定 seed，manifest 含 size/hash/行数/时间域 |
| 基线 | `docs/r6/baseline.md`、`docs/reference/source-manifest.json` | `001` 产出 |
| 浏览器脚本 | `qa/r6/<场景>.py --base-url --out` | 真实导航、真实 Worker、真实 HTTP；`set_content` 只能做局部视觉 |
| Gate 证据 | `qa/evidence/M#-gate.md` | 逐项 PASS/FAIL/BLOCKED + 命令 + 版本 + 证据路径 |
| 票级证据 | `qa/evidence/R6-0xx.md` | 变更文件、测试命令与退出码、输入规模、关键断言 |
| 性能 | `tools/bench_r6.py`、`qa/r6/perf_browser.py` | 冷/暖缓存分开，≥30 次采样报 p50/p95 |

命令约定：

```bash
python3.9 -m unittest discover -s tests -p 'test_*.py' -v
node --test tests/*.test.cjs tests/r6/*.test.cjs
python qa/r6/<scene>.py --base-url http://<host>:50005 --out qa/evidence/<id>
```

判定约定：旧版本测试数量、原型截图、Linear Done 均不能代替本轮证据；未取得目标环境的项写 `BLOCKED` 并保留原因。

---

## 9. 风险登记

| # | 风险 | 触发条件 | 缓解（对应票） |
|---|---|---|---|
| R1 | 目标机无 Node / rclone / OpenSSH | `007` 预检失败 | 新能力置 `disabled + reason`，本机模式与旧 API 必须仍可用；不得自动下载安装 |
| R2 | SQLite 构建含 WAL-reset 竞态且无法确认已回补 | `sqlite3.sqlite_version` 未知 | 默认 rollback journal；`007` 判定，`037` 落实；不得以"Python 3.9.11 能启动"代替 |
| R3 | 私网 HTTP 非安全上下文导致 OPFS 不可用 | `isSecureContext=false` | IndexedDB 为必需基线；`091` 必须给出 HTTP 基线证据 |
| R4 | 双路 E5 / 真实协议端点 / 原生浏览器档案不可及时获得 | `041`、`069`、`077`、`091`、`092` | 先交付脚本与 `BLOCKED` 记录；不得用开发机数据代签；把环境申请前置到波次 A |
| R5 | 1.8 秒超时与整批 `records[]` 克隆的真实成本未被量化就"优化" | `006` 缺失 | `006` 先建分段计量基线；性能结论必须区分克隆/计算/渲染 |
| R6 | 双引擎分叉（`src/engine.js` 与 `src/shared/*` 各自演化） | `011` 复制而非抽取 | 单一实现 + golden 差分；`018` Gate 强制校验 |
| R7 | 热更新时任务与租约未排空，检查点超前于落盘 | `086` 未覆盖 | 切换顺序：停领新任务 → 有界提交 → 存检查点 → 释放租约 → 换进程 |
| R8 | IndexedDB 配额或 R5 迁移丢数据 | `016`/`017` 简化处理 | 保留旧副本、逐文件验证、分卷备份与校验后恢复 |

---

## 10. 派发顺序建议

不设日期与工时（项目约束）。以下为"可立即派发"的顺序，前提是第 1.1 节 4 项已处理或已登记：

| 批 | 并行票 | 前置 |
|---|---|---|
| 1 | `001` | 无（唯一串行起点） |
| 2 | `002`、`003`、`005` | `001` |
| 3 | `004`、`006`、`007` | `002`/`003`（004）、`005`（006） |
| 4 | `008` Gate | 批次 2–3 全部产出 |
| 5 | `009`、`011`、`013` | `008` 关闭 |
| 6 | `010`、`012`、`014` | 批次 5 对应票 |
| 7 | `015`、`016`、`017` | `014`/`013` |

同时启动的**非开发**准备：申请双路 E5 与 Python 3.9.11 目标环境、确认一个可实连的 SFTP/SMB/HTTP 测试源、准备独立浏览器档案（供 `091` 使用，不得清理用户真实档案）。

---

## 11. 立即可执行的三件事

1. **补 Gate M9（`093`）并修正 M0 统计口径**：让"R6 发布"有唯一出口，里程碑完成率不再被参考资料票污染。
2. **回填 92 张票的真实 `blockedBy`**：以第 5 节为底稿逐条核对，契约票（`002`/`012`/`060`）的下游必须显式挂接。
3. **启动波次 A 的三条线**：`001`（基线）串行开工；`003`（设计映射）与 `005`（夹具）可同时进行，前者直接复用本机 `unify-design` v1.0.1 skill 包，后者不依赖任何设计决定。

---

## 附录 A · 92 张任务索引

### M0 · 基线、契约与设计目标

| 票 | 任务 | 类型 | 主要落点 / 交付物 |
|---|---|---|---|
| R6-001 | [INH-2244](https://linear.app/inhandy/issue/INH-2244) 固定 v3.0.3 源码基线、资料校验与既有行为清单 | 实现 | `docs/r6/baseline.md` |
| R6-002 | [INH-2245](https://linear.app/inhandy/issue/INH-2245) 冻结记录、任务、查询与 ReaderProvider 公共契约 | 契约冻结 | `docs/r6/contracts.md` |
| R6-003 | [INH-2246](https://linear.app/inhandy/issue/INH-2246) 建立 unify-design 令牌映射与 R5 视觉回归基线 | UI | `docs/r6/design-map.md` |
| R6-004 | [INH-2247](https://linear.app/inhandy/issue/INH-2247) 补齐 Rev2 导入、查询和私有上传的原型目标 | UI | `docs/prototypes/r6-rev2.html` |
| R6-005 | [INH-2248](https://linear.app/inhandy/issue/INH-2248) 建立解析与匹配 golden corpus 和大文件夹具生成器 | 实现 | — |
| R6-006 | [INH-2249](https://linear.app/inhandy/issue/INH-2249) 建立旧导入/搜索分段诊断与隐私安全性能指标 | 实现 | `src/shared/diagnostics.js` |
| R6-007 | [INH-2250](https://linear.app/inhandy/issue/INH-2250) 实现 Python/Node/rclone/SQLite 与浏览器能力预检 | 实现 | `server/capabilities.py` |
| R6-008 | [INH-2251](https://linear.app/inhandy/issue/INH-2251) Gate M0：基线、契约、原型补全与测量可复现 | Gate/验收 | `qa/evidence/M0-gate.md` |

### M1 · 公共引擎与本机持久索引

| 票 | 任务 | 类型 | 主要落点 / 交付物 |
|---|---|---|---|
| R6-009 | [INH-2252](https://linear.app/inhandy/issue/INH-2252) 抽取公共流式分帧器与精确字节/行检查点 | 实现 | `src/shared/framer.js` |
| R6-010 | [INH-2253](https://linear.app/inhandy/issue/INH-2253) 流式 Traceback 聚合、临时尾部与稳定记录修订 | 实现 | `src/shared/traceback.js` |
| R6-011 | [INH-2254](https://linear.app/inhandy/issue/INH-2254) 抽取共用规则引擎、证据快照与分层语义签名 | 实现 | `src/shared/rules.js` |
| R6-012 | [INH-2255](https://linear.app/inhandy/issue/INH-2255) 统一查询 AST、搜索投影版本与精确匹配语义 | 契约冻结 | `src/shared/query.js` |
| R6-013 | [INH-2256](https://linear.app/inhandy/issue/INH-2256) 实现身份隔离的 IndexedDB 分块原文与元数据存储 | 数据迁移 | `src/storage/local-store.js` |
| R6-014 | [INH-2257](https://linear.app/inhandy/issue/INH-2257) 实现本机结构、规则与检索块的持久提交和版本发布 | 实现 | `src/storage/index-store.js` |
| R6-015 | [INH-2258](https://linear.app/inhandy/issue/INH-2258) 实现跨标签页租约、fencing 与本机任务恢复检查点 | 数据迁移 | — |
| R6-016 | [INH-2259](https://linear.app/inhandy/issue/INH-2259) 渐进迁移 R5 本机缓存并实现元数据优先恢复 | 数据迁移 | `src/storage/migrations.js` |
| R6-017 | [INH-2260](https://linear.app/inhandy/issue/INH-2260) 实现大工作区分卷备份与校验后恢复 | 数据迁移 | `src/storage/backup.js` |
| R6-018 | [INH-2261](https://linear.app/inhandy/issue/INH-2261) Gate M1：公共引擎一致性与本机存储故障安全 | Gate/验收 | — |

### M2 · 本机大文件导入、搜索与阅读

| 票 | 任务 | 类型 | 主要落点 / 交付物 |
|---|---|---|---|
| R6-019 | [INH-2262](https://linear.app/inhandy/issue/INH-2262) 实现 LocalProvider 与常驻本机引擎 RPC | 实现 | `src/providers/local.js` |
| R6-020 | [INH-2263](https://linear.app/inhandy/issue/INH-2263) 实现有界批量导入流水线、背压和暂停恢复 | 实现 | `src/workers/import-jobs.js` |
| R6-021 | [INH-2264](https://linear.app/inhandy/issue/INH-2264) 实现本机导入入口与非阻塞任务浮层 | UI | — |
| R6-022 | [INH-2265](https://linear.app/inhandy/issue/INH-2265) 实现常驻本机查询、紧凑结果与渐进覆盖状态 | 实现 | `src/workers/query-jobs.js` |
| R6-023 | [INH-2266](https://linear.app/inhandy/issue/INH-2266) 实现本机元数据索引与零漏报的块级候选剪枝 | 实现 | — |
| R6-024 | [INH-2267](https://linear.app/inhandy/issue/INH-2267) 隔离危险正则并实现外部终止与恢复 | 安全 | — |
| R6-025 | [INH-2268](https://linear.app/inhandy/issue/INH-2268) 接通渐进搜索 UI、IME、取消续查与明确结果状态 | UI | `src/ui/search-controller.js` |
| R6-026 | [INH-2269](https://linear.app/inhandy/issue/INH-2269) 实现仅可见记录高亮与多规则重叠命中缓存 | UI | — |
| R6-027 | [INH-2270](https://linear.app/inhandy/issue/INH-2270) 将日志阅读器改为 Provider 分页与局部更新虚拟列表 | UI | `src/ui/reader-controller.js` |
| R6-028 | [INH-2271](https://linear.app/inhandy/issue/INH-2271) 接入懒加载详情/原文/多行卡片并保持阅读锚点 | UI | — |
| R6-029 | [INH-2272](https://linear.app/inhandy/issue/INH-2272) 时间线改用索引分页、覆盖率和稳定位置定位 | UI | — |
| R6-030 | [INH-2273](https://linear.app/inhandy/issue/INH-2273) 增量保存阅读视角、偏好、书签与大集合状态 | 数据迁移 | `src/storage/workspace.js` |
| R6-031 | [INH-2274](https://linear.app/inhandy/issue/INH-2274) Gate M2：本机 5×20MiB / 10×100MiB 导入搜索纵切 | Gate/UI/验收 | — |

### M3 · 服务端调度、解析池与缓存

| 票 | 任务 | 类型 | 主要落点 / 交付物 |
|---|---|---|---|
| R6-032 | [INH-2275](https://linear.app/inhandy/issue/INH-2275) 增量扩展控制库模型与兼容迁移基础 | 数据迁移 | — |
| R6-033 | [INH-2276](https://linear.app/inhandy/issue/INH-2276) 实现持久任务状态机、租约与幂等控制 API | 安全 | `server/jobs/repository.py/service.py` |
| R6-034 | [INH-2277](https://linear.app/inhandy/issue/INH-2277) 实现全局资源预算、优先级、公平调度与背压 | 实现 | — |
| R6-035 | [INH-2278](https://linear.app/inhandy/issue/INH-2278) 实现常驻 Node 解析/查询进程池与有界 IPC | 安全 | `server/indexing/node_pool.py` |
| R6-036 | [INH-2279](https://linear.app/inhandy/issue/INH-2279) 实现服务端原文范围读取与流式解析检查点流水线 | 实现 | `server/indexing/pipeline.py` |
| R6-037 | [INH-2280](https://linear.app/inhandy/issue/INH-2280) 实现分片 SQLite 结构索引、批量写入和精确定位 | 实现 | `server/indexing/shards.py/writer.py` |
| R6-038 | [INH-2281](https://linear.app/inhandy/issue/INH-2281) 实现服务端分层缓存、版本重建与安全发布切换 | 实现 | — |
| R6-039 | [INH-2282](https://linear.app/inhandy/issue/INH-2282) 实现多文件并行与完整记录批次的有序归并 | 实现 | — |
| R6-040 | [INH-2283](https://linear.app/inhandy/issue/INH-2283) 提供授权记录详情、原文范围、时间线与索引状态 API | 安全 | `server/indexing/read_api.py` |
| R6-041 | [INH-2284](https://linear.app/inhandy/issue/INH-2284) Gate M3：真实 Python 3.9.11 的 1GiB 索引与缓存恢复 | Gate/外部证据/验收 | `qa/r6/server_index_gate.py` |

### M4 · 服务端查询与私有分片上传

| 票 | 任务 | 类型 | 主要落点 / 交付物 |
|---|---|---|---|
| R6-042 | [INH-2285](https://linear.app/inhandy/issue/INH-2285) 实现异步查询 API、固定快照与授权游标 | 安全 | — |
| R6-043 | [INH-2286](https://linear.app/inhandy/issue/INH-2286) 实现服务端 B-tree/FTS 候选规划与精确语义回退 | 实现 | `server/query/planner.py` |
| R6-044 | [INH-2287](https://linear.app/inhandy/issue/INH-2287) 实现服务端查询执行、严格归并、计数与预算续查 | 实现 | — |
| R6-045 | [INH-2288](https://linear.app/inhandy/issue/INH-2288) 实现 ServerProvider 并替换服务器整文件下载解析 | 实现 | — |
| R6-046 | [INH-2289](https://linear.app/inhandy/issue/INH-2289) 实现私有上传会话、身份归属、配额与保留策略 | 安全 | `server/uploads/sessions.py` |
| R6-047 | [INH-2290](https://linear.app/inhandy/issue/INH-2290) 实现流式分片上传、落盘确认、幂等续传与封存 | 安全 | — |
| R6-048 | [INH-2291](https://linear.app/inhandy/issue/INH-2291) 实现显式服务端加速、私有上传进度与断点恢复 UI | UI/安全 | — |
| R6-049 | [INH-2292](https://linear.app/inhandy/issue/INH-2292) 实现按查询快照的流式导出与权限校验 | 安全 | — |
| R6-050 | [INH-2293](https://linear.app/inhandy/issue/INH-2293) 验证私有上传、索引、查询和下载的端到端隔离 | 安全 | — |
| R6-051 | [INH-2294](https://linear.app/inhandy/issue/INH-2294) Gate M4：私有上传到索引查询的真实前后端闭环 | Gate/验收 | `qa/r6/private_acceleration_gate.py` |

### M5 · 多存储根与统一文件库

| 票 | 任务 | 类型 | 主要落点 / 交付物 |
|---|---|---|---|
| R6-052 | [INH-2295](https://linear.app/inhandy/issue/INH-2295) 实现多存储根登记 API 与 legacy 根兼容映射 | 数据迁移 | — |
| R6-053 | [INH-2296](https://linear.app/inhandy/issue/INH-2296) 实现根目录安全访问、挂载身份与离线写保护 | 安全 | — |
| R6-054 | [INH-2297](https://linear.app/inhandy/issue/INH-2297) 实现跨存储根逻辑目录、文件代次与懒加载目录 API | 实现 | — |
| R6-055 | [INH-2298](https://linear.app/inhandy/issue/INH-2298) 扩展单文件/目录/映射默认规则与继承来源解释 | 实现 | — |
| R6-056 | [INH-2299](https://linear.app/inhandy/issue/INH-2299) 按 R6 原型实现统一文件库与存储位置管理页面 | UI | — |
| R6-057 | [INH-2300](https://linear.app/inhandy/issue/INH-2300) 扩展多根文件回收恢复与活动引用保护 | 安全/数据迁移 | — |
| R6-058 | [INH-2301](https://linear.app/inhandy/issue/INH-2301) Gate M5：多物理根、目录继承和离线恢复验收 | Gate/验收 | `qa/r6/multi_root_gate.py` |

### M6 · 多协议接入与采集中心

| 票 | 任务 | 类型 | 主要落点 / 交付物 |
|---|---|---|---|
| R6-059 | [INH-2302](https://linear.app/inhandy/issue/INH-2302) 实现远程凭据引用、主机准入与密钥/证书校验 | 安全 | — |
| R6-060 | [INH-2303](https://linear.app/inhandy/issue/INH-2303) 定义并实现受控传输适配器骨架与能力探测合同 | 契约冻结 | — |
| R6-061 | [INH-2304](https://linear.app/inhandy/issue/INH-2304) 实现 SSH/SFTP 目录发现与受校验范围读取 | 安全 | — |
| R6-062 | [INH-2305](https://linear.app/inhandy/issue/INH-2305) 实现 SMB2/3 共享发现、增量读取与重连校验 | 安全 | — |
| R6-063 | [INH-2306](https://linear.app/inhandy/issue/INH-2306) 实现 HTTP(S) Range、验证器与安全快照读取 | 安全 | — |
| R6-064 | [INH-2307](https://linear.app/inhandy/issue/INH-2307) 实现 HTTP 目录清单、显式 URL 与代次 manifest 发现 | 安全 | — |
| R6-065 | [INH-2308](https://linear.app/inhandy/issue/INH-2308) 实现传统 SCP 稳定快照兼容与明确能力降级 | 安全 | `server/collectors/scp_snapshot.py` |
| R6-066 | [INH-2309](https://linear.app/inhandy/issue/INH-2309) 实现远程源、多目录映射、策略与版本化预检 API | 安全 | — |
| R6-067 | [INH-2310](https://linear.app/inhandy/issue/INH-2310) 按 R6 HTML 实现连接—映射—策略—预检四步向导 | UI | — |
| R6-068 | [INH-2311](https://linear.app/inhandy/issue/INH-2311) 实现采集中心源列表、详情、动作与真实状态展示 | UI | — |
| R6-069 | [INH-2312](https://linear.app/inhandy/issue/INH-2312) Gate M6：真实协议能力预检与采集配置流程 | Gate/外部证据/验收 | — |

### M7 · 增量归档、续采与实时事件

| 票 | 任务 | 类型 | 主要落点 / 交付物 |
|---|---|---|---|
| R6-070 | [INH-2313](https://linear.app/inhandy/issue/INH-2313) 实现原文归档事务、确认区间与崩溃恢复 | 数据迁移 | — |
| R6-071 | [INH-2314](https://linear.app/inhandy/issue/INH-2314) 实现文件代次、轮转、截断、替换与缺口识别 | 实现 | — |
| R6-072 | [INH-2315](https://linear.app/inhandy/issue/INH-2315) 实现持久采集调度、增量追踪、退避与断线补采 | 实现 | — |
| R6-073 | [INH-2316](https://linear.app/inhandy/issue/INH-2316) 接通归档增量解析、尾部修订与索引计数撤旧更新 | 实现 | — |
| R6-074 | [INH-2317](https://linear.app/inhandy/issue/INH-2317) 实现授权复用 SSE、有限回放与轮询降级 | 安全 | — |
| R6-075 | [INH-2318](https://linear.app/inhandy/issue/INH-2318) 实现实时跟随、增量节点与采集运行状态联调 | UI | — |
| R6-076 | [INH-2319](https://linear.app/inhandy/issue/INH-2319) 实现磁盘压力背压、缓存回收与保留策略保护 | 安全 | — |
| R6-077 | [INH-2320](https://linear.app/inhandy/issue/INH-2320) Gate M7：归档、续采、轮转与实时阅读故障验收 | Gate/外部证据/验收 | — |

### M8 · 跨来源视图、规则与缓存管理

| 票 | 任务 | 类型 | 主要落点 / 交付物 |
|---|---|---|---|
| R6-078 | [INH-2321](https://linear.app/inhandy/issue/INH-2321) 实现本机/私有/共享混合范围的 CombinedProvider | 安全 | — |
| R6-079 | [INH-2322](https://linear.app/inhandy/issue/INH-2322) 实现跨来源全局时间筛选与可保存范围视角 | UI | — |
| R6-080 | [INH-2323](https://linear.app/inhandy/issue/INH-2323) 实现 2–4 文件按时间窗口对齐与同步对比 | UI | — |
| R6-081 | [INH-2324](https://linear.app/inhandy/issue/INH-2324) 扩展规则管理的影响预览、分层重算与历史命中证据 | UI | — |
| R6-082 | [INH-2325](https://linear.app/inhandy/issue/INH-2325) 按 R6 模板实现解析缓存与重建管理页面 | UI | — |
| R6-083 | [INH-2326](https://linear.app/inhandy/issue/INH-2326) 实现运行记录、资源预算与脱敏诊断管理 | UI/安全 | — |
| R6-084 | [INH-2327](https://linear.app/inhandy/issue/INH-2327) Gate M8：跨来源筛选、时间对比、规则与管理 UI | Gate/UI/验收 | — |

### M9 · 升级、备份、安全与发布验收

| 票 | 任务 | 类型 | 主要落点 / 交付物 |
|---|---|---|---|
| R6-085 | [INH-2328](https://linear.app/inhandy/issue/INH-2328) 实现从 v3.0.3 原地升级与兼容回退检查 | 数据迁移 | — |
| R6-086 | [INH-2329](https://linear.app/inhandy/issue/INH-2329) 扩展覆盖热更新的进程排空、检查点与发布清单 | 数据迁移 | — |
| R6-087 | [INH-2330](https://linear.app/inhandy/issue/INH-2330) 扩展多存储根、私有区与检查点备份恢复工具 | 安全/数据迁移 | — |
| R6-088 | [INH-2331](https://linear.app/inhandy/issue/INH-2331) 制作低依赖部署包、版本校验与运维说明 | 实现 | — |
| R6-089 | [INH-2332](https://linear.app/inhandy/issue/INH-2332) 完成全链路权限、输入边界与秘密泄漏安全回归 | 安全/验收 | — |
| R6-090 | [INH-2333](https://linear.app/inhandy/issue/INH-2333) 完成 R5/R6 模板对照的视觉、键盘与响应式回归 | UI/验收 | `qa/r6/visual_regression.py` |
| R6-091 | [INH-2334](https://linear.app/inhandy/issue/INH-2334) 完成原生浏览器持久化、双标签与升级重开验收 | 外部证据/验收 | `qa/r6/native_persistence.py` |
| R6-092 | [INH-2335](https://linear.app/inhandy/issue/INH-2335) 完成双路 E5 与办公客户端的大文件性能基准 | 外部证据/验收 | `tools/bench_r6.py` |
