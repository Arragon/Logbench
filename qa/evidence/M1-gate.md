# M1 Gate 证据：公共引擎一致性与本机存储故障安全

> Gate 票：`R6-018`　｜　核验范围：`R6-009`–`R6-017`　｜　日期：2026-11-03
> 结论：**未通过（1 项部分、1 项受限）**。8 张实现票中 7 张 Done，故障注入路径尚未证明。

## 1. 逐项判定

| # | 必须证据 | 判定 | 依据 |
|---|---|---|---|
| 1 | 随机字节分块、snapshot/restore、UTF8/CRLF/Traceback 链差分等价，原文 span 可往返 | **PASS** | `tests/r6/framer.test.cjs`（11 项：1/2/3/5/7/13/64/257/1024/4096/65536 切块一致；6 个偏移 checkpoint/restore；每条记录 byte slice 反查 `raw`）、`tests/r6/traceback.test.cjs`（12 项）、与真实 `parseLocal` 逐字段差分 |
| 2 | R5 与新投影/规则语义兼容边界明确，Unicode/大整数/缺失值不静默改变 | **PASS** | `tests/r6/rules-signature.test.cjs`（与冻结 v3.0.3 引擎逐记录一致；BigInt 19 位；`0`/`0x0`/缺失三态）、`tests/r6/query.test.cjs`（18 项） |
| 3 | 原生 HTTP IndexedDB 提交、跨刷新/整个浏览器重开、双页租约与旧 token 拒写 | **PASS（受限）** | `qa/evidence/R6-013/idb-store.json` 17/17（`isSecureContext=false`；重开后 chunkReads=0 且原文可读；库版本更高得 `schema_mismatch`）、`qa/evidence/R6-015/leases.json` 11/11（双真实页面）。**完整浏览器重启（持久 profile）未验证** → B-015 |
| 4 | quota/abort/crash/损坏库/半文件重选不清空旧文件；结构与检查点一致 | **部分（未通过）** | 已证：批量事务全成或全不成（`invalid_record` 后无部分写入）、库版本不匹配可识别、迁移 checkpoint 断点续迁、旧库不被删除。**未证**：真实 quota 耗尽、浏览器进程崩溃后重放、损坏库恢复、半文件重选 → B-014 |
| 5 | 分卷备份恢复完整 hash 匹配，错误卷不能发布；刷新使用有效索引不全量解析 | **PASS** | `qa/evidence/R6-017/backup.json` 10/10（真实文件往返：4 卷 64/64/64/40 B，篡改卷拒发布，恢复后摘要一致）、`tests/r6/backup.test.cjs` 11 项；`R6-014` 暂存不可见、指针原子切换、prune 不删原文 |

## 2. 本次执行数字

| 套件 | 命令 | 结果 |
|---|---|---|
| Python | `env -u PYTHONPATH python3.9 -m unittest discover -s tests -p "test_*.py"` | **93/93 OK** |
| Node | `node --test tests/*.test.cjs tests/r6/*.test.cjs` | **138/138 pass** |
| 契约 | `tools/r6_contract_validate.py` | 25 + 6 例，0 失败 |
| 自检 | `worker.py --self-test` | 退出 0 |
| 浏览器 013 | `qa/r6/idb_store.py --lan` | 17/17 PASS |
| 浏览器 014 | `qa/r6/idb_store.py --lan --page qa/r6/index_harness.html --name index-store` | 10/10 PASS |
| 双页 015 | `qa/r6/local_leases.py --lan` | 11/11 PASS |
| 浏览器 016 | `qa/r6/cache_migration.py --lan` | 11/11 PASS |
| 往返 017 | `qa/r6/local_backup.py --lan` | 10/10 PASS |

真实浏览器来源：Chrome 156（`channel='chrome'`），页面统一从 `http://192.168.1.3:<port>` 打开以获得 `isSecureContext=false`（私网 HTTP），未使用内存适配器抵扣持久化验收。

## 3. 关键实现证据（非测试断言）

- 分帧器与 v3.0.3 的 `parseLocal` 在 4 个 golden 夹具上逐字段一致；100 MiB 分帧 **712 ms**。
- 释放租约时若删除记录，fencing 会从 1 重新计数——双页测试当场发现并已修为单调递增。
- `publish()` 只写一条工作区指针，因此代次切换天然原子。
- 备份卷篡改一个字节 → `volume_digest_mismatch`，且 `readPublished` 仍为 null（未发布）。

## 4. 未通过的原因与后续

`R6-018` 保持 **In Progress**，不标记 Done。缺的是**故障注入**，不是功能缺失：

1. **B-014**：quota 耗尽、浏览器崩溃重放、损坏库、半文件重选四项未自动化。需要在独立的浏览器 profile 上注入 Storage quota（DevTools protocol 或写满配额）、`kill -9` 渲染进程、手工破坏 IDB 文件、以及一次真实"同名不同内容"的重选流程。属 `R6-015` 的故障注入点与 `R6-017` 的恢复脚本扩展。
2. **B-015**：完整浏览器重启（`--user-data-dir` 持久 profile）未验证；当前只验证了页面刷新与同 context 重开。

这两项都可在下一批闭合，且不阻塞 M2 的 UI 接线（`R6-019`–`021`）——UI 只依赖已冻结的 Provider/Query 契约。
