# R6-001 · v3.0.3 交付基线与 R5 行为清单

> 对应 Linear：`INH-2244` · `[R6-001]`，里程碑 M0。
> 目的：把"以 v3.0.3 为唯一实现基线"从一句约定变成可复核的证据，并固定 R6 不得回退的既有行为。
> 本票只做接入与记录，不重构任何功能。日期：2026-11-02。

---

## 1. 结论

| 判定项 | 结果 | 证据 |
|---|---|---|
| 交付包文件完整性 | **PASS**：89/89 文件哈希与 `SHA256SUMS` 一致 | `qa/r6/baseline/sha256sums-check.txt` |
| 树身份（release 等价摘要） | `3ae5222b…a097032` | `docs/reference/source-manifest.json` |
| 交付 ZIP 原始哈希 | **BLOCKED**：工作区是解包后的树，非 ZIP，无法复核 `07aa5d88…` | 同上 `package.zip_verification` |
| 既有 Python 测试（3.9.11） | **PASS**：55/55，7.9 s | `qa/r6/baseline/py39-tests.txt` |
| 既有 Python 测试（3.13.12） | **PASS**：55/55，9.0 s | `qa/r6/baseline/py313-tests.txt` |
| 工作进程预检 | **PASS**：`worker.py --self-test` 退出码 0 | `qa/r6/baseline/worker-self-test-py39.txt` |
| 前端引擎/契约测试（Node） | **PASS**：33/33 | `qa/r6/baseline/node-tests.txt` |
| 前端资源组装 | `/assets/workbench.js` 295,046 B，`96c3cd59…` | `docs/reference/source-manifest.json` |

一键复跑：

```bash
env -u PYTHONPATH ~/.local/bin/python3.9 -m unittest discover -s tests -p 'test_*.py' -v
env -u PYTHONPATH ~/.local/bin/python3.9 -B worker.py --self-test
node --test tests/*.test.cjs
shasum -a 256 -c SHA256SUMS
env -u PYTHONPATH ~/.local/bin/python3.9 tools/r6_manifest.py
```

> 说明：本机 `PYTHONPATH` 被外部注入了一个 `sitecustomize.py`，它会让 `mkdir(exist_ok=True)` 抛出 `EEXIST` 并干扰文件监控。所有 LogBench 命令必须以 `env -u PYTHONPATH` 运行，否则会出现 2 个假失败与 1 个假的热更新失败。这是工具环境问题，不是产品缺陷，已记入 `docs/r6/blockers.md`。

### 1.1 基线身份

| 项 | 值 |
|---|---|
| 产品 / 版本 | LogBench R5 Server · `3.0.3`（Floating Details / Python 3.9 兼容包） |
| Git | `d2422a781a39d1c6a2587e79debfc2d74c470380` · *Publish LogBench 3.0.3 as a standalone deployment package* |
| 工作树 | 干净（已跟踪文件无改动） |
| 文件数 | 89（`SHA256SUMS` 覆盖全部交付文件） |
| 声明 ZIP 哈希 | `07aa5d88de44f7794233fc1b69d66dacf41512520f5e176fcb88cc598b2e2d54`（来自 Linear 文档 01） |

树摘要是"排序后的 `路径\0文件sha256`"再求 sha256，与 `run.py` 生成 release 身份的方式一致，因此可作为**在没有 ZIP 时的替代身份**。它不能替代 ZIP 哈希本身——`package.zip_verification` 必须保持 `unavailable`，直到原始 ZIP 被重新取得。

---

## 2. 运行形态

| 维度 | 实现 | 位置 |
|---|---|---|
| 启动 | `python run.py`（父进程常驻），`worker.py` 为工作进程 | `run.py:193`、`worker.py:28` |
| 端口 | 默认 `50004`；用户既有部署为 `50005`，不得被覆盖 | `config.example.json` |
| 配置 | `config.json`（白名单字段：host/port/data_dir/state_dir/allowed_hosts/trusted_proxies/public_origin/max_connections） | `server/httpd.py:139` |
| 初始化 | `--admin-ip <ip> --init-only` 显式登记管理员后退出 | `server/httpd.py:142`、`run.py:206` |
| 传输 | `ThreadingHTTPServer`，信号量限并发（默认 32）、请求体 ≤2 MiB、`Connection: close` | `server/httpd.py:36` |
| 托管 | 可选 `server.wsgi:create_application`；systemd/Docker 示例在 `deploy/` | `deploy/README.md` |
| 隔离 | 单实例锁 `var/runtime/supervisor.lock`（POSIX flock / Windows msvcrt） | `run.py:107` |

---

## 3. 前端入口与状态

