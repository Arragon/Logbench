# Python 3.9.11 兼容说明 · 3.0.1

本版本在 v3.0.0/R5 部署包上做兼容修改。R5 的 HTML、JavaScript、CSS、规则种子均保持逐字节一致；不新增 pip、Node 或其他运行依赖。SQLite 数据结构、用户命名空间、IP 权限和重启式热更新协议不变。

## 修改内容

- `run.py`：最低版本从 3.10 改为 3.9.11，低于此版本会在初始化数据前退出并说明原因。
- `server/app.py`：四处 `Path | None` / `BinaryIO | None` 改为 `typing.Optional`，避免 3.9 运行时求值类型注解的问题。原来使用了延迟注解，因此它们并非必然阻止模块导入，不能把问题泛化为“所有管道类型语法都无法解析”。
- 文件库列表与递归目录索引的两处 `Path.stat(follow_symlinks=False)` 改为 `Path.lstat()`。前者的关键字参数是 Python 3.10 新增，3.9 会抛 TypeError；后者仍保持不跟随符号链接的语义。原有路径约束、隐藏目录和链接过滤不删除。
- `tools/backup.py` 显式关闭在线备份的 SQLite 连接后再清理临时目录，不依赖垃圾回收释放句柄。此问题由本轮回归中的 ResourceWarning 定位，并补充真实连接关闭测试。
- 热更新回归测试不再把版本号写死为 3.0.0；每次从正在运行的服务读版本后注入测试版本。
- 增加 8 项版本/语法/路径 API/类型注解兼容测试，以及 1 项备份连接释放测试。

## 新部署

Windows（已安装 Python Launcher 和目标 3.9.11）：

```bat
py -3.9 --version
py -3.9 -B worker.py --self-test
py -3.9 -B run.py --admin-ip 127.0.0.1 --init-only
py -3.9 -B run.py
```

Linux/macOS：

```bash
python3.9 --version
python3.9 -B worker.py --self-test
python3.9 -B run.py --admin-ip 127.0.0.1 --init-only
python3.9 -B run.py
```

v3.0.2 起默认浏览器地址为 `http://127.0.0.1:50004/`（v3.0.1 原默认 `8765`）。**已有 `config.json` 中显式设置的端口优先于新默认值**，可运行 `python tools/set_port.py 50004` 持久修改，然后重启。若当前 `python` 已经是 3.9.11，原来的 `python run.py` 命令仍可用。版本选择器只能选择已安装的解释器，不负责安装 Python。存在多个 3.9 安装时，用完整可执行路径选定 3.9.11。

`start-py39.sh` 默认调用 `python3.9`，`start-py39.bat` 默认调用 `py -3.9`；可设置 `PYTHON39` 为 Python 可执行文件完整路径。该变量只接受路径，不接受一串带参数的命令。原有通用 `start.sh`、`start.bat` 保留。

所有预检、批量更新预检、热更新子进程都沿用 `sys.executable`，因此启动器使用哪个 Python，后续工作进程就使用哪个。SQLite 使用目标解释器自带的 `sqlite3` 模块；精简/自编译 Python 必须保留此模块。

## 从 3.0.0 升级

1. 使用旧环境运行 `python tools/backup.py --include-data --out ../logbench-before-py39.zip`，并将本机工作区按需导出。备份包含敏感数据，应妥善保存。
2. 停止原启动器/系统服务，不在运行中替换 Python 环境。
3. 把本包应用文件覆盖到原部署目录，包括 **`run.py`、`worker.py`、`server/`、`src/`、`rules/`、`tools/`**。保留 `data/`、`var/`、`config.json`，不要新建数据库替换原库。备份中也保留原程序文件，便于回退。
4. 用目标 3.9.11 执行 `worker.py --self-test`，再运行 `run.py`。已初始化用户无需再次传 `--admin-ip`。
5. 保持原来的域名/IP、协议、端口和浏览器档案，浏览器身份缓存不会因本次代码版本主动重置。其他浏览器回收/无痕限制仍遵循原说明。

**本次不能只运行旧的 `tools/apply_update.py`。** 它按设计排除 `run.py` 和 `tools/`；这样更新会遗留 3.10 启动门槛及旧工具。完成这次停服迁移后，后续应用代码仍支持原有覆盖自动重启，`run.py` 或解释器自身变化继续需要人工重启。

## 验证状态

当前验证机是 CPython 3.13.5/Linux，不是 3.9.11。已尝试从 Python 官方获取 3.9.11 源码；容器 DNS/下载不可用，未取得解释器。因此本包完成的是兼容代码修改、3.9 语法检查、针对旧 API 的边界测试，以及可用解释器上的完整回归，**未宣称完成原生 3.9.11 验收**。

`tests/test_python39.py` 的旧版本检查在启动器边界使用版本信息替身，在路径调用边界限制 3.9 的方法参数，同时继续操作真实磁盘/SQLite。它不能模拟整个 3.9 虚拟机。请在实际 3.9.11 环境重跑：

```bash
python --version
python -m unittest discover -s tests -p "test_*.py" -v
python -B worker.py --self-test
```

46 项 Python 测试应通过；Windows 上创建符号链接需要相应权限，相关测试可能按权限条件跳过，须查看具体测试输出。Windows/macOS、Docker、反向代理和真实业务日志的既有待验收项不因本次版本适配自动变为通过。完整记录见 `docs/QA.md`。

## 安全维护边界

Python 官方已于 2025-10-31 结束 3.9 维护，3.9.11 也早于 3.9 系列最后的安全修复版。本包只是应用兼容，不会为旧解释器及其 OpenSSL/SQLite 等底层库补安全更新。仅建议用于已有、受控的内网环境；能升级环境时仍应使用组织维护的解释器。IP 身份也仍不是公网强认证。

官方参考：
- https://www.python.org/downloads/release/python-3911/
- https://peps.python.org/pep-0596/
- https://docs.python.org/3.10/library/pathlib.html#pathlib.Path.stat
- https://docs.python.org/3.10/library/typing.html#typing.Optional
