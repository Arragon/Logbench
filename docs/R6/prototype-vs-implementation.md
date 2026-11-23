# R6 设计原型 HTML 与当前前端实现：差异比对

> 比对日期：2026-11-04　｜　基线：`eff4edc`
> 原型：`LogBench_R6_Remote_Prototype.html`（399,228 B · SHA-256 `4d655689…71aa0` · Linear `INH-2336`）
> 实现：`src/`（服务路由见 `server/app.py:1172–1192`）
> 依据：源码静态比对 + Chrome 无头浏览器对两份 HTML 的几何实测（1366×768，CSS 本地加载、JS 仅原型侧完整启动）。样式声明值与实测值已分别标注。

---

## 1. 结论摘要

**这不是"设计稿 vs 实现"的线性关系，而是三个代码体的关系。** 最有价值的发现是：

1. **原型本身是「工作台版前端（`/`，R5 系列）+ R6 覆盖层」的产物，不是全新写的设计稿。** 原型的 CSS 块与 `src/style.css` 逐行完全相同率 **87.1%（74/85 行）**，前 35 行逐字一致；`src/app.js` 中 91.9%（374/407）的长代码行在原型中逐字出现，140 个函数里 139 个同名，`data-act` 动作 66/66 全部包含于原型。时间线也支持这一方向：`src/app.js`/`style.css` 为 10-31，原型 HTML 为 11-03 09:50。
2. **当前项目同时存在两套前端**：`/` → `src/index.html`（工作台版，与原型 R5 层同源、已接后端）；`/r6` → `src/r6.html`（R6 大文件前端，**另起架构**，并非原型的移植）。因此所有"原型有而实现没有"都必须标注是哪个入口：很多功能在 `/` 上完整可用，只在 `/r6` 上缺失，**不是功能丢失**。本报告第 7 节逐项标注了这一点。
3. **R6 前端没有照原型实现设计层，而是另起一套 DOM/CSS/交互重写了外壳。** 采集中心四个 tab 的标签、四步向导的标题与顺序、日志源表前四列确实一致，但这只是**交互文案契约**对齐；外壳布局、视图组织、控件密度、可见设置入口均未以原型为准。按既定目标「前端设计严格按 R6 原型实现、后端能力在此基础上补齐」，以下设计层差异一律判为**待整改**，不是已接受的取舍。

综合判断：**设计层未按原型落地，这是需要纠正的偏离，不是架构取舍。** `/r6` 与原型代码复用度为零，等于把原型降级成了一份「交互契约清单」。但原型零 `fetch` 且数据入口集中（`makeDemo`、`files`、`R6_SEED`、`restoreCache`），完全可以在**保留原型 DOM/CSS/交互**的前提下，把合成数据换成 `/api/v2/*` 并接入现有 Worker/provider 引擎。结论：**后端该补，设计不该换。** 详见第 9 节整改方向。

---

## 2. 比对对象与方法

| 维度 | 原型（单文件） | 工作台版前端（`/`） | R6 大文件前端（`/r6`） |
|---|---|---|---|
| 入口 | `LogBench_R6_Remote_Prototype.html`（本地打开） | `/`、`/index.html` → `src/index.html` | `/r6`、`/r6.html` → `src/r6.html` |
| 代码体量 | 848 行 / 399 KB（CSS、JS 压缩在长行里） | `app.js` 430 行 + `style.css` 93 行 | `r6.html` 177 + `r6-app.js` 1,479 + `r6.css` 1,312 + `ui/*.js` 7,938 + `shared/*.js` 等 |
| 模块形态 | 单个 `<script>`，全局函数 | IIFE，`/assets/workbench.js` 由 `server/frontend.py` 拼装 | `window.LB*` 工厂 + IIFE，`/assets/r6.js` 按 `server/frontend.py:73–94` 顺序打包 |
| 渲染 | `innerHTML` 模板字符串全量重绘（`L720`、`L477`、`L804`） | 同原型（同源） | `el()` DOM 构造，局部更新；虚拟窗口 `reader-controller.js` |
| 状态 | 全局 `S`（R5，`L207`）+ 全局 `R6`（`L744–756`） | 全局 `S`（`app.js:19`），前缀 `logbench-user-<uid>-` | 模块内 `state` + `LBWorkspace`（IndexedDB/OPFS） |
| 数据 | **零 `fetch`**；`makeDemo`（`L177`）、`files`（`L202`）、`R6_SEED`（`L744`）全为合成 | **`/api/v1/*` + `/admin/*`**（`service.js:13,73,192,252–261`），不是 v2 | **`/api/v2/*` 为主**（约 45 条路径，路由分派见 `server/app.py:530–1151`），仅身份等少量 v1 调用 |
| 浏览器库 | 原型预览命名空间 + IndexedDB 缓存 | `logbench-r4-files`（`r5.js:104`） | `logbench-r6`（`shared/local-store.js:26`）；本机阅读链路走 Worker+IndexedDB，不发 HTTP |
| 测试钩子 | `window.LogBenchRemotePreview`（`L841`）、`window.LogBenchPrototype`（`L846`） | **同样导出** `window.LogBenchPrototype` / `window.LogBench`（`app.js:427–428`）——这是同源的又一硬证据 | `window.__r6`（`r6-app.js:1454–1474`）；原型专属的 `LogBenchRemotePreview` / `R6_SEED` 在 `src/` 中 **0 命中**，是替换而非沿用（见附录 B） |

原型自己明确声明边界：`L804` "设计原型 · 合成数据 · 不连接远程服务"、`L219` "合成演示数据 · 无服务端连接"。**因此原型中任何容量、覆盖率、延迟、检查点数字都不能作为验收基准**，只能比对信息结构与文案承诺。

