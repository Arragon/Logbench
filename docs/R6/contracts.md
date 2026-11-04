# R6 前后端公共契约（已冻结）

> 契约版本：`logbench.r6/1`　｜　票：`R6-002`　｜　冻结日期：2026-11-02
> 唯一形状所有者：`contracts/r6/contracts.schema.json`
> 状态机所有者：`contracts/r6/state-machine.json`
> 一致性用例：`tests/fixtures/r6/contracts/{cases,projection}.json`
> 双侧实现：`src/shared/contracts.js`（浏览器）｜`tools/r6_contract_validate.py`（服务端）

## 1. 为什么需要这份契约

v3.0.3 的记录对象是"前端 `parseLocal` 造出来的 JS 对象"，字段没有 schema，服务端对它一无所知。R6 要同时跑本机、服务端、合并三种来源，如果沿用"各处自己拼对象"，三套实现会在 `0 / false / null / ""`、字节范围、计数语义上分叉，而这三处恰好是排障时最容易被误读的地方。

因此本契约的目标不是"多一层校验"，而是**把容易说谎的地方变成不可表达的**：

| 容易出错的说法 | 契约怎么阻止 |
|---|---|
| "共 243 条"（其实只扫了 34% 文件） | `CountRelation.relation` 为 `at_least` / `unknown` 时，`matched` 不允许伪装成精确值；`Coverage.complete=false` 与 `relation=exact` 不能同时成立 |
| "上传完成，可以搜了" | `TaskProgress.phase="uploaded"` 强制 `indexed=false`（C6） |
| "0 条结果"（其实是预算中断） | `state="partial"` 强制 `coverage.complete=false`，且 `relation≠exact` |
| "这个字段是 0"（其实是空串） | `FieldValue.value_type` 必须与所载 JSON 类型一致（C7） |
| 大文件偏移量算错几位 | 字节偏移一律十进制字符串，禁止 JSON number（超出 2^53 会静默丢精度） |

## 2. 文档所有权

| 文档 | `$defs` 所有者 | 谁生产 | 谁消费 |
|---|---|---|---|
| `RecordRef` | `RecordRef` | Framer（本机 / 服务端索引） | 全部读取路径、`Finding`、书签、审计 |
| `RecordHeader` | `RecordHeader` | Framer + 规则引擎 | 日志表、时间线、搜索命中行 |
| `Finding` | `Finding` | 规则引擎 | 详情抽屉、告警视图、导出 |
| `TaskProgress` | `TaskProgress` | 导入 / 私有上传 / 采集任务 | 任务面板、文件库状态列 |
| `QuerySpec` | `QuerySpec` | UI → 查询层 | Local / Server / Combined Provider |
| `QueryState` | `QueryState` | 查询层 | 结果头、分页脚、暂停/继续按钮 |
| `ReaderProvider` | `ReaderProvider` | Provider 工厂 | 视图层（只认能力，不认来源） |
| `WorkerEnvelope` | `WorkerEnvelope` | Worker 两侧 | 主线程消息泵 |

下游入口在代码注释里必须写出上表的所有者名；新增字段只能改这里，不能在下游就地加。

## 3. 语义不变量（C1–C13）

这些不是 JSON Schema 能表达的，双侧实现各写一遍，由同一份 fixture 校验。

| # | 不变量 | 反例后果 |
|---|---|---|
| C1 | `bytes.start <= bytes.end`（半开区间） | 反向切片返回空、`end` 被当成"最后一行" |
| C2 | `lines.last >= lines.first`，且都 ≥ 1，物理行号含首含末 | 跳行、重复行 |
| C3 | `relation=unknown` ⇒ `matched=null`；`exact/at_least` ⇒ `matched` 为整数；`returned <= matched` | 把未扫完的下界当总数 |
| C4 | `complete === (scanned_bytes >= total_bytes)` | 部分覆盖被标成完整 |
| C5 | `paused` ⇒ 有 `resume_token`；`partial` ⇒ `coverage.complete=false`；`done` ⇒ `relation≠unknown` | 无法恢复的"暂停"、假的"完成" |
| C6 | `bytes_done <= bytes_total`；`phase=uploaded` ⇒ `indexed=false`；`phase=indexed` ⇒ `indexed=true` | 上传完成误报为可检索 |
| C7 | `value_type` 与实际 JSON 类型一致（`integer` 不是 `number`，`number` 不是字符串） | `0` / `"0"` / `""` / `null` 互相顶替 |
| C8 | 取消（`cancel_requested` / `state=cancelled`）**不等于**删除；删除只由显式删除动作产生 | 用户取消上传后发现原文没了 |
| C9 | 同一 `action_id` 的重复请求必须幂等：返回同一 `TaskProgress`，不得二次写入 | 双击"继续"产生两份数据 |
| C10 | `RecordRef.bytes=null` ⇒ `RecordHeader.provisional=true` | 未定位的记录被当成最终结果计数 |
| C11 | `ts=null` ⇒ 不得出现 `ts_ns` | 无时间行被塞进时间线 |
| C12 | `time_range.from_ns <= to_ns` | 空区间被解释成"全量" |
| C13 | `ts` 必须是真实时刻（月份 1–12、按闰年校验日、时 <24、分 <60、秒 ≤60） | `2020-13-01 99:99:99` 通过正则后进入排序 |

