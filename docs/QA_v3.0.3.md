# LogBench 3.0.3 · 悬浮详情抽屉验证

2026-10-31。基于 v3.0.2 的 R5 页面，保持 Python 3.9.11 与无运行时第三方依赖约束。

## 故障根因与更改边界

- 原因：详情 `aside.inspector` 在 `.workpanel` 的 Flex 流中，占用 350px，且 `.inspecting` 在部分视口隐藏时间线；因此打开详情会压缩日志工作区并造成位置变化。
- 处理：详情改为工作区内绝对定位的非模态抽屉，阅读器列宽与时间线宽度不变。保留原生关闭、详情标签、上一条/下一条、Esc 与操作按钮。
- 阅读锚点：打开、关闭、切换详情时使用记录 ID 和相对滚动视口的屏幕偏移，配合 R5 原有虚拟列表重新定位。
- 边界：此版本不修改生产解析器、服务器 API、用户及存储模式、目录绑定与热更新机制。

## 验收命令

```bash
python qa/inspector_drawer_test.py
python -m unittest discover -s tests -p 'test_*.py'
node --test tests/*.test.cjs
python qa/browser_service_test.py --bridge --output qa/drawer-service-results.json
python worker.py --self-test
```

`qa/inspector_drawer_test.py` 和 `qa/browser_service_test.py --bridge` 使用 Chromium 渲染和真实 TCP/SQLite 服务，通过适配桥读取页面。它们不能替代目标机器上的本机浏览器原生导航、持久存储或公网代理验收。

## 本次结果

- Python 标准库测试：55/55 通过。
- 前端引擎测试：33/33 通过。
- 真实 Chromium＋真实 TCP/SQLite 桥接联调：23/23 通过。
- 悬浮抽屉专项：1440/1024/390，打开后阅读区宽度与时间线保持不变，焦点位置、标签切换、关闭和 Esc 均通过。
- 运行环境为 Python 3.13 / Linux；Python 3.9.11 目标解释器仍需在部署服务器上实测。