另有一个容易误判的点：**原型启动即进入采集中心**——`L847` `S.view="sync";render(false)`，并在 `restoreCache()` 结束后再次强制回 `sync`。所以直接打开 HTML 看到的第一个页面不是日志工作台，而是 R6 覆盖层的采集中心；当前 `/r6` 默认落在 `logs`。

### 复现命令

```bash
cd <repo>
shasum -a 256 LogBench_R6_Remote_Prototype.html     # 4d655689…71aa0
python3 - <<'PY'
import re
p=open('LogBench_R6_Remote_Prototype.html',encoding='utf-8').read()
css=p[p.find('<style>')+7:p.find('</style>')]
s=open('src/style.css',encoding='utf-8').read()
A,B=css.split('\n'),s.split('\n')
same=sum(1 for i in range(min(len(A),len(B))) if A[i]==B[i])
print('CSS 逐行相同率: %.1f%% (%d/%d)'%(100*same/min(len(A),len(B)),same,min(len(A),len(B))))
a=set(l.strip() for l in open('src/app.js',encoding='utf-8') if len(l.strip())>20)
P=[l.strip() for l in p.split('\n') if len(l.strip())>20]
print('app.js 长行在原型中逐字出现:',sum(1 for l in P if l in a),'/',len(P))
PY
```

---

## 3. 关键结构发现：原型是四层叠加，且含旧代残留代码

原型按代次累加，最终生效的是最外两层：

```
L804  render()               ← R6 覆盖层：S.view === 'sync' 时整页替换为采集中心
  └─ L720  render()          ← R5 层：追加存储状态徽标、版本号
       └─ L477  renderR4()   ← R4 层：logs / files / rules / compare / admin 分派
            └─ L618+         ← 当前代 renderLogTable / renderBody / renderInspector …
L219–260  renderLegacy*()    ← R3 旧代：13 个函数中至少 8 个只有定义、无外部调用点
L463 / L613 Object.assign(S,…)  ← 各代在启动时往同一个 S 上追加字段
```

R6 覆盖层用**运行时替换函数引用**挂载（`L799–804`）：保存 `r6OldSidebar/r6OldHeader/r6OldRender/r6OldFiles/r6OldLogs`，重写 `renderSidebar` 注入"采集中心"按钮、重写 `renderFiles` 为按存储根聚合的总库、重写 `renderLogWorkspace` 注入实时跟随条，另起一套 `data-r6` 事件委托（`L805–838`，switch 共 **35 个 case**）与 `data-act` 委托（`L318`）并行，并有第三组 `input` / `change` 委托（`L839–840`）负责草稿与预检失效。

R6 状态独立种子化：`R6_SEED`（`L744–755`）含 3 个存储根、4 个日志源（SFTP/SMB/HTTP/SCP）、5 条示例事件、`tuning` 7 个键（弹窗只暴露其中 6 个，`poll` 未展示）；持久化用 `localStorage['logbench-r6-review-state-v1']`（`L743`、`L764`），与 R5 层的 `logbench-r6-preview-local-*` 命名空间隔离。

**比对时必须区分可达与不可达代码**：按函数名 grep 原型会得到 238 个函数，而 `src/app.js` 只有 140 个；差额主要来自 `Legacy`/`R4` 旧代与 R6 覆盖层，实际生效的是最后一代加 R6 覆盖层。同理 `data-act` 81 个 vs `app.js` 66 个的 15 个差额，多在未挂载的旧代渲染函数里，**不代表工作台版功能退化**。反过来，`src/app.js` 的 66 个动作全部能在原型找到（交集 66/66），这才是同源关系的可靠证据。

---

## 4. 页面 / 视图对比

| 原型视图 | 当前 R5 前端 (`/`) | 当前 R6 前端 (`/r6`) | 说明 |
|---|---|---|---|
| `logs` 日志工作台 | 有（同源，接后端） | 有，`r6.html:77–81` 虚拟表 | R6 是重写：50k 逻辑窗口、`reader-controller.js` |
| `files` 文件库 | 有 | 有，`library.js`（1,313 行） | R6 加总库/本机/私有三源、回收站、批量删除 |
| `rules` 规则 | 有 | 有，`rule-impact.js`（仅管理员可见） | R6 加影响预览、分层重算、历史证据 |
| `compare` 时间对比 | 有 | 有，`compare.js`（2–4 文件共享时间轴） | R6 改为不加载全量时间戳的对齐矩阵 |
| `admin` 管理后台 | 有 | **无独立页面**，拆为 `rules/cache/runtime/storage` 四个管理页 | `r6.html:53–56`，由 `/api/v1/me` 决定显隐（`r6-app.js:1360–1367`） |
| `sync` 采集中心 | 无 | 有，`intake.js` + `source-wizard.js` | 见第 5 节 |
| — | — | 导入任务浮层 `jobs.js`、缓存仪表 `cache-meter.js`、服务端加速 `server-mode.js` | **实现有、原型无**；来自 Rev2 原型 `docs/prototypes/r6-rev2.html` |
| 全局时间工具 | **原型已有完整实现**：`showTime()`（`L254` 快速预设、A/B 起止日期+时刻、清除/应用范围），入口 `L479` `B('时间范围','time-panel',…)`，分派 `L331`；工作台版 `app.js` 同源亦有 | `/r6` 有 `global-time.js`：跨来源范围 + 文件树（`r6.html:64–65`）、UTC 标注、**9 位小数秒校验**（`:223–226`、`:396–404`）、最近使用与收藏视角（`:431–440`） | ⚠️ 改判：不是「实现有、原型无」，而是**原型已有、R6 重写并增强**；原型只缺 UTC / 小数秒 / 视角收藏 |

管理后台权限分层是原型完全没有的：原型的"管理后台"带 `演示` 徽标且任何人可点（`L221`），当前实现由后端 `capabilities.admin` 决定，**前端隐藏只是提示，写操作由 API 拒绝**（`r6-app.js:1358–1359`）。

