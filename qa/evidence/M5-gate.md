# Gate M5 · 多物理根、目录继承与离线恢复

**日期**：2026-11-03 · **票**：R6-058 · **结论**：`PARTIAL`

- 交付项 11 项 `PASS`，0 项 `FAIL`，1 项 `BLOCKED`（真实卸载/换盘挂载）。
- 原始数据：`qa/evidence/multi_root_gate.json`（逐项含测量值）；浏览器证据：`qa/evidence/R6-056/library-storage.json`。
- `PARTIAL` 的理由：本机没有可安全卸载的独立挂载点，2d 项只能注入身份模拟，未在目标机验证。其余全部为真实目录、真实 SQLite、真实 HTTP 处理链的实测。

## 环境

| 项 | 值 |
|---|---|
| 解释器 | **Python 3.9.11**（`~/.local/bin/python3.9`，与部署目标一致）；另在 3.13.12 复跑同一套用例 |
| 平台 | darwin |
| 语料 | 临时部署目录 + 两个部署目录外物理根（`physical/archive`、`physical/private`）+ 320 个日志文件 |
| 运行命令 | `python3 qa/r6/multi_root_gate.py --out qa/evidence` / `python3 qa/r6/library_storage_ui.py --out qa/evidence/R6-056` |

## 逐项结果

| # | 清单项 | 结果 | 证据 |
|---|---|---|---|
| 1 | legacy 根 + 两个部署目录外根并存；同名 `logs/app.log` 不覆盖，两个 file_id 不同 | PASS | `roots=3`, 同名行 2, 独立 id 2, 两文件大小不同（180 B / 315 B） |
| 1b | 规则 ID 与 legacy 绑定映射保持不变 | PASS | `cpqpsgx-default` 仍在，`/api/v1/bindings/resolve` 仍返回 `scope=default` |
| 2a | 越根路径（`../`）、根内符号链接被拒绝，正常读取不受影响 | PASS | 越根 400/403；`link.log` 不出现在目录列表；根状态仍 `online` |
| 2b | 挂载掉线（sentinel 缺失）后停止写入，登记与元数据保留，不当作空库 | PASS | 写入 409 `storage_root_offline`；`tree` 返回 `state=offline` + `registrationKept`；行数不变 |
| 2c | 不同盘重挂被识别并拒绝写入 | PASS | 注入 `volume_id` 变化后 probe 409，`reason=volume_changed` |
| 2d | **真实卸载 / 换盘挂载的内核行为** | **BLOCKED** | 本机无可安全卸载挂载点；需目标机执行，未验证 |
| 3a | 数百文件按需分页，全程不读原文 | PASS | 320 文件 / 50 条一页 / 7 页；`guard.open_regular` 调用 0 次 |
| 3b | 私有范围与物理路径不向普通身份泄露 | PASS | 普通身份 `/storage-roots` 403；私有列表空；`/library/sources` 不含物理路径 |
| 4 | 映射默认、个人覆盖、恢复继承、解释链完整 | PASS | `mapping_default` → `personal_directory` → 清除后回 `mapping_default`；链为固定 6 层 |
| 5a | 活动引用阻止删除；同名冲突不覆盖；恢复后字节一致 | PASS | 任务在跑时 409 `active_references`；冲突 409 `already_exists`；恢复后 sha256 相同 |
| 5b | 移动中崩溃可由回收清单修复，原文不丢 | PASS | `pending` 行 + 已移动文件 → `repair()` 置 `trashed`，恢复成功 |
| 6 | 文件库 / 存储位置页面视觉与键盘 | PASS | 18 项浏览器检查全通过；4 个视口截图（1366/1024/768/375）；挂载恢复后根回到 online |

## 全量回归

```bash
python3 -m unittest discover -s tests -p "test_*.py"   # 551 tests, OK
node --test tests/*.test.cjs tests/r6/*.test.cjs       # 263 passed
python3 worker.py --self-test                          # 0（含 /r6 页面与 r6 资源预检）
```

附带修复：`Application.__init__` 对 `data_dir` / `.logbench-trash` / `Store` 目录改用“不存在才创建”，
同一部署二次启动不再因 `EEXIST` 失败（此前 `test_audit_and_persistence` 等 3 个用例因此在沙箱中报错）。

## 未覆盖 / 已知限制

1. **真实挂载故障**（2d）：需要目标机的可卸载挂载点（U 盘、独立分区或 NFS/SMB 共享）。脚本已具备注入路径（`volume_id` / sentinel），但内核级 unplug 行为未验证。
2. ~~Python 3.9.11~~：本 Gate、5 个 M5 测试模块与浏览器验收均已在该解释器下执行通过（见上表与 `multi_root_gate.json` 的 `environment.python`）。
3. **双路 E5 负载**：分页与扫描只在本机测量，未在目标机报 p50/p95。
4. 浏览器证据来自 Chrome（Playwright `channel=chrome`），非用户实际档案；未验证 Safari/Firefox。

## 复跑

```bash
python3.9 -m unittest tests.test_r6_roots tests.test_r6_storage_guard \
    tests.test_r6_catalog tests.test_r6_bindings tests.test_r6_trash   # 63 passed on 3.9.11
node --test tests/r6/bindings-compat.test.cjs                            # 12 passed
python3.9 qa/r6/multi_root_gate.py --out qa/evidence
python3.9 qa/r6/library_storage_ui.py --out qa/evidence/R6-056
```
