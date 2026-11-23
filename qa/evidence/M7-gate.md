# Gate M7 · 归档、续采、轮转与实时阅读故障验收

> Linear：`INH-2320` · 里程碑 M7 · 结论：**五段通过，协议段 BLOCKED（B-007）**

```bash
env -u PYTHONPATH ~/.local/bin/python3.9 qa/r6/remote_vertical_gate.py --out qa/evidence/M7-gate
# archive-crash PASS · rotation PASS · faults PASS · events PASS · browser-close PASS · protocols BLOCKED
```

原始数据：`qa/evidence/M7-gate/*.json`。本页每个数字都来自这些文件，没有手工填写。
Python 3.9.11；Node v22.22.2。

## 1 · 崩溃点（`archive-crash.json`）

真实子进程在三个点被 `SIGKILL`（`subprocess` + `os.kill(getpid(), 9)`），父进程重新打开
同一份 SQLite 与真实文件复核。归档文件 `f_e6a2e85ea4cc9098` 代次 1，最终
`intervals=4`、`interval_bytes=35`、`archive_bytes=35`、`final_hash=3f30af8e…`。

| 崩溃点 | 结果 | 证据 |
|---|---|---|
| 写后、确认前 | `contiguous` 不变，回放时 `trimmed=7` 字节 | 未提交字节被丢弃，原文 hash 与确认前缀一致 |
| 确认后、索引前 | `contiguous` 前进 7，`trimmed=0`，`verify.ok=true` | 确认区间保留，未索引字节不丢失 |
| 索引后、通知前 | 重启后 `sync()` 返回 `state=current`、`newRecords=null` | 不重复解析：`5` 条记录没有翻倍 |

水位从未领先有效落盘内容；`recover()` 现在也会裁掉“水位之后”的未确认尾部（本轮修复，
见下节“本轮发现”）。

## 2 · 轮转矩阵（`rotation.json`）

真实目录上的真实 `rename` / `truncate` / 覆盖写 / 删除，经真实调度器与真实归档：

| 场景 | 判定 | 附加断言 |
|---|---|---|
| 追加 | `append` | 只归档新增字节 |
| rename 轮转 | `rewritten`（新代次） | 旧代 `app.log.1` 24 字节完整保留；新代从 0 开始 |
| 截断 | `truncated` | `gap_state=unknown_length`，代次 +1 |
| 截断后重新增长 | `append` | 不把截断窗口算成连续 |
| 等长替换 | `replaced` | 同大小、前缀不同 → 新代次 |
| copytruncate | `truncated` | 先拷贝再清空：旧内容留在 `.2` |
| 快速删除 | `disappeared` | 本地原文保留，状态不伪装成成功 |
| 压缩轮转 | `compressed-snapshot` | 只登记不索引（需解压预算，R6-071 记录） |
| 身份歧义 | `replaced`（`same-size-different-prefix`） | 不猜拼接 |

## 3 · 故障边界（`faults.json`）

| 注入 | 结果 |
|---|---|
| 文件被锁 | `state=retry` + `retry_at` |
| 列目录失败 | `transport_error`（不是“空目录”） |
| 恢复后 | `state=archived`，`retry_at` 被清除 |
| hostkey 变化 / 认证失败 / 权限不足 | `state=stopped`，无 `retry_at`，`needs_human=true` |
| 磁盘低于低水位 | 源 `paused=1`、`policy.waitingStorage=legacy`、`pausedBy=storage`，查询仍可用 |
| 空间恢复 | 自动恢复，`waitingStorage` 标记清除 |

## 4 · 事件（`events.json`）

真实 TCP socket、真实 SSE 帧：握手 `200`、3 帧、`ids=[1,2,3]` 单调、正文零泄漏；
过期回放返回 `resync_required`（`event_retention_exceeded`）；轮询与流对同一游标给出
相同的 `[4,5]`；成员身份看不到 `shared` scope（0 条）。

## 5 · 关浏览器（`browser-close.json`）

`subscribers=0` 时调度照常推进：第一轮 `bytes=4`，第二轮 `bytes=10`，连续水位 14 字节。
采集属于服务端，浏览器只是订阅者。

## 6 · 协议（`protocols.json`）—— BLOCKED

SFTP / SMB / legacy SCP 本机没有可连端点，也没有 `rclone`（B-007）。三个探针脚本以退出码 3
（BLOCKED）结束，未用 mock 抵扣。HTTP 协议在 M6 已用真实 loopback socket 验证
（`qa/evidence/M6-gate.md`）。

## 本轮发现并修复

1. `Archive.plan` 让**每个代次写同一个物理路径**，新代次从 0 写入会覆盖上一代已确认原文
   （违反“旧原文不被截掉”）。现在代次 > 1 落到 `g<N>/` 子目录，`file_id` 仍按逻辑路径稳定，
   `logical_files` 指向最新代次物理路径。`rename` 后旧代 24 字节完整保留即为回归证据。
2. `Archive.recover()` 只删除摘要不符的区间，不裁剪“水位之后”的未确认字节。现在会裁剪并
   在结果里给出 `trimmed`。
3. `apply_decision` 成功判定后不清 `retry_at`，界面会永远显示“等待重试”。现在清除。
4. 调度器只遍历当前列目录结果，文件在两次扫描之间消失时无人标记。新增 `_mark_missing`：
   状态置 `disappeared` 并发 `rotation` 事件，原文保留。
5. 事件帧正文没有 `id`（浏览器只把 `id:` 行暴露为 `lastEventId`，轮询端没有头），客户端
   把每帧都当成无法排序的通知。现在 `id` 同时出现在正文。
6. 客户端与服务端的游标参数名不一致（客户端 `lastEventId`，服务端读 `last`），导致重连
   永远从头开始。已统一，并在门禁里以“轮询==流”交叉验证。

## 不能承诺的事

- 源端已删除的内容无法恢复：`disappeared` 只保证本地原文仍在，`gap_state=unknown_length`
  表示丢失长度未知，不编造行数。
- 压缩轮转内部未索引，只有快照登记。
- 未实连的 SFTP/SMB/SCP 不能以本页任何结论抵扣。