---

## 5. 采集中心：设计契约的逐项落地情况

这是原型的核心交付物，也是对齐度最高的部分。

| 项目 | 原型 | 当前实现 | 判定 |
|---|---|---|---|
| tab 结构 | `远程日志源 / 存储位置 / 解析缓存 / 运行记录`（`L776`） | 同名四 tab（`intake.js:248–253`） | ✅ 一致 |
| 日志源表列 | `日志源 / 连接`、`目录 → 存储位置`、`同步状态`、`归档 / 索引覆盖`、`操作`（`L777`） | 前 3 列同名，第 4 列改为 `归档 / 滞后`（`intake.js:291–295`） | ⚠️ 有意收窄，见下 |
| 源详情抽屉 | 右侧 `r6-drawer` + 采集链路三步（`L778`） | 覆盖式抽屉 + `当前采集链路`（`intake.js`） | ✅ 结构一致，数据改真实 |
| 四步向导 | `连接 / 目录映射 / 采集策略 / 确认与预检`（`L789`） | `STEPS` 同名同序（`source-wizard.js:23`） | ✅ 一致 |
| 协议能力模型 | `r6Capabilities()`（`L766–770`），SFTP/SMB/HTTP/SCP 四协议，`list/range/identity/live` | 由 `/api/v2/intake` 的 `adapters` 下发，前端只呈现适配器实际支持项（`source-wizard.js:8–14`） | ✅ 更严谨，前端不再自行判定能力 |
| 预检 | 下拉切换 `ready / auth / nolist / norange` 四种**演示情形**（`L788`、`L821`） | 真实 `POST /api/v2/sources/{id}/probe`（`intake.js:600`），返回 `checks / configRevision / fingerprint`（`source-wizard.js:493–520`） | ✅ 演示态 → 真实态；演示情形切换器**已移除**（符合"不伪装"要求） |
| 校验规则 | 端口 1–65535、目录映射非空、目标路径禁止 `..`、目标子目录重叠检测（`L771–772`） | `validateStep()`（`source-wizard.js:122–176`）+ 服务端校验 | ✅ 保留并有扩展 |
| 暂停 / 继续 | 本地切 `status`，写一条示例事件（`L812`） | `POST /api/v2/sources/{id}/actions`，带幂等与 `expected_revision` | ✅ 真实化 |
| 存储位置管理 | `r6RootDialog` 添加/编辑（`L792`、`L824`） | 独立页 `storage-roots.js`，读写 `/api/v2/storage-roots` | ⚠️ 位置分离，见第 7 节 |
| 解析缓存 | tab 内 6 列表（文件/代次、归档原文、结构索引、规则结果、覆盖/状态、操作）+ 详情/重建弹窗（`L780`、`L794`） | 独立页 `cache-manager.js`（`/api/v2/cache/*`） | ⚠️ 位置分离，见第 7 节 |
| 运行记录 | tab 内示例事件列表 + `导出示例记录`（`L781`、`L833`） | tab 内真实 `collect` 任务表（`intake.js:411–437`）+ 独立 `runtime.js`（预算、审计、脱敏诊断） | ✅ 强化，导出示例按钮移除 |
| 资源设置弹窗 | 6 项并发/内存预算（`r6Tuning`，`L793`）；`R6_SEED.tuning`（`L753`）多一个 `poll` 键，保存时只校验 6 个 | `runtime.js` 的 `BUDGET_FIELDS`（`:33–45`）扩到 **13 项**：解析/查询/历史补采/写入/上传/连接/每身份限额/内存与热页预算；改动真实影响 Node worker 池（`qa/r6/runtime_ui.py`） | ✅ 真实化并大幅扩展 |
| 实时跟随条 | `跟随最新 / 追加事件示例 / 新增 N 条`（`L797`、`L835–837`） | `有 N 条新记录 · 跳到最新`（`r6.html:83–85`）+ `/api/v2/events/poll` 事件流 | ✅ 语义保留，去掉"追加示例"这种造假入口 |
| 连接表单 | 名称 / 地址 / 端口 / 凭据引用（4 个固定选项）（`L785`） | 另有用户名、`known_hosts` 指纹、SMB 共享名、允许环回、组织 CA、凭据引用列表来自 `/api/v2/credentials` | ✅ 实现超出原型，凭据不落浏览器 storage |
| 目录映射 | 远程目录 / 存储位置 / 子目录 / 规则（`L786`），可增删 | 另有包含/排除模式、递归子目录、最多 8 条，路径预览与 `..` 校验 | ✅ 实现超出原型 |
| 并发冲突 / 启用门禁 | 无（本地单用户） | `expected_revision` 409 保留草稿；未通过预检不允许启用 | ✅ 原型未覆盖的工程约束 |

**第 4 列的收窄是刻意且正确的**：`intake.js:403–405` 明写"本页只显示采集侧的原文水位；结构与检索层的版本、覆盖率与可清理项不在 M6 范围内，避免在没有真实数据时给出'全部已索引'的假状态"。原型该列显示 `924 MiB / 100%` 之类的合成覆盖率，属于只可参照、不可照搬的部分。

**四步向导是逐字段对齐后加强的**：`source-wizard.js` 与原型 `L785–789` 一一对应，但把"凭据引用下拉写死 4 个名字"换成服务端下发的凭据列表，把"演示情形预检"换成绑定 `configRevision` 的真实预检。原型 `r6Validate()` 的路径穿越与重叠目录检测被保留并扩展到包含/排除模式。

---

## 6. 视觉 / 几何对比

