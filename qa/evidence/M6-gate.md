# Gate M6 · 真实协议能力预检与采集配置流程

> Linear：`INH-2312`（R6-069）· 判定日期：2026-11-03
> 判定口径：每项只能是 `PASS` / `FAIL` / `BLOCKED`；`BLOCKED` 必须附原因与解锁条件。
> 产物：真实 HTTP 服务器（loopback，Range/ETag）、真实 SQLite、真实 Chrome 156、真实 API。

环境：macOS（darwin aarch64）、Python 3.9.11、Node v22.22.2；**rclone 未安装**；
本机无受控 SFTP / Samba / 传统 SCP 端点（B-007 未解除）。

## 1. 逐项判定

| # | 要求 | 判定 | 证据 |
|---|---|---|---|
| 1 | 每协议 probe/list/stat/range 或 snapshot 字节正确 | **HTTP PASS；SFTP/SMB/SCP BLOCKED** | `tests/test_r6_http_range.py`（17）、`R6-061/062/065.md` |
| 2 | 不支持能力显式降级，配置页与后端一致 | **PASS** | `test_r6_adapter_contract`（10）；`test_r6_http_range` 中 200-忽略-Range 用例；向导检查项 “持续追踪” 由能力禁用 |
| 3 | HTTP 206/200/416、无清单/弱验证器/编码错误 | **PASS** | `tests/test_r6_http_range.py`；`qa/evidence/R6-063/` |
| 4 | SSH key 变化 / 证书错误 / DNS 重绑定 / 跨域 redirect / 注入参数 | **部分 PASS，真实链路 BLOCKED** | 本地拒绝逻辑在 `test_r6_collector_security`；<br>跨主机 redirect 丢弃 Authorization 在真实 loopback 上验证；<br>真实 SSH hostkey 变化无端点 |
| 5 | SMB 锁 / 重连 | **BLOCKED** | `test_r6_smb` 用工具输出验证分类，非真实共享；`qa/evidence/M6-gate/protocol_smb.json` 记录 BLOCKED |
| 6 | 传统 SCP 整文件路径与 hash | **BLOCKED** | `qa/evidence/M6-gate/protocol_scp.json`；快照语义在 `test_r6_scp_snapshot` 验证 |
| 7 | 多映射不同根、重叠目标、旧 probe、409、权限不足、取消 | **PASS** | `tests/test_r6_sources.py`（16）；`qa/r6/source_wizard.py`（17） |
| 8 | 目录/预检操作有界，不读整份 1 GiB | **PASS** | 预检只取 1 KiB 样本；`maxEntries`/`maxDirectoryBytes`/`readWindow` 上限；`read_bounded` 超限抛错 |
| 9 | 凭据不出 API、日志、异常、本机缓存 | **PASS** | `test_r6_collector_security`；`test_r6_sources` 凭据生命周期；`test_r6_http_range` Bearer 不回显 |
| 10 | 向导/list/detail 真实状态与 unify 样式，无模拟进度/假成功 | **PASS** | `qa/r6/source_wizard.py`、`qa/r6/intake_ui.py`；截图见 `qa/evidence/R6-067/`、`R6-068/` |

## 2. 协议实连结果

| 协议 | 工具 | 端点 | 结果 | 原因 |
|---|---|---|---|---|
| HTTP(S) | 标准库 | 本机 loopback fixture | **PASS** | 真实 socket、真实 206/200/416/redirect |
| SFTP | rclone（缺失） | 无 | **BLOCKED** | `qa/evidence/M6-gate/protocol_sftp.json`：`missingTools: [rclone]` |
| SMB2/3 | rclone（缺失） | 无 | **BLOCKED** | 同上 |
| 传统 SCP | OpenSSH scp（存在） | 无 | **BLOCKED** | 无受控 SSH 目标；不伪造成功 |

解锁条件（任一即可推进对应行）：

1. 在目标机安装经验证的 rclone 二进制（记录版本与许可 notice），并提供一个
   受控 SFTP 主机（已知 host key）与一个受控 Samba/Windows 共享；
2. 提供一台可关闭 SFTP 子系统的旧服务器用于传统 SCP；
3. 运行 `qa/r6/protocol_{sftp,smb,scp}.py --endpoint … --known-hosts … --out …`，
   退出码 `0` 才算 PASS，`3` 仍是 BLOCKED。

## 3. 复跑命令

```bash
# 单元/集成
env -u PYTHONPATH ~/.local/bin/python3.9 -m unittest \
  tests.test_r6_collector_security tests.test_r6_adapter_contract \
  tests.test_r6_http_range tests.test_r6_sftp tests.test_r6_smb \
  tests.test_r6_scp_snapshot tests.test_r6_sources

# 协议实连（无端点时记录 BLOCKED）
for k in sftp smb scp; do
  env -u PYTHONPATH ~/.local/bin/python3.9 qa/r6/protocol_$k.py --out qa/evidence/M6-gate/protocol_$k.json
done

# 隔离实例 + 浏览器
env -u PYTHONPATH ~/.local/bin/python3.9 -B run.py --no-reload --port 50955 \
  --admin-ip 127.0.0.1 --data-dir /tmp/r6m6/data --state-dir /tmp/r6m6/var
env -u PYTHONPATH ~/.local/bin/python3.9 qa/r6/source_wizard.py --base-url http://127.0.0.1:50955
env -u PYTHONPATH ~/.local/bin/python3.9 qa/r6/intake_ui.py     --base-url http://127.0.0.1:50955
```

## 4. 本轮结论

**M6 的配置流程与 HTTP 能力可以判定通过；三种 SSH/SMB/SCP 传输的真实链路未取得证据，
因此 Gate M6 不整体 PASS。** 按项目规则，`INH-2312` 保持 **BLOCKED**，
已实现部分以 `R6-059`–`R6-068` 的逐票证据记录，不用 mock 或原型截图抵扣。

## 5. 本轮暴露并修复的缺陷

| 缺陷 | 现象 | 处理 |
|---|---|---|
| 禁用源仍可 pause | API 返回 200 并记录一次无意义的暂停 | `_apply_action` 把 `pause` 纳入 `source_disabled` 校验；UI 同时禁用按钮 |
| 采集列表映射数 undefined | 列表列显示 “undefined 个目录映射” | `intake()` 输出 `mappingCount`；测试断言 |
| 向导策略未知字段 | `activeInterval`/`discoveryInterval` 被 400 拒绝 | 纳入 `POLICY_KEYS` |
| 向导预检结果空白 | `appendChild` 收到数组，整步渲染失败 | 警告项改为兄弟节点 `.concat` |
| 环回预检无入口 | 状态码 `loopback_denied` 无 UI 选项 | 向导新增显式“允许环回”勾选（默认关闭） |
| 375 px 头部溢出 | `scrollWidth 430 > 375` | 窄屏头部内部滚动 + 状态文本截断 |
| 预检前展示能力默认值 | 未预检就显示“目录清单需配置”等推断 | 未预检只显示“尚未预检 · 能力未知” |
