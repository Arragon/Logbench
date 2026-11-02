# 服务托管与反向代理

主入口为 `python run.py`，默认启用覆盖更新。不要同时让多个入口监听同一端口或写同一 state_dir。最简单的受控内网方式无需安装 Python 以外的包；长期运行用操作系统服务托管。不要把 IP 身份当作公网强认证。

## systemd

示例文件 `logbench.service` 需修改用户、目录。用无特权账号运行，账号需可写 `data/`、`var/`；覆盖更新工具还需写应用源码目录。管理员完成初始化后再启动服务。`KillMode=mixed` 与 45 秒停止超时用于让父进程停止子进程并排空请求；不是直接套用 MCChecker 的 `--no-reload` 单元。

```bash
sudo cp deploy/logbench.service /etc/systemd/system/logbench.service
sudo systemctl daemon-reload
sudo systemctl enable --now logbench
journalctl -u logbench -f
```

这些命令涉及系统配置，请先检查文件并以实际管理员执行。本交付没有在你的服务器上安装服务。

## 端口配置

程序默认端口为 `50004`。永久修改运行配置可执行 `python tools/set_port.py 50005` 或双击 `change-port.bat`；重新启动服务后生效。若采用 Nginx，记得同时修改 `proxy_pass`；若采用 Docker，记得修改发布端口映射。只改应用端口不等于自动修改反向代理或容器映射。

## HTTPS 反代

`nginx.conf.example` 给出单个受信代理的设置。代理覆盖 X-Forwarded-For，后端只信任你声明的代理 CIDR。别把整个用户网段列为代理。启动后检查 `/api/v1/me` 显示的 IP 是否为实际浏览器客户端 IP，再登记管理员；否则所有人可能共享代理 IP 身份。

代理下必须设置 `public_origin` 为浏览器访问的外部 HTTPS origin；否则写请求的 Origin 检查会拒绝它。`allowed_hosts` 填不带端口的主机名/IP，不接受 `*`。多代理链需要逐跳验证，不直接照抄单代理示例。

Python 标准库 HTTP 服务仅作受控内网传输，具有连接/请求体/路径/权限限制，不等于高负载互联网网关。若暴露到更复杂网络，请在前置代理上配置 TLS、访问限制和限速。另提供 `server.wsgi:create_application`，可集成现有 WSGI 托管环境；第三方 WSGI 服务器不是默认依赖，且本包的自动换进程更新只覆盖 `run.py` 默认启动方式，不能宣称 WSGI 托管也自动具备相同机制。

## Docker（可选，示例未在此环境构建验收）

```bash
docker build -f deploy/Dockerfile -t logbench:3.0.2 .
```

按环境将 `data/`、`var/` 和 `config.json` 挂载为持久卷，设置正确的 host/allowed_hosts/trusted_proxies，并先用 `--admin-ip <实际客户端IP> --init-only` 初始化。不要把数据库留在容器可写层。若希望在宿主机覆盖源码触发更新，还需 bind mount `src/`、`server/`、`rules/` 与 `worker.py`；只更新镜像本身需要重新创建容器，不属于“宿主文件覆盖热更新”。Docker 网络与代理会影响客户端 IP，需要实际检查，不能默认容器看到的就是用户真实 IP。

Python 3.9.11 现有环境直接使用主机解释器即可。可选 Docker 示例仍采用 Python 3.12 基础镜像，不要求为兼容旧主机而降级容器解释器；该 Docker 示例本轮未构建验证。