| 项 | 原型（CSS 声明值） | 当前 R6（`r6.css` / `style.css`） | 判定 |
|---|---|---|---|
| 侧栏宽 | **实测 208px**（`L33` `.lb-app` grid；`grid-template-columns: 208px 1158px`），≥1650px 时 224、≤1180px 时 186 | **实测 208px**（`r6.css:80`，硬编码） | ✅ 数值一致；但令牌 `--ud-sidebar-w:234px`（`style.css:15`）在两边都被绕过 |
| 顶栏高 | **实测 52px**（`grid-template-rows: 52px 716px`，`L33`） | **实测 54px**：`.r6-header{height:var(--ud-topbar-h,52px)}`（`r6.css:22`）里的 `52px` **只是令牌缺失时的回退字面量**；`r6.html:11` 根节点带 `.ud-app`，而 `style.css:15` 定义 `--ud-topbar-h:54px`，故计算值与实测值均为 54px | ⚠️ **差 2px，已实测确认**：`#app` 计算样式 `display:flex`（`r6.css:11–16` 覆盖 `.lb-app` 的 grid），使 52px grid rows 失效；同时 `--ud-sidebar-w:234px` 被 208px 硬编码绕过。这正是 `design-map.md` 结论 3 在 R6 的残留 |
| 日志视图工具条 | `.toolbar{height:48px}`，`L49` 覆盖为 `46px`；Rev2 原型 `docs/prototypes/r6-rev2.html` 实测 **44px**（`qa/evidence/R6-004/prototype-check.json`）；`qa/evidence/R6-003/geometry.json` 中 R5 生产页为 46px | **实测 40px**（`r6.css:112–114`），`overflow:hidden` 不换行 | ⚠️ 比 R5 基线少 6px、比 Rev2 目标少 4px；当前更紧凑，但把功能挤出视口的风险更高 |
| 采集中心页头 / tab / 内部条 | **实测** `.r6-head` 50px、`.r6-tabs` 39px、`.r6-toolbar` 49px（均在 `L80`，窄屏覆盖 `L83`） | `.r6-tabs` 无固定高度（自适应） | ⚠️ **同名不同物**：原型 `.r6-toolbar` 指采集中心内部条，当前 `.r6-toolbar` 指日志工具条，按类名比对会误判 |
| 日志行高 | `28–36px`（`.log-row` 36，`.log-grid` 最小 28） | `28px`（`r6.css:147`） | ✅ 落在原型下界 |
| 日志列 | `COLUMNS` 8 列：行号/时间/等级/软件/脚本:行号/函数/PID/TID（`L609`），可显示设置调整列宽 | 3 列：时间 148 / 等级 62 / 消息 1fr（`r6.css:144`） | ⚠️ **显著简化**：软件、脚本、函数、PID/TID 移入详情抽屉，且无列设置 UI |
| 详情浮层 | 布局内 `aside.inspector` 350px（旧代）、R6 源详情 `r6-drawer` 380px | `position:absolute` 覆盖式，`min(460px,46vw)`（`r6.css:190–192`） | ✅ 都保证"打开不挤压表格"；当前几何由 `qa/r6/visual_baseline.py` 实测锚点 0px 位移 |
| 底栏 | `.app-status{height:22px}`（`L40`；`L38` 是 `.page-heading`） | 无独立底栏，状态位在顶栏 `#status` | ⚠️ 已按 `eff4edc` 提交改为内联提示 |
| 断点 / 变体 | 480 / 650 / 760 / 800 / 1000 / 1180 / 1250 / 1280 / ≥1650；`pointer:coarse`、`prefers-reduced-motion`、密度/对比度/强调色变体齐全 | `r6.css` 宽度断点收敛为 600 / 640 / 860 / 900 / 1100；`style.css:21–24` 的全局 coarse-pointer / reduced-motion / 密度 / 对比度 / 强调色**仍会继承生效** | ⚠️ 全局变体没有丢，但新增 R6 组件没有自己的变体适配，也没有对应的设置 UI |
| 令牌别名 | 直接用 `--ud-line` 等 kit 令牌 | `r6.css:513–518` 自建 `--ud-border/--ud-danger/--ud-warn/--ud-mark` 别名，因 `style.css` 未定义这些名 | ⚠️ 不破坏主题，但令牌口径分叉 |
| 主题写入位置 | `document.body.dataset.theme=S.theme`（`L720`、`L804`，共 3 处） | `data-theme` 写在 `#app` 上（`r6.html:11`） | ⚠️ 选择器作用域不同：`--reader-*` 定义在 `.ud-app`，两边都能生效，但任何依赖 `body[data-theme]` 的旧 CSS/测试会失效 |
| 主题 | `data-theme` 四套 + 显示设置浮层（`themeCards` `L249`、`showDisplayR4` `L257`、`showDisplay` `L625`） | 继承 `style.css` 令牌，四主题下拉（`r6.html:25–30`） | ✅ 令牌层一致（`design-map.md` 记 47/47 对齐）；⚠️ 密度/对比度/强调色**无 UI 入口** |

视觉层结论：**侧栏宽度、行高、令牌色板、浮层不挤占原则一致**；差异集中在顶栏 2px 的令牌偏差、工具条 40 vs 44/46px、日志 8 列 → 3 列、断点收敛与底栏形态。原型把 8 列高密度表格作为既定基线，当前 R6 只保留 3 列——这是阅读体验上最实质的偏离，需确认是否为 Rev2 的有意决策。另外 `qa/evidence/R6-003/geometry.json` 的 52/208/46 是 **R5 页面（`/`）** 的实测值，不能直接当作 `/r6` 的验收基线。本次实测方法：把 `r6.html` 的两条 CSS 路径改成本地 `file://`、去掉外链脚本后交给无头 Chrome 量 `getBoundingClientRect()`；原型 HTML 自包含，直接 `file://` 打开量同名选择器。两份均只量外壳，未启动业务脚本逻辑，因此数值稳定可比。

---

## 7. 差异清单（按优先级）

### A. 原型有、`/r6` 没有（但 `/` 工作台版多数仍保留）

