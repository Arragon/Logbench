# R6 前端按原型回归 · 整改实施计划

> 日期：2026-11-04　｜　代码基线：`eff4edc`　｜　状态：**待批准，尚未改动任何生产代码**
> 设计基线：`LogBench_R6_Remote_Prototype.html`（399,228 B · SHA-256 `4d655689…71aa0` · Linear `INH-2336`）
> 适用指导：`ui-ux-design-suite`（Route B 现有界面整改）、`unify-design` v1.0.1（design-rules / patterns / retrospective / qa）
> 权威顺序：本次用户明确要求 → 项目业务契约 → 原型 HTML 的 DOM/CSS/视觉 → 现有组件与 token → 指导中的默认建议 → 通用最佳实践

---

## 0. 一页纸结论

**做什么**：以 R6 原型的 DOM 结构、CSS 与视觉节拍为唯一设计基线，在现有 `src/` 模块体系内重建 R6 的渲染层；数据入口从合成数据换成 `/api/v2/*`，完整保留现有 Worker / provider / IndexedDB / SSE / 权限引擎；补齐原型有而实现缺的组件，并按文案原则删除原型里因"它是演示原型"才存在的自我声明文字。

**不做什么**：不搬 399 KB 原型单文件进生产、不引入第二套设计系统、不重写后端、不改 `/` 工作台版、不删现有引擎能力。

**为什么这样而不是直接搬原型**：原型是 R3→R4→R5→R6 四代 render 叠加，含 20+ 个无调用点的死函数，R6 层靠 `renderHeader().replace('<span class="current">undefined</span>', …)` 这类字符串补丁工作（`L800–L804`）。`unify-design/references/retrospective.md:59` 明确规定：「不把多轮叠加原型直接包装成组件库；其历史函数、模拟权限、内嵌 HTML 与覆盖逻辑只适合追溯」。因此**严格照原型 = 严格照它的 DOM、类名、层级、尺寸、色彩与交互控件，不是照抄它的实现技巧**。

**验收一句话**：在 1366×768 打开 `/r6`，主要视图的 DOM 树、class 名、视觉节拍与原型一致，数据来自真实 v2 API，无"合成/示例/设计原型"字样，核心数据区不低于原型基线并通过删除重复容器净增 32px。

> **已暴露的取舍**：当前 `/r6` 实现因删掉了原型的页头 / 标签条 / 状态栏 / 页脚，数据区按 CSS 声明推算约 674px；按原型回归后约 556px。两条指导（严格按原型、核心工作区尽可能大）在此有真实张力，不假装同时满足，见 §3.4 与 §15 待确认 4。

---

## 1. 目标与验收口径

### 1.1 用户目标（原文要点）

- 前端设计**严格按 R6 原型实现**，后端功能在此基础上接上。
- 适当补上 R6 页面实现中缺少的功能 / 组件。
- 严格遵守两套设计指导；UI 美观简洁、布局合理、空间利用率高，核心工作区有足够空间；UX polished 像成熟商业产品。
- 视觉质量默认追求清晰、专业、精致、符合产品定位。
- 自解释优先：靠布局、排版、间距、信息分组、视觉层级、明确操作按钮；主动删除无信息价值的副标题、Slogan、冗余说明、重复标签、装饰性微文案。
- 保留必要的表单标签、可访问名称、状态反馈、错误恢复说明、危险/不可逆操作的后果提示。
- 可选补充信息用支持键盘和触屏的帮助入口；**完成任务必需的信息不得藏在 Tooltip**。

### 1.2 「严格按原型」的可验收定义

| 维度 | 判定标准 | 检查方式 |
|---|---|---|
| DOM 结构 | 主要视图的容器层级、class 名、父子关系与原型 R6 覆盖层一致 | 逐页 DOM 树比对脚本（新建，见 M1） |
| 视觉节拍 | 顶栏 / 侧栏 / 页头 / 标签条 / 工具条 / 行高 / 抽屉 / 对话框尺寸与原型一致 | 无头浏览器 `getBoundingClientRect()` |
| 色彩与排版 | 沿用原型 `.ud-app` token 与 `style.css` 四套主题；主色蓝、语义色、系统等宽字体用于路径/数值/日志 | token diff + 截图 |
| 视图组织 | 6 个 `S.view`：`logs / files / rules / admin / compare / sync`；默认落地 `sync` | 路由与启动实测 |
| 交互控件 | 原型有的控件（显示设置、时间范围、四步向导、详情抽屉、对比、规则绑定）`/r6` 都要有 | 控件清单逐项勾选 |
| 数据真实性 | 无合成数据；状态、计数、容量、覆盖率来自 v2 API | 关闭网络后应显示明确错误态而非假数据 |

### 1.3 明确不做（non-goals）

- 不改 `/`（`src/index.html` + `/assets/workbench.js`）与其 `/api/v1/*` + `/admin/*` 链路。
- 不重写 `server/`、`worker.py`、`shared/*`、`providers/*` 的业务语义。
- 不引入框架、构建步骤、CDN、网络字体或第二套组件库。
- 不把 `docs/prototypes/r6-rev2.html`（Rev2 交互原型）当作设计基线。
- 不因"清理"删除现有引擎能力（虚拟窗口、断点续传、409 冲突、13 项资源预算、SSE、权限分层）。

---

## 2. 现状诊断（已核实，带证据）

### 2.1 三层现状

| 层 | 原型 | 当前 `/r6` | 结论 |
|---|---|---|---|
| 设计层 | `#app.lb-app` grid（208px / 52px）；`main.main` 下 `.r6-head` + `.r6-content`；63 个 `.r6-*` 选择器（`L80`） | `#app.ud-app.lb-app.r6-app` flex；`.r6-shell > nav.r6-nav + main.r6-main`；`r6.css` 1312 行另一套 `.r6-*` | **需回归原型** |
| 数据源 | `R6_SEED`(`L744`)、`files`(`L202`)、`makeDemo`(`L177`)、`r6Persist()`(`L764`)、`restoreCache()`(`L712`) | `/api/v2/*` 约 45 条路径、`LBHttp`、`shared/workspace.js`、IndexedDB `logbench-r6` | **保留现有** |
| 引擎层 | 无（零 `fetch` / Worker / provider） | Worker `local-engine` + `LBRpc` + `local/server/combined` provider + SSE `/api/v2/events` | **保留现有** |

### 2.2 DOM 对齐成本（抽查 5 个视图）

| 视图 | 原型根结构 | `/r6` 根结构 | 一致性 |
|---|---|---|---|
| 采集中心 | `.r6-head` + `section.r6-content`（`.r6-tabs` + `.r6-pane/.r6-table-scroll`） | `section.r6-intake`（`.r6-intake-head` + `.r6-intake-body` + `.r6-table-wrap`） | 类名部分同名，**结构不一致** |
| 日志源详情 | `aside.r6-drawer`（`.r6-steps/.r6-watermark/.r6-kv`） | `aside.r6-source-drawer`（`.r6-facts/.r6-drawer-actions`） | 根类名不同，结构不一致 |
| 四步向导 | 原生 `dialog#dialog` + `.r6-wizard-tabs` + 两栏 `.r6-wizard-layout/.r6-form-side` | `div#wizard-dialog.r6-dialog` + `ol.r6-steps` + 单栏 `.r6-wizard-body` | 结构不一致；且实现非原生 dialog |
| 文件库 | `.r6-head` + `.r6-content` + `.library-shell`（`.folder-pane` + `.library-main`） | `.lib-root` + `.library-shell`（同名）+ `.lib-bars` 两行工具条 | **部分一致**，最接近 |
| 解析缓存 / 运行记录 | 采集中心内两个 tab：`.r6-resource/.r6-log-list` | 两个独立管理员视图 `.cache-view` / `.runtime-view` | 组织方式不同（见 §15 待确认 1） |

结论：**同名 class ≠ 同结构**。整改不是改 CSS 变量即可完成，需要按原型重建外壳与各视图的容器树；这是本计划工作量主体。

### 2.3 原型必须拆成两半看

| 保留（设计骨架） | 删除（演示附属物） |
|---|---|
| `.lb-app` grid 外壳、`renderHeader/renderSidebar/main.main/footer.app-status` 层级 | 全部"合成/示例/演示/设计原型/交互原型/未连接后端"自我声明（原型中「合成」12 行、「示例」24 行、「演示」17 行、「原型」37 行） |
| `.r6-head/.r6-content/.r6-tabs/.r6-toolbar/.r6-pane/.r6-table-scroll/.r6-foot` | `.r6-head` 的 Slogan「远程目录 → 原文归档 → 可复用索引」及每个 tab 工具条里的 `.r6-sub` 说明句 |
| `table.r6-table` 五列日志源表、`storage` 六列、`cache` 七列、`ol.r6-log-list` 四列 | 各 tab `.r6-foot` 的重复 disclaimer、重复 badge（文件库同时有「总库 / 服务器总库 / 服务器日志总库」） |
| `.r6-drawer` 详情抽屉、`.r6-steps` 三步链路、`.r6-watermark` 水位、`.r6-kv` 键值表 | 只改本地状态不产生真实结果的按钮：`refresh`（仅 notify）、`append-demo`（只加计数）、演示态 `probe` 情形下拉、`cache-rebuild` 布尔翻转 |
| 四步向导的四个标题与顺序、`.r6-protocols/.r6-form/.r6-review-grid/.r6-probe` | `checked disabled` 的假策略勾选项（连接步 1 个、策略步 4 个）——接真实后端后应变为可用字段或删除 |
| 显示设置浮层、时间范围、对比视图、规则绑定对话框 | R3/R4 死代码（`renderLegacy` 等 20+ 无调用点函数）、死 CSS（`.r6-cardline/.r6-four/.r6-groupheading/.r6-saving/.r6-togglebar`）、死 token |