**启动链**：`/`（`src/index.html`）→ `/assets/boot.js` → `GET /api/v1/me`（校验 24 位十六进制用户 ID）→ 注入 `/assets/workbench.js`。身份失败**不回退到演示管理员**，直接显示可重试的错误页（`src/boot.js:8`）。

**组装**：`server/frontend.py` 在请求时把 `engine.js` 前置、把 `app.js` 中的 `@@R5@@`/`@@SERVICE@@` 替换为 `r5.js`/`service.js`，单文件输出 295,046 B。无构建步骤、无 CDN。

| 项 | 值 |
|---|---|
| 导航 | 3 个主视图（日志浏览 / 文件库 / 规则管理）＋ 管理后台 ＋ 操作帮助；导航为 2 列网格（`grid-template-columns:1fr 1fr`） |
| 阅读主题 | 4 套：`ink` 墨白 / `paper` 暖纸 / `graphite` 石墨 / `nord` |
| 阅读分页 | 1000 / 5000 / 10000 / 30000 / 50000 条每页，虚拟列表 |
| 文件库分页 | 60 / 100 / 200 条每页 |
| 单文件上限 | 64 MiB |
| 打开原文总量 | 128 MiB |
| 本机设置键 | `localStorage['logbench-user-<24hex>-<key>']` |
| 本机文件 | IndexedDB `logbench-r4-files` v1，objectStore `files` |
| 详情面板 | 覆盖式悬浮抽屉（v3.0.3 起），打开不改变阅读区与时间线几何 |

---

## 4. 身份与权限

- 身份来源：实际 TCP 对端 IP，或经显式 `trusted_proxies` 逐跳验证的 `X-Forwarded-For`；代理链从右往左剥离，缺失时返回 400 而非回落 localhost（`server/app.py:client_ip`）。
- 三种角色：未登记 = 访客（只读）、`member`、`admin`。
- 能力位：读侧 `read / localImport / preferencesWrite`；管理侧 `admin / rulesWrite / filesWrite / usersWrite / settingsWrite`。
- **每个请求独立重新授权**，不依赖前端隐藏按钮；停用用户返回 `USER_DISABLED`，前端进入阻断态。
- 写请求校验 CSRF（`X-LogBench-CSRF`）、Origin、FetchMetadata 与请求体上限。
- 外部身份模式（MCChecker）：只读 `ip_mapping.json` / `admin_users.json`，不导入模块、不执行代码、不回写；外部模式下用户表只读。
- 恢复路径：`run.py --local-identity --admin-ip <ip> --init-only`。

---

## 5. 数据归属

| 数据 | 存放 | 生命周期 |
|---|---|---|
| 共享规则、绑定、用户、审计、设置 | `var/logbench.sqlite3` | 服务端持久，含命名空间密钥 |
| 共享日志正文 | `data/` | 运维放置；删除进 `data/.logbench-trash/` |
| 本机日志正文 | 浏览器 IndexedDB | 绑定 origin + 浏览器档案 + IP 身份；无 TTL |
| 个人设置、视角、草稿 | `localStorage`（按身份前缀） | 同上 |
| 运行快照与发布 | `var/runtime/releases/<hash>` | 历史快照不自动清理 |

关键含义：换端口/主机/协议/浏览器档案/IP 即换工作区；删除数据库等于换命名空间，旧缓存不会自动迁移。

---

## 6. 规则与绑定

- 结构：`extractors`（正则+命名捕获组）、`anomalies`（字段条件+严重度）、`nodes`（1–12 条件+形状+颜色）。
- 上限：每类 ≤200 条、profile ≤1 MiB、正则 ≤2000 字符；`format` 目前仅支持 `bracket`（`server/r5_schema.py`）。
- 正则语义只在浏览器 Worker 中执行，Python 只做结构校验——这是 R6 必须保持的单一实现原则。
- 修订：每次写入带 `expectedRevision`，冲突返回 409 并保留个人草稿。
- 绑定优先级：文件级(3) > 最近目录级(2，最长路径) > 默认(1)；已绑定规则禁止直接删除。
- 异常等级不改写原始 `level`；`issue`/`event` 为派生视图。

---

## 7. 时间筛选与对比

- 时间键 `key` 归一化为 `YYYY-MM-DD HH:MM:SS.` + 9 位小数，保留原始精度参与排序。
- A / B 起止、K 关键点；区间折叠按时间键切片。
- 2–4 文件对比：共用精确时间点，不同密度用空槽对齐，滚动同步。
- 全局筛选作用于已打开文件集合，不做服务端全库扫描。

---

## 8. 已确认的性能瓶颈（R6 的直接输入）

以下三点是**代码事实**，不是推测，构成 M2 的验收对象：