> **口径声明**：本表严格按入口区分。"工作台版"指 `/`（`src/index.html` + `src/app.js` + `src/service.js`，`server/app.py:1172–1173`），"R6 大文件前端"指 `/r6`（`server/app.py:1174–1179`）。不要把前者缺位写成整个项目缺失。

| # | 项目 | 原型证据 | `/r6` 状态 | `/` 工作台版 | 影响 |
|---|---|---|---|---|---|
| A1 | 书签 UI | `S.bookmarks`（`L207`）、侧栏"我的书签" | `shared/workspace.js:150–188`、`migrate-r5.js:226` 有数据层与迁移，**无 UI** | 有（同源 `app.js`） | 数据已迁移但用户无法访问；属"已建未接" |
| A2 | 列选择 / 列宽 / 列设置 | `S.columns`、`r5-columns-reset` | 0 命中 | 有（`app.js` 同源） | 与第 6 节 8 列→3 列相关 |
| A3 | Traceback 折叠、上下文行数 | `r5-trace-fold`、`S.contextSize` | 0 命中 | 有（`app.js`、`r5.js`、`service.js`） | 多行日志可读性下降 |
| A3b | 标记形状 / 配色 | `Engine.markerShapes` 10 种、`markerColors` 12 色（`L113–114`） | **并未缺失**：`rule-impact.js:38–41` 的 `SHAPES` 10 种逐项同名，`COLOURS` 12 色（6 色重合）；UI 在 `:421` 形状下拉、`:751` 色板。只是从阅读工具条移到了规则编辑页 | 有 | ⚠️ 早前「0 命中」是检索词偏差，已更正 |
| A4 | 拖放导入 | `#drop-overlay`（`L85`）、`importFilesLegacy` | 0 命中，仅 `file-input` 点击选择 | **有**（`src/index.html` 同名 `#drop-overlay`；`app.js:269` 的 `dragenter/dragover/dragleave/drop` 全链路） | 只在 `/r6` 缺 |
| A5 | 管理后台用户/IP 表 | `S.users`（`L207`）、`renderAdmin` | 无用户管理页，只有 `/api/v1/me` 只读身份 | **有**（`service.js:3` `adminTab:'users'`、`:5` `ROLE_NAMES`、`:148` 管理员门禁） | 需确认是否由 `/` + 运维承担 |
| A6 | 规则绑定到目录/文件 | 绑定对话框 `bindingDialog` / `directoryDialog`（`L511–512`）、提交与标签同步（`L513–514`） | `/api/v2/bindings*` 存在、`library.js` 有引用，无绑定对话框 | 有（`app.js` 同源） | 部分覆盖 |
| A7 | 显示设置：密度 / 高对比度 / 强调色 | `L249` / `L257` / `L625` 的阅读显示浮层 | `style.css:19–23` 令牌与变体都在，**无触发入口** | 有 | 无障碍与触屏适配能力存在却不可达 |
| A8 | 工作区备份导出 / 导入 / 请求持久许可 | `L722` 存储面板弹窗、`L724` `workspaceBackup()` | **`r6.html` 无入口**；`/assets/r6.js` 的 `R6_PAGE_PARTS`（`frontend.py:55–95`）不含 `backup.js` / `migrate-r5.js`，页面侧只有 `workspace.js` 的 IndexedDB 设置 | **有**：`r5.js:117–119` 的 `workspaceBackup/exportWorkspace/validateBackup` + `app.js:427` 导出 | 换浏览器/换 origin 的迁移路径在 `/r6` 不可见；格式关系见下 |
| A9 | 原生 `<dialog>` 焦点约束 | `<dialog id="dialog">` + `.showModal()`（`L85`） | `r6.html` 0 个 `<dialog>`，全为 `div role="dialog"` | 有（`<dialog>`） | 模态焦点陷阱与 Esc 需自行维护 |
| A10 | **原型预览态本地存储未迁移** | 两个命名空间：`logbench-r6-preview-local-*`（`L170`，设置）与 `logbench-r6-review-state-v1`（`L743`，R6 覆盖层） | `migrate-r5.js:1–8` 仅从 v3.0.3 的 `logbench-user-<uid>-db` 迁移；原型两个预览命名空间在 `src/` 中 0 命中。两版 IndexedDB 也各自独立：工作台版 `logbench-r4-files`、R6 版 `logbench-r6` | 工作台版同样不读预览命名空间 | 评审人员在原型里保存的源配置/规则草稿/视角不会自动进入 R6 |

**数据迁移的两个边界（需明确写进升级说明）**：

1. `migrate-r5.js` 的迁移源是工作台版快照；原型 HTML 是独立预览态，其 `preview-local-*` 与 `review-state-v1` 两套键均未被读取。
2. **备份格式要分三套，不能一句「不兼容」概括**（已核实）：
   - 原型 `L724` `workspaceBackup()` 与工作台版 `r5.js:117` **逐字相同**：`{format:'LogBench Workspace Backup',version:1,workspace:{schema:'logbench.workspace',version:1,state…},files[]}`。两者**互相兼容**，这是同源关系的又一证据。
   - `/r6` 页面侧用 `workspace.js:30–37` 的 IndexedDB `preferences / view / bookmarks / meta` 四类键，不读写上述 JSON 备份。
   - 另有 `shared/backup.js` 的 `logbench.backup/1`（`manifest.json` + 分片 `volume-N.bin`，`:1–8`），属服务端分阶段恢复，**不进入 `/assets/r6.js`**（不在 `R6_PAGE_PARTS` 清单）。
   - 结论：**原型备份可导入工作台版（`/`），但不能直接导入 `/r6`**；`/r6` 也没有导出入口。升级说明需写清这条单向兼容关系。