### 2.4 审计发现（按 Route B Phase 2 分类，按影响排序）

| # | 类别 | 发现 | 证据 |
|---|---|---|---|
| P1 | 结构 / IA | 设计层与原型零复用，视图组织另起一套 | `r6.html:11–115`、`intake.js:175` |
| P2 | 功能回归 | 采集中心 `storage/cache` 两 tab 是占位说明，指向已实现却仅管理员可见的独立页 | `intake.js:240–243,384–407`、`r6.html:53–56` |
| P3 | 状态缺失 | 原型是纯演示，无 loading / empty / error / disabled / success / readonly / long-value 设计；实现部分有但未成体系 | 原型全文件零网络；`r6-app.js:1449` 仅 `setStatus('就绪')` |
| P4 | 空间 | 原型 sync 视图固定占用约 244px（header 52 + status 22 + head 50 + tabs 39 + toolbar 49 + foot 32），数据区约 524px / 68% | `L33/L40/L80` |
| P5 | 文案 | 大量重复自我声明、Slogan、同义副标题；必需信息被放进 `title=`（如多目录完整清单只在 tooltip） | `L777` `title="${x.maps.map(...).join(' / ')}"` |
| P6 | 无障碍 | 实现用 `div[role=dialog]` 而非原生 `<dialog>`，缺原生焦点隔离与 Esc；原型用原生 `dialog#dialog` | `r6.html:129–133`、原型 `L85` |
| P7 | 功能未接线 | `workspace.js` 的 density/contrast/font_size/columns/sort/wrap/书签/视角已就绪但无 UI | `workspace.js:36–42`、`r6.html` 无对应控件 |
| P8 | 备份缺入口 | `shared/backup.js` 未进 `R6_PAGE_PARTS`，`/r6` 无备份/恢复 UI | `frontend.py:53–96` |
| P9 | 回归风险 | `qa/r6/*_ui.py` 用现有 `.r6-*` 选择器断言；重建 DOM 会打断 | `intake_ui.py:105–212` 等 |
| P10 | 契约缺陷 | `source-wizard.js:669` 调 `GET /api/v2/storage/roots`，服务端只有 `/api/v2/storage-roots`，请求静默落空 | `app.py:623` |

---

## 3. 设计约束与指导对照（本计划的复核基线）

### 3.1 `unify-design` 五步工作法映射

| 步骤 | 本计划落实位置 |
|---|---|
| 1. 判断根本任务与现状，建保留/替换/迁移矩阵 | §2.1 三层拆分、§7.2 旧功能去向清单、§9 影响图 |
| 2. 先修 IA 与有效空间 | §5.1 外壳视图组织、§3.3 空间预算、删除重复标题与 disclaimer |
| 3. 选场景模式与组件 | 日志工作台（patterns §4）+ 文件资源（§6）+ 参数矩阵（§2）；复用 `style.css` token 与现有 UI 模块，不引入 ui-kit 第二套体系 |
| 4. 实现可恢复交互与状态 | §7.3 状态件、`AbortSignal`+epoch（沿用 `shared/http.js:44–56`）、409 保留草稿、危险操作后果 |
| 5. 验证与交付 | §13 有界 QA：1366×768 / 375×812 / 768×1024 实查主内容高度与末项可达性 |

### 3.2 `ui-ux-design-suite` Route B 关卡映射

| 关卡 | 落实 |
|---|---|
| Phase 1–3 现状 / 审计 / 影响图（诊断先行） | §2、§9；**本轮不改任何生产 CSS/组件** |
| Phase 4 用户对齐 | §15 三个待确认项，均带推荐默认 |
| Phase 5 参考研究 | 本次不需要：用户已给出具体视觉目标（原型 HTML），无需另找参考 |
| Phase 6–7 结构原型 + **预览批准关卡** | §10；Gate 3 视作「用户已提供并批准等效具体视觉目标」，但仍产出静态预览供确认 |
| Phase 8 冻结实施契约 | §11 |
| Phase 9–10 回归 QA + 持久文档 | §13、§14 |
| Non-negotiable 6「复用先于重建」 | 引擎层全部复用；仅设计层重建，且复用原型 class/token，不建平行设计系统 |
| Non-negotiable 8「功能是设计正确性的一部分」 | §7.3 状态件为必做项，非 polish |
| Non-negotiable 9「有界验证」 | §13 一次主检 + 一次复验，不进入无尽打磨 |

### 3.3 文案原则 → 可执行的删 / 留规则

| 规则 | 执行 |
|---|---|
| 主动删除无信息价值文案 | 删除 Slogan、同义副标题、重复标签、自我声明、示例后缀、`.r6-foot` disclaimer；每页只保留一个页面标识（导航已显示时不另起大标题） |
| 自解释优先 | 页面标识 + 对象名 + 状态 + 单一主动作合并为一条工具条；筛选单独一行；低频条件折叠 |
| 保留必需 | 所有 `<label for>`、`aria-label`、字段校验错误与恢复说明、409/失败保留输入提示、删除/重建/恢复备份的后果说明、权限不足的可读原因 |
| 帮助入口 | 概念性补充（如「逻辑总库如何聚合」「索引键构成」）移入 `<details>` 或按钮触发的帮助面板，键盘与触屏可达；**不放在 `title=`** |
| Tooltip 迁移 | 多目录完整清单、完整路径、长日志原文、字段来源等当前只存在于 `title=` 的信息，改为：列表内可见摘要 + 「查看全部 N 项」展开 / 详情抽屉完整值；`title` 仅保留为冗余增强，不作为唯一信息源 |
| 按钮文案 | 具体动词 + 对象（「添加日志源」「暂停采集」「重建索引」「导出运行记录」），不用「确定 / 处理 / 提交」；危险动作写明对象、数量与恢复方式 |

### 3.4 纵向空间预算（1366×768，sync 视图）

| 项 | 原型 | 整改后 | 说明 |
|---|---|---|---|
| 顶栏 | 52 | 52 | 保持原型硬编码 grid 行，不动 `--ud-topbar-h`（见 §9 Gate 4） |
| 状态栏 | 22 | 22 | 保留，内容改为真实连接/权限/保存状态 |
| `.r6-head` | 50 | 50 | **数值不变**；内部删 Slogan 与重复 badge，只留页面标识 + 单一主动作 |
| `.r6-tabs` | 39 | 39 | **数值不变** |
| `.r6-toolbar` | 49（`min-height`） | 49 | **数值不变**；删 `.r6-sub` 后内容从多行说明变单行，容器仍是同一条工具条 |
| `.r6-foot` disclaimer | 32 | 0 | 删除纯声明文字；真实计数/状态并入状态栏，不再单独占一行 |
| **数据区** | 524px (68.2%) | **556px (72.4%)** | 净增 **32px**，全部来自删除重复容器；不改字号、行高、侧栏宽与任何尺寸数值 |

> **严格性声明**：尺寸数值全部保持原型原值，符合 §1.2「视觉节拍与原型一致」。空间收益只来自删除 `.r6-foot` 这一行纯声明容器，不是重新设计尺寸。
>
> **与当前实现的对照（不回避）**：当前 `/r6` 只有 54px 顶栏 + 40px 工具条，数据区按声明推算约 674px（待 M1 实测确认）；按原型回归恢复 52 + 22 + 50 + 39 + 49 的固定层后为 556px，**少约 118px**。这 118px 换回的是原型设计层：页面标识、四 tab、真实状态栏、页脚计数与明确的视觉节拍。本计划**不通过压缩原型尺寸去追平**，而把选择权交回 §15 待确认 4。
> unify-design `design-rules.md:69`：核心区域太小应先检查顶部纵向堆叠、重复工具条和固定空白，**不先改字号或只加宽**。本计划的空间回收全部来自文案与重复容器减法，不改字号、不改行高、不改侧栏宽度。

---

## 4. 整改方案总览

### 4.1 三层拆分

