# R6-003 · unify-design 令牌映射与 R5 视觉基线

> 对应 Linear：`INH-2246` · `[R6-003]`，里程碑 M0。
> 目的：把"R6 视觉沿用 unify-design v1.0.1"从口号变成可复跑的对照表与可 diff 的几何数字，并冻结 R5 已确认的界面承诺。
> 日期：2026-11-02。基线版本：LogBench 3.0.3（`d2422a7`）。

---

## 1. 结论

| # | 事实 | 证据 |
|---|---|---|
| 1 | **令牌层已经完整落地**：`src/style.css` 的 `.ud-app` 块与 kit `tokens.json` **47/47 逐值一致**，无缺失、无多余、无拼写差异 | `qa/r6/token_check.py` → `qa/evidence/R6-003/token-diff.json` |
| 2 | **产品级覆盖 4 项，属受控变体**，不是另起一套体系：`--ud-muted` 加深、`--ud-control-h` 34→32、`--ud-radius-lg` 9→8，另有 density / contrast / coarse-pointer 三个变体块 | `src/style.css:21–27` |
| 3 | **外壳布局绕过令牌**：`.lb-app` 硬编码 `208px / 52px`，而令牌声明 `--ud-sidebar-w:234px`、`--ud-topbar-h:54px`；宽屏断点再改为 `224px`、窄屏 `186px` | `src/style.css:33,41,42` + 实测 |
| 4 | **R5 的抽屉承诺在五个视口全部成立**：双击记录打开 `.inspector` 后，阅读区与时间线几何不变，锚点位移 `0px`，Esc 可关闭 | `qa/evidence/R6-003/geometry.json#drawer` |

结论 3 是唯一需要在 R6 里做决定的问题：要么让外壳改读 `--ud-sidebar-w`（一致性更好，但会改动全部断点），要么把 208/224/186 登记为产品事实并修正令牌注释。**本轮只登记，不改动**——`R6-003` 只采基线，生产样式变更属于 UI 实施票。

---

## 2. 令牌对照（关键项）

kit 值取自 `~/.workbuddy/skills/unify-design/assets/ui-kit/tokens.json`（v1.0.1，`$description` 明确"CSS remains authoritative"）。完整 47 项见 `token-diff.json`。

| 令牌 | kit v1.0.1 | R5 实际（computed） | 判定 |
|---|---|---|---|
| `--ud-bg` | `#f4f6f9` | `#f4f6f9` | 一致 |
| `--ud-surface` / `-alt` / `-strong` | `#ffffff` / `#f8fafc` / `#f0f4fa` | 同 | 一致 |
| `--ud-ink` / `-soft` | `#162238` / `#3d4b60` | 同 | 一致 |
| `--ud-muted` | `#65748a` | `#596b82`（产品加深） | 受控覆盖：正文可读性优先 |
| `--ud-subtle` | `#8793a5` | 同 | 一致 |
| `--ud-line` / `-strong` / `-control` | `#e2e7ef` / `#cdd6e2` / `#bbc7d6` | 同 | 一致 |
| `--ud-primary` / `-hover` / `-ink` / `-tint` | `#2563eb` / `#1d4ed8` / `#1d4ed8` / `#edf4ff` | 同 | 一致 |
| `--ud-positive` / `--ud-negative` | `#16835b` / `#bc3f4a` | 同 | 一致 |
| `--ud-amber` / `--ud-purple` / `--ud-sky` | `#b8741b` / `#7658b8` / `#347aa6` | 同 | 一致 |
| `--ud-focus` | `0 0 0 3px #93b7fa77` | 同 | 一致 |
| `--ud-font` / `--ud-mono` | 系统 UI / 系统等宽 | 同 | 一致；无网络字体、无 CDN |
| `--ud-xs/sm/md/lg/xl` | 11/12/13/15/20 | 同 | 一致 |
| `--ud-radius-xs/…/xl` | 4/6/9/12 | 4/6/**8**/12（产品覆盖 lg） | 受控覆盖 |
| `--ud-control-h` | `34px` | **`32px`**（产品覆盖） | 受控覆盖：更紧凑工作台 |
| `--ud-table-row-h` | `40px` | `40px`（实测行距 50–62，含徽标增高） | 一致；行高按内容自然增高 |
| `--ud-topbar-h` / `--ud-sidebar-w` | `54px` / `234px` | **`52px` / `208px`**（外壳硬编码） | **不一致**，见结论 3 |
| `--ud-speed` / `--ud-ease` | `160ms` / `cubic-bezier(.2,0,0,1)` | 同 | 一致 |

