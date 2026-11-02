"""IP-scoped LogBench API, SQLite storage, and a jailed shared log library.

No log upload route exists: local imports stay in browser storage. Shared files are
managed by an operator under data/. Identity providers never execute deployment code.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Optional
from urllib.parse import parse_qs, quote, urlsplit

VERSION = '3.0.3'
MAX_JSON = 2 * 1024 * 1024
MC_REFERENCE = '429dafaa4938ee42f59807b1720e56c225d8cbfa'
READ_CAPABILITIES = ('read', 'localImport', 'preferencesWrite')
ADMIN_CAPABILITIES = ('admin', 'rulesWrite', 'filesWrite', 'usersWrite', 'settingsWrite')


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **details: Any):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def fail(status: int, code: str, message: str, **details: Any):
    raise ApiError(status, code, message, **details)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def clean_ip(value: Any) -> str:
    raw = str(value or '').strip().strip('[]')
    if '%' in raw:
        fail(400, 'INVALID_IP', '请输入不带网络范围标识的 IPv4 或 IPv6 地址。')
    try:
        parsed = ipaddress.ip_address(raw)
        if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
            parsed = parsed.ipv4_mapped
        return str(parsed)
    except ValueError:
        fail(400, 'INVALID_IP', '请输入有效的 IPv4 或 IPv6 地址。')


def text(value: Any, label: str, limit: int = 100, empty: bool = False) -> str:
    if not isinstance(value, str):
        fail(400, 'INVALID_INPUT', label + '必须是文本。')
    result = value.strip()
    if (not result and not empty) or len(result) > limit or any(ord(c) < 32 for c in result):
        fail(400, 'INVALID_INPUT', label + '长度或格式不正确。')
    return result


def integer(value: Any, label: str, minimum: int = 0, maximum: int = 1000000000) -> int:
    try:
        if isinstance(value, bool) or str(int(value)) != str(value):
            raise ValueError()
        result = int(value)
        if not minimum <= result <= maximum:
            raise ValueError()
        return result
    except (ValueError, TypeError):
        fail(400, 'INVALID_INPUT', label + '必须是有效整数。')


@dataclass
class Config:
    root: Path
    data_dir: Optional[Path] = None
    state_dir: Optional[Path] = None
    host: str = '127.0.0.1'
    port: int = 50004
    allowed_hosts: list[str] = field(default_factory=lambda: ['localhost', '127.0.0.1', '::1'])
    trusted_proxies: list[str] = field(default_factory=list)
    public_origin: str = ''
    bootstrap_admins: list[str] = field(default_factory=list)
    max_connections: int = 32
    code_root: Optional[Path] = None

    def __post_init__(self):
        self.root = Path(self.root).resolve()
        self.code_root = Path(self.code_root or self.root).resolve()
        self.data_dir = Path(self.data_dir or self.root / 'data').resolve()
        self.state_dir = Path(self.state_dir or self.root / 'var').resolve()
        if self.data_dir == self.state_dir or self.data_dir in self.state_dir.parents or self.state_dir in self.data_dir.parents:
            raise ValueError('data_dir 与 state_dir 不能相同或互相包含。')
        self.bootstrap_admins = [clean_ip(x) for x in self.bootstrap_admins]
        self.trusted_networks = [ipaddress.ip_network(x, strict=False) for x in self.trusted_proxies]
        self.allowed_hosts = [str(x).lower().strip('[]') for x in self.allowed_hosts]
        if not self.allowed_hosts or '*' in self.allowed_hosts:
            raise ValueError('allowed_hosts 必须明确列出主机名，不能使用 *。')
        if self.public_origin:
            origin = urlsplit(self.public_origin)
            if origin.scheme not in ('http', 'https') or not origin.netloc or origin.path not in ('', '/') or origin.query or origin.fragment:
                raise ValueError('public_origin 必须是 http(s)://host[:port]。')
            self.public_origin = self.public_origin.rstrip('/')


@dataclass
class Response:
    status: int = 200
    value: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    stream: Optional[BinaryIO] = None
    length: int = 0

    def close(self):
        if self.stream:
            self.stream.close()


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = directory / 'logbench.sqlite3'
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS users (ip TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, enabled INTEGER NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS preferences (actor TEXT PRIMARY KEY, value TEXT NOT NULL, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS rules (id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, profile TEXT NOT NULL, revision INTEGER NOT NULL, deleted_at TEXT, updated_at TEXT NOT NULL, updated_by TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS bindings (scope TEXT NOT NULL, path TEXT NOT NULL, rule_id TEXT NOT NULL REFERENCES rules(id), revision INTEGER NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(scope,path));
                CREATE TABLE IF NOT EXISTS trash (id TEXT PRIMARY KEY, path TEXT NOT NULL, type TEXT NOT NULL, deleted_at TEXT NOT NULL, deleted_by TEXT NOT NULL, restored_at TEXT);
                CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, detail TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS audit_at ON audit(at);
                CREATE INDEX IF NOT EXISTS rules_deleted ON rules(deleted_at);
            ''')
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def connect(self):
        db = sqlite3.connect(str(self.path), timeout=10)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.lock, self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            yield db

    def get_setting(self, key: str, default: Any = None):
        with self.connect() as db:
            row = db.execute('SELECT value,revision FROM settings WHERE key=?', (key,)).fetchone()
        return (json.loads(row['value']), row['revision']) if row else (default, 0)

    def audit(self, db, actor: str, action: str, target: str, detail: Any = None):
        db.execute('INSERT INTO audit(at,actor,action,target,detail) VALUES(?,?,?,?,?)', (now(), actor, action, target, compact(detail or {})))


class Application:
    def __init__(self, config: Config):
        self.config = config
        config.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(config.state_dir)
        self.fs_lock = threading.RLock()
        self.trash_root = config.data_dir / '.logbench-trash'
        if self.trash_root.is_symlink():
            raise ValueError('回收站目录不能是符号链接。')
        self.trash_root.mkdir(mode=0o700, exist_ok=True)
        with self.store.transaction() as db:
            if not db.execute("SELECT 1 FROM settings WHERE key='secret'").fetchone():
                db.execute("INSERT INTO settings(key,value) VALUES('secret',?)", (compact(secrets.token_hex(32)),))
            if not db.execute("SELECT 1 FROM settings WHERE key='identity'").fetchone():
                db.execute("INSERT INTO settings(key,value) VALUES('identity',?)", (compact({'identityMode': 'local', 'mccheckerDirectory': '', 'mccheckerDataDirectory': ''}),))
            for ip in config.bootstrap_admins:
                # An explicit operator startup flag is the recovery/bootstrap action.
                db.execute('INSERT INTO users(ip,name,role,enabled,revision,updated_at) VALUES(?,?,?,?,1,?) ON CONFLICT(ip) DO UPDATE SET role=excluded.role,enabled=1,revision=users.revision+1,updated_at=excluded.updated_at', (ip, ip, 'admin', 1, now()))
                self.store.audit(db, 'operator', 'user.bootstrap', ip)
            if not db.execute('SELECT 1 FROM rules LIMIT 1').fetchone():
                seed = config.code_root / 'rules' / 'cpqpsgx-default.json'
                if seed.is_file():
                    profile = json.loads(seed.read_text('utf-8'))
                    rid = 'cpqpsgx-default'
                    db.execute('INSERT INTO rules VALUES(?,?,?,?,1,NULL,?,?)', (rid, profile.get('name', 'CPQPSGX'), '内置方括号日志规则，可复制或修改', compact(profile), now(), 'system'))
                    db.execute('INSERT INTO bindings VALUES(?,?,?,?,?)', ('default', '', rid, 1, now()))
        self.secret = bytes.fromhex(self.store.get_setting('secret')[0])

    def trusted(self, raw: str) -> bool:
        address = ipaddress.ip_address(raw)
        return any(address in network for network in self.config.trusted_networks)

    def client_ip(self, peer: str, headers: dict[str, str]) -> str:
        peer = clean_ip(peer)
        if not self.trusted(peer):
            return peer
        forwarded = headers.get('x-forwarded-for', '').strip()
        if not forwarded:
            # A trusted proxy with no original address must never become a local admin.
            fail(400, 'MISSING_CLIENT_IP', '受信代理没有传递客户端地址。')
        if len(forwarded) > 2048:
            fail(400, 'INVALID_CLIENT_IP', '代理地址链过长。')
        chain = [clean_ip(part) for part in forwarded.split(',')]
        # Discard only trusted hops from the right, so a client-controlled leftmost
        # value cannot elevate access when a proxy appends to an existing header.
        for address in reversed(chain):
            if not self.trusted(address):
                return address
        return chain[0]

    def csrf(self, ip: str) -> str:
        return hmac.new(self.secret, ('csrf:' + ip).encode(), hashlib.sha256).hexdigest()

    def namespace(self, ip: str) -> str:
        return hmac.new(self.secret, ('namespace:' + ip).encode(), hashlib.sha256).hexdigest()[:24]

    def read_mcchecker(self, config: dict) -> tuple[list, list]:
        directory = text(config.get('mccheckerDirectory', ''), '部署目录', 2048)
        deployment = Path(directory).expanduser()
        if not deployment.is_absolute():
            fail(400, 'INVALID_DIRECTORY', '部署目录必须是服务器上的绝对路径。')
        override = config.get('mccheckerDataDirectory') or ''
        data = Path(override).expanduser() if override else deployment / 'data'
        if not data.is_absolute():
            fail(400, 'INVALID_DIRECTORY', '数据目录必须是服务器上的绝对路径。')
        rows = []
        for filename in ('ip_mapping.json', 'admin_users.json'):
            path = data / filename
            try:
                if path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
                    fail(503, 'IDENTITY_SOURCE_UNAVAILABLE', '人员配置不可读或超过大小限制。')
                value = json.loads(path.read_text('utf-8-sig'))
            except (OSError, UnicodeError, ValueError):
                fail(503, 'IDENTITY_SOURCE_UNAVAILABLE', '无法读取人员配置，请检查部署目录与 JSON 文件。')
            if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
                fail(503, 'IDENTITY_SOURCE_INVALID', '人员配置必须是对象数组。')
            rows.append(value)
        return rows[0], rows[1]

    @staticmethod
    def mc_actor(ip: str, mapping: list, admins: list) -> dict:
        # Exact equality deliberately matches the verified MCChecker storage rules.
        names = [str(row.get('name') or '').strip() for row in mapping if str(row.get('ip') or '').strip() == ip]
        name = names[0] if names else ''
        admin = False
        legacy_names = set()
        for row in admins:
            stored = str(row.get('ip') or '').strip()
            if stored:
                if stored == ip:
                    admin = True
            elif str(row.get('name') or '').strip():
                legacy_names.add(str(row.get('name')).strip())
        if any(value in legacy_names for value in names):
            admin = True
        return {'ip': ip, 'name': name or ip, 'role': 'admin' if admin else ('member' if name else 'viewer'), 'source': 'mcchecker'}

    def actor(self, ip: str) -> dict:
        settings, _ = self.store.get_setting('identity')
        if settings['identityMode'] == 'mcchecker':
            actor = self.mc_actor(ip, *self.read_mcchecker(settings))
        else:
            with self.store.connect() as db:
                row = db.execute('SELECT * FROM users WHERE ip=?', (ip,)).fetchone()
            if row and not row['enabled']:
                fail(403, 'USER_DISABLED', '当前 IP 的访问已被停用。')
            actor = {'ip': ip, 'name': row['name'] if row else ip, 'role': row['role'] if row else 'viewer', 'source': 'local'}
        actor['id'] = self.namespace(ip)
        return actor

    @staticmethod
    def require_admin(actor: dict):
        if actor['role'] != 'admin':
            fail(403, 'FORBIDDEN', '此操作需要管理员权限。')

    def handle(self, method: str, target: str, headers: dict, body: bytes = b'', peer: str = '127.0.0.1') -> Response:
        headers = {k.lower(): v for k, v in headers.items()}
        try:
            host = headers.get('host', '')
            try:
                parsed_host = urlsplit('//' + host)
                hostname = parsed_host.hostname
                if not hostname or parsed_host.username or parsed_host.path or parsed_host.query or parsed_host.fragment:
                    raise ValueError()
                _ = parsed_host.port
            except ValueError:
                fail(400, 'INVALID_HOST', '无效的请求主机。')
            if hostname.lower() not in self.config.allowed_hosts:
                fail(421, 'HOST_NOT_ALLOWED', '请求主机未在 allowed_hosts 中登记。')
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or '\\' in parsed.path:
                fail(400, 'INVALID_URL', '无效的请求路径。')
            path = parsed.path.rstrip('/') or '/'
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=30)
            def q(key, default=''):
                values = query.get(key, [default])
                if len(values) != 1:
                    fail(400, 'DUPLICATE_PARAMETER', '参数不能重复。', field=key)
                return values[0]
            if method not in ('GET', 'HEAD', 'POST', 'PUT', 'DELETE'):
                fail(405, 'METHOD_NOT_ALLOWED', '不支持此请求方法。')
            if not path.startswith('/api/'):
                if method not in ('GET', 'HEAD'):
                    fail(405, 'METHOD_NOT_ALLOWED', '静态资源只支持读取。')
                return self.static(path)
            ip = self.client_ip(peer, headers)
            actor = self.actor(ip)
            if method in ('POST', 'PUT', 'DELETE'):
                if headers.get('sec-fetch-site', '') == 'cross-site':
                    fail(403, 'CSRF_REJECTED', '拒绝跨站写入请求。')
                expected_origin = self.config.public_origin
                if not expected_origin:
                    proto = headers.get('x-forwarded-proto', 'http') if self.trusted(clean_ip(peer)) else 'http'
                    proto = 'https' if proto == 'https' else 'http'
                    expected_origin = proto + '://' + host
                origin = headers.get('origin')
                if origin is not None and origin != expected_origin:
                    fail(403, 'CSRF_REJECTED', '写入请求必须来自当前站点。')
                token = headers.get('x-logbench-csrf', '')
                if not hmac.compare_digest(token, self.csrf(ip)):
                    fail(403, 'CSRF_REJECTED', '操作凭据已失效，请刷新页面。')
                if len(body) > MAX_JSON:
                    fail(413, 'BODY_TOO_LARGE', '请求内容超过 2 MiB。')
                if body and headers.get('content-type', '').split(';')[0].lower() != 'application/json':
                    fail(415, 'JSON_REQUIRED', '写入请求需要 application/json。')
            payload = {}
            if body:
                try:
                    payload = json.loads(body.decode('utf-8'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                except (UnicodeError, ValueError):
                    fail(400, 'INVALID_JSON', '请求不是有效的 JSON。')
                if not isinstance(payload, dict):
                    fail(400, 'INVALID_JSON', 'JSON 请求必须是对象。')
            result = self.route(method, path, q, payload, actor, headers)
            if isinstance(result, Response):
                return result
            return Response(value=result)
        except ApiError as exc:
            return Response(status=exc.status, value={'error': {'code': exc.code, 'message': exc.message, 'details': exc.details}})
        except (sqlite3.Error, OSError):
            return Response(status=500, value={'error': {'code': 'STORAGE_ERROR', 'message': '存储操作失败，请检查目录权限与磁盘空间。', 'details': {}}})
        except (ValueError, TypeError, KeyError):
            return Response(status=400, value={'error': {'code': 'INVALID_INPUT', 'message': '请求字段格式不正确。', 'details': {}}})

    def route(self, method, path, q, payload, actor, headers):
        ip = actor['ip']
        if path == '/api/v1/me' and method in ('GET', 'HEAD'):
            return {'user': actor, 'capabilities': {**{k: True for k in READ_CAPABILITIES}, **{k: actor['role'] == 'admin' for k in ADMIN_CAPABILITIES}}, 'csrfToken': self.csrf(ip), 'version': VERSION, 'identityMode': actor['source']}
        if path == '/api/v1/health' and method in ('GET', 'HEAD'):
            return {'status': 'ok', 'version': VERSION, 'release': os.environ.get('LOGBENCH_RELEASE', 'direct'), 'pid': os.getpid()}
        if path == '/api/v1/preferences':
            if method in ('GET', 'HEAD'):
                with self.store.connect() as db:
                    row = db.execute('SELECT value,revision FROM preferences WHERE actor=?', (ip,)).fetchone()
                return {'value': json.loads(row['value']) if row else {}, 'revision': row['revision'] if row else 0}
            if method == 'PUT':
                value = payload.get('value')
                if not isinstance(value, dict) or len(compact(value).encode()) > 1024 * 1024:
                    fail(400, 'INVALID_PREFERENCES', '个人配置必须是小于 1 MiB 的对象。')
                revision = integer(payload.get('expectedRevision'), '版本号')
                with self.store.transaction() as db:
                    old = db.execute('SELECT revision FROM preferences WHERE actor=?', (ip,)).fetchone()
                    self.check_revision(revision, old['revision'] if old else 0)
                    db.execute('INSERT INTO preferences VALUES(?,?,?) ON CONFLICT(actor) DO UPDATE SET value=excluded.value,revision=excluded.revision', (ip, compact(value), revision + 1))
                return {'value': value, 'revision': revision + 1}
        if path == '/api/v1/catalog' and method in ('GET', 'HEAD'):
            return self.catalog()
        if path == '/api/v1/files':
            if method in ('GET', 'HEAD'):
                return self.files(q('path'), q('q'), integer(q('cursor', '0'), '页码'), integer(q('limit', '100'), '数量', 1, 200))
            if method == 'DELETE':
                self.require_admin(actor)
                return self.delete_file(q('path'), ip)
        if path == '/api/v1/files/content' and method in ('GET', 'HEAD'):
            return self.file_content(q('path'), headers)
        if path == '/api/v1/files/directory' and method == 'POST':
            self.require_admin(actor)
            rel = self.normal_path(payload.get('path'), allow_root=False)
            target = self.safe_path(rel, exists=False)
            with self.fs_lock:
                if target.exists():
                    fail(409, 'ALREADY_EXISTS', '目录已存在。')
                target.mkdir()
            with self.store.transaction() as db:
                self.store.audit(db, ip, 'directory.create', rel)
            return {'path': rel}
        if path == '/api/v1/files/trash' and method in ('GET', 'HEAD'):
            self.require_admin(actor)
            with self.store.connect() as db:
                rows = db.execute('SELECT * FROM trash WHERE restored_at IS NULL ORDER BY deleted_at DESC LIMIT 1000').fetchall()
            return {'items': [{'id': r['id'], 'path': r['path'], 'type': r['type'], 'deletedAt': r['deleted_at'], 'deletedBy': r['deleted_by']} for r in rows]}
        if path == '/api/v1/files/restore' and method == 'POST':
            self.require_admin(actor)
            return self.restore_file(text(payload.get('id'), '回收站编号'), ip)
        if path == '/api/v1/rules':
            if method in ('GET', 'HEAD'):
                deleted = q('deleted') == '1'
                if deleted:
                    self.require_admin(actor)
                with self.store.connect() as db:
                    rows = db.execute('SELECT * FROM rules WHERE deleted_at IS ' + ('NOT NULL' if deleted else 'NULL') + ' ORDER BY name,id').fetchall()
                return {'items': [self.rule_row(r) for r in rows]}
            if method == 'POST':
                self.require_admin(actor)
                return {'item': self.create_rule(payload, ip)}
        match = re.fullmatch(r'/api/v1/rules/([A-Za-z0-9_-]+)(?:/(copy|restore))?', path)
        if match:
            rid, action = match.groups()
            if method in ('GET', 'HEAD') and not action:
                return {'item': self.get_rule(rid)}
            self.require_admin(actor)
            if method == 'PUT' and not action:
                return {'item': self.update_rule(rid, payload, ip)}
            if method == 'DELETE' and not action:
                return {'item': self.change_rule_deleted(rid, payload, ip, True)}
            if method == 'POST' and action == 'restore':
                return {'item': self.change_rule_deleted(rid, payload, ip, False)}
            if method == 'POST' and action == 'copy':
                item = self.get_rule(rid)
                return {'item': self.create_rule({'name': payload.get('name') or item['name'] + ' 副本', 'description': item['description'], 'profile': item['profile']}, ip)}
        if path == '/api/v1/bindings':
            if method in ('GET', 'HEAD'):
                with self.store.connect() as db:
                    rows = db.execute('SELECT * FROM bindings ORDER BY scope,path').fetchall()
                return {'items': [self.binding_row(r) for r in rows]}
            if method in ('PUT', 'DELETE'):
                self.require_admin(actor)
                return self.write_binding(payload, ip, method == 'DELETE')
        if path == '/api/v1/bindings/resolve' and method in ('GET', 'HEAD'):
            return self.resolve_binding(self.normal_path(q('path'), allow_root=True))
        if path.startswith('/api/v1/admin/'):
            self.require_admin(actor)
            return self.admin_route(method, path, q, payload, actor)
        fail(404, 'NOT_FOUND', '接口或资源不存在。')

    @staticmethod
    def check_revision(expected, actual):
        if expected != actual:
            fail(409, 'REVISION_CONFLICT', '内容已被其他管理员修改，请刷新后重试。', currentRevision=actual)

    @staticmethod
    def normal_path(raw, allow_root=False):
        if not isinstance(raw, str) or len(raw) > 2048 or raw.startswith('/') or '\\' in raw or ':' in raw or '\x00' in raw:
            fail(400, 'INVALID_PATH', '请输入日志库中的相对路径。')
        parts = raw.split('/') if raw else []
        if any(not part or part in ('.', '..') or part.startswith('.') or any(ord(c) < 32 for c in part) for part in parts):
            fail(400, 'INVALID_PATH', '路径不能包含空段、隐藏目录或上级目录。')
        if not parts and not allow_root:
            fail(400, 'INVALID_PATH', '不能对日志库根目录执行此操作。')
        return '/'.join(parts)

    def safe_path(self, rel, exists=True):
        rel = self.normal_path(rel, allow_root=True)
        current = self.config.data_dir
        parts = rel.split('/') if rel else []
        for i, part in enumerate(parts):
            current = current / part
            if current.is_symlink():
                fail(403, 'SYMLINK_DENIED', '日志库不读取符号链接。')
            if not current.exists():
                if not exists and i == len(parts) - 1:
                    break
                fail(404, 'FILE_NOT_FOUND', '文件或目录不存在。')
        return current

    def open_file(self, rel):
        target = self.safe_path(rel)
        if not target.is_file():
            fail(400, 'NOT_A_FILE', '请选择一个日志文件。')
        if os.name == 'posix' and hasattr(os, 'O_NOFOLLOW'):
            directory = os.open(str(self.config.data_dir), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                parts = rel.split('/')
                for part in parts[:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                    os.close(directory)
                    directory = child
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    os.close(fd)
                    fail(400, 'NOT_A_FILE', '请选择常规文件。')
                return os.fdopen(fd, 'rb')
            finally:
                os.close(directory)
        return target.open('rb')

    def files(self, rel, query, cursor, limit):
        root = self.safe_path(rel)
        if not root.is_dir():
            fail(400, 'NOT_A_DIRECTORY', '请选择一个目录。')
        query = text(query, '搜索词', 300, empty=True).casefold()
        entries = []
        seen = 0
        scanned_limit = 100000
        if query:
            for current, dirs, names in os.walk(root, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not (Path(current) / d).is_symlink())
                for name in dirs + names:
                    seen += 1
                    if seen > scanned_limit:
                        fail(422, 'SEARCH_SCOPE_TOO_LARGE', '搜索目录超过 100,000 项，请先选择更小的目录。')
                    item = Path(current) / name
                    if name.startswith('.') or item.is_symlink():
                        continue
                    relative = item.relative_to(self.config.data_dir).as_posix()
                    if query in relative.casefold():
                        entries.append((item, relative))
        else:
            for item in root.iterdir():
                if not item.name.startswith('.') and not item.is_symlink():
                    entries.append((item, item.relative_to(self.config.data_dir).as_posix()))
        entries.sort(key=lambda pair: (not pair[0].is_dir(), pair[1].casefold(), pair[1]))
        total = len(entries)
        with self.store.connect() as db:
            bindings = db.execute('SELECT bindings.* FROM bindings JOIN rules ON rules.id=rule_id WHERE rules.deleted_at IS NULL').fetchall()
        result = []
        for item, relative in entries[cursor:cursor + limit]:
            try:
                info = item.lstat()
                if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    continue
                binding = self.choose_binding(relative, bindings)
                result.append({'name': item.name, 'path': relative, 'type': 'directory' if item.is_dir() else 'file', 'size': 0 if item.is_dir() else info.st_size, 'modified': datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(timespec='seconds'), 'ruleId': binding['rule_id'] if binding else None, 'ruleSource': self.binding_row(binding) if binding else None})
            except FileNotFoundError:
                continue
        return {'path': rel, 'items': result, 'total': total, 'nextCursor': cursor + limit if cursor + limit < total else None}

    def catalog(self):
        """Metadata only, bounded recursive enumeration; raw logs are read on selection."""
        result = []
        with self.store.connect() as db:
            bindings = db.execute('SELECT bindings.* FROM bindings JOIN rules ON rules.id=rule_id WHERE rules.deleted_at IS NULL').fetchall()
        for current, dirs, names in os.walk(self.config.data_dir, followlinks=False):
            dirs[:] = sorted(d for d in dirs if not d.startswith('.') and not (Path(current)/d).is_symlink())
            for name in dirs + sorted(names):
                item = Path(current)/name
                if name.startswith('.') or item.is_symlink():
                    continue
                try:
                    info=item.lstat()
                    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                        continue
                    rel=item.relative_to(self.config.data_dir).as_posix()
                    kind='directory' if stat.S_ISDIR(info.st_mode) else 'file'
                    bound=self.choose_binding(rel,bindings)
                    result.append({'path':rel,'name':name,'type':kind,'size':info.st_size if kind=='file' else 0,
                        'modified':datetime.fromtimestamp(info.st_mtime,timezone.utc).isoformat(timespec='seconds'),
                        'fingerprint':str(info.st_mtime_ns)+':'+str(info.st_size),
                        'ruleId':bound['rule_id'] if bound else None})
                except FileNotFoundError:
                    continue
                if len(result)>100000:
                    fail(422,'CATALOG_TOO_LARGE','日志库超过 100,000 项，请拆分数据目录。')
        result.sort(key=lambda x:(x['path'].casefold(),x['path']))
        return {'items':result,'total':len(result),'complete':True}

    def file_content(self, rel, headers):
        rel = self.normal_path(rel)
        stream = self.open_file(rel)
        info = os.fstat(stream.fileno())
        size = info.st_size
        etag = '"' + hashlib.sha256((rel + ':' + str(info.st_mtime_ns) + ':' + str(size)).encode()).hexdigest()[:24] + '"'
        common = {'Content-Type': 'text/plain; charset=utf-8', 'Content-Disposition': "attachment; filename*=UTF-8''" + quote(Path(rel).name, safe=''), 'ETag': etag, 'Accept-Ranges': 'bytes'}
        if headers.get('if-none-match') == etag:
            stream.close()
            return Response(status=304, headers=common)
        start, end, status = 0, size - 1, 200
        requested = headers.get('range')
        if requested:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested)
            try:
                if not match or not any(match.groups()) or not size:
                    raise ValueError()
                left, right = match.groups()
                if not left:
                    count = int(right)
                    if count <= 0:
                        raise ValueError()
                    start = max(0, size - count)
                else:
                    start = int(left)
                    end = min(int(right), size - 1) if right else size - 1
                if start > end or start >= size:
                    raise ValueError()
            except ValueError:
                stream.close()
                return Response(status=416, headers={'Content-Range': f'bytes */{size}'}, value={'error': {'code': 'INVALID_RANGE', 'message': '文件范围无效。', 'details': {}}})
            status = 206
            common['Content-Range'] = f'bytes {start}-{end}/{size}'
        stream.seek(start)
        return Response(status=status, headers=common, stream=stream, length=max(0, end - start + 1))

    def delete_file(self, rel, actor):
        rel = self.normal_path(rel)
        with self.fs_lock, self.store.transaction() as db:
            source = self.safe_path(rel)
            if not (source.is_file() or source.is_dir()):
                fail(400, 'NOT_A_FILE', '只能删除常规文件或目录。')
            kind = 'directory' if source.is_dir() else 'file'
            ident = uuid.uuid4().hex
            target = self.trash_root / ident
            try:
                os.replace(source, target)
            except OSError as exc:
                if exc.errno == 18:
                    fail(409, 'CROSS_DEVICE_TRASH', '文件所在目录跨越了独立挂载点，无法安全移入回收站。')
                raise
            try:
                db.execute('INSERT INTO trash VALUES(?,?,?,?,?,NULL)', (ident, rel, kind, now(), actor))
                self.store.audit(db, actor, 'file.delete', rel, {'trashId': ident})
            except Exception:
                os.replace(target, source)
                raise
        return {'item': {'id': ident, 'path': rel, 'type': kind}}

    def restore_file(self, ident, actor):
        with self.fs_lock, self.store.transaction() as db:
            row = db.execute('SELECT * FROM trash WHERE id=? AND restored_at IS NULL', (ident,)).fetchone()
            if not row:
                fail(404, 'TRASH_NOT_FOUND', '回收站条目不存在。')
            target = self.safe_path(row['path'], exists=False)
            if target.exists():
                fail(409, 'ALREADY_EXISTS', '原路径已有同名文件，恢复不会覆盖它。')
            source = self.trash_root / ident
            if not source.exists() or source.is_symlink():
                fail(409, 'TRASH_MISSING', '回收站文件缺失，无法恢复。')
            os.replace(source, target)
            try:
                db.execute('UPDATE trash SET restored_at=? WHERE id=?', (now(), ident))
                self.store.audit(db, actor, 'file.restore', row['path'])
            except Exception:
                os.replace(target, source)
                raise
        return {'path': row['path']}

    @staticmethod
    def rule_row(row):
        return {'id': row['id'], 'name': row['name'], 'description': row['description'], 'profile': json.loads(row['profile']), 'revision': row['revision'], 'deletedAt': row['deleted_at'], 'updatedAt': row['updated_at'], 'updatedBy': row['updated_by']}

    def validate_rule(self, payload):
        name = text(payload.get('name'), '规则名称', 100)
        description = text(payload.get('description', ''), '规则说明', 1000, empty=True)
        profile = payload.get('profile')
        if isinstance(profile, dict) and 'extractors' in profile:
            from .r5_schema import validate
            try:
                validate(profile)
            except (ValueError, TypeError) as exc:
                fail(400, 'INVALID_RULE', str(exc))
            profile = dict(profile)
            profile['name'] = name
            return name, description, compact(profile)
        if not isinstance(profile, dict) or not isinstance(profile.get('headerPattern'), str) or not profile['headerPattern']:
            fail(400, 'INVALID_RULE', '规则需要 profile.headerPattern。')
        if len(compact(profile).encode()) > 1024 * 1024 or len(profile['headerPattern']) > 4000:
            fail(400, 'INVALID_RULE', '规则包或表达式过长。')
        for key in ('moduleRules', 'stepRules', 'errorRules'):
            if not isinstance(profile.get(key), list) or len(profile[key]) > 100:
                fail(400, 'INVALID_RULE', key + '必须是最多 100 项的数组。')
        # JS regular expressions are trialled in the browser Worker, never executed in Python.
        profile = dict(profile)
        profile['name'] = name
        return name, description, compact(profile)

    def get_rule(self, ident, include_deleted=False):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM rules WHERE id=?', (ident,)).fetchone()
        if not row or (row['deleted_at'] and not include_deleted):
            fail(404, 'RULE_NOT_FOUND', '规则不存在或已被删除。')
        return self.rule_row(row)

    def create_rule(self, payload, actor):
        name, description, profile = self.validate_rule(payload)
        ident = uuid.uuid4().hex
        with self.store.transaction() as db:
            db.execute('INSERT INTO rules VALUES(?,?,?,?,1,NULL,?,?)', (ident, name, description, profile, now(), actor))
            self.store.audit(db, actor, 'rule.create', ident, {'name': name})
        return self.get_rule(ident)

    def update_rule(self, ident, payload, actor):
        name, description, profile = self.validate_rule(payload)
        expected = integer(payload.get('expectedRevision'), '版本号', 1)
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM rules WHERE id=? AND deleted_at IS NULL', (ident,)).fetchone()
            if not row:
                fail(404, 'RULE_NOT_FOUND', '规则不存在或已删除。')
            self.check_revision(expected, row['revision'])
            db.execute('UPDATE rules SET name=?,description=?,profile=?,revision=revision+1,updated_at=?,updated_by=? WHERE id=?', (name, description, profile, now(), actor, ident))
            self.store.audit(db, actor, 'rule.update', ident, {'previousRevision': expected})
        return self.get_rule(ident)

    def change_rule_deleted(self, ident, payload, actor, deleted):
        expected = integer(payload.get('expectedRevision'), '版本号', 1)
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM rules WHERE id=?', (ident,)).fetchone()
            if not row:
                fail(404, 'RULE_NOT_FOUND', '规则不存在。')
            self.check_revision(expected, row['revision'])
            if bool(row['deleted_at']) == deleted:
                fail(409, 'RULE_STATE_CONFLICT', '规则状态已改变，请刷新。')
            bound = db.execute('SELECT count(*) FROM bindings WHERE rule_id=?', (ident,)).fetchone()[0]
            if deleted and bound:
                fail(409, 'RULE_IN_USE', '规则仍被共享目录或文件使用，请先解除绑定。', bindings=bound)
            db.execute('UPDATE rules SET deleted_at=?,revision=revision+1,updated_at=?,updated_by=? WHERE id=?', (now() if deleted else None, now(), actor, ident))
            self.store.audit(db, actor, 'rule.delete' if deleted else 'rule.restore', ident, {'bindingsRetained': bound})
        result = self.get_rule(ident, True)
        result['bindingsRetained'] = bound
        return result

    @staticmethod
    def binding_row(row):
        return {'scope': row['scope'], 'path': row['path'], 'ruleId': row['rule_id'], 'revision': row['revision'], 'updatedAt': row['updated_at']}

    @staticmethod
    def choose_binding(path, bindings):
        candidates = []
        for row in bindings:
            scope, bound = row['scope'], row['path']
            if scope == 'file' and bound == path:
                candidates.append((3, len(bound), row))
            elif scope == 'directory' and (path == bound or path.startswith(bound + '/') or not bound):
                candidates.append((2, len(bound), row))
            elif scope == 'default':
                candidates.append((1, 0, row))
        return max(candidates, key=lambda x: (x[0], x[1]))[2] if candidates else None

    def resolve_binding(self, path):
        with self.store.connect() as db:
            rows = db.execute('SELECT bindings.* FROM bindings JOIN rules ON rules.id=rule_id WHERE rules.deleted_at IS NULL').fetchall()
        chosen = self.choose_binding(path, rows)
        if not chosen:
            return {'rule': None, 'source': None, 'scope': None, 'path': None}
        source = self.binding_row(chosen)
        return {'rule': self.get_rule(chosen['rule_id']), 'source': source, 'scope': chosen['scope'], 'path': chosen['path']}

    def write_binding(self, payload, actor, deleted):
        scope = payload.get('scope')
        if scope not in ('file', 'directory', 'default'):
            fail(400, 'INVALID_SCOPE', '绑定范围必须是 file、directory 或 default。')
        rel = self.normal_path(payload.get('path', ''), allow_root=scope != 'file')
        if scope == 'default' and rel:
            fail(400, 'INVALID_PATH', '默认规则的路径必须为空。')
        expected = integer(payload.get('expectedRevision'), '版本号')
        if not deleted and scope != 'default':
            path = self.safe_path(rel)
            if (scope == 'file' and not path.is_file()) or (scope == 'directory' and not path.is_dir()):
                fail(400, 'INVALID_SCOPE', '绑定类型与路径类型不一致。')
        rid = payload.get('ruleId')
        with self.store.transaction() as db:
            old = db.execute('SELECT * FROM bindings WHERE scope=? AND path=?', (scope, rel)).fetchone()
            self.check_revision(expected, old['revision'] if old else 0)
            if deleted:
                db.execute('DELETE FROM bindings WHERE scope=? AND path=?', (scope, rel))
            else:
                if not db.execute('SELECT 1 FROM rules WHERE id=? AND deleted_at IS NULL', (rid,)).fetchone():
                    fail(404, 'RULE_NOT_FOUND', '规则不存在或已删除。')
                db.execute('INSERT INTO bindings VALUES(?,?,?,?,?) ON CONFLICT(scope,path) DO UPDATE SET rule_id=excluded.rule_id,revision=excluded.revision,updated_at=excluded.updated_at', (scope, rel, rid, expected + 1, now()))
            self.store.audit(db, actor, 'binding.delete' if deleted else 'binding.set', rel, {'scope': scope, 'ruleId': rid})
        return {'removed': True} if deleted else {'item': {'scope': scope, 'path': rel, 'ruleId': rid, 'revision': expected + 1}}

    def admin_route(self, method, path, q, payload, actor):
        ip = actor['ip']
        if path == '/api/v1/admin/config':
            config, revision = self.store.get_setting('identity')
            if method in ('GET', 'HEAD'):
                return {'config': config, 'revision': revision, 'runtime': {'dataDirectory': str(self.config.data_dir), 'trustedProxies': self.config.trusted_proxies, 'allowedHosts': self.config.allowed_hosts, 'publicOrigin': self.config.public_origin}, 'mccheckerReference': MC_REFERENCE}
            if method == 'PUT':
                proposed = self.validate_identity_config(payload.get('config'))
                preview = self.identity_preview(proposed, ip)
                if preview['currentUser']['role'] != 'admin':
                    fail(409, 'ADMIN_LOCKOUT', '切换后当前 IP 不是管理员。请先在目标人员配置中登记当前 IP。')
                expected = integer(payload.get('expectedRevision'), '版本号', 1)
                with self.store.transaction() as db:
                    row = db.execute("SELECT revision FROM settings WHERE key='identity'").fetchone()
                    self.check_revision(expected, row['revision'])
                    db.execute("UPDATE settings SET value=?,revision=revision+1 WHERE key='identity'", (compact(proposed),))
                    self.store.audit(db, ip, 'identity.config', proposed['identityMode'])
                return {'config': proposed, 'revision': expected + 1}
        if path == '/api/v1/admin/identity-preview' and method == 'POST':
            return self.identity_preview(self.validate_identity_config(payload.get('config', payload)), ip)
        if path == '/api/v1/admin/users':
            mode = self.store.get_setting('identity')[0]['identityMode']
            if method in ('GET', 'HEAD'):
                if mode == 'mcchecker':
                    mapping, admins = self.read_mcchecker(self.store.get_setting('identity')[0])
                    ips = sorted({str(row.get('ip') or '').strip() for row in mapping + admins if str(row.get('ip') or '').strip()})
                    return {'items': [{**self.mc_actor(value, mapping, admins), 'enabled': True, 'revision': None, 'readOnly': True} for value in ips], 'readOnly': True}
                with self.store.connect() as db:
                    rows = db.execute('SELECT * FROM users ORDER BY ip').fetchall()
                return {'items': [{'ip': r['ip'], 'name': r['name'], 'role': r['role'], 'enabled': bool(r['enabled']), 'revision': r['revision'], 'updatedAt': r['updated_at'], 'readOnly': False} for r in rows], 'readOnly': False}
            if mode != 'local':
                fail(409, 'EXTERNAL_IDENTITY_READ_ONLY', '当前人员信息由外部部署管理，请在原服务修改。')
            target = clean_ip(payload.get('ip') if method == 'PUT' else q('ip'))
            expected = integer(payload.get('expectedRevision'), '版本号')
            with self.store.transaction() as db:
                old = db.execute('SELECT * FROM users WHERE ip=?', (target,)).fetchone()
                self.check_revision(expected, old['revision'] if old else 0)
                if method == 'DELETE':
                    if target == ip:
                        fail(409, 'ADMIN_LOCKOUT', '不能删除当前管理员本人。')
                    db.execute('DELETE FROM users WHERE ip=?', (target,))
                    self.store.audit(db, ip, 'user.delete', target)
                    return {'removed': bool(old)}
                if method == 'PUT':
                    name = text(payload.get('name'), '姓名', 100)
                    role = payload.get('role')
                    enabled = payload.get('enabled', True)
                    if role not in ('admin', 'member', 'viewer') or not isinstance(enabled, bool):
                        fail(400, 'INVALID_USER', '角色或启用状态不正确。')
                    if target == ip and (role != 'admin' or not enabled):
                        fail(409, 'ADMIN_LOCKOUT', '不能停用或降级当前管理员本人。')
                    db.execute('INSERT INTO users VALUES(?,?,?,?,?,?) ON CONFLICT(ip) DO UPDATE SET name=excluded.name,role=excluded.role,enabled=excluded.enabled,revision=excluded.revision,updated_at=excluded.updated_at', (target, name, role, int(enabled), expected + 1, now()))
                    self.store.audit(db, ip, 'user.set', target, {'role': role, 'enabled': enabled})
                    return {'item': {'ip': target, 'name': name, 'role': role, 'enabled': enabled, 'revision': expected + 1}}
        if path == '/api/v1/admin/audit' and method in ('GET', 'HEAD'):
            cursor = integer(q('cursor', '0'), '页码')
            limit = integer(q('limit', '100'), '数量', 1, 200)
            with self.store.connect() as db:
                total = db.execute('SELECT count(*) FROM audit').fetchone()[0]
                rows = db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT ? OFFSET ?', (limit, cursor)).fetchall()
            return {'items': [{**dict(r), 'detail': json.loads(r['detail'])} for r in rows], 'total': total, 'nextCursor': cursor + limit if cursor + limit < total else None}
        if path == '/api/v1/admin/stats' and method in ('GET', 'HEAD'):
            with self.store.connect() as db:
                rules = db.execute('SELECT count(*) FROM rules WHERE deleted_at IS NULL').fetchone()[0]
                deleted_rules = db.execute('SELECT count(*) FROM rules WHERE deleted_at IS NOT NULL').fetchone()[0]
                users = db.execute('SELECT count(*) FROM users').fetchone()[0]
                trash = db.execute('SELECT count(*) FROM trash WHERE restored_at IS NULL').fetchone()[0]
            return {'version': VERSION, 'rules': rules, 'deletedRules': deleted_rules, 'localUsers': users, 'trash': trash, 'databaseBytes': self.store.path.stat().st_size, 'diskFreeBytes': shutil.disk_usage(self.config.data_dir).free, 'localUploadsOnServer': False}
        fail(404, 'NOT_FOUND', '管理接口不存在。')

    @staticmethod
    def validate_identity_config(value):
        if not isinstance(value, dict) or value.get('identityMode') not in ('local', 'mcchecker'):
            fail(400, 'INVALID_CONFIG', '请选择 local 或 mcchecker 身份来源。')
        return {'identityMode': value['identityMode'], 'mccheckerDirectory': text(value.get('mccheckerDirectory', ''), '部署目录', 2048, empty=True), 'mccheckerDataDirectory': text(value.get('mccheckerDataDirectory', ''), '数据目录', 2048, empty=True)}

    def identity_preview(self, config, ip):
        if config['identityMode'] == 'mcchecker':
            mapping, admins = self.read_mcchecker(config)
            actor = self.mc_actor(ip, mapping, admins)
            return {'valid': True, 'mappingCount': len(mapping), 'adminEntries': len(admins), 'currentUser': actor, 'readOnly': True, 'reference': MC_REFERENCE}
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM users WHERE ip=? AND enabled=1', (ip,)).fetchone()
        return {'valid': True, 'currentUser': {'ip': ip, 'name': row['name'] if row else ip, 'role': row['role'] if row else 'viewer', 'source': 'local'}, 'readOnly': False}

    def static(self, path):
        # Explicit allowlist: never expose data/, var/, Python, configuration or backups.
        import io
        code = self.config.code_root
        if path == '/healthz':
            return Response(value={'status':'ok','version':VERSION,'release':os.environ.get('LOGBENCH_RELEASE','direct'),'pid':os.getpid()})
        if path == '/favicon.ico':
            return Response(status=204)
        if path in ('/', '/index.html', '/LogBench.html'):
            target=code/'src'/'index.html';mime='text/html; charset=utf-8'
        elif path == '/assets/workbench.js':
            from .frontend import bundle
            content=bundle(code)
            return Response(headers={'Content-Type':'text/javascript; charset=utf-8'},stream=io.BytesIO(content),length=len(content))
        elif path in ('/assets/boot.js','/assets/style.css','/assets/service.css'):
            target=code/'src'/path.rsplit('/',1)[1]
            mime='text/javascript; charset=utf-8' if path.endswith('.js') else 'text/css; charset=utf-8'
        else:
            fail(404,'NOT_FOUND','资源不存在。')
        if not target.is_file():
            fail(503,'ASSET_MISSING','页面文件缺失，请完整部署 src 目录。')
        stream=target.open('rb')
        return Response(headers={'Content-Type':mime},stream=stream,length=os.fstat(stream.fileno()).st_size)