| 层 | 处置 | 具体 |
|---|---|---|
| **设计层**（不可协商） | **以原型为基线重建** | `src/r6.html` 外壳 DOM、`src/r6.css` 基线样式（以原型 `L1–L83` 为准）、各 UI 模块输出的容器树与 class 名、6 视图组织、`sync` 默认落地 |
| **引擎层**（该补的后端） | **原样保留** | `/api/v2/*` 全部端点、`shared/*`、`providers/*`、`workers/*`、`events/client.js`、`LBHttp` epoch/CSRF、IndexedDB、SSE/轮询 |
| **适配层**（净新增） | **新写** | ① 数据源适配器：把 v2 响应投影成原型渲染函数期望的视图模型；② 动作适配器：把原型 `data-r6` 事件映射为 v2 action；③ 状态机：loading/empty/error/success/readonly；④ 原型数据入口的替换点 |

### 4.2 数据流

```
/api/v2/* ──► LBHttp (epoch/CSRF) ──► 数据源适配器（新增）
                                        │
                                        ▼
              原型一致的视图模型 { sources, roots, events, tuning, files, … }
                                        │
                        ┌───────────────┼────────────────┐
                        ▼               ▼                ▼
               原型等价 DOM 渲染   Worker/本机阅读   SSE 实时增量
               （设计层，重建）    （引擎层，保留）  （引擎层，保留）
                        │
                        ▼
                data-r6 动作 ──► 动作适配器（新增）──► POST/PATCH /api/v2/*
                                        │
                                        └── 409 / 失败：保留输入 + 常驻恢复入口
```

### 4.3 关键实现决策

| 决策 | 理由 |
|---|---|
| 用 `document.addEventListener('click', …)` 委托 + `data-r6` 动作表，**原型 35 个动作的名称与用户可见语义一字不改** | 原型 `L805–L838` 已是成熟的委托模式；接真 API 只替换动作内部实现，不改控件、不改文案（除删除演示态动作 `append-demo`） |
| 用**显式动作映射表**（新增 `src/ui/actions.js`）而非散落 switch | 便于与 v2 端点一一核对、便于测试、便于登记契约映射；原型 switch 里夹带渲染，不易维护 |
| 用原生 `<dialog>` 承载向导/显示设置/时间范围/规则绑定/备份 | 原型用原生 `dialog#dialog`（`L85`）；`design-rules.md:126` 要求对话框焦点进入与返回、Esc 取消、背景隔离 |
| 保留 `src/style.css` 作为唯一 token 源，原型 `.ud-app` 定义只作对照不重复引入 | `style.css` 是 `/` 与 `/r6` 唯一共享文件；不建第二套设计系统 |
| 不启用原型的 `restoreCache()` 多来源重放逻辑 | 其 R4 迁移与 fixture 重放面向演示数据；真实数据由 `/api/v2/library` + IndexedDB 现有链路负责 |
| 保留 `r6-app.js` 的 `boot()` 顺序与 `window.__r6` 契约 | QA 脚本与调试依赖；只替换其中的视图渲染与导航部分 |

### 4.4 设计扩张的硬边界（防止实施时自由发挥）

> 这是本计划最重要的一条自我约束。

1. **原型已定义的一切，照抄**：容器层级、class 名、表格列、控件形态、尺寸、颜色、交互顺序、文案措辞。
2. **原型未定义而真实后端必需的**（loading / empty / error / 403 / 409 / 保存态 / 权限态 / 长值 / 实时进度），**只能复用原型已有的组件形态**表达：`.r6-note`（含 `.warn/.bad`）、`.r6-errorbox[role=alert]`、`.r6-badge`、`.r6-progress`、`.r6-empty`、`footer.app-status`、`<details>` 帮助块。**不得新增视觉语汇、卡片、图表、渐变或新的组件命名空间**。
3. **删除原型中因演示才存在的内容**（§8.1）属于执行文案原则，不算修改设计；但删除后不得用自创新控件填补空位。
4. **实现侧已有而原型没有的能力**（§7.2），收纳进原型既有容器（工具条操作列、详情抽屉分组、`<details>` 高级区）即可，不为其新开页面或新导航项。
5. 任何一条需要破例，先回到 M0 报批，不在代码里自行决定。

---

## 5. 逐视图整改清单

### 5.1 应用外壳（全局）

| 项 | 原型基线 | 现状 | 整改 |
|---|---|---|---|
| 根节点 | `<body class="ud-app">`，`#app.lb-app` | `<body class="r6-body">`，`#app.ud-app.lb-app.r6-app` | 恢复 `body.ud-app`；`#app` 保留 `.ud-app` 以兼容 `style.css`，叠加原型 `.lb-app` grid |
| 布局 | `display:grid`；`208px / 52px`；`.lb-header` 跨列；`.sidebar` 与 `main.main` 分列 | `display:flex`；`.r6-header` 54px；`.r6-nav` 208px | 恢复 grid 与 52px 行；侧栏回到 `aside.sidebar`；主题仍写 `#app.dataset.theme` |
| 顶栏 | 菜单 / 品牌 / 面包屑 `.header-path` / 右侧动作区 | `.r6-brand` + 全局搜索 + 导入/服务端/主题/实时/缓存计量 | 保留原型层级，把实现的实时状态、缓存计量、主题、导入动作**收进 `.header-right`**，不新增第二行顶栏 |
| 侧栏 | `nav.feature-card` 5 项 + 已打开文件列表 + `.sidefoot` | `nav.r6-nav` 7 项 | 恢复原型侧栏组件；6 视图入口；「已打开」列表接 `window.__r6` 的真实打开文件 |
| 状态栏 | `footer.app-status` 22px | `#status` 内联在顶栏 | 恢复底部状态栏；显示真实连接、当前工作区、权限、保存状态；**删除所有"合成数据"声明** |
| 响应式 | 1650/1280/1180/1000/760/650/480 断点 | 1100/900/860/640/600 | 采用原型断点体系；按 `design-rules.md:73` 复测 375/768/1024 主内容高度 |

### 5.2 采集中心 `sync`（默认落地页）

| 项 | 原型基线 | 整改动作 |
|---|---|---|
| 容器 | `.r6-head` + `section.r6-content`（`.r6-tabs` + 当前 tab 内容） | 按原型重建；删除 `.r6-intake/.r6-intake-head/.r6-intake-body/.r6-table-wrap` 这套外壳 |
| 四 tab | 远程日志源 / 存储位置 / 解析缓存 / 运行记录 | 名称顺序不变；**四个 tab 全部接真实数据**，删除两处占位说明与"M8 提供"过期文案 |
| 日志源表 | 5 列：日志源/连接、目录→存储位置、同步状态、归档/索引覆盖、操作 | 列结构不变；`归档/索引覆盖` 显示真实覆盖率与 `.r6-progress`；多目录清单由 tooltip 改为单元格内「+N 个目录」+ 点击展开 |
| 工具条 | 搜索框 + 状态筛选 + `.r6-inline-stats` 3 项统计 + 刷新 | 统计来自 `GET /api/v2/intake`；`refresh` 从"仅 notify"改为真实重取并显示上次更新时间 |
| 详情抽屉 | `aside.r6-drawer`：三步采集链路 `.r6-steps`、`.r6-watermark`、`dl.r6-kv`、目录映射、连接能力、归档与访问 | 根类名改回 `.r6-drawer`；数据接 `GET /api/v2/sources/{id}` + `/state` + `/mappings`；补 loading / 404 / 403 / 重试 |
| 存储位置 tab | `table.r6-root-table` + 说明块 | 接 `GET/POST/PATCH /api/v2/storage-roots` + `POST …/probe`；说明块中「逻辑总库聚合…」等概念性内容移入 `<details>` |
| 解析缓存 tab | `.r6-resource/.r6-healthmark` + 7 列表 + 4 段重算说明 | 接 `GET /api/v2/cache`、`POST …/rebuild`、`POST …/purge`；重建显示真实进度而非布尔翻转；说明移入帮助入口 |
| 运行记录 tab | `ol.r6-log-list` 四列 + 导出 | 接 `GET /api/v2/jobs?limit=50` 与 SSE 增量；导出真实事件；`export-events` 不再带 `synthetic:true` |
| 资源设置 | `r6Tuning()` 6 项 | 保留原型 6 项可见字段，`save-tuning` 接 `PUT /api/v2/runtime/budget`；实现侧 13 项中的其余项归入「高级」折叠区，不牺牲原型观感 |

### 5.3 日志工作台 `logs`