> 结论：A1–A9 中，**A4 / A5 / A8 明确在 `/` 上完整可用**，不能算项目级功能丢失；A1 / A7 / A8 / A9 是 `/r6` 真实缺口。若产品口径是"R6 最终替换工作台版"，这些是迁移缺口；若口径是"工作台版继续服务高密度阅读、`/r6` 服务大文件与远程采集"，则属可接受的双轨差异，**需要明确写进范围**。A1 / A7 的数据层与令牌层已存在，只差 UI 接线，性价比最高。

### B. 实现有、原型没有（Rev2 与工程化增量）

- 真实 API 全套（`/api/v2/sources|probe|actions|jobs|intake|library|cache|runtime|storage-roots|uploads|bindings|rules|queries|events/poll` 等）。
- Worker RPC（`shared/rpc.js`、`workers/*`）、provider 抽象（`providers/local|server|combined`）、租约/检查点（`leases.js`）、索引库（`index-store.js`）、能力探测（`capabilities.js`）、契约（`contracts.js`）、R5→R6 迁移（`migrate-r5.js`）。
- 管理员分级、显式服务端加速授权（`server-mode.js`）、私有上传断点续传、回收站与批量删除、配额与持久化许可、缓存仪表、内联操作提示、渐进搜索三种诚实文案（`search-controller.js`）。
- 真实浏览器门禁：`qa/r6/visual_baseline.py`、`intake_ui.py`、`library_storage_ui.py`、`runtime_ui.py`、`prototype_rev2.py`（后者针对 Rev2 原型）。

### C. 同名但行为/位置不一致（最需要决策）

| # | 项目 | 具体差异 | 建议 |
|---|---|---|---|
| C1 | **采集中心 storage / cache 两个 tab 是占位说明** | `intake.js:384–407` 显示"由存储位置管理维护(R6-056)""解析缓存与重建管理在 M8（R6-082）提供"。但 `storage-roots.js` 与 `cache-manager.js` **当前已实现**，且这两个管理页仅管理员可见（`r6.html:53–56`）。非管理员点这两个 tab 会看到指向自己无权访问的页面的说明 | 把已实现页面直接挂载/链接到 tab（按权限降级为只读摘要），或移除空 tab；并更新已过期的"M8 提供"文案 |
| C2 | 日志源表第 4 列 | 原型 `归档 / 索引覆盖`，实现 `归档 / 滞后`；真实索引覆盖在独立缓存页 | 在表头或详情补一个到缓存页的链接，避免覆盖率信息从采集视角消失 |
| C3 | 状态来源 | 原型 `R6_SEED` 写死 `following / recovering / limited`；实现由 `/api/v2/intake` 与 `/api/v2/jobs` 返回 | 无风险，属正确落地 |
| C4 | 工具条 46 → 40px、8 列 → 3 列 | 见第 6 节 | 用 `visual_baseline.py` 补一组 1440/1024/390 的截图对照，确认是否为有意收敛 |
| C5 | 原型 `app-status` 底栏 → 顶栏 `#status` | `eff4edc` 已把操作结果改为内联提示，顶栏只报阅读上下文 | 与原型不同但符合 Rev2；建议在报告中留痕即可 |
| C6 | 顶栏 52 → 54px 的令牌偏差 | `r6.css:22` 读 `--ud-topbar-h`(54px)，侧栏却硬编码 208px 绕过 `--ud-sidebar-w`(234px)；同一外壳一半用令牌一半不用。**根因已定位**：`52px` 是 `var(--ud-topbar-h, 52px)` 的 CSS 回退字面量，`r6.html:11` 的 `.ud-app` 由 `style.css:15` 提供 `54px`，所以 52 是回退值、54 是计算/实测值，二者并不矛盾。**口径分歧已收口**：交叉复核曾认为"顶栏 52px 一致"，但那取自 `qa/evidence/R6-003/geometry.json`（量的是 `/` 工作台版）；对 `/r6` 单独实测 `getBoundingClientRect()` = 54px、`display:flex`、令牌 54px，以实测为准 | 二选一：让外壳统一读令牌（会改动全部断点），或把 54/208 登记为产品事实并修正注释；与 `design-map.md` 结论 3 一并闭环 |
| C7 | 源码注释已过期 | `r6-app.js:18–21` 仍写"server-side sources are not wired yet, so the provider list is local only"，但 `r6-app.js:1034` 已挂 `window.__r6.server.open`、`server-mode.js` 已接 v2 上传/断点续传 | 删改注释，避免后来者按注释判断范围 |
| C8 | 默认落地页 | 原型启动强制 `S.view='sync'`（`L847`），首个画面是采集中心 | 当前 `/r6` 默认 `logs`。若评审以"打开第一屏"为准，需明确以哪个为准 |
| C9 | **状态枚举改名，中文文案保留** | 原型 `r6States`（`L759`）：`following / recovering / idle / limited / paused / error`；实现 `intake.js:18–26`：`following / catching / disabled / snapshot / paused / gap / error`。映射 `recovering→catching`、`idle→disabled`、`limited→snapshot`，新增 `gap`，中文文案基本一致 | 文案对齐但契约不对齐；若 QA 脚本按原型枚举键断言会误报，需登记进 `docs/R6/contracts.md` |
| C10 | **视图粒度重构** | 原型 6 个 `S.view`：logs / files / rules / compare / admin / **sync**，解析缓存/运行记录/存储位置只是 sync 页内 3 个 tab；`/r6` 把 cache / runtime / storage 提升为独立 `data-view`（`r6.html:53–56`），采集中心内仍保留同名 4 tab 且其中 2 个是占位说明 | 这是原型未覆盖的组织变更；需以 C1 收口，避免两套导航并存 |
| C11 | 注释准确性 | 原型 `L86` 声明"lossless strings / No eval"与实现一致；`r6-app.js:18–21` 则称"server-side sources are not wired yet"，但 `r6-app.js:1034` 已挂 `window.__r6.server.open` | 只修实现侧注释，原型注释无需改动；详见 C7 |
| C12 | QA 钩子替换而非沿用 | 原型 `window.LogBenchRemotePreview`（`L841`，含 `state/capabilities/validate/getDraft/reset`）在 `src/` 中 **0 命中**；替代物是 `window.__r6`（`r6-app.js:1454–1474`）与 `qa/r6/*_ui.py`；`R6_SEED`(`L744`) 也无对应常量 | 若旧评审脚本依赖 `LogBenchRemotePreview` 将全部失效；按附录 B 登记替换关系 |

