# 远程采集：能力、边界与运维

> 面向 `server/collectors/`。对应里程碑 M6（R6-059 … R6-069）与 M7 的调度部分。
> 本文只描述**已实现**的行为；未实测项在 `qa/evidence/M6-gate.md`、`M7-gate.md` 写 `BLOCKED`。

## 1. 一句话

采集是"只读传输 + 上层归档"：适配器只负责**发现、定位、读字节**，代次判定由
`generations.py`、提交与水位由 `archive.py`、调度由 `scheduler.py` 负责。
传输能读，不等于可以增量续采；这一条由能力矩阵显式表达，不由 UI 猜测。

## 2. 能力矩阵（`base.Capabilities`）

每个能力只有三种取值：`supported` / `unverified` / `unsupported`。
`unsupported` 在代码里**必须抛错**（`CollectorError.unsupported`），
不能返回空列表——"没有文件"和"不能列目录"是两件事。

| 协议 | 目录发现 | 范围读取 | 稳定标识 | 追加约定 | 备注 |
|---|---|---|---|---|---|
| `sftp` | supported | supported | `size-mtime` | `unverified`（可由策略声明为 declared） | SFTP 通常没有可靠 inode |
| `smb` | supported | supported | `size-mtime` | `unverified` | 每次重连必须复核 identity |
| `http` | 取决于清单/索引 | supported（实测 206） | 强 ETag 才有 `validators` | 需 `generationId` + 声明 | 见 §4 |
| `scp` | unsupported | unsupported | `snapshot-hash` | unsupported | 只做整文件快照 |

## 3. 凭据与主机准入（R6-059）

- 凭据正文只存在 `state/credentials/`（目录 0700、文件 0600）；数据库只存
  `credential_ref`、类型、标签与指纹。API、审计、异常、日志都拿不到正文。
- 文件权限被放宽时**拒绝读取**（`credential_permissions`），而不是照读。
- 主机按名字批准：`allowedHosts` 未列出的主机名即使解析进允许网段也拒绝
  （防 DNS 重绑定）；解析结果每次连接重新计算，并与预检时的地址集合比较。
- 云元数据地址（`169.254.169.254`、`fd00:ec2::254`）无条件拒绝。
- 环回地址默认拒绝，必须由管理员显式打开 `allowLoopback`（向导里有独立勾选项）。
- 子进程 `shell=False`、固定 argv；`ProxyCommand`、`--sftp-ssh`、`--config`、
  `-o`、`-e` 等可执行程序的参数不允许来自配置。
- SSH 必须提供 `knownHostsFile` 或已批准的主机指纹；否则预检失败，
  不会退回 `StrictHostKeyChecking=no`。

## 4. HTTP 的四种必须区分的响应（R6-063）

| 响应 | 处理 |
|---|---|
| 206 且 `Content-Range` 起始与请求一致 | 采用；强 ETag 时标 `verified` |
| 206 但范围不同 | `generation_changed`，不拼接 |
| 200（忽略 Range） | 视为**新完整表示**；仅当 offset=0 且长度等于请求时才可用 |
| 416 | `total` 与记录一致 → 结尾；不一致 → `generation_changed` |
| 弱 ETag | 身份降级为 `size-mtime`；读完复核验证器是否变化 |

重定向逐跳重新准入；跨主机跳转丢弃 `Authorization`。
普通 HTTP 目录不提供发现；没有 `logbench.manifest/1` 清单时，
`appendContract=unsupported`，向导只开放"稳定快照"。

## 5. 配置与预检（R6-066/067）

- `config_fingerprint` = adapter + endpoint + 凭据指纹 + 主机指纹 + policy +
  全部映射的哈希。改动任一项都会让旧预检失效（`probe_stale`）。
- `enable` 要求：存在修订一致、未过期且 `ok=true` 的预检；否则 409。
- `enable` 只**排队** `collect` 任务（`state=queued`），不把任务写成 `running`。
- 暂停/继续/扫描/重试都带 `actionId`，同 id 重复请求返回同一结果。
- 删除源只删配置；`source_files`、归档区间与原文都保留。

## 6. 目录映射

- 一个源可有多条映射，每条独立 `remoteDir → rootId/targetSubdir`。
- 同一 root 下目标子目录不得相同或互为祖先（409 `target_overlap`）。
- `rootId` 走 M5 存储注册表；`private` 根不能作为共享采集目标。
- 目标子目录拒绝 `..`、隐藏目录、绝对路径与冒号。

## 7. 复跑

```bash
env -u PYTHONPATH ~/.local/bin/python3.9 -m unittest \
  tests.test_r6_collector_security tests.test_r6_adapter_contract \
  tests.test_r6_http_range tests.test_r6_sftp tests.test_r6_smb \
  tests.test_r6_scp_snapshot tests.test_r6_sources

# 起一个隔离实例后：
env -u PYTHONPATH ~/.local/bin/python3.9 qa/r6/source_wizard.py --base-url http://127.0.0.1:50955
env -u PYTHONPATH ~/.local/bin/python3.9 qa/r6/intake_ui.py     --base-url http://127.0.0.1:50955
```

## 8. 当前未验证项

- 真实 SFTP / SMB / 传统 SCP 端点：本机没有可实连服务，且未安装 rclone；
  对应证据写 `BLOCKED`，不用 mock 抵扣。
- HTTP keep-alive：每个窗口一次请求（不逐 4 MiB 重建连接），但未做连接复用。
- `rclone` 版本/许可与 argv 组装的真实行为，仅在 fake tool 下验证参数形状。