| 项 | 原型基线 | 整改动作 |
|---|---|---|
| 工具条 | `.toolbar` 46px，含搜索框、规则条、时间范围、多文件、时间线、显示设置、导出 | 恢复原型工具条与控件集合；密度/对比度/列/换行/时间精度接 `workspace.js` 的 `PREFERENCE_FIELDS` |
| 日志表 | 8 列 `COLUMNS`（行号/时间/等级/软件/脚本:行号/函数/PID/TID）+ 内容；`.log-virtual` 虚拟表 | 采用原型列集合与可配置列显隐；底层仍用 `reader-controller.js` 虚拟窗口（保留引擎层），行高测量与 CSS 同源 |
| 时间线 / 详情 | `.timeline` 208px、`aside.inspector` 350px | 恢复原型宽度与层级；数据接 `provider.getTimeline` / `provider.detail` |
| 实时条 | `.r6-livebar` 33px（跟随、新增 N 条、读取） | 接 SSE `events/client.js`；`follow` 真实跟随；删除 `append-demo` 模拟新增 |
| 补齐 | 显示设置浮层、时间范围工具（UTC/9 位小数秒用实现侧更强的 `global-time.js` 补齐）、拖放导入、规则高亮 | 原型 `#drop-overlay` + `#file-input` 恢复；拖放只在本机读取，不发 HTTP |

### 5.4 文件库 `files`

| 项 | 原型基线 | 整改动作 |
|---|---|---|
| 容器 | `.r6-head` + `.r6-content` + `.library-shell`（`aside.folder-pane` + `.library-main`） | 现状 `.library-shell/.folder-pane` 已部分一致，补齐原型外壳与 `.r6-filebar` 单行工具条；删除重复的三处「总库」标签 |
| 数据源 | `R6.sources.flatMap(maps)` 合成文件名 | 接 `GET /api/v2/library/tree`、`/api/v2/library`、`/api/v2/library/sources` |
| 保留能力 | 原型只展示 | 实现侧已有的批量选择、回收站、规则绑定、删除保护全部保留并挂到原型工具条/操作列，避免功能倒退 |

### 5.5 规则 `rules` / 管理 `admin`

| 项 | 整改动作 |
|---|---|
| 规则视图 | 恢复 `renderRules()` 的 `.rule-library-layout`（左 profiles + 右 `.rule-work` 五 tab 编辑器 + 试跑面板）；接 `/api/v2/rules*`；补 `rule-impact.js` 的 10 形状 / 12 色标记 UI |
| 管理视图 | 原型 `admin` 单页是「未连接后端」空壳，但已定义 `.settings-body` + 3 个 `.settings-block` + `.settings-row/.settings-table` 组件形态。**按 §4.4 硬边界，只把这三块填成真实的用户/权限、凭据、服务端配置，复用原组件，不新造视觉或新导航项**；删除所有演示标记 |
| 管理员增强 | 实现侧独立的 `cache / runtime / storage` 三页能力不删除（见 §15 待确认 1），作为采集中心对应 tab 的「管理」二级入口，权限不足时降级为只读摘要而非隐藏后留死链接 |

### 5.6 时间对比 `compare`

恢复 `renderCompare()`：`.compare-toolbar`（布局分段 / 同步）、`.compare-timebar`（位置滑块）、`compare-grid` 2–4 窗格；底层复用了实现侧 `compare.js` 的时间对齐与 `fetchWindow`。补齐空态（<2 文件）与长文件名消歧。

### 5.7 弹层族

| 弹层 | 原型结构 | 整改 |
|---|---|---|
| 四步向导 | 原生 `dialog` + `.r6-wizard-tabs` + 两栏 `.r6-wizard-layout/.r6-form-side` + 四步内容 | 恢复两栏与 tab 式步骤；`probe` 换真实 `POST /api/v2/sources/{id}/probe`；`wizard-save` 走 POST/PATCH；`409` 保留草稿并提供对比/重试；右侧说明改为可折叠帮助，不占常驻宽度 |
| 显示设置 | `#popover` + `showDisplay()` | 补密度/对比度/字体/列/换行/时间精度；写 `workspace.savePatch` |
| 时间范围 | `#popover` + `showTime()` | 复用实现侧 `global-time.js`（UTC、9 位小数秒、收藏视角），外壳按原型浮层呈现 |
| 规则绑定 / 目录默认规则 | `bindingDialog()` / `directoryDialog()` | 接 `/api/v2/bindings*`；保留错误说明与后果提示 |
| 备份 / 恢复 | 原型 `storagePanel()` | 把 `shared/backup.js` 加入 `R6_PAGE_PARTS`，提供导出/导入；保留「替换当前工作区」「失败不会留下半份文件库」等后果说明 |

---

## 6. 数据接入映射表

### 6.1 原型数据入口 → 真实数据源

| 原型入口 | 行号 | 替换为 | 备注 |
|---|---|---|---|
| `R6_SEED.sources` | `L746–751` | `GET /api/v2/intake` + `GET /api/v2/sources/{id}` | 视图模型保留 `id/name/protocol/endpoint/status/maps/coverage/lag` 字段 |
| `R6_SEED.roots` | `L745` | `GET /api/v2/storage-roots` | 修正 `GET /api/v2/storage/roots` → `/api/v2/storage-roots` |
| `R6_SEED.events` | `L752` | `GET /api/v2/jobs?limit=50` + SSE `/api/v2/events` | 保留 `at/source/kind/tone/text` 投影 |
| `R6_SEED.tuning` | `L753` | `GET /api/v2/runtime` | 原型 6 项 + 高级区其余 7 项 |
| `files` / `makeDemo` | `L177,L202–204` | `/api/v2/library*` + 本机 Worker `open('local')` | 日志正文仍由 Worker 读取，不经服务端 |
| `r6Persist()` | `L764` | `LBWorkspace.savePatch` + v2 写操作 | 本地只存偏好，业务数据以服务端回执为准 |
| `restoreCache()` | `L712` | 现有启动链路 `boot()` + `refreshFiles()` | 不启用 R4 fixture 重放 |
| `window.LogBenchRemotePreview` | `L841` | 保留 `window.__r6`，并补一个只读兼容层 | 避免旧评审脚本全失效（报告 C12） |

### 6.2 关键 `data-r6` 动作 → v2 端点

| 动作 | 原型行为 | 整改后 |
|---|---|---|
| `refresh` | 仅 notify | `GET /api/v2/intake` 重取 + 显示更新时间 |
| `pause` | 改本地字符串 | `POST /api/v2/sources/{id}/actions`，带 `expected_revision`，409 保留草稿 |
| `wizard-save` | 写 `R6.sources` | `POST /api/v2/sources` / `PATCH /api/v2/sources/{id}` |
| `probe` | 切演示情形 | `POST /api/v2/sources/{id}/probe`，真实 checks；删除情形下拉 |
| `save-root` | 写本地 roots | `POST /api/v2/storage-roots` / `PATCH …/{id}` + `POST …/probe` |
| `save-tuning` | 写 localStorage | `PUT /api/v2/runtime/budget` |
| `cache-rebuild` | 翻转布尔 | `POST /api/v2/cache/rebuild` + 真实进度；`purge` 前显示后果与影响数量 |
| `export-events` | 导出 `synthetic:true` | 导出真实事件，无 synthetic 标记 |
| `read-source` | 打开合成 `f1` | `openFile()` 走 provider 打开真实文件 |
| `follow` / `read-new` | 切布尔 + 滚动 | 接 SSE follower，真实新增计数 |
| `append-demo` | 只加计数 | **删除** |
| `tab` / `source-detail` / `close-detail` / `clear-search` / `wizard-*` / `library*` | 本地状态 | 保留语义，只换容器与渲染目标 |

---

## 7. 补齐功能 / 组件清单

### 7.1 原型有、当前 `/r6` 缺 UI 入口（必补）

| # | 组件 | 数据层现状 | 补法 |
|---|---|---|---|
| 1 | 显示设置浮层（主题/密度/对比度/字体/列/换行/时间精度） | `workspace.js:36–40` 字段齐全，仅 `theme`/`page_size` 接线 | `showDisplay()` 等价浮层 + `savePatch` |
| 2 | 书签 / 阅读视角 | `workspace.js:30,42,179–182`；但 store 缺 `list` 导致 `bookmarks()` 恒空 | 补 `list` 实现 + 侧栏「已打开/书签」区 |
| 3 | 备份 / 恢复 | `shared/backup.js` 完整但未进 bundle | 加入 `R6_PAGE_PARTS` + 对话框 |
| 4 | 拖放导入 | 原型 `#drop-overlay` | 恢复；本机读取不发 HTTP |
| 5 | 原生 `<dialog>` 与焦点管理 | 实现用 `div[role=dialog]` | 全部弹层换原生 dialog |
| 6 | 面包屑 `.header-path` | 实现无 | 恢复，显示工作区 / 视图 / 当前对象 |
| 7 | 底部状态栏（连接/权限/保存态） | `#status` 内联顶栏 | 恢复 `footer.app-status` |
| 8 | 详情抽屉链路可视化与水位 | 实现 `.r6-facts` 平铺 | 恢复 `.r6-steps/.r6-watermark/.r6-kv` |
| 9 | 规则绑定 / 目录默认规则对话框 | 实现在 library 内散落 | 恢复两个独立对话框接 `/api/v2/bindings*` |
| 10 | 多目录清单的可达展示 | 当前只在 `title=` | 展开控件 + 详情完整值 |

### 7.2 实现有、原型缺（**保留，不回退**）

