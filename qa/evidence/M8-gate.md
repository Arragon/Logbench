# Gate M8 · 跨来源视图、规则与缓存管理

> Linear：`INH-2327`（R6-084）· 里程碑 M8 · 结论：**六票通过。目标机与 Safari/Firefox 未复跑，记 BLOCKED（B-050）**

```bash
env -u PYTHONPATH ~/.local/bin/python3.9 -m unittest discover -s tests -p 'test_*.py'   # 551 tests OK
node --test tests/*.test.cjs tests/r6/*.test.cjs                                        # 322 tests pass
env -u PYTHONPATH ~/.local/bin/python3.9 qa/r6/product_workflows.py --out qa/evidence/M8-gate
# 20/20，6 个子报告全部引用成功
```

原始数据：`qa/evidence/M8-gate/product_workflows.json` 与 `qa/evidence/M8-*/*.json`。
本页每个数字都来自这些文件，没有手工填写。
Python 3.9.11；Node v22.22.2；
浏览器为系统 Chrome（Playwright `channel='chrome'`），不是 headless Chromium。

## 0 · 子脚本汇总

`product_workflows.py` 先跑自己的产品级流程（R5 回看 + M8 新面），再逐个引用六个场景报告；
任一子报告缺失或 `ok=false`，Gate 直接失败（`missing` 列表为空）。

| 票 | 脚本 | 项数 | 结果 | 证据 |
| --- | --- | ---: | --- | --- |
| R6-078 | `mixed_scope.py` | 15 | PASS | `M8-mixed_scope/mixed_scope.json` |
| R6-079 | `global_time.py` | 14 | PASS | `M8-global_time/global_time.json` |
| R6-080 | `compare_windows.py` | 12 | PASS | `M8-compare_windows/compare_windows.json` |
| R6-081 | `rule_impact.py` | 26 | PASS | `M8-rule_impact/rule_impact.json` |
| R6-082 | `cache_manager.py` | 23 | PASS | `M8-cache_manager/cache_manager.json` |
| R6-083 | `runtime_ui.py` | 29 | PASS | `M8-runtime_ui/runtime_ui.json` |
| R6-084 | `product_workflows.py` | 20 | PASS | `M8-gate/product_workflows.json` |

合计 139 项浏览器级断言，0 失败。

## 1 · R5 能力没有被新页面挤掉

Gate 第一段刻意只做 R5 的事，因为"新页面能开"和"旧阅读路径还能用"是两个断言：

| 断言 | 实测 |
| --- | --- |
| 本机文件经真实 `<input type=file>` 导入 | `1` 个文件 |
| 打开后渲染记录行 | `2` 行 |
| 时间线渲染节点 | `1` 个 |
| 搜索命中并在可见行渲染高亮 | `#rows mark` 2 个（断言 ≥1） |
| 悬浮详情显示记录内容与证据 | `时间 2026-05-01 10:00:02.000000 / 行 1–1 / 版本 1` |

截图：`qa/evidence/M8-gate/product-workflows.png`（R5 阅读面 + M8 页签，同一会话同一进程）。

## 2 · 混合范围（R6-078）

真实服务端文件 + 真实 `File` 对象（浏览器内存中的本机文件）放进同一个视图：

- 合并结果同时含 `{server, local}` 两个来源，按时间升序，**同一时间点的两条记录都保留**（不因去重丢掉一条）。
- 全部子查询完成时 `order_final=true`、`count.relation='exact'`；计数文案是"共 10 条"，未完成时只报完成度。
- **没有权限的文件返回 `relation: "unknown"`、`matched: 0`**，不是 `exact 0`；失败文件在 `coverage.files` 里指名
  `f_ffffffffffffffff / server / not_found / 文件不存在。`——不静默丢弃。
- 纯本机范围**不发起任何服务端查询**（`server_queries=0`，`/api/v2/queries` 请求 0 次），仍返回 2 条记录。
- 本机文件标记 `LOCALFILE-XYZZY` 从未出现在任何离开页面的请求里（见第 6 节）。

## 3 · 全局时间与视角（R6-079）

- 工具条时间入口不独占一行：实测高度 `40`px（断言 ≤44px），与 32px 控件、28px 数据行同一密度体系。
- 选择树同时列出本机与服务器来源（6 个根节点）；勾选目录得"已选 2 个文件"；取消其中一个文件后文案变成
  **"已选 1 个文件（含部分选择目录 2）"**——部分选择说出来，不假装全选。
- 应用范围后渲染 10 条，计数"共 10 条"；点取消不改变当前结果（10 → 10）。
- 起点晚于终点：提示"开始时间不能晚于结束时间。"并禁用"应用范围"，不让非法区间进查询。
- 视角保存到本机（`saved_views=1`，含目录选择与时间），刷新后恢复上次视角并重新解析出 2 个文件、重新出记录。
- 更细的错误编码 `invalid_date`（`2026-13-01`）、`invalid_time`（`24:00`）、`invalid_fraction`（10 位小数）、
  `range_inverted` 由 `tests/r6/global-time.test.cjs` 覆盖；浏览器侧只验了区间倒置的文案与禁用态。

## 4 · 时间窗口对比（R6-080）

- `rows_per_pane = [7, 7]`：两个 pane 各 7 个时间槽，行数一致靠"同一时点矩阵"而不是各自滚动。
- `empty_rows = 4`：缺记录的槽留空，**不补前值**；同一时间戳的两条记录占两行（`duplicate_slot=2`）。
- 工具条锚点显示 `2026-05-01 09:59:00.000`；滚动后锚点跟到 `10:00:15.000`。
- `offsets = [84, 84]`：同步开启时两个 pane 的 `scrollTop` 字节级一致（`84 = 3 × 28`）；关掉同步后
  `[0, 84]`，另一个 pane 不跟随——同步是"同一时点行"，不是滚动百分比。