1. **每次搜索新建 Worker 并传入全量记录**：`runSearch()` 先 `queryWorker?.terminate()`，再 `newWorker()`，然后 `postMessage({records: baseRecords(), …})`（`src/app.js:87`）。记录数组被结构化克隆整批传输，成本随文件规模线性增长。
2. **1800 ms 墙钟硬超时**：`setTimeout(…, 1800)` 到点直接 `terminate()` 并提示"搜索已超时，已保留上一份结果"（同处）。超过 1.8 s 的**普通**查询同样被杀，无法与真正的病态正则区分。
3. **整根重渲染**：`renderR4` 重建 `#app` 内容，虚拟列表只在结果层减少 DOM，不减少状态克隆与重排成本。

导入侧：`importFilesR4` 对每个文件执行 `await file.text()` 全量读入，再经两次 Worker 往返完成解析，缺少分块、背压与检查点。

---

## 9. 环境能力探测（本机实测）

| 能力 | 实测 | 含义 |
|---|---|---|
| Python | `3.9.11`（`~/.local/bin/python3.9`）＋ `3.13.12` | **3.9.11 目标解释器已可用**，此前"未在原生 3.9.11 运行"的缺口在本机解除 |
| SQLite | `3.38.1`（2022-03） | 低于含 WAL 相关修复的 3.44（2023-11）→ R6-007 应默认保守 journal |
| FTS5 / FTS3 | 均启用 | 服务端索引可用 FTS5 |
| trigram 分词 | 可用 | 支持子串检索规划 |
| JSON 函数 | 可用（`json_valid` = 1） | 元数据列可存 JSON |
| WAL 切换 | 可用（`journal_mode=wal` 成功） | 是否启用仍待 R6-007 的安全判定 |
| Node | `v22.22.2` | Node 侧测试与解析池前提成立 |
| Chrome | `156.0.8078.80` | 浏览器验收可用；Playwright 可走 `channel=chrome` |
| Playwright | **未安装** | R6-003/031/090 的自动截图需要先安装 |

证据：`qa/r6/baseline/sqlite-probe.txt`、`docs/reference/source-manifest.json#environment`。

---

## 10. 行为清单：已实现 / 历史未验收 / 本次新增结论

### 10.1 已实现且有测试或代码证据

- IP 身份与逐请求授权、CSRF/Origin/Host 校验、路径围猎（POSIX `O_NOFOLLOW` + 目录描述符）。
- 共享规则 CRUD + 修订冲突 + 软删除 + 绑定保护、目录/文件绑定解析优先级。
- 文件库目录元数据分页、Range/ETag 读取、回收站删除与恢复（同名不覆盖）、审计分页。
- 前端：字段提取、异常规则、节点时间线、全局时间筛选、2–4 文件对比、四套主题、列显隐、Aa/整词/正则、多行与 Traceback 视图、悬浮详情、阅读视角保存。
- 覆盖式热更新：内容哈希快照 → 编译/导入/接口预检 → 优雅停机 → 健康校验 → 失败回退。

### 10.2 历史未验收（R6 不得当作已完成）

| 项 | 状态 | 原因 |
|---|---|---|
| 原生浏览器持久化重开 | 未验收 | 交付期被环境策略 `ERR_BLOCKED_BY_ADMINISTRATOR` 阻止（`docs/IMPLEMENTATION.md`） |
| 真实 MCChecker 目录 | 未验收 | 仅按提交 `429dafaa…` 验证 JSON 结构兼容，未连实际部署目录 |
| Windows 路径/符号链接边界 | 未验收 | 仅 POSIX 有 `O_NOFOLLOW` 实现 |
| Docker 示例 | 未构建 | `deploy/README.md` 已声明本轮未构建 |
| 公网强认证 | 不在范围 | 明确要求前置网关 |

### 10.3 本次新增结论

1. **Python 3.9.11 真实解释器已在本机跑通全部 55 项测试**——此前 README 声明的"尚未用原生 3.9.11 运行"限制，在开发环境已解除；目标服务器仍需复验。
2. **ZIP 哈希无法在本工作区复核**，需重新取得原始交付 ZIP 才能闭合 `R6-008` 第一条检查。
3. **SQLite 3.38.1 低于 3.44**，WAL 默认策略必须在 `R6-007` 显式判定，不能沿用"启用 WAL 更快"的直觉。

---

## 11. 对 R6 的硬约束（从本清单推导）

1. `src/engine.js`、`src/r5.js`、`src/service.js`、`src/app.js` 的覆盖链必须保留可运行，新增代码走新文件，不重写外壳。
2. 浏览器仍是唯一规则语义实现；Node 侧只能复用同一份 JS。
3. 既有 `/api/v1` 语义不得改动，R6 新接口走 `/api/v2`。
4. 64 MiB / 128 MiB 是 R5 的**安全边界**，R6 不得简单提高，而应由 Provider 分页与持久索引替代。