---

## 8. 风险与边界

1. **不要用原型里的数字做验收**：`R6_SEED` 的容量、覆盖率、延迟、资源预算全是合成值（`L744–755`），原型自己多处声明"示例/不连接远程服务"。
2. **不要用 grep 原型函数名统计功能**：四代叠加 + 13 个 `Legacy` 渲染函数（至少 8 个无外部调用点）会让功能数虚高；按 `data-act`/`data-r6` 可达动作统计更可靠（原型 81 个 `data-act`、35 个 `data-r6` case）。
3. **几何已实测但只测了外壳**：本次用无头 Chrome 在 1366×768 量了两份 HTML 的外壳（结果见第 6 节），`/r6` 侧去掉了外链脚本、未启动业务逻辑，业务渲染后的布局仍以 `visual_baseline.py` 为最终裁决。
4. **权限路径未验证**：C1 的"非管理员看到无效指引"由源码推断，建议用 `intake_ui.py` 加一个 viewer 身份用例确认。
5. **别把三份不同原型的数字混用**：本文件（R6 Remote Prototype）日志工具条为 46px、采集中心内部条 49px；`docs/prototypes/r6-rev2.html` 实测 44px；`qa/evidence/R6-003/geometry.json` 的 52/208/46 是 R5 工作台页。交叉验证中出现过把 44px 当作"本原型"的说法，需按来源区分。
6. **代码行数统计口径**：`src/ui/*.js` 为 7,938 行，但整个 `src/`（含 shared / providers / workers / events / 样式）超过 2 万行；不要用单个子集代表实现体量。

## 9. 整改方向：按原型回归设计层

> 目标口径：**前端设计严格按 R6 原型实现；后端能力在原型设计基础上补齐。** 本报告用途由此从「差异说明」改为「整改清单」，第 4–7 节所有设计层差异一律判为待整改，不再视为已接受的取舍。
> **详细执行计划见 `docs/R6/r6-frontend-restore-plan.md`**（DOM 级诊断、逐视图清单、数据映射、预览关卡、有界 QA、待确认项）。

**可以只补后端、不换设计。** 依据：

- 原型零 `fetch`，但数据入口高度集中：`makeDemo`（`L177`）、`files`（`L202`）、`R6_SEED`（`L744–755`）、`restoreCache()`；并非把假数据散落在各渲染函数里。
- 原型已有可复用的完整前端骨架：全局 `S` / `R6` 状态、`data-r6` 35 个事件分派（`L805–838`）、分层 render（`L804` 覆盖层）、本地缓存恢复与 `window.LogBenchPrototype` 导出。
- 原型 R5 层与 `/` 工作台版同源（CSS 87.1%、代码行 91.9%），它不是不可维护的一次性 demo。

**分三层处理，只重写中间那一层：**

| 层 | 内容 | 处置 |
|---|---|---|
| 设计层（不可协商） | DOM 结构、CSS 外壳与节拍、视图组织、交互控件、可见设置入口 | **以原型为基线回归**：恢复 6 视图与 `sync` 默认落地、四 tab 完整内容、显示设置浮层、52px 顶栏 / 208px grid 外壳、46–49px 工具条与留白 |
| 引擎层（该补的后端） | `/api/v2/*`、`shared/*`、`providers/*`、Worker + IndexedDB、真实 probe、权限与冲突处理 | **保留现有 `/r6` 实现**，不动设计；这是相对原型真正该新增的部分 |
| 适配层（要新写的） | 把引擎层异步数据源接到原型数据入口、把演示态情形切换换成真实预检、把渲染触发点接上 SSE / 轮询 | 净新增代码；工作量集中在这里 |

**当初为什么会走成重写（推断，非已证实结论）**：`src/shared/*` + `providers/*` + `workers/*` 是为「本机大文件解析（Worker + IndexedDB、文本不上服务端）」建立的，原型没有这个需求。换数据引擎是必要的，换 DOM / CSS / 交互不是；实际把两件事捆在一起做了，且未将设计层按原型回归。

**执行顺序**：① 逐页比对原型 R6 覆盖层 DOM 树与 `/r6` 的 DOM 树，量化对齐成本（尚未做，是第一步）；② 按第 7 节 A / C 表把设计层差异排成回退清单；③ 在保留引擎层的前提下改外壳与视图；④ 用 `visual_baseline.py` 以原型为基线做截图验收。

---

## 10. 建议的下一步（按性价比排序）

1. 修 C1：回填采集中心 storage/cache 两个 tab，或按权限收敛，并清掉过期文案。
2. 修 C7：删改 `r6-app.js:18–21` 的过期注释；顺手按 C6 统一外壳令牌口径。
3. 明确 A1–A9 的范围口径：`/r6` 是否要替换 `/` 工作台版。若"是"，排迁移计划；若"否"，把双轨写进 README 与 Linear 验收口径（这点纠正了"按整个项目判缺失"的常见误判）。
4. 在已有 1366 实测基础上补 1024/390 的 `/r6` vs 原稿截图对照，重点看工具条 40px 下的控件溢出、顶栏 54px 与 3 列表的信息密度；顺便确认 C6 是否要统一令牌。
5. 按 C9 把状态枚举映射登记进 `docs/R6/contracts.md`，避免 QA 脚本按原型键名断言。
6. 接线已存在的数据层与变体（A1 书签/视角、A7 显示设置、A8 备份入口），成本最低、收益直接。
7. 按 A10 在升级说明里写清：原型预览态存储不迁移、原型备份 JSON 与 R6 不兼容。

