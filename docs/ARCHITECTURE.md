# 架构与维护接口

## 设计选择

采用“R5 本机解析前端 + Python/SQLite 数据服务”。日志正文在本机 Worker 解析，一套 JavaScript 引擎同时用于字段提取、Traceback、条件判断和试跑，避免服务端 Python 再实现一套不一致的正则语义。Python 后端负责共享资产与授权，不依赖 Node/浏览器自动化。

部署器是稳定父进程，工作服务运行于 `var/runtime/releases/<内容哈希>` 的快照。所有持久路径通过 `LOGBENCH_HOME` 指向真实部署根，不能随发布目录改变。子进程有序终止用 stdin stop 协议；父进程退出时子进程收到 EOF 后停止。代码扫描排除持久数据和编译缓存。

## 文件职责

| 文件 | 职责 |
|---|---|
| `src/index.html` / `boot.js` | 加载样式；先向 `/me` 确认身份，再加载工作台。身份请求失败不回退到演示管理员。 |
| `src/app.js` | R4/R5 工作台主功能，包括虚拟列表、浏览/文件/规则/时间筛选/对比交互。保留原设计已有的覆写结构，不为服务化全面重写。 |
| `src/r5.js` | Traceback、列配置、时间线规则编辑与证据、本机存储事务和备份。 |
| `src/engine.js` | 声明式规则计算；无 eval；保留数值原字符串和精确小数秒。 |
| `src/service.js` | 真 API 适配：共享资料、个人/共享绑定、修订冲突、管理员页面、按需读取及实际身份命名空间。 |
| `server/frontend.py` | 将上述脚本组装为 `/assets/workbench.js`，无需构建工具。 |
| `server/app.py` | API路由、IP身份、逐请求权限、SQLite事务、路径隔离、文件流、审计。 |
| `server/r5_schema.py` | R5共享配置结构校验。JavaScript正则语法/计算试跑在前端Worker校验，不在Python中执行。 |
| `server/httpd.py` | 有限连接数/请求体/超时的HTTP入口，静态白名单、响应安全头、排空停止。 |
| `server/wsgi.py` | 可选托管适配，不是默认零依赖启动链的一部分。 |
| `run.py` / `worker.py` | 文件监控、不可变快照、预检、健康检测、回退；不做 importlib.reload。 |

## API 合约概览

API 前缀 `/api/v1`；JSON 响应。错误为 `{error:{code,message,details}}`。共享写操作都要当前管理员能力；并发配置写必须带 `expectedRevision`。

| 方法 / 路径 | 用途 |
|---|---|
| GET `/me` | 返回当前 IP 身份、能力、CSRF token、稳定浏览器命名空间 ID、版本。 |
| GET `/health` | 已授权服务健康、发布标识、PID；`/healthz` 用于启动器健康检查。 |
| GET `/catalog` | 全库文件/目录元数据（含空目录，不含正文）；最多 100,000 项，超过时明确报错。 |
| GET `/files?path=&q=&cursor=&limit=` | 分页目录/文件列表。 |
| GET `/files/content?path=` | 文件流，支持 Range/ETag；路径必须位于日志库内且不是符号链接。 |
| POST `/files/directory` | 创建目录，body `{path}`。 |
| DELETE `/files?path=` | 文件/目录移入回收站。 |
| GET `/files/trash`; POST `/files/restore` | 查看/恢复，恢复 body `{id}`，同名不覆盖。 |
| GET/POST `/rules` | 规则列表/创建，body `{name,description,profile}`；列表 `deleted=1` 为管理员回收站。 |
| GET/PUT/DELETE `/rules/{id}` | 读取/改动/软删除；绑定规则禁止直接删除。 |
| POST `/rules/{id}/copy`, `/restore` | 复制、恢复；恢复需要修订号。 |
| GET/PUT/DELETE `/bindings` | scope 为 file/directory/default；path 为库内相对路径；共享绑定独立于浏览器个人覆盖。 |
| GET `/bindings/resolve?path=` | 返回 `{rule,source,scope,path}`，不能把 `source.ruleId` 误当顶层字段。 |
| GET/PUT `/preferences` | 可选按当前 IP 隔离的设置接口。R5 默认个人存储仍在浏览器，不主动上传个人工作区。 |
| GET/PUT/DELETE `/admin/users` | 用户与权限，删除用查询 ip + body expectedRevision；外部模式只读。 |
| GET/PUT `/admin/config` | 身份来源管理；非监听地址配置。 |
| POST `/admin/identity-preview` | 只读检查 MCChecker 人员资料及当前IP权限。 |
| GET `/admin/audit`, `/admin/stats` | 后台审计和运行概况。 |

代码内测试是精确请求体契约，可直接参考 `tests/test_backend_v2.py` 与 `test_service.py`。没有本机日志上传 API；日志库由部署者放置文件或挂载数据目录。

## 安全约束

- 仅实际TCP对端或经明确可信代理验证的客户端IP参与身份识别。客户端不能通过请求体指定 actor。
- POST/PUT/DELETE 检查 CSRF token、Origin、FetchMetadata 和请求体上限；不启用宽泛 CORS。
- 同一用户身份的所有请求独立重新授权。未知IP读取共享库是明确产品策略；停用用户拒绝API，而不是仅隐藏按钮。
- 只从白名单路由返回前端文件，不将部署根变成静态目录；`config.json`、数据库、后台源码、回收站不可直接下载。
- 文件读拒绝路径穿越/符号链接。POSIX使用目录描述符与 O_NOFOLLOW；Windows路径/链接边界仍需目标环境复验，不声称抵抗所有本机特权竞态。
- 浏览器内容使用转义/文本呈现；日志中脚本不会被当成HTML执行。管理员自定义正则在Worker中有时间限制。
- 本机设置不是服务端授权。篡改浏览器状态不能使后台通过共享写请求。

## 扩展方式

增加解析语法时，在 `src/app.js` 的记录组装与 Worker 对应路径新增适配，并同步引擎 fixture；不要只改变显示文本却保持错误的物理行边界。规则字段结构变更需更新 `server/r5_schema.py`、前端 normalize/validate、备份兼容策略和回归测试。未知字段是否迁移需明确处理，不能静默把旧数据覆盖为默认值。

新增后台接口必须先登记权限、测试只读用户、CSRF与路径范围；共享写成功后更新修订与审计。UI失败不得回退成模拟成功。

## 数据迁移

本包为 R5 服务化新部署基线，复用了 V2 代码与测试，但**不宣称能直接使用任意 V2 数据库升级**。V2非R5 profile结构虽然仍可被部分API兼容读取，R5工作台只装载具有 extractors/anomalies/nodes 的配置。已有V2部署请先备份并导出规则，显式转换或新目录部署。R5原型用户通过完整工作区备份迁移，个人规则不自动公开发布。