### 2.1 变体矩阵（kit 之外的既有能力，R6 必须保留）

| 变体 | 选择器 | 效果 | R6 处置 |
|---|---|---|---|
| 密度 | `.ud-app[data-density='comfortable']` | 控件 40、行 48、字号 +1 | 保留；新组件必须响应 |
| 对比度 | `.ud-app[data-contrast='high']` | 边框/控件线加深，`--ud-muted` → `#44536a` | 保留；新增浮层需覆盖 |
| 触屏 | `@media(pointer:coarse)` | 控件 42、行 46 | 保留；任务浮层按钮 ≥44 目标 |
| 强调色 | `.ud-app[data-accent='teal|indigo']` | 主色替换 | 保留，不作为 R6 默认 |
| 阅读主题 | `body[data-theme='ink|paper|graphite|nord']` | `--reader-*`、`--tok-*` 全套替换 | 保留四套，R6 不得减少 |

### 2.2 产品扩展令牌（kit 无对应，属 LogBench 领域语义）

- `--reader-bg / -alt / -ink / -muted / -line / -select`：阅读区独立配色，与外壳解耦，四主题各自定义。
- `--reader-error / -warn / -error-bg / -warn-bg`：仅用于行级语义，不用于装饰。
- `--tok-key / --tok-num / --tok-bool / --tok-str`：字段着色，只作用于日志正文，不进入普通表格。
- `--hm1 / --hm2 / --hm3 / --hmink`：多规则命中叠色，必须可叠加且不与语义色混淆。
- `--lb-time`：时间列宽度（208px 基准）。

**规则**：新增语义一律走 `--reader-*` 或新前缀，**不得**复用 `--ud-primary` 表达业务状态，也不得让主题变量渗入外壳。

---

## 3. 布局基线（实测，1366×768 为主基准）

固定输入：服务器文件库中的 `cpqpsgx_demo_A.log`（41 KB / 401 行），打开首屏默认分页，`ink` 主题。

| 视口 | header | sidebar | toolbar 行数 | 工作面板高 | 首行 y | 已挂载行 | 行距 | 虚拟区高 |
|---|---|---|---|---|---|---|---|---|
| 1366×768 | 52 | 208 | 1 | 600 | 167 | 18 | 61.9 | 10,866 |
| 1920×1080 | 52 | 224 | 1 | 912 | 167 | 24 | 50.3 | 10,018 |
| 1024×768 | 52 | 186 | 1 | 600 | 167 | 18 | 61.9 | 12,516 |
| 768×1024 | 52 | 0（收起） | 1 | 856 | 167 | 20 | 58.9 | 12,516 |
| 375×812 | 52 | 0（收起） | 3 | 571 | 242 | 17 | 63.6 | 12,516 |

判读：

- 桌面首屏数据区占 600/768 ≈ **78%**，高于"数据区不应被推到 200px 以下"的红线，符合 unify-design 对 1366×768 的要求。
- **toolbar 在 ≥768 宽度恒为 1 行**，时间范围没有独占一行；375 宽度折成 3 行属预期收纳，但仍保留 571px 数据区（70%）。
- 已挂载行 17–24，说明虚拟列表确实有界；`virtual-region` 高度 10k–12.5k 是内容总高，不是 DOM 数量。
- 侧栏宽度存在三套值（208 / 224 / 186）＋ 令牌 234，见结论 3。

### 3.1 抽屉几何承诺（R5 v3.0.3 的关键不回退项）