---

## 11. 附录：原型标识符 → 实现标识符映射

用于把原型评审脚本、截图断言、设计文档里的旧名字换算到当前代码。**"无对应"表示实现里确实不存在，不是没找到。**

### A. 概念 / 页面

| 原型 | 原型位置 | 当前实现 | 位置 | 关系 |
|---|---|---|---|---|
| `S.view='sync'` 采集中心 | `L776`、`L804`、`L847` | `view='intake'` + `LBIntake.create()` | `r6.html:52`、`r6-app.js:1346–1351` | 同名异物（改键名） |
| `R6.tab` 四 tab | `L776` | `intake.js` `state.tab` 四键同名 | `intake.js:248–253` | 完全一致 |
| `r6Page / r6Sources / r6Storage / r6Cache / r6Events` | `L776–781` | `intake.js` 的 `render / renderSources / renderStorage / renderCache / renderJobs` | `intake.js:238–245` | 结构对应，后两者退化为占位说明 |
| `r6Detail` 源详情抽屉 | `L778` | `intake.js` `renderDrawer()` | `intake.js:229–235` | 覆盖式抽屉，几何不同 |
| `r6Wizard` 四步向导 | `L789` | `LBSourceWizard.open()`，`STEPS` 同名 | `source-wizard.js:23`、`intake.js:188–198` | 一致 + 增强 |
| `r6Capabilities(protocol, scenario)` | `L766–770` | `/api/v2/intake` 下发 `adapters` | `intake.js:205–208` | 前端判定 → 服务端能力 |
| `r6Validate(d)` | `L772` | `validateStep(step)` + 服务端校验 | `source-wizard.js:122–176` | 保留并扩展 |
| `r6RootDialog / r6Tuning / r6CacheDetail` | `L792–794` | `storage-roots.js`、`runtime.js`、`cache-manager.js` | `docs/R6/storage-roots.md` 等 | 从弹窗升级为独立管理页 |
| `r6Library` 按根归集总库 | `L795` | `library.js` 三源统一库 | `src/ui/library.js` | 概念延续，来源模型更细 |
| `r6Livebar / follow / append-demo` | `L797`、`L835–837` | `#new-records` 按钮 + `/api/v2/events/poll` | `r6.html:83–85`、`events/client.js` | 演示入口移除，语义保留 |
| `renderAdmin` 管理后台 | `L248` | 无独立页；拆为 rules/cache/runtime/storage | `r6.html:53–56` | 视图粒度重构 |

### B. 状态、数据与 QA 钩子

| 原型 | 原型位置 | 当前实现 | 位置 | 关系 |
|---|---|---|---|---|
| `S`（R5 工作台全局状态） | `L207`、`L463`、`L613` | 工作台版全局 `S`；`/r6` 改为模块内 `state` | `app.js:19`、`r6-app.js:25` | 一处同源、一处重写 |
| `R6_SEED` / `R6` | `L744–756` | **无对应**：真实数据来自 `/api/v2/intake` | `intake.js:205` | 合成种子移除 |
| `R6_KEY='logbench-r6-review-state-v1'` | `L743` | **无对应**；`/r6` 用 IndexedDB `r6/workspace/*` | `workspace.js:30–37` | 存储层整体替换 |
| `logbench-r6-preview-local-*` | `L170` | **无对应**；工作台版用 `logbench-user-<uid>-` | `app.js:9` | 命名空间不互通 |
| `makeDemo / FILES` 合成记录 | `L177`、`L202–203` | **无对应**：Worker 解析真实文件 | `workers/local-engine.js` | 演示数据移除 |
| `window.LogBenchPrototype` | `L846` | 工作台版同名导出；`/r6` 改为 `window.__r6` | `app.js:427`、`r6-app.js:1454` | 一半沿用、一半替换 |
| `window.LogBenchRemotePreview` | `L841` | **无对应**；由 `qa/r6/*_ui.py` 承担 | `qa/r6/intake_ui.py` 等 | 旧评审脚本会失效 |
| `data-act` 事件委托 | `L318` | 工作台版沿用；`/r6` 各模块自行 `addEventListener` | `app.js:318` 同源 | 事件模型分叉 |
| `data-r6` 事件委托（35 case） | `L805–838` | **无对应**；`intake.js` 用 `onclick` 闭包 | `intake.js:248–268` | 无统一 action 表 |

### C. 样式 / 几何

| 原型 | 原型位置 | 当前实现 | 位置 |
|---|---|---|---|
| `.lb-app{208px / 52px}` grid | `L33` | `.r6-app{display:flex}` + `.r6-nav{208px}` + `.r6-header{54px}` | `r6.css:11–22`、`:79–80` |
| `.toolbar{46px}`、`.log-grid` 8 列、`COLUMNS` | `L35`、`L49`、`L609` | `.r6-toolbar{40px}`、`.r6-row` 3 列 | `r6.css:112–114`、`:142–150` |
| `.r6-head/.r6-tabs/.r6-toolbar{50/39/49px}` | `L80`、`L83` | `.r6-tabs` 自适应高度 | `r6.css` |
| `.inspector{350px}` / `r6-drawer{380px}` | `L36`、`L80` | `.r6-drawer{min(460px,46vw)}` 覆盖式 | `r6.css:190–192` |
| `--ud-line` 等 kit 令牌 | `style.css` 同源 | 另建 `--ud-border/--ud-danger/--ud-warn/--ud-mark` 别名 | `r6.css:513–518` |