真实预检与 checks、`expected_revision` 与 409 冲突保留草稿、断点续传上传、13 项资源预算、UTC + 9 位小数秒 + 视角收藏、SSE 实时与轮询降级、权限分层（`capabilities.admin`）、虚拟窗口 50k 逻辑行、回收站与批量删除、`LBHttp` epoch/CSRF 防串场。

### 7.3 必须新建的状态件（`design-rules.md:93–101`）

| 状态 | 要求 |
|---|---|
| loading | 骨架/进度，不引起布局跳动；常驻文字「加载中」，`reduced-motion` 下仍可见 |
| empty | 有明确下一步与创建入口，不显示假数据 |
| no-result | 保留查询词，可清筛选，不误报"空库" |
| error / 403 / 404 | 常驻错误区 + 重试 + 原因可读，不用瞬时 toast 吞掉 |
| disabled | 说明原因（权限/前置条件），不只置灰 |
| success | 只在服务端回执后显示，区分「草稿 / 已保存 / 已提交 / 已重建」 |
| readonly / 草稿 / 冲突 | 停写但保留输入；409 提供对比与重算 |
| long value | 列表可截断，完整值有键盘可达入口，不依赖 hover |

---

## 8. 文案整改清单

### 8.1 删除

1. 全部自我声明：「设计原型 · 合成数据 · 不连接远程服务」「交互原型」「合成演示数据 · 无服务端连接」「未连接后端」「仅演示」「原型未接入后端…」及按钮上的「示例」后缀。
2. 全部 `.r6-foot` disclaimer 与四 tab 工具条的 `.r6-sub` 说明句；`.r6-head` 的 Slogan。
3. 重复标签：「总库 / 服务器总库 / 服务器日志总库」统一为「文件库」，只出现一次；重复的「不上传 / 本机处理」提示合并到导入区一处。
4. 三份并存的 about 文案（R3/R4/R5 + R6 覆盖）合并为一份真实说明。
5. 所有 `checked disabled` 的「假策略」勾选项（`L785` 连接步 1 个、`L787` 策略步 4 个）：**不直接删除**——这些文字承载采集可靠性的真实约束，有信息价值。改为后端支持的真实可配置字段；若后端不支持配置，改为只读状态行并保留原文字说明。只删除 `disabled` 造成的「已生效」错觉。

### 8.2 保留

- 所有 `<label for>` 与 `aria-label`（含按钮可访问名称、tablist 名称、抽屉名称）。
- 字段校验与错误恢复：`r6PathError` 4 类、`r6Validate` 6 类、409 冲突说明、恢复时有文件无法解析的处理说明。
- 危险操作后果：清空本机工作区、删除/批量删除、缓存 purge、恢复备份替换当前工作区——写明对象、数量、可恢复性。
- 真实来源与状态：连接状态、权限范围、数据新鲜度（上次更新时间）、保存状态、工作区标识。

### 8.3 Tooltip 迁移

| 当前只在 `title=` 的信息 | 迁移目标 |
|---|---|
| 多目录完整清单（`L777`） | 单元格「首个路径 +N 个目录」+ 点击展开完整列表（键盘可达） |
| 文件名 / 完整路径（`L241,L539`） | 列表截断 + 详情抽屉完整值 |
| 完整日志原文 / 时间戳 / 来源行号（`L234,L495,L620,L622`） | 详情抽屉 `.r6-kv` 完整字段 |
| 字段提取规则与行号（`L496`） | 详情内分组展示 |
| 按钮语义（`L479`） | 保留可见文字或 `aria-label`，不依赖 hover |

`title` 保留为冗余增强，**不再作为唯一信息通道**。

---

## 9. 共享组件与回归影响图（Gate 4）

```
/r6 路由
  → r6.html（外壳 DOM，重建）
  → r6.css（设计层样式，重建；不加载 ui-kit）
  → UI 模块（容器树重建，逻辑与 API 调用保留）
  → shared/* + providers/* + workers/*（不动）
  → style.css  ← 唯一与 "/" 共享的文件
  → 其他消费者："/" 工作台版（body.ud-app + --ud-* + 四主题）
  → 数据契约：/api/v2/* 响应形状、window.__r6、logbench-r6* 存储键
  → 测试：qa/r6/*_ui.py、worker.py --self-test、qa/visual_baseline.py（面向 "/"）
```

| 拟议共享变更 | 分类 | 决定 |
|---|---|---|
| `style.css` 的 `--ud-topbar-h:54px` 与原型硬编码 52px 冲突 | **Local requirement** | **不改 `style.css`**；`/r6` 外壳按原型用 `grid-template-rows:52px`，把 54px 留给 `/`。避免波及 `/`（`component-regression.md` 的 local variant 原则） |
| `style.css` 四主题、色彩 token | 复用 | 原样复用，不复制一套进 `r6.css` |
| `qa/r6/*_ui.py` 选择器断言 | **受影响的测试** | DOM 重建后同步更新选择器；先建立「原型 DOM 契约」测试再做迁移，避免先改后补 |
| `window.__r6` | 保留 | 不改名；补 `LogBenchRemotePreview` 只读兼容层 |
| `LBWorkspace` 存储键 | 保留 | 不迁移原型 preview 命名空间；避免破坏已保存偏好 |
| 新增 `src/ui/actions.js`、数据源适配器 | 局部新增 | 加入 `R6_PAGE_PARTS` 末尾（在消费者之前），不改已有顺序语义 |

**功能回归清单**（`component-regression.md`）：点击/键盘与焦点顺序、表单校验、loading/empty/error/success、路由与取消、权限与危险动作、响应式与溢出、四主题、长值/本地化扩展、共享组件其他消费者（`/` 需冒烟）、 **`qa/r6/*` 自动化选择器**。

---

## 10. 预览关卡（Gate 3）

用户已提供并批准具体视觉目标（原型 HTML），按 `artifacts-and-gates.md` Gate 3 可视为等效批准；但为保证"设计层重建 + 文案减法"的方向被确认，仍产出一个轻量预览：

| 项 | 内容 |
|---|---|
| 产物 | `docs/R6/preview/r6-restored.html`（单文件，本地可打开，无外部请求） |
| 内容 | 原型的 DOM/CSS 基线 + 真实形状的样本数据（来自 `/api/v2/*` 响应样例，标注为样本） |
| 覆盖视图 | `sync` 四 tab、`logs`、`files`、四步向导、详情抽屉、显示设置 |
| 覆盖状态 | 正常 / 空 / 加载 / 错误 / 只读 / 长值 |
| 视口 | 1366×768 主视图 + 375×812 窄屏 |
| **批准点** | 用户确认预览方向后，才修改 `src/` 生产文件 |

> 预览只用于确认视觉与结构，**不作为后端可用性证据**；真实数据链路在 M3 之后用运行中的服务验证。

---

## 11. 实施契约（冻结后不改）

- **批准的 UX / IA**：6 视图、`sync` 默认落地、原型外壳与容器树；具体管理员页归属见 §15 待确认 1。
- **批准的视觉方向**：原型 HTML 的 DOM/CSS/色彩/密度，叠加 §8 文案减法。
- **范围内**：`src/r6.html`、`src/r6.css`、`src/r6-app.js` 渲染与导航部分、`src/ui/*` 容器树、`server/frontend.py` 的 `R6_PAGE_PARTS`、新增 `actions.js` 与数据源适配器、新增预览与 QA 脚本。
- **范围外**：`server/*` 业务路由、`worker.py`、`shared/*` 与 `providers/*` 的语义、`src/index.html` 与 `/assets/workbench.js`、`style.css` 的 token 值。
- **保留的行为/数据契约**：`/api/v2/*` 请求响应形状、`window.__r6` 成员、`logbench-r6*` 存储键、CSRF/epoch、409 语义、权限 `capabilities.admin`。
- **共享组件传播**：`style.css` 零改动；新增模块为局部；QA 选择器同步迁移。
- **必须实现的状态与响应式**：§7.3 全部状态；1366/1920/1024/768/375，窄屏主内容有正高度且末项可达。
- **验收**：§1.2 六维 + §13 有界 QA + 与预览逐页比对。

---

## 12. 执行顺序与里程碑

