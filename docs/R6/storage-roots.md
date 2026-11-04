# 多存储根与统一文件库（M5）

> 依据：R6 完整设计 Rev2 §11/§13/§15/§16/§17；Linear M5（R6-052…R6-058）。
> 本文件记录**已实现**的接口与不变量，不是设计意图清单。证据见 `qa/evidence/M5-gate.md`。

## 1. 物理根与逻辑总库

| 概念 | 实现 |
|---|---|
| 根 | `storage_roots` 一行：`id`、`kind`（`shared_raw` / `private_raw` / `index`）、`label`、绝对 `path`、`volume_id`、`sentinel_id`、`quota_bytes`、`revision` |
| legacy 根 | `id='legacy'`，路径 = 配置 `data_dir`；启动时幂等登记，不搬动文件 |
| 逻辑文件 | `logical_files`：`id = f(file_id_for(root_id, rel_path))`、`rel_path`、`display_name`、`size`、`generation`、`content_sig`、`owner`、`scope`、`state` |
| 代次 | `generations(file_id, generation, raw_bytes, content_sig, state)`；内容变化只增代次，旧代次保留 |
| 目录树 | 不落库；由 `logical_files` 的 `rel_path` 前缀 + 实际目录列举合成，按展开请求加载 |

不变量：

1. `path` 唯一且不互为祖先；重复 → `root_path_duplicate`，嵌套 → `root_overlap`。
2. 有文件登记的根不允许直接改路径（`root_path_migration_required`），迁移是显式任务。
3. `file_id` 只由 `(root_id, rel_path)` 决定；改名、改根标签、改显示名都不改 id。
4. `private_raw` 根的行只对 `owner` 可见；管理员不因身份获得私有内容。

## 2. 挂载身份与离线

- 登记/探测时记录 `volume_id`（`st_dev`）+ 唯一 `sentinel_id`（根内 `.logbench-root.json`，0600）。
- 每次读写前复核：根存在 → 不是符号链接 → `st_dev` 相同 → sentinel 一致。任一不满足即 `storage_root_offline`，`reason` 区分 `missing` / `became_symlink` / `volume_changed` / `sentinel_missing` / `sentinel_mismatch`。
- 离线时**读取保留登记、写入拒绝**；目录 API 返回 `registrationKept=true`，不会显示成空库。
- 探测用服务账号写 1 字节临时文件再删除；不 sudo、不 chmod、不 mount。

## 3. 接口

所有接口前缀 `/api/v2`，错误体 `{error:{code,message,details}}`；写操作沿用 CSRF / Origin / Host 校验，且逐请求重新授权。

| 方法 / 路径 | 说明 |
|---|---|
| `GET /storage-roots` | 管理员；根列表 + `summary`（在线/离线/文件数） |
| `POST /storage-roots` | 管理员；`{label,path,kind,quotaBytes}`，登记前探测（失败即拒） |
| `GET /storage-roots/{id}` · `PATCH` | 管理员；`PATCH` 需 `expectedRevision`，路径变更需迁移 |
| `POST /storage-roots/{id}/probe` | 管理员；写临时文件复核身份与空间，记录 `storage_root_probes` |
| `GET /library/sources` | 所有身份；逻辑来源与计数，**不含物理路径** |
| `GET /library?scope=&root=&directory=&q=&status=&cursor=&limit=` | keyset 分页；`scope=shared` 只含共享根，`private` 只含本人行 |
| `GET /library/tree?root=&path=&limit=&sync=` | 单层懒加载；`sync=1`（默认）先登记该目录元数据 |
| `POST /library/scan` | 管理员；`{rootId,path,limit,offset}` 分片扫描 |
| `GET /library/files/{id}` · `/generations` | 元数据与代次；`physicalPath` 仅管理员 |
| `POST /library/files/{id}/index` | 按 id 触发索引（不接收路径） |
| `GET /library/files/{id}/content` | 下载，支持 Range/ETag；离线 409 |
| `DELETE /library/files/{id}` | 移入该根回收站；`?detachBindings=1` 显式解绑 |
| `GET /library/trash` · `POST /library/trash/{id}/restore` | 回收清单与恢复 |
| `GET/PUT/DELETE /bindings` · `/bindings/personal` | 共享（管理员）/个人（本人）绑定 |
| `GET /bindings/effective?rootId=&path=` | 生效规则 + 六层解释链 |

## 4. 绑定优先级

```
个人单文件 → 个人目录 → 共享单文件 → 共享目录 → 映射默认 → 服务默认
```

- 链固定 6 层，`chain[].matched` 表示是否命中，`reason='rule_missing'` 表示该层引用了已删除规则（跳过但报告）。
- 绑定是 root 作用域的；`service_default` 是唯一全局层（存放在 legacy 的 `bindings` 表）。
- 写共享需管理员 + `expectedRevision`；个人覆盖不可写 `mapping`。

## 5. 回收与引用

- 每根 `<root>/.logbench-trash/`；清单先写（`pending` + 目标路径 + sha256）再 `os.replace`，失败置 `failed` 并让原文留在原位。
- 阻止删除的引用：未完成任务、上传会话、查询快照、索引 pin、指向该文件或其父目录的共享/个人绑定（服务默认不算）。
- 恢复：根离线拒绝；同名不覆盖；恢复后比对 digest 并回报 `digestMismatch`。
- `repair()` 在启动时把 `pending` 收敛为 `trashed` 或 `failed`。

## 6. 前端

| 模块 | 职责 |
|---|---|
| `src/shared/http.js` | JSON 客户端：CSRF、统一错误码、workspace epoch 丢弃旧回包 |
| `src/ui/library.js` | 本机/私有/总库、懒加载树、文件表、目录规则（共享/仅我/恢复继承）、回收站、收藏 |
| `src/ui/storage-roots.js` | 存储位置表、登记/设置对话框、探测、只读态 |

约束：不新增第三级导航；工具栏最多两行；窄屏（≤860）导航改横向标签条、≤640 隐藏次要列；离线显示原因而不是 0 文件；非管理员不显示写按钮（接口仍拒绝）。

## 7. 未完成 / 归属其他里程碑

- 远程目录映射（`directory_mappings`）写入 `mapping` 绑定：M6（R6-066）接入，本层已支持读取与解释。
- 归档增量与实时事件：M7；本层的代次与水位字段已被其复用。
- 真实挂载故障与目标机 Python 3.9.11：见 `qa/evidence/M5-gate.md` 的 BLOCKED 项。
