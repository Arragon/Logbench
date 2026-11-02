# LogBench 3.0.1 / Python 3.9.11 兼容验证

日期：2026-10-31。目标是兼容现有 3.9.11 环境，不改变 R5 前端。

## 本次实际执行

| 范围 | 结果 | 证据 |
| --- | --- | --- |
| 完整 Python 测试：API、权限、路径、身份隔离、共享规则、在线备份、真实进程热更新/拒绝/回退及兼容回归 | **46/46 通过** | `qa/python39/backend-final.txt` |
| R5 JavaScript 解析/规则/时间/Traceback 引擎 | **33/33 通过** | `qa/python39/engine-final.txt` |
| 全部 Python 文件按 3.9 grammar 解析；公开注解可求值；旧 Path.stat 签名边界 | 通过，计入46项 | `tests/test_python39.py` |
| worker 启动预检 | 返回0 | `qa/python39/worker-self-test.txt` |
| 重新解压完整ZIP、校验清单、管理员初始化、监督启动、R5资源、目录/API和UTF-8日志读取 | 见逐项结果 | `qa/python39/package-smoke.json` |
| R5前端与规则种子逐字节比对 | 与3.0.0一致 | `qa/python39/unchanged-assets.json` |

**实际解释器是 CPython 3.13.5/Linux，不是 3.9.11。** 下载官方 3.9.11 源码时容器无法解析域名，独立下载工具也失败，没有安装到目标解释器。语法检查、版本信息替身和 API 签名限制不能当成原生虚拟机验收。本轮没有声称原生3.9.11已通过。

新增版本/路径测试先在旧包上失败，再在改动后通过。备份连接释放测试使用真实 SQLite Connection 子类记录关闭动作，在旧实现上失败，显式关闭后通过。最后一轮完整测试没有 ResourceWarning。

## 重跑

在实际 Python 3.9.11 环境中，先检查 `python --version`。然后执行：

```bash
python -m unittest discover -s tests -p "test_*.py" -v
python -B worker.py --self-test
python qa/package_smoke.py ../LogBench_R5_Server_v3.0.1_Python39.zip --output qa/target-runtime-smoke.json
```

`package_smoke.py` 的结果记录真实解释器及 `nativeTargetRuntime`，只有实际为3.9.11时才为true。Node可选：`node --test tests/*.test.cjs`。部署无需Node或pip包。

## 未重新验收的范围

本次不改动前端，也没有重新做浏览器视觉/原生IndexedDB持久化验收；原v3.0.0记录保存在 `docs/QA_v3.0.0.md`，`qa/`根目录原报告均为历史证据，不计入此次通过数量。Windows/macOS、Docker、TLS/代理、真实MCChecker目录和实际业务日志仍需目标环境验收。原生3.9.11解释器运行未验证。

启动器修改必须停服替换并重启；旧版批量更新故意不覆盖run.py。数据及配置不变，详见 `docs/PYTHON39.md`。
