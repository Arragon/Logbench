# LogBench 3.0.2 / 默认端口 50004 / Python 3.9.11 兼容包

2026-10-31。此版本只修改端口默认值、端口配置工具、部署示例、文档与对应测试，不改动 R5 的前端、解析规则与数据库结构。原 v3.0.1 Python 兼容回归保留在 `docs/QA_v3.0.1.md`；更早版本记录在 `docs/QA_v3.0.0.md`。

## 本次验证

| 项目 | 实测结果 | 证据 |
| --- | --- | --- |
| 全量 Python 回归（API、权限、备份、真实热更新、Python 3.9 语法/API、端口行为） | **55/55 通过** | `qa/port-release/backend-final.txt` |
| 其中新增端口配置回归：默认值、CLI 优先级、永久写入、保留其他配置、交互输入、无效端口/破损 JSON、符号链接拒绝 | **9/9 通过** | `tests/test_port_config.py` |
| R5 前端规则/Traceback/解析引擎 | **33/33 通过** | `qa/port-release/engine-final.txt` |
| 后端 `worker.py --self-test` | 返回码 0 | `qa/port-release/worker-self-test.txt` |
| R5 前端与规则文件 | 与 v3.0.1 包中 **9 个文件逐字节一致** | 旧包与新包源码比对 |
| ZIP 完整性、独立解包、初始化、启动、真实文件读取、持久端口与启动参数覆盖 | 打包后实际执行验证 | `qa/package_smoke.py`，最终报告单独保存 |

测试运行环境是 **CPython 3.13.5 / Linux**。Python 3.9.11 是目标兼容版本，当前环境没有该原生解释器。通过的 3.9 语法/边界测试不能替代在你的目标机器上实际运行。

## 行为契约

- 无 `config.json`、无 `--port`：监听 `50004`。
- 有 `config.json`：其 `port` 字段覆盖默认值。升级旧部署时，原有 `8765` 不会因覆盖源码自动变为 `50004`。
- `python run.py --port N`：仅本次启动覆盖端口，不改变配置。
- `python tools/set_port.py N`：只创建或更新 `config.json` 的 `port`，其他配置字段保留；非法值、损坏的配置不会被覆盖。Windows 双击 `change-port.bat` 可交互输入，Linux/macOS 使用 `change-port.sh`。
- 修改配置后必须**重启父进程 `run.py`**；文件覆盖监控不会热加载 `config.json`。`server/` 等应用源码继续支持原有受控热更新。
- 改变浏览器访问端口会改变 Origin，本机浏览器 IndexedDB、LocalStorage 属于不同存储空间；升级前建议从旧地址备份工作区数据。服务端 `data/` / `var/` 保持原状。

## 目标环境建议命令

```bash
python --version
python -B worker.py --self-test
python -m unittest discover -s tests -p 'test_*.py' -v
python tools/set_port.py 50004
# 重启 LogBench；随后访问 http://服务器IP:50004/
```

若使用 Nginx/Docker/防火墙，请同步配置转发目标、映射和入站规则。本次没有在 Windows、macOS、Python 3.9.11、Docker 或目标办公机上进行运行验收；本轮也没有重新开展前端视觉、原生 IndexedDB 跨刷新测试。这些边界不因默认端口修改而改变。