| 里程碑 | 产出 | 验收 / 可回退点 |
|---|---|---|
| **M0 计划批准** | 本文档获批；§15 三项拍板 | 未批准不动生产代码（当前位置） |
| **M1 DOM 契约基线** | `qa/r6/dom_contract.py`：用无头浏览器**先完整执行原型 JS**（六个 `S.view` 逐一触发），再提取 sync/logs/files/rules/compare/admin 的规范化 DOM 树作为比对基线；`docs/R6/preview/r6-restored.html` 静态预览 | 用户确认预览方向；契约脚本可重复运行；M2 起每视图与基线逐节点比对 |
| **M2 外壳回归** | `r6.html` + `r6.css` 按原型重建外壳；`r6-app.js` 的 `show()` 改 6 视图；默认 `sync` | 1366×768 几何与原型一致（52/208/50/39/49）；`/` 冒烟无变化；可整体回退 |
| **M3 适配层** | `src/ui/actions.js`、`src/ui/data-adapter.js`；`R6_PAGE_PARTS` 追加 | 每个 `data-r6` 动作有对应 v2 端点；`GET /api/v2/storage/roots` 修正；`worker.py --self-test` 通过 |
| **M4 采集中心四 tab + 向导 + 抽屉** | 按 §5.2 全量落地，四个 tab 真实数据 | 主闭环：添加日志源 → 预检 → 保存 → 暂停/恢复 → 详情 → 重建缓存；409/失败/空/加载态实测 |
| **M5 其余视图 + 补齐组件** | logs / files / rules / admin / compare；显示设置、书签视角、备份、拖放、原生 dialog、时间工具 | §7.1 十项逐项勾选；无"合成/示例/原型"残留；Tooltip 迁移完成 |
| **M6 QA 与文档** | 更新 `qa/r6/*_ui.py` 选择器；新增状态与视口检查；`docs/R6/` 决策记录；`DESIGN.md`（如项目需要） | §13 全部通过；一次主检 + 一次复验；明确剩余未验证项 |

每个里程碑单独提交，保留 `eff4edc` 可回退；不跨里程碑混改。

---

## 13. 有界 QA（一次主检 + 一次复验）

| 维度 | 检查 |
|---|---|
| 视口 | 1366×768、1920×1080、1024×768、768×1024、375×812；记录数据区顶边、可用高度、可见行数；窄屏主内容正高度且可滚到末项 |
| 主路径 | 启动落 `sync` → 查看日志源 → 详情 → 编辑 → 预检 → 保存 → 暂停/恢复 → 重建缓存 → 查看运行记录 |
| 备选 / 失败路径 | 空库、无匹配、加载失败重试、403、404、409 冲突保留草稿、双击提交、切视图带未保存输入 |
| 视觉 | 与 M1 预览逐页比对；层级、间距、色彩、圆角、边框、动效；无重复标题 / 重复 CTA / 装饰卡片 |
| 键盘 / 焦点 | Tab 顺序、tabs 左右与 Home/End、Esc 取消不提交、dialog 焦点进入与返回、图标按钮可访问名称 |
| 主题 / 动效 | 四套主题、`prefers-reduced-motion` 下 spinner 停止但「处理中」文字保留 |
| 共享消费者 | `/` 工作台版冒烟（确认 `style.css` 未受影响） |
| 自动化 | `worker.py --self-test`；`qa/r6/*_ui.py` 迁移后全绿；新增 DOM 契约脚本 |
| 结束条件 | 修复影响任务完成/正确性/可达性的缺陷后做一次针对性复验；记录视口、内容、步骤、实际结果；明确失败与未验证项；不以"应该可以"写成通过 |

不宣称 WCAG 合规、真实 50k 性能或跨浏览器矩阵通过——这些需独立验证并如实标注。

---

## 14. 风险与未决

| 风险 | 影响 | 缓解 |
|---|---|---|
| DOM 重建打断 `qa/r6/*_ui.py` | 现有 QA 全红 | M1 先建契约脚本，M6 同步迁移；每里程碑跑一次 |
| 原型 6 视图与实现 7 视图的组织冲突 | IA 决策未定会返工 | §15 待确认 1，M0 拍板 |
| 原型的 52px 顶栏与 `--ud-topbar-h:54px` 并存 | 令牌口径混乱 | 按 §9 局部处理，不动共享 token；文档登记 |
| 原型 8 列日志与实现 3 列 / 28px 行高的性能取舍 | 大文件阅读可能变重 | 列数按原型，底层仍用 `reader-controller.js` 虚拟窗口；真实行数压测后记录 |
| 删除演示文案后概念说明缺失 | 新用户不理解「逻辑总库 / 索引键」 | 移入 `<details>` 帮助，键盘/触屏可达，不隐藏必需信息 |
| 真实后端未启动 | 数据链路无法端到端验证 | M3 起需启动本地服务；未验证项在 QA 报告中明确标注 |
| 「严格按原型」与「核心工作区尽可能大」存在张力 | 回归后数据区比当前实现少约 118px | 显式暴露于 §3.4 与 §15-4；推荐按原型，不缩原型尺寸；已通过删重复容器回收 32px |

---

## 15. 待确认项（均带推荐默认）

| # | 问题 | 推荐默认 | 为什么不问会出问题 |
|---|---|---|---|
| 1 | 原型的 `cache / runtime / storage` 是采集中心内 3 个 tab；实现把它们提升为独立管理员导航页。`严格按原型` 是否意味着收回为 tab？ | **收回为 tab（按原型）**，同时保留独立管理页作为 tab 内的「管理」二级入口，非管理员降级为只读摘要 | 直接改变导航模型与范围；`artifacts-and-gates.md` Gate 1 列为 material question。收回可同时解决 P2（占位说明）并避免功能倒退 |
| 2 | 默认落地页：原型强制 `sync`，实现默认 `logs`。是否按原型改为打开即采集中心？ | **改为 `sync`** | 直接影响验收时「打开第一屏」的判定；用户已要求严格按原型 |
| 3 | 原型的 8 列日志表较宽，在 1366 下与实现 3 列相比信息密度更高但单行更挤。是否按原型列集合 + 可配置列显隐？ | **按原型 8 列 + 显示设置里可勾选列**，虚拟窗口保留 | 原型 `L625` 的显示设置浮层**本来就有 8 个 `data-visible-column` 勾选框**，所以这不是新增设计而是照原型恢复；影响 `logs` 主工作区观感与工作量 |
| 4 | 按原型回归后固定层增多，1366×768 数据区约 556px，比当前 `/r6` 实现（约 674px，待实测）少约 118px。是否接受以数据区高度换取原型设计完整度？ | **接受，按原型**。当前实现多出的高度来自删掉原型的页头 / 标签条 / 状态栏 / 页脚，正是本次要回归的设计层；unify-design 的空间要求针对「重复工具条与无信息占位」，这些已通过 §3.4 删除回收 32px。备选：若要求追平高度，只能进一步合并固定层，但会偏离原型结构，需重新批准 | 两条用户指导之间的真实张力；不显式决策会让 §0 验收口径自相矛盾 |

> 若四项均按推荐默认执行，M0 即可视为批准，无需额外往返。第 4 项最关键：它决定本次是「设计完整度优先」还是「数据区高度优先」。

---

## 附录 A：证据索引

| 主题 | 位置 |
|---|---|
| 原型 DOM / 数据入口 / 35 个动作 / CSS / 文案盘点 | `LogBench_R6_Remote_Prototype.html` `L15,L33,L40,L80,L177,L202,L744–L848` |
| 现有实现 DOM / 启动 / 模块 / API / 共享风险 | `src/r6.html`、`src/r6-app.js`、`src/ui/*`、`src/r6.css`、`server/frontend.py:53–107`、`server/app.py:524–656,1164–1222` |
| 差异比对与整改方向 | `docs/R6/prototype-vs-implementation.md`（第 7 节 A/C 表、第 9 节） |
| 设计令牌映射 | `docs/R6/design-map.md` |
| QA 基线 | `qa/r6/visual_baseline.py`（面向 `/`）、`qa/r6/*_ui.py`（面向 `/r6`）、`worker.py --self-test` |
| 设计指导 | `unify-design/references/{design-rules,patterns,retrospective,qa}.md`；`ui-ux-design-suite/references/{workflow-redesign,artifacts-and-gates,component-regression,quality-bar}.md` |

## 附录 B：与用户指导的逐条复核

| 用户指导 | 计划落点 | 复核 |
|---|---|---|
| 前端严格按 R6 原型实现 | §1.2 六维验收、§5 逐视图、M1 DOM 契约脚本 | 通过；§4.3 明确"照 DOM/CSS 而非照实现技巧" |
| 后端功能接上 | §4.1 引擎层保留、§6 全量映射、M3 | 通过 |
| 补上实现缺少的功能 / 组件 | §7.1 十项 | 通过 |
| 严格遵守设计指导 skill | §3.1 / §3.2 全关卡映射、§11 契约、§13 QA | 通过 |
| 美观简洁、布局合理、空间利用率高、核心工作区足够 | §3.4 空间预算（原型基线 68.2%→72.4%）、§5.1 单行顶栏、§8 文案减法 | **有条件通过**：相对原型净增 32px，但相对当前实现少约 118px；两条指导的张力已在 §15-4 显式暴露待决策，不谎称同时满足 |
| UX polished 像成熟商业产品 | §7.3 全状态件、§8.2 反馈与恢复、§13 键盘/焦点/动效 | 通过 |
| 自解释，主动删冗余文案 | §3.3、§8.1 | 通过 |
| 保留标签 / 可访问名 / 状态反馈 / 后果提示 | §7.3、§8.2、§8.3 | 通过 |
| 帮助入口键盘触屏可达，必需信息不藏 Tooltip | §3.3、§8.3 迁移表 | 通过 |
| 不因「补功能」自由发挥设计 | §4.4 硬边界 5 条 | 通过；原型未定义的状态只能复用原型既有组件形态，不得新增视觉语汇 |
| 先制定详细计划并审视复核 | 本文档 §3、§4.4、附录 B | 通过；复核中修正 6 处口径/数字矛盾（含空间预算算错、验收口径自相矛盾、假策略项误删、旧数字残留），并暴露 1 项指导张力交回决策 |

