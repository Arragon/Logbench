"""Bounded development/internal-network HTTP transport.

For a managed production process use server.wsgi:application behind a WSGI server
and a trusted reverse proxy. This stdlib transport intentionally defaults to loopback.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .app import Application, Config, MAX_JSON, Response, compact, VERSION

LOG = logging.getLogger('logbench')


def response_headers(response):
    return {
        'X-Content-Type-Options': 'nosniff',
        'X-Frame-Options': 'DENY',
        'Referrer-Policy': 'same-origin',
        'Cache-Control': 'private, no-store',
        'Cross-Origin-Resource-Policy': 'same-origin',
        'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; worker-src 'self' blob:; img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        **response.headers,
    }


class Server(ThreadingHTTPServer):
    daemon_threads = False
    request_queue_size = 32

    def __init__(self, address, application):
        self.application = application
        self.slots = threading.BoundedSemaphore(application.config.max_connections)
        if ':' in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\nRetry-After: 2\r\n\r\n')
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'LogBench'
    sys_version = ''

    def setup(self):
        self.request.settimeout(20)
        super().setup()

    def do_GET(self):
        self.serve()

    do_HEAD = do_POST = do_PUT = do_DELETE = do_GET

    def serve(self):
        response = None
        try:
            if self.headers.get('Transfer-Encoding'):
                response = Response(400, {'error': {'code': 'UNSUPPORTED_TRANSFER', 'message': '不接受分块请求体。', 'details': {}}})
            elif len(self.headers.get_all('Content-Length') or []) > 1 or len(self.headers.get_all('Host') or []) != 1:
                response = Response(400, {'error': {'code': 'INVALID_HEADERS', 'message': '重复或缺失的请求头。', 'details': {}}})
            else:
                raw_length = self.headers.get('Content-Length', '0')
                if not raw_length.isdigit() or int(raw_length) > MAX_JSON:
                    response = Response(413, {'error': {'code': 'BODY_TOO_LARGE', 'message': '请求内容超过限制。', 'details': {}}})
                else:
                    length = int(raw_length)
                    body = self.rfile.read(length) if length else b''
                    if len(body) != length:
                        response = Response(400, {'error': {'code': 'INCOMPLETE_BODY', 'message': '请求内容不完整。', 'details': {}}})
                    else:
                        response = self.server.application.handle(self.command, self.path, dict(self.headers.items()), body, self.client_address[0])
            self.emit(response)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, socket.timeout):
            pass
        except Exception:
            LOG.exception('Request failed')
            if response is None:
                self.emit(Response(500, {'error': {'code': 'INTERNAL_ERROR', 'message': '服务暂时无法完成请求。', 'details': {}}}))
        finally:
            if response:
                response.close()
            self.close_connection = True

    def emit(self, response):
        body = compact(response.value).encode('utf-8') if response.value is not None and not response.stream else b''
        headers = response_headers(response)
        headers.setdefault('Content-Type', 'application/json; charset=utf-8')
        headers['Content-Length'] = str(response.length if response.stream else len(body))
        headers['Connection'] = 'close'
        self.send_response(response.status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command == 'HEAD':
            return
        if response.stream:
            remaining = response.length
            while remaining:
                chunk = response.stream.read(min(128 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        elif body:
            self.wfile.write(body)

    def log_message(self, fmt, *args):
        # Avoid exposing query-string contents or arbitrary log/file names in console.
        LOG.info('%s %s', self.command, args[1] if len(args) > 1 else '')


def load_config(argv=None):
    parser = argparse.ArgumentParser(description='LogBench 独立日志工作台')
    parser.add_argument('--config', help='JSON 配置文件')
    parser.add_argument('--init-only', action='store_true', help='初始化数据库/管理员后退出，不启动监听')
    parser.add_argument('--host')
    parser.add_argument('--port', type=int)
    parser.add_argument('--data-dir')
    parser.add_argument('--state-dir')
    parser.add_argument('--admin-ip', action='append', default=[], help='显式新增/恢复本地管理员 IP，可重复；外部身份模式不授予权限')
    parser.add_argument('--local-identity', action='store_true', help='显式恢复本地身份模式（要求同时 --admin-ip）')
    parser.add_argument('--allow-host', action='append', default=[], help='允许的请求主机名/IP，不含端口；可重复')
    args = parser.parse_args(argv)
    code_root = Path(__file__).resolve().parent.parent
    root = Path(os.environ.get('LOGBENCH_HOME', str(code_root))).resolve()
    filename = args.config or os.environ.get('LOGBENCH_CONFIG') or (str(root / 'config.json') if (root / 'config.json').is_file() else None)
    if filename and not Path(filename).is_absolute():
        filename = str(root / filename)
    values = {}
    if filename:
        values = json.loads(Path(filename).read_text('utf-8'))
        if not isinstance(values, dict):
            parser.error('配置文件必须是 JSON 对象。')
    allowed = {'data_dir', 'state_dir', 'host', 'port', 'allowed_hosts', 'trusted_proxies', 'public_origin', 'max_connections'}
    unknown = set(values) - allowed
    if unknown:
        parser.error('未知配置字段：' + ', '.join(sorted(unknown)))
    for key in ('host', 'port', 'data_dir', 'state_dir'):
        value = getattr(args, key)
        if value is not None:
            values[key] = value
    if args.allow_host:
        values['allowed_hosts'] = list(dict.fromkeys(values.get('allowed_hosts', ['localhost', '127.0.0.1', '::1']) + args.allow_host))
    for key in ('data_dir', 'state_dir'):
        if key in values and not Path(values[key]).is_absolute():
            values[key] = root / values[key]
    values['bootstrap_admins'] = args.admin_ip
    config = Config(root=root, code_root=code_root, **values)
    config.init_only = args.init_only
    if not 1 <= config.port <= 65535 or not 1 <= config.max_connections <= 256:
        parser.error('端口或并发数量超出范围。')
    if args.local_identity and not args.admin_ip:
        parser.error('--local-identity 需要同时指定 --admin-ip。')
    return config, args.local_identity


def main(argv=None):
    config, recovery = load_config(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    application = Application(config)
    if recovery:
        with application.store.transaction() as db:
            db.execute("UPDATE settings SET value=?,revision=revision+1 WHERE key='identity'", (compact({'identityMode': 'local', 'mccheckerDirectory': '', 'mccheckerDataDirectory': ''}),))
            application.store.audit(db, 'operator', 'identity.recovery', 'local')
    if getattr(config, 'init_only', False):
        LOG.info('LogBench 初始化完成。')
        return 0
    server = Server((config.host, config.port), application)
    LOG.info('LogBench %s; serving http://%s:%s', VERSION, config.host, config.port)
    stopping = threading.Event()
    def stop(*_):
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=server.shutdown, daemon=True).start()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: signal.signal(sig, stop)
        except (ValueError, OSError): pass
    if os.environ.get('LOGBENCH_SUPERVISED') == '1':
        def commands():
            try:
                for line in sys.stdin:
                    if line.strip() == 'stop':
                        break
            finally: stop()
        threading.Thread(target=commands, daemon=True).start()
    try:
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