`ts` 与 `ts_ns` 的关系：`ts` 是**原文文本**，可能没有时区；`ts_ns` 是**排序与比较的唯一依据**。禁止用字符串比较跨时区排序。

## 4. 三个 Provider 的同一性

`Local` / `Server` / `Combined` 必须暴露完全相同的方法签名、错误码、取消语义与分页上限：

```text
open(dataset_id) -> ReaderProvider
headers(cursor, limit) -> { items: RecordHeader[], next_cursor, count: CountRelation }
detail(ref) -> { header: RecordHeader, body: string, findings: Finding[] }
search(spec: QuerySpec) -> QueryState          # 同步返回首个快照
poll(query_id) -> QueryState                    # 服务端链路用；本机链路立即返回终态
cancel(query_id) -> QueryState
resume(query_id, resume_token) -> QueryState
```

分页硬约束：**单页 ≤ 2000 条且 ≤ 4 MiB**（`limit.maximum=2000`、`ProviderCapabilities.max_page_bytes=4194304`）。超出必须由实现拒绝，而不是截断后假装成功。

`kind` 只改变传输方式，不改变语义：

| 能力 | local | server | combined |
|---|---|---|---|
| `search` | ✓ 浏览器内 Worker | ✓ SQLite 索引 | ✓ 两路合并 |
| `stream_bytes` | ✓ File.slice | ✓ HTTP Range | 各自按来源 |
| `resume` | ✓ 断点续读 | ✓ 游标 | 逐来源 |
| `write` | ✗（R6 不改原文） | ✓ 私有上传 | 由来源决定 |

`Combined` 合并规则：按 `ts_ns` 归并，计数用 `relation=at_least` 直到两路都 `complete`；任一路 `disabled_reason` 非空时，结果头必须显示"仅 N 个来源"，不得静默降级。

## 5. 状态机

见 `contracts/r6/state-machine.json`，未列出的边一律拒绝。

查询：`queued → running → {paused | partial | done | cancelled | failed}`；`paused`/`partial` 可回到 `running`；`done`/`cancelled`/`failed` 为终态。

任务：`queued → reading → {framing | uploading} → … → indexing → indexed`。**`uploading` 不能直接到 `indexed`**——这条边被删掉是刻意的，它对应"上传完就宣布可搜"这个 R5 时代最容易犯的错。

## 6. R5 视角（v1）到 v2 的迁移

`projectV1()`（JS 与 Python 各一份镜像，用例 `projection.json`）执行确定性投影：

- `dataset_id ← fileId`，`record_id ← id`，`ordinal ← ordinal`，`lines ← {line, lineEnd}`
- `bytes ← null`，因此 `provisional ← true`（C10）
- `fields: [[k,v]] → [{key, value, value_type:"string"}]`，**不猜类型**
- `ts: "" → null`；`has_trace ← hasTrace === true`；`kind` 必须在闭集内，否则拒绝投影

**正例**：`retries="0"`、`converged="false"`、`note=""` 三条都保留为字符串，且互不相等。这正是 v1 已经丢失的信息，投影不得凭直觉补回来。
**反例**：`fields` 是对象（已丢重复键与顺序）、`line=0`、`kind="binary"`、`id=""` 四种输入必须返回错误而不是勉强投影。

迁移到 v2 后重新分帧的记录会拿到真实 `bytes` 与 `version=2`；此时 `provisional` 才可为 `false`。`RecordRef.version` 递增，过期版本的写请求必须返回冲突（409），不得覆盖。

## 7. 错误、取消与失效

| 场景 | 契约行为 |
|---|---|
| 数据集已被替换（重命名/重写） | `ReaderProvider.disabled_reason` 非空，读请求返回 `dataset_replaced` |
| 索引版本过期 | `QueryState.index_version` 与当前索引不一致 ⇒ 丢弃结果并重建，不得混用 |
| 游标过期（进程重启/超预算） | 返回 `cursor_expired`，UI 必须重新从头部翻页，不得静默跳回第一页 |
| 取消 | 立即置 `cancel_requested`，任务进入 `cancelled`；**原文与已写入分片保留** |
| ACL 变化 | 每次 `headers`/`detail`/`search` 都要按当前身份复核，不复用打开时的判定 |
| 重复 action | 同 `action_id` 幂等（C9） |

## 8. 验证方式

```bash
env -u PYTHONPATH python3.9 tools/r6_contract_validate.py     # 25 案例 + 6 投影
env -u PYTHONPATH python3.9 -m unittest tests.test_r6_contracts
node --test tests/r6/contracts.test.cjs
```

两侧读同一份 fixture 并各自断言期望值，因此"Python 通过、JS 不通过"会直接暴露为失败。`test_only_declared_entry_points_are_unreferenced` 保证新增 `$defs` 不会变成没人引用的死形状，`test_v2_protocol_bumped_from_r5` 保证 Worker 协议升级不被悄悄回退。
