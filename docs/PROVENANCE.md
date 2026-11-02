# 来源与复用记录

此次集成基于用户已经提供/已交付的 `LogBench_R5_Prototype_Package.zip` 与 `LogBench_v2.0.zip`。

- 前端视觉、交互、解析/条件引擎、Traceback、本机缓存：复用 R5 源文件，服务化适配集中在 `src/service.js`、身份启动链与局部数据入口。
- 后端路由、SQLite持久化、IP权限、MCChecker只读身份适配、文件隔离/回收站、规则与绑定修订、HTTP/WSGI、原20项回归：复用 V2 后端并增量扩展。
- MCChecker 当前参考提交：`429dafaa4938ee42f59807b1720e56c225d8cbfa`。核对 `app/server/reloader.py` 的独立父/子进程重载策略，以及 `app/core/storage.py` / `app/services/people.py` 的身份资料格式。
- 没有把 MCChecker 整库、NiceGUI、调度业务或其运行依赖打包进 LogBench；没有导入/执行目标 MCChecker 部署中的代码。此次是兼容格式和实现思路参考，不是以不明许可证将其整套源码重新发布。
- 运行时没有第三方包、下载字体、远程脚本、遥测或 CDN。源码内图标与CSS来自已有R5设计资产。可选测试工具不属于运行依赖。

参考原始路径：

https://github.com/Arragon/MCChecker/blob/429dafaa4938ee42f59807b1720e56c225d8cbfa/app/server/reloader.py
https://github.com/Arragon/MCChecker/blob/429dafaa4938ee42f59807b1720e56c225d8cbfa/app/core/storage.py
https://github.com/Arragon/MCChecker/blob/429dafaa4938ee42f59807b1720e56c225d8cbfa/app/services/people.py

运行限制的官方参考：

https://docs.python.org/3/library/http.server.html
https://docs.python.org/3/library/sqlite3.html
https://developer.mozilla.org/en-US/docs/Web/API/StorageManager/persist