| 视口 | 打开 | `.inspector` 位置 | 阅读区 Δx/Δw | 时间线 Δx/Δw | 锚点位移 | Esc 关闭 |
|---|---|---|---|---|---|---|
| 1366×768 | 是 | x=913, w=430 | 0 / 0 | 0 / 0 | **0px** | 是 |
| 1920×1080 | 是 | x=1467, w=430 | 0 / 0 | 0 / 0 | **0px** | 是 |
| 1024×768 | 是 | x=571, w=430 | 0 / 0 | 0 / 0 | **0px** | 是 |
| 768×1024 | 是 | x=315, w=430 | 0 / 0 | 0 / 0 | **0px** | 是 |
| 375×812 | 是 | x=15, w=430 | 0 / 0 | 0 / 0 | **0px** | 是 |

`qa/r6/visual_baseline.py` 把"锚点位移 > 2px"或"阅读区/时间线宽高变化 > 1px"直接判为 FAIL 并返回非零退出码。当前五档全部通过，**这是 R6 后续所有 UI 票的回归闸门**。

---

## 4. 组件契约（R6 新增组件必须遵守）

| 层 | 既有事实 | R6 要求 |
|---|---|---|
| 外壳 | `.lb-app` grid：header 52 + sidebar（208/224/186）+ main | 不重写外壳；新增一级区域须走 grid 轨道，不绝对定位遮住数据区 |
| 导航 | 侧栏为双列 `feature-button` 网格（日志浏览为宽卡）+ `nav-button`（管理后台）+ 帮助 | 保持双列；不新增第二个同级入口 |
| 工具条 | `.toolbar` 单行：搜索框、筛选项、视图/时间/对比按钮、图标按钮 | 新增任务入口放进本行；**不得**把时间范围或任务进度单独占一行 |
| 阅读区 | `.log-virtual` > `.virtual-head` + `.virtual-region` > `.record-item[role=row]` | 沿用虚拟表；不得换成 kit 的普通 `<table>` |
| 详情 | `.inspector` 覆盖式，打开不改几何 | R6 的任务浮层沿用同一覆盖语言（右侧或底部浮层），不改阅读区尺寸 |
| 状态 | 行级语义色 + 文本徽标 | 颜色必须配文字；新增 `partial` / `at_least` / `unknown` 状态需显式文案 |
| 字体 | 正文 13px，辅助 11–12px，等宽只用于时间/路径/数值 | 新页面不得整体等宽，也不得把正文降到 10px |

---

## 5. 未覆盖与后续

| 项 | 状态 | 去向 |
|---|---|---|
| 四主题的文本对比度计算 | 未做（仅截图） | `R6-090` 视觉回归票按 WCAG 计算普通/选中/叠色/错误态 |
| 键盘路径与焦点返回 | 仅验证 Esc 关闭抽屉 | `R6-090` 与各 UI 票 |
| 载荷更重时的行距变化（Traceback 展开、多行卡片） | 未测 | `R6-028`/`R6-031` |
| 外壳令牌接线（结论 3） | 只登记 | 待 UI 负责人决定；若接线，需同步 41/42 行断点与 `--lb-time` |
| 任务浮层、私有上传、渐进搜索的原型 | 未产出 | `R6-004`（本票不越界） |

---

## 6. 复跑

```bash
# 1. 起一个隔离实例（不污染仓库 data/var）
env -u PYTHONPATH ~/.local/bin/python3.9 -B run.py --no-reload --port 50901 \
  --admin-ip 127.0.0.1 --data-dir /tmp/r6d/data --state-dir /tmp/r6d/var

# 2. 令牌对照（不需要浏览器）
env -u PYTHONPATH ~/.local/bin/python3.9 qa/r6/token_check.py

# 3. 五档视口 × 四主题截图 + 几何 + 抽屉闸门
env -u PYTHONPATH ~/.local/bin/python3.9 qa/r6/visual_baseline.py \
  --base-url http://127.0.0.1:50901 --out qa/evidence/R6-003 \
  --themes ink,paper,graphite,nord --open-file
```

`visual_baseline.py` 需要 `playwright` 与系统 Chrome（`channel='chrome'`，不下载 Chromium）。安装方式见 `docs/r6/blockers.md`。