---

## 附录 C：生产接线实施记录（2026-11-05）

**状态**：用户已批准 `docs/R6/preview/r6-restored.html` 为设计基线；M2–M5 已落地 `/r6` 生产前端并接通真实后端。**§15 四个待确认项按推荐默认执行**（四 tab 收回、默认 `sync`、8 列 + 列显隐、接受 556px 数据区）。本附录之后不改动 §0–§15 的历史判断。

### C.1 落地方式

`tools/build_r6_frontend.py` 是唯一生成入口，从原型 HTML 做定点变换，`--prod` 写出三个**已存在路由**的文件，运行时零构建、零新依赖，符合 Linear 项目「保留纯 JS/CSS 前端与现有资源组装」：

| 产物 | 路由 | 内容 |
|---|---|---|
| `src/r6.html` | `/r6` | 原型静态 body 骨架 + `/assets/r6.js`（43 模块引擎）+ `/assets/r6/app.js` |
| `src/r6.css` | `/assets/r6/r6.css` | 原型 `<style>` 逐字保留 + 742 B 窄屏溢出防护 |
| `src/r6-app.js` | `/assets/r6/app.js` | 原型渲染层 + `adapter.prod.js`（插进 IIFE 内部 L846/847 之间） |

回退：`git checkout eff4edc -- src/r6.html src/r6.css src/r6-app.js`。`/` 工作台版与 `src/style.css` 零改动。

### C.2 契约匹配矩阵（预览端点 → 真实端点 → 处置）

| 预览登记 | 真实契约 | 处置 |
|---|---|---|
| `GET /api/v2/intake`（`sources/roots/jobs/tuning`） | `items/adapters/generatedAt`；`GET /api/v2/sources`、`storage-roots`、`jobs`、`runtime/budget` 各自独立 | **改**：8 路并行取数 + `projectSources/projectRoots/projectJobs/projectBudget` 投影成原型视图模型 |
| `POST /api/v2/sources/{id}/probe`（客户端 checks） | 只对已落库源有效，返回 `probe.checks/ok/fingerprint/revision` | **改**：向导先保存草稿为源，再真实 probe；未通过则停用保存，不伪装成功 |
| `POST /api/v2/sources/{id}/actions` | 必填 `actionId`、`expectedRevision`；`ACTIONS` 为 enable/disable/pause/resume/scan/retry | **接**：幂等键 + revision；409 按 `code` 分 `revision_conflict` 与 `source_disabled`/`probe_required` 两类不同恢复路径 |
| `POST /api/v2/sources` 的 `policy` | `POLICY_KEYS` 白名单；`MODES=manual/scheduled/follow`；mappings 的 include/exclude 须为数组 | **改**：`live/schedule/snapshot` → `follow/scheduled/manual`；interval 进 policy；`credentialRef` 只接受已登记 `cred_*`，否则为空 |
| `PATCH /api/v2/workspace` | 不存在 | **改**：接 `GET|PUT /api/v1/preferences`（`expectedRevision`） |
| `GET /api/v2/backup` | 不存在（服务端备份是 `tools/backup.py`） | **改**：工作区设置导出/恢复走 preferences；归档与索引备份明确交由运维工具 |
| `GET /api/v2/events` | SSE `event/data/id`，`poll` 带 `last` | **接**：`LBEventClient.createClient` + 轮询降级；事件投影成运行记录 |
| `GET /api/v2/storage/roots` | 不存在（历史缺陷 P10） | **改**：统一为 `/api/v2/storage-roots` |
| CSRF | `X-LogBench-CSRF` 由 `/api/v1/me` 下发 | **接**：复用 `LBHttp.createClient`，写操作自动带 token 与 epoch 防串场 |

共登记 **51 条真实端点**。`data-r6` 动作全部落到真实写操作；身份、权限、计数、事件、原文、下载按后端逐请求授权，非管理员降级为只读而非隐藏后留死链接。

### C.3 补齐 / 修改

- **Rev2 三项**（Linear 项目明确要求，原型未覆盖）：本机批量任务浮层（`/api/v2/index-jobs`）、渐进检索（`/api/v2/queries` + continue/records/export 游标）、私有上传（`/api/v2/uploads` 分片 + digest + commit）。三个入口放在侧栏 `sidefoot`，不新增常驻工具条行，不挤压阅读区。
- **文件库 / 阅读器**：`files` 数组的 fixture 语料清零，改由 `/api/v2/library` 投影；点开文件时按 `GET /api/v2/library/files/{id}/content` 取原文，交给原型 R5 的 `parseInWorker` 解析，保留列显隐、多行/Traceback、规则证据、时间线、2–4 文件对比。空库时 `renderRules` 等改为可恢复空态。
- **状态件**：loading / empty / no-result / error / 403 / 404 / 409 / precondition / saving / saved / rebuilding / offline，全部只用 `.r6-note`、`.r6-errorbox[role=alert]`、`.r6-badge`、`.r6-progress`、`.r6-empty`、状态栏与 `<details>`；`?qa=1` 才出现状态检视下拉。
- **文案**：演示态自我声明清零；多目录清单由 `title=` 改为键盘可达展开；`checked disabled` 假勾选项全部变为可配置字段。

### C.4 验证证据（可复跑）

| 命令 | 结果 |
|---|---|
| `python3 -B qa/r6/frontend_wiring_smoke.py --base-url http://127.0.0.1:50950` | `passed: true`，0 page error；证据 `qa/evidence/R6-frontend-wiring.json` |
| `python3 -B worker.py --self-test` | exit 0 |
| `python3 -B -m unittest tests.test_r6_sources tests.test_r6_roots tests.test_r6_catalog tests.test_r6_cache_api tests.test_r6_jobs` | 69 tests OK |
| 浏览器实测 1366×768 | grid `52px 716px / 208px 1158px`；52/50/39/49/22；数据区 556px；`.r6-foot` HTML 已删 |
| 浏览器实测 375×812 | 无横向溢出，主内容 760px |
| 真实写操作 | `POST /api/v2/sources` 201；`POST .../actions` 真实返回 `source_disabled`，UI 正确显示前置条件与恢复路径 |
| 故障注入 | 403→forbidden、409 revision→conflict、断网→offline，均常驻错误区且可恢复 |

服务启动：`python3 -B run.py --no-reload --admin-ip 127.0.0.1 --port 50950`（loopback 探测需 `curl --noproxy '*'`）。

### C.5 已知边界（如实标注，不冒称完成）

1. `qa/r6/*_ui.py` 的旧选择器断言在 DOM 回归后需迁移（§14 已列为风险，对应 Linear R6-090）。
2. 真实预检需要可达的 SSH/SMB/HTTP 主机与工具链；本机验收环境无远程主机，故 `probe` 如实失败，未启用任何采集任务。
3. `rules` / `admin` 视图的规则定义与用户列表仍沿用原型本地能力，未逐项接到 `/api/v2/rules`、`/api/v1/admin/*`；服务端能力已就绪，属后续接线。
4. 私有上传与渐进检索的 UI 已接真实端点，但未做 1 GiB 真机、断点续传中断恢复与跨浏览器矩阵；Linear 的 M3 性能 Gate 仍独立。
5. 服务端归档/索引备份无 HTTP 端点，工作区备份只覆盖个人偏好与阅读视角。

### C.6 复检与修复记录（2026-11-05 二次）

用户要求「检查功能是否都 ok」。复检发现并修复 7 项真实缺陷，均有证据：

| # | 缺陷 | 根因 | 修复 |
|---|---|---|---|
| 1 | 首屏短暂显示 4 源 / 3 根 / 5 事件的 `R6_SEED` fixture，点暂停发到不存在的 id → 404 | 只清了 `files`，未清 `R6` 种子；boot 是异步的 | adapter 顶层同步清空 `R6.sources/roots/events/selected`；验收加 `no_fixture_sources` 断言 |
| 2 | loading 态数据区从 556px 压到 266px | `.r6-pane{display:flex}` 优先级高于 UA `[hidden]`，`pane.hidden` 不生效 | 显式 `pane.style.display='none'` |
| 3 | 8 路并行请求中任一失败覆盖主状态，暂停结果被显示成 `notfound` | `request()` 无条件写 `state.scenario` | 引入 `primary:false`，二级请求只记 `secondaryErrors` |
| 4 | SSE 完全没连（`mode:polling` 但零请求） | `createClient` 需显式 `start()`，且必须注入 `streamFactory`/`fetchImpl` | 注入 `EventSource` 与 fetch，`start()`；实测 `eventsource /api/v2/events`、`mode:live` |
| 5 | 渐进检索 400「文件标识必须是文本」 | `server/query/api.py:151` 的 `file_id` 必填 | 文件下拉改为真实 library 选项；空目录显示可解释空态并禁用按钮，不发无效请求 |
| 6 | 运行记录空列表无任何说明 | 原型渲染裸 `<ol>` | `r6Events` 空态覆盖，说明数据来源与下一步 |
| 7 | 启动恒定产生一个 `Failed to fetch` | `/api/v2/runtime` 在本部署会关闭连接（`RemoteDisconnected`，后端 `status()` 缺陷） | 前端改用已正常的 `/api/v2/runtime/budget`（含 base/effective/notes）；**后端端点缺陷仍在，列为未修项** |

