# M0 Gate 证据：基线、契约、原型补全与测量可复现

> Gate 票：`R6-008`　｜　核验范围：`R6-001`–`R6-007`　｜　判定日期：2026-11-02
> 本文件只做核验，不重复实现。每条给出**判定 / 证据路径 / 版本 / 复跑命令**。

## 1. 逐项判定

| # | 检查项 | 判定 | 证据 |
|---|---|---|---|
| 1 | 真实 v3.0.3 源码与原型资源哈希一致、资料实际可取得；附件未上传的限制已补齐或明确阻塞 | **PASS（1 项 BLOCKED）** | `docs/reference/source-manifest.json`、`qa/r6/baseline/sha256sums-check.txt`（89/89 OK）、`qa/r6/baseline/`。原始 ZIP 哈希 `07aa5d88…` 无法复核 → **B-002 BLOCKED**；已用树身份摘要 `3ae5222b…a097032` 作替代身份。unify-design 以本机 v1.0.1 skill 包为输入，47/47 令牌逐值一致（**B-005 已绕过**） |
| 2 | 记录 / 查询 / 进度 / ReaderProvider / schema 统一，旧语义迁移与隐私默认明确 | **PASS** | `contracts/r6/contracts.schema.json`（`logbench.r6/1`）、`docs/r6/contracts.md`、25+6 fixture、Python 15 项 / Node 10 项测试 |
| 3 | Rev2 新增任务/查询/私有上传有目标 HTML 与状态表，已有 R5 功能没有删除 | **PASS** | `docs/prototypes/r6-rev2.html`（12 状态）、`docs/r6/prototype-coverage.md`、`qa/evidence/R6-004/`（16/16 检查 PASS，15 张截图） |
| 4 | golden corpus 含跨块 / Unicode / Traceback / 重复无时间；20 / 100 MiB 与 1 GiB 可重复生成 | **PASS** | `tests/fixtures/r6/golden/`（4 夹具 21 记录）、`tests/r6/golden.test.cjs` 9 项、`tests/test_r6_fixtures.py` 4 项（含两次生成逐字节一致）；1 GiB 实测 20.2 s 生成 10 文件 / 6,465,534 条 |
| 5 | 旧链路诊断区分克隆、计算和渲染；性能目标与实测分开 | **PASS（渲染分段受限）** | `docs/r6/performance-baseline.md`、`qa/evidence/R6-006/search-baseline.json`、`src/shared/diagnostics.js` 11 项单测。克隆经 `postMessage` 单独计量（248–273 ms）；`render` 未能独立分段 → **B-011** |
| 6 | Python 3.9.11 与运行时能力预检有可复跑证据，缺失能力可降级，无环境误报 | **PASS** | `qa/r6/baseline/py39-tests.txt`（55/55）、`worker.py --self-test` 退出 0、`tests/test_r6_capabilities.py` 18 项、`tests/r6/capabilities.test.cjs` 7 项、`/api/v2/capabilities` 管理员限定 |

## 2. 交付物清单

```text
contracts/r6/contracts.schema.json       18 个 $defs / 8 个公开入口
contracts/r6/state-machine.json          查询与任务的状态机（未列出的边一律拒绝）
docs/r6/contracts.md                     C1–C13 不变量、Provider 同一性、v1→v2 迁移
docs/r6/baseline.md                      v3.0.3 行为基线
docs/r6/design-map.md                    unify-design 令牌映射与五档布局基线
docs/r6/performance-baseline.md          旧链路实测（目标与实测分列）
docs/r6/prototype-coverage.md            原型覆盖（A/B/C 三类 + 反例清单）
docs/r6/blockers.md                      B-001…B-013
docs/prototypes/r6-rev2.html             自包含原型
docs/reference/source-manifest.json      源码/包/测试清单
src/shared/contracts.js                  契约校验 + v1 投影（UMD）
src/shared/capabilities.js               浏览器能力探测（IndexedDB/OPFS/Blob.slice）
src/shared/diagnostics.js                11 阶段埋点 + 脱敏 + 预算
server/capabilities.py                   7 探针 → 7 特性
tools/r6_contract_validate.py            服务端契约校验（与 JS 同 fixture）
tools/inspect_environment.py             能力报告 CLI
qa/r6/{visual_baseline,token_check,search_baseline,prototype_rev2}.py
qa/evidence/{R6-001,R6-002,R6-003,R6-004,R6-005,R6-006,R6-007}.md
qa/evidence/{R6-003,R6-004,R6-006}/      截图与 JSON 证据
```

## 3. 测试总数（本 Gate 实测）

| 套件 | 命令 | 结果 |
|---|---|---|
| Python | `env -u PYTHONPATH python3.9 -m unittest discover -s tests -p "test_*.py"` | **92/92 OK**（8.65 s） |
| Node | `node --test tests/*.test.cjs tests/r6/*.test.cjs` | **70/70 pass** |
| 自检 | `env -u PYTHONPATH python3.9 -B worker.py --self-test` | 退出码 0 |
| 契约 | `python3.9 tools/r6_contract_validate.py` | 25 + 6 例，0 失败 |
| 原型 | `python3.9 qa/r6/prototype_rev2.py` | 16/16 PASS |
| 设计令牌 | `python3.9 qa/r6/token_check.py` | 47/47 一致 |

> 注：以上为**本次执行**的数字，不作为后续 Gate 的替代证据。

## 4. 不标 Done 的项

- **`R6-001` 保持 In Progress**：源码树身份已固定，但原始 ZIP 哈希无法复核（B-002）。Gate 判定不依赖该哈希，故本条不阻塞 Gate 通过。
- `R6-006` 的 `render` 分段缺失（B-011），由 `R6-011` 闭合；本 Gate 接受"克隆已分离、渲染未分离"的现状，因为继续等待会阻塞 M1 起的所有实现。
- 三处产品确认（打开方式默认值、任务角标口径、窄屏任务入口）保留待评审，不影响 Gate：原型已给出可切换形态。

## 5. 结论

**M0 Gate 通过（PASS），1 项资料 BLOCKED 已登记且不阻塞下游。**

可以进入波次 B（本机纵切）：`R6-009`–`R6-018`。契约已冻结，任何实现都必须以 `contracts/r6/contracts.schema.json` 为准；如需变更，必须升 `contract_version` 并同步双侧校验与 fixture。