- 切 2×2 布局后网格确实两列，并写入个人设置 `compare_layout: "grid"`；768px 视口下页面横向溢出 `0`。
- **浏览器只跑了 2 文件。** 3 文件默认三列、4 文件默认 2×2 由 `tests/r6/compare-axis.test.cjs` 断言
  （13 项，含"上限被拒绝而不是静默裁剪"），但 3/4 pane 的真实滚动同步未在浏览器验过，见 B-051。

## 5 · 规则影响与缓存管理（R6-081 / R6-082）

| 场景 | 可见 | 受影响 | 规则层 | 检索层 | 结构层 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 当前生效规则 | 2 | 0 | 0 | 0 | 0 |
| 只改颜色/形状/名称 | 2 | 0 | 0 | 0 | 0 |
| 新增一条异常条件 | 2 | 2 | 2 | 2 | 0 |
| 应用并重建之后 | 2 | 0 | 0 | 0 | 0 |

应用 2 个文件（`applied=2`），结构层签名前后相同——**改规则不重分帧**。
缓存页：清理 `rules + search` 两层后原文 sha256 `1a1908ad…3dfe13` 逐字节未变，
`raw` 清理请求被 `400 raw_not_purgeable` 拒绝，覆盖退回 `structure` 而不是报"没有缓存"。

## 6 · 网络隐私断言

Gate 全程挂 `request` 监听，对每个请求的 URL、postData、header 做子串搜索：

- 本机文件名 `browser-only.log`：**0 次**出现在任何离开页面的请求里。
- 标记串 `LOCALFILE-XYZZY`：**0 次**。
- 检查结果 `[]`（空违规列表）。
- 本机文件的行只经本地 IndexedDB 与内存合并，服务端只收到 `file_id` 与时间范围。

## 7 · 权限与错误恢复

| 场景 | 期望 | 实测 |
| --- | --- | --- |
| 非管理员读运行状态 | 403 | `403` |
| 非管理员预览规则影响 | 403 | `403` |
| 不存在的对比文件 | 记入 coverage，页面可继续 | `not_found` 条目 + 其余 pane 正常 |
| 故意触发 404 的控制台错误 | 只允许资源加载类 | 过滤 `Failed to load resource`，其余 0 |
| 页面脚本错误 | 0 | `[]` |

`Origin` 与 CSRF 校验在 Gate 里用真实 `http://localhost:<port>` 通过，没有放宽服务端。

**没有原型模拟残留。** 三张管理页首屏都是真实 API：没有采样数据时运行页打印"未采样"而不是 `0`
（`runtime_ui` 断言 `first_page_ms=null`、`disk.latency_ms=null`），"查询结果缓存"一项如实标注未实现，
缓存页的"未建/待重建"来自真实指针缺失而不是占位常量。规则页在真实 Node 池里跑试跑，不是前端假执行。

## 8 · 本轮发现并修复

1. 本机 provider 在 `generation` 为空时把 `null` 传给 `IDBKeyRange`，混合范围首屏直接抛
   `Invalid key range` —— 现在先 `getMetadata()` 解析代次。
2. 服务端 provider 读 `lastQuery.query_version`（恒 `undefined`），导致对自己的查询
   `poll/cancel/resume` 报 `stale_query` —— 改用 `lastQueryVersion`。
3. 规则重建用新规则 tag 拼 shard 路径，重算 `rules` 时 `ShardError: 索引分片不存在` ——
   改为读**已发布结构版本**的 `stats.shard`，这也是"不重新分帧"能成立的前提。
4. 对比面 `scrollTop` 在 `replaceChildren` 后未布局即赋值，被浏览器重置为 0；
   且 scroll 事件触发重新查询 —— 现在先设 `applying`、强制一次布局，改为按偏移复制同步。
5. 运行页保存预算后结果面板被 `render()` 重建而清空 —— 改为从 `state.appliedResult` 渲染，
   刷新也不丢"服务端确认的生效结果"。
6. `window.__r6` 在 `boot()` 末尾才赋值，早期的调试/自动化入口拿不到 `state` —— 提前到开头创建、末尾合并。

## 9 · 不能承诺的事（BLOCKED / 未覆盖）

- **目标机未复跑（BLOCKED）。** 全部数据来自本机 macOS（Apple Silicon）+ 系统 Chrome。Linux 双路 E5、
  HDD 数据盘上的解析池收缩、磁盘延迟采样、4 MiB 以上单批，均未实测（B-050）。
- **Safari / Firefox 未测。** 代码未用 Chrome 专有 API，但"没测过"不等于"支持"（B-050）。
- **范围模式没有"继续加载"。** 混合范围一次取完当前窗口，超出上限时只报完成度，不能翻页续取（B-042）。
- **10 分钟对比跨度不可调。** 槽宽与跨度是常量，长窗口对比会退化成"每槽很多行"（B-044）。
- **无时间戳的记录在对比视图里没有替代展示。** 它们被排除在时间槽之外，只在列表模式可见（B-043）。
- **规则编辑器没有撤销栈、快捷键与批量导入**（B-045）；草稿不跨会话（B-046）。
- **缓存清理只到单文件粒度**，没有按存储根批量清理（B-047）。
- **运行指标不落库**，`first_page_p50/p95` 进程重启即归零（B-048）。
- **诊断导出不含文件名**，"哪个文件卡住"仍需回任务面板（B-049）。
- **对比视图浏览器门禁只跑 2 文件。** 3/4 pane 的布局与同步只有单元测试覆盖（B-051）。