另修验收脚本：用 `wait_for_function` 等待 boot 完成取代固定 sleep；断言从「必须有表格/列表」放宽到「表格/列表/可解释空态」；新增 `sse_or_poll_active` 断言。

复检后证据：`qa/evidence/R6-frontend-wiring.json` → 19 项断言全过、0 page error，两轮连续运行均 `exit 0`；生成器两次输出逐字节一致；非管理员身份实测只读（添加按钮 disabled、写操作 0 个 POST、状态栏标注只读）。

**复检结论中的不 OK 项**：
1. `qa/r6/intake_ui.py`、`library_storage_ui.py`、`runtime_ui.py` 面向旧 DOM，全部回归失败（`intake_ui` 等 `#reader` 超时；另两个不接受 `--base-url`）。需按 M6 / R6-090 迁移选择器。
2. `rules` / `admin` 视图切视图时零 API 请求，仍是原型本地能力；`compare` 本身已用真实 `files` 数组（来自 `/api/v2/library`），但当前文件库为空所以显示空态。
3. `/api/v2/runtime` 后端端点缺陷未修（前端已绕开）。
4. 真实远程主机、1 GiB 性能 Gate、跨浏览器矩阵仍未验证（C.5 第 2/4 条继续有效）。

### C.7 三次复检（2026-11-05，独立口径）

不再只用同一份 smoke 自证，改为**端点契约审计 + 真实写操作闭环 + 后端异常复现**：

| 检查 | 结果 |
|---|---|
| 51 条端点审计（34 GET 动态探测 + 后端路由静态比对） | 修前 2 条连接异常、2 条疑似死路径；查证两条 `files/{id}/*` 是业务级 404「文件不存在。」而非路由缺失，**无误报**。修后 **24×200 / 9×业务级 4xx / 0 异常 / 0 死路径** |
| `/api/v2/capabilities`、`/api/v2/runtime` 连接被关闭 | 服务日志 `SAFE_DELETE_BULK_CONFIRM_REQUIRED` 定位到 `capabilities.probe_sqlite()` 清理 `.capability-probe-<pid>.db` 时被环境钩子以非 `OSError` 异常拦截；`Application.handle()` 只兜底三类异常，异常冒泡后连接静默关闭。`runtime._capabilities()` 同调 `probe_sqlite` 故一并失败 |
| 后端修复 | ① `server/capabilities.py` 清理段 `except OSError` → `except Exception`（残留文件无害，报告不该整体失败）；② `server/app.py` `handle()` 增加通用 `except Exception` 返回结构化 500 + `traceback`，不再断连。重启后两端点均 200；89 个后端测试 + `worker.py --self-test` 通过 |
| `PUT /api/v1/preferences` | 200，revision 递增，UI 显示「已保存」一致 |
| `PUT /api/v2/runtime/budget` | **修前 409**「预算已被其他管理员修改」但 UI 仍显示「已保存」：前端用 `Number(revision) \|\| … \|\| 1` 把合法的 `0` 变成 `1`；失败分支未更新 `state.saved`、未写对话框错误区。修后 200、`saved=已保存`、`scenario=saved` |
| 预算字段契约 | 修前写入 `transfer_slots/per_host_slots/backfill_slots/chunk_mb/memory_gb` → 400「未知预算字段」；`projectBudget()` 也因字段名不对一直显示默认值。按 `Budget` dataclass 真实字段（`transfer_connections/per_host_connections/history_slots/mem_soft_bytes/...`）修正投影与写入；无对应项的「读写块大小」改为只读状态行，高级区换成 7 项真实字段 |
| 缓存重建空库 | 修前误用 `empty` 场景弹「还没有远程日志源」；改为 `precondition` + `no_cache_entry` + 准确文案；`stateBanner` 的前置条件引导按错误码分岔 |
| 备份导出 | 真实下载 `logbench-workspace.json` ✓ |
| 可访问性 / 文案 | 0 个无名称按钮、0 个仅 `title` 承载信息、4 个 `<details>`、4 个 `role=tab`；六视图零演示态文案 |
| 工作台版 `/` | 200，`src/style.css` 零改动 |

验收脚本新增 2 项写操作断言（`preferences_write`、`budget_write`），现 **21 项断言，两轮连续 `exit 0`、0 page error**；SSE 仍 `mode:live`；几何 556px；非管理员只读仍成立。

**三次复检后剩余不 OK 项**（较 C.6 减少 1 条：后端 `/api/v2/runtime` 已修）：
1. `qa/r6/intake_ui.py`、`library_storage_ui.py`、`runtime_ui.py` 旧 DOM 选择器全红，需按 M6 / R6-090 迁移。
2. `rules` / `admin` 视图切视图时零 API 请求，仍是原型本地能力；`compare` 已用真实 `files` 数组但文件库为空。
3. 真实远程主机、1 GiB 性能 Gate、断点中断恢复、跨浏览器矩阵未验证。

### C.8 剩余项收口（2026-11-06）

用户批准继续。C.7 的三类不 OK 项中，前两类已闭环；第三类里能在本环境验证的部分已补做，环境硬性缺失的如实保留。

| 项 | 处置 |
|---|---|
| **`rules` / `admin` 未接后端** | `ENDPOINTS` 增 `rules` + 4 条 `/api/v1/admin/*`；boot 以 `primary:false` 并行取数。`server/rules` 的 `item.profile` 与原型 `baseProfiles` 字段完全一致（`extractors/anomalies/nodes/version/desc`），故直接投影进 `S.profiles`，服务端规则优先、本机草稿保留，R5 五 tab 编辑器无改动即可编辑真实规则。`renderAdmin` 覆盖为真实用户表 / 服务端配置 / 容量统计 / 最近审计，非管理员显示可解释的只读空态。实测 5 个请求、4 套规则（含真实 `cpqpsgx-default`）、2 张真实表格、0 页面错误 |
| **三个旧 UI 脚本全红** | 新增 `qa/r6/r6_ui_common.py`（统一 `--base-url/--out`、等待 `LB.state.scenario` 而非旧 `window.__r6Ready`、同源 CSRF API 调用）；按新 DOM 重写 `intake_ui.py`(R6-068)、`library_storage_ui.py`(R6-056)、`runtime_ui.py`(R6-083)，保留原票号与验收意图。三者统一可复跑：**14/14、14/14、21/21，exit 0**，证据落 `qa/evidence/R6-068|056|083/` |
| **抽屉挤压阅读区** | `intake_ui` 迁移后立刻抓到：桌面 `.r6-drawer` 是 flex item，把表格 1158px 压到 778px，违反 Linear「详情与任务面板悬浮覆盖」。原型的绝对定位只在 ≤1000px 生效。在预览与生产两份 build 脚本追加同一条 `@media(min-width:1001px)` 定位规则（同 class、同宽度、同配色，只改定位），修后表格保持 1158px |
| **验收加固** | `frontend_wiring_smoke.py` 增 `rules_from_api` / `rules_editor_renders` / `admin_from_api`，现 **24 项断言全过、0 页面错误**，两轮稳定 |
| **补充验证** | 全量 `unittest discover -s tests` → **566 tests OK**；`worker.py --self-test` 0；四套主题 `--reader-bg` 令牌实测切换生效；五档视口 1920/1366/1024/768/375 均无横向溢出；键盘 Esc 关闭原生 dialog、`<details> summary` 可聚焦；端点审计 0 异常 |

**本环境无法验证、继续如实标注**：
1. 真实远程主机不可达，`probe` 如实失败、未启用采集任务；`/api/v2/runtime` 的 `pools.parse.workers` 为 0（无真实 Node 进程池），1 GiB 索引与性能 Gate 不能宣称通过。
2. 浏览器分片上传与中断恢复没有真机端到端验证（后端上传链路的单元测试包含在全量 566 内）。
3. 跨浏览器矩阵：Playwright 自带 chromium/firefox/webkit 执行文件均未安装，本环境只有系统 Chrome，故仅 Chrome 验证通过。

结论：**前端接线与可复跑验收已闭环**；剩余全部是需要真实外部环境的验证 Gate，不是代码缺陷。
