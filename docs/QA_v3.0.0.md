# 3.0.0 / R5 服务化验证记录

验证日期：2026-10-31。测试使用当前交付源码；不把之前原型版本的数量叠加到本次报告。

## 已执行

| 范围 | 结果 | 证据 |
|---|---:|---|
| Python 标准库单元、真实HTTP、权限、路径、代理、规则修订、MCChecker格式适配、运维 | 37/37 通过 | `qa/backend-final.txt` |
| R5 JavaScript 条件/时间/字段/Traceback 引擎 | 33/33 通过 | `qa/engine-final.txt` |
| 真实 Chromium 渲染 + 真实 HTTP 适配联调 | 23/23 通过 | `qa/browser-service-results.json`、`qa/browser-service-run.txt` |
| 最终ZIP独立解压、校验清单、管理员初始化、监督进程启动、真实静态/API/文件读取 | 7项通过 | `qa/package-smoke.json` |
| 后端启动预检、前端实际组装、JS语法 | 通过 | `python worker.py --self-test`、`node --check` |
| 独立进程的覆盖更新、错误拒绝、启动回退与数据保留 | 通过，计入37项 | `tests/test_hot_update.py` |
| 批量ZIP更新隔离、非法路径/坏源码拒绝、SQLite备份/回收数据 | 通过，计入37项 | `tests/test_operations.py` |

浏览器联调的23项包括：不注入演示文件、仅加载目录元数据、真实身份、服务器文件读取、Traceback卡片、列配置、共享规则创建/发布、自定义提取与节点编辑/试跑、目录共享绑定、个人覆盖独立、409草稿保留/放弃恢复、全局时间筛选、双文件时间轴、本机导入不上传、实际目录创建、文件删除恢复、用户登记、审计、两种真实TCP源IP的权限/命名空间隔离、四种视口及无未处理脚本错误。

热更新测试是真正启动父/子进程并覆盖磁盘文件：有效Python改变发布与PID；语法错误不关闭旧服务；仅在实际启动时失败的版本回退旧快照。前后比较数据库规则ID、身份命名空间和管理员权限。它不是用mock证明“调用了restart”。

SQLite在线备份在启用WAL且仍有连接时读取快照，恢复副本可读。默认元数据备份也包含真实回收站内容；完整备份包含日志原文。

## 浏览器证据的准确范围

本环境策略阻止 Chromium 导航到 localhost，返回 **ERR_BLOCKED_BY_ADMINISTRATOR**。没有关闭、修改或绕过浏览器管理员策略。UI测试用 `set_content` 载入从真实服务读取的同一 HTML/CSS/JS；其 fetch 通过测试适配器转成实际TCP请求，后端确实执行SQLite与磁盘操作。页面解析Worker与渲染是真实浏览器运行，不是DOM模拟器；接口数据也不是合成响应或拦截器mock。

这条替代测试路径**不验证**原生导航、CSP的浏览器强制效果、原生下载、浏览器网络栈或跨刷新持久化。不能把它称作完整原生端到端浏览器验收。

另实际尝试 `qa/native_persistence_test.py`，结果是 **blocked / 0项完成**，详情在 `qa/native-persistence-results.json`。它会在允许正常HTTP导航的目标机器验证原生IndexedDB文件、列设置、刷新恢复及关闭浏览器档案后重开；被环境阻止返回77，不会标为通过。

## 重跑

```bash
# 部署本身不用额外依赖；下列是可选开发/验收命令。
python -m unittest discover -s tests -p "test_*.py" -v
python worker.py --self-test
node --test tests/*.test.cjs
# 安装Playwright/浏览器的测试机上：
python qa/browser_service_test.py
python qa/native_persistence_test.py
# 仅用于受限自动化环境的真实HTTP适配测试，不验证持久化：
python qa/browser_service_test.py --bridge
```

## 仍需目标环境验收

Windows/macOS、systemd/Docker/Nginx/TLS托管未实际安装；实际公司的 MCChecker 目录未挂载，仅验证已核对的资料格式及故障处理；原始21MB/2.7MB两份日志不在本次输入中，未做它们的内容正确性回归。实际低配置办公电脑、大库并发、设备时钟偏差和长时运行仍应测试。

本服务是可启动、可读写真数据的独立内网版本，不再是模拟后台；上述测试范围不等于渗透测试、整站无障碍认证、高负载公网验收或浏览器“永不丢缓存”保证。
