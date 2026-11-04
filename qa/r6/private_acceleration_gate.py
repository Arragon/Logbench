#!/usr/bin/env python3
"""Gate M4: private upload → index → query → export, through the real HTTP server (R6-051).

This gate runs the actual `server.httpd.Server` on a loopback port and speaks to it
over sockets: multipart-free binary part uploads, CSRF headers, the query API, the
export stream and the browser UI. A second principal is exercised through
`Application.handle` with a different peer address, because the transport derives
identity from the socket peer and one process cannot bind two source addresses
portably. That limitation is recorded in the evidence rather than hidden.

    python qa/r6/private_acceleration_gate.py
    python qa/r6/private_acceleration_gate.py --skip-ui     # headless server only

Exit code 0 only when every case passes; `BLOCKED` exits 2 and says why.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.app import Application, Config                     # noqa: E402
from server.httpd import Server                                # noqa: E402

EVIDENCE = ROOT / 'qa' / 'evidence'
ADMIN = '127.0.0.1'
FOREIGN = '10.9.9.9'
PART = 4 * 1024 * 1024


class Client:
    def __init__(self, base: str, peer_hint: str = ADMIN):
        self.base = base.rstrip('/')
        self.peer = peer_hint
        self.csrf = ''
        self.calls = 0
        self.bytes = 0
        # Loopback must not go through the system proxy: with Clash on 7890 an
        # http_proxy-aware client turns a local gate into a network timeout.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def raw(self, method: str, path: str, body: bytes = b'', headers=None):
        url = self.base + path
        request = urllib.request.Request(url, data=body if body else None, method=method)
        request.add_header('Host', self.base.split('//', 1)[1])
        cookie = getattr(self, 'cookie', '')
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        self.calls += 1
        try:
            with self.opener.open(request, timeout=30) as response:
                payload = response.read()
                self.bytes += len(payload)
                return response.status, dict(response.headers), payload
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            self.bytes += len(payload)
            return exc.code, dict(exc.headers or {}), payload

    def json(self, method: str, path: str, payload=None, extra=None):
        headers = {'Content-Type': 'application/json'}
        if method != 'GET':
            headers['X-LogBench-CSRF'] = self.csrf
        headers.update(extra or {})
        body = json.dumps(payload).encode() if payload is not None else b''
        status, response_headers, raw = self.raw(method, path, body, headers)
        value = json.loads(raw.decode('utf-8')) if raw else {}
        if status >= 400:
            error = value.get('error') or {}
            raise GateError(error.get('code') or 'http_%d' % status,
                            error.get('message') or 'request failed', status=status)
        return value

    def login(self):
        value = self.json('GET', '/api/v1/me')
        self.csrf = value.get('csrfToken') or ''
        return value


class GateError(RuntimeError):
    def __init__(self, code, message, **details):
        super().__init__('%s: %s' % (code, message))
        self.code = code
        self.details = details


def generate_log(path: Path, size: int) -> dict:
    line = (b'[2024-09-01 12:00:00.000] [INFO] [worker] [1] [2] [job.py:10] [run] [alice] '
            b'f() step=%d payload=private-upload-marker-%08d\n')
    digest = hashlib.sha256()
    written = 0
    lines = 0
    with open(path, 'wb') as handle:
        chunk = bytearray()
        while written < size:
            lines += 1
            chunk += line % (lines, lines % 100000000)
            if lines % 5000 == 0:
                chunk += b'[2024-09-01 12:00:00.500] [ERROR] [db] [1] [2] [pool.py:7] [get] [bob] f() Traceback (most recent call last):\n'
                chunk += b'  File "/srv/app/db.py", line 42, in get\n'
                chunk += b'RuntimeError: private-upload-failure %d\n' % lines
            if len(chunk) >= 1 << 20:
                data = bytes(chunk)
                handle.write(data)
                digest.update(data)
                written += len(data)
                chunk = bytearray()
        if chunk:
            data = bytes(chunk)
            handle.write(data)
            digest.update(data)
            written += len(data)
    return {'bytes': written, 'lines': lines, 'sha256': digest.hexdigest()}


def boot(root: Path):
    (root / 'rules').mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / 'rules' / 'cpqpsgx-default.json', root / 'rules' / 'cpqpsgx-default.json')
    # Background executors stay enabled: the gate must exercise the production
    # path (upload returns, indexing and querying run off the request thread).
    # `code_root` points at the checkout while data/state live in the temp root:
    # without it /r6 renders as ASSET_MISSING/503 and the UI case has no page.
    app = Application(Config(root, code_root=ROOT, bootstrap_admins=[ADMIN]))
    server = Server(('127.0.0.1', 0), app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = 'http://127.0.0.1:%d' % server.server_address[1]
    return app, server, thread, base


# --------------------------------------------------------------------------- cases
def case_upload(client: Client, source: Path, info: dict) -> dict:
    started = time.perf_counter()
    session = client.json('POST', '/api/v2/uploads',
                          {'name': 'private.log', 'size': info['bytes'], 'chunkSize': PART})['item']
    blob = source.read_bytes()
    parts = []
    for number in range(1, session['parts_expected'] + 1):
        start = (number - 1) * PART
        chunk = blob[start:start + PART]
        status, _, raw = client.raw(
            'PUT', '/api/v2/uploads/%s/parts/%d' % (session['session_id'], number), chunk,
            {'Content-Type': 'application/octet-stream',
             'X-LogBench-CSRF': client.csrf,
             'X-LogBench-Digest': hashlib.sha256(chunk).hexdigest()})
        if status != 200:
            raise GateError('part_failed', '分片 %d 上传失败：%s' % (number, raw[:200].decode('utf-8', 'replace')))
        parts.append(number)
    status = client.json('GET', '/api/v2/uploads/' + session['session_id'])['item']
    sealed = client.json('POST', '/api/v2/uploads/%s/commit' % session['session_id'], {})['item']
    upload_seconds = round(time.perf_counter() - started, 2)

    job_id = sealed['job_id']
    deadline = time.time() + 180
    states = []
    while time.time() < deadline:
        job = client.json('GET', '/api/v2/index-jobs/' + job_id)['item']
        states.append(job['state'])
        if job['state'] in ('indexed', 'failed', 'cancelled'):
            break
        time.sleep(0.2)
    index_seconds = round(time.perf_counter() - started - upload_seconds, 2)
    if job['state'] != 'indexed':
        raise GateError('index_failed', '索引未完成：%s' % job['state'])
    return {
        'ok': True,
        'session': {'parts': len(parts), 'bytes_done': status['bytes_done'],
                    'parts_expected': status['parts_expected']},
        'seal': {'state': sealed['state'], 'bytes': sealed['bytes'],
                 'content_sig': sealed['content_sig'][:16]},
        'file_id': sealed['file_id'],
        'job': {'id': job_id, 'states': states[-4:], 'seconds': index_seconds},
        'upload_seconds': upload_seconds,
        'mb_per_s': round(info['bytes'] / max(upload_seconds, 1e-6) / 1048576, 2),
        'input': info,
    }


def case_resume(client: Client, source: Path) -> dict:
    blob = source.read_bytes()
    session = client.json('POST', '/api/v2/uploads',
                          {'name': 'resume.log', 'size': len(blob), 'chunkSize': PART})['item']
    sid = session['session_id']
    first = blob[:PART]
    part_headers = {'Content-Type': 'application/octet-stream', 'X-LogBench-CSRF': client.csrf,
                    'X-LogBench-Digest': hashlib.sha256(first).hexdigest()}
    client.raw('PUT', '/api/v2/uploads/%s/parts/1' % sid, first, part_headers)
    # Same bytes again: accepted, flagged idempotent, and it must not be counted twice.
    repeat_status, _, repeat_raw = client.raw('PUT', '/api/v2/uploads/%s/parts/1' % sid,
                                              first, part_headers)
    repeat = (json.loads(repeat_raw) or {}).get('item') or {}
    partial = client.json('GET', '/api/v2/uploads/' + sid)['item']
    # A digest that does not match the body is refused before anything is written.
    bad_status, _, bad_raw = client.raw(
        'PUT', '/api/v2/uploads/%s/parts/1' % sid, first,
        dict(part_headers, **{'X-LogBench-Digest': 'f' * 64}))
    bad = (json.loads(bad_raw) or {}).get('error') or {}
    missing = partial['resume']
    for number in missing:
        start = (number - 1) * PART
        chunk = blob[start:start + PART]
        client.raw('PUT', '/api/v2/uploads/%s/parts/%d' % (sid, number), chunk,
                   {'Content-Type': 'application/octet-stream', 'X-LogBench-CSRF': client.csrf,
                    'X-LogBench-Digest': hashlib.sha256(chunk).hexdigest()})
    after = client.json('GET', '/api/v2/uploads/' + sid)['item']
    sealed = client.json('POST', '/api/v2/uploads/%s/commit' % sid, {})['item']
    # Retried seal: same dataset, same job, no second index run.
    again_raw = client.json('POST', '/api/v2/uploads/%s/commit' % sid, {})
    again = again_raw['item']
    return {
        'ok': (not partial['indexed']
               and partial['bytes_done'] == len(first)
               and repeat_status == 200 and repeat.get('idempotent') is True
               and repeat.get('size') == len(first)
               and bad_status == 400 and bad.get('code') == 'digest_mismatch'
               and after['bytes_done'] == len(blob) and after['resume'] == []
               and sealed['bytes'] == len(blob)
               and again['file_id'] == sealed['file_id']
               and again['job_id'] == sealed['job_id']
               and again_raw.get('accepted') is False),
        'idempotent_part': {'status': repeat_status, 'idempotent': repeat.get('idempotent'),
                            'bytes_done': partial['bytes_done']},
        'bad_digest': {'status': bad_status, 'code': bad.get('code')},
        'missing_after_first_part': missing,
        'bytes_done_after_resume': after['bytes_done'],
        'expected_bytes': len(blob),
        'seal': {'file_id': sealed['file_id'], 'job_id': sealed['job_id']},
        'seal_retry': {'file_id': again['file_id'], 'job_id': again['job_id'],
                       'accepted': again_raw.get('accepted'), 'index_state': again.get('index_state')},
    }


def _run_query(client: Client, file_id: str, text: str, limit: int = 50):
    created = client.json('POST', '/api/v2/queries',
                          {'file_id': file_id, 'text': text, 'flags': {},
                           'budget_ms': 5000})['item']
    query_id = created['query_id']
    deadline = time.time() + 120
    state = created
    while time.time() < deadline:
        state = client.json('GET', '/api/v2/queries/' + query_id)['item']
        if state['state'] in ('done', 'failed', 'cancelled', 'paused'):
            break
        time.sleep(0.1)
    if state['state'] != 'done':
        raise GateError('query_failed', '查询未完成：%s' % state['state'])
    page = client.json('GET', '/api/v2/queries/%s/records?limit=%d' % (query_id, limit))
    return query_id, state, page


def case_query(client: Client, file_id: str) -> dict:
    started = time.perf_counter()
    # Broad term: hits almost every line, so it proves the whole file is covered
    # (scanned bytes == total bytes) rather than a lucky subset.
    query_id, state, page = _run_query(client, file_id, 'private-upload-marker')
    cursor = page.get('next_cursor')
    paged = len(page['items'])
    if cursor:
        second = client.json('GET', '/api/v2/queries/%s/records?limit=50&cursor=%s'
                             % (query_id, urllib.parse.quote(cursor)))
        paged += len(second['items'])
    status, headers, raw = client.raw('GET', '/api/v2/queries/%s/export?format=ndjson&max=5'
                                      % query_id, b'', {'X-LogBench-CSRF': client.csrf})
    lines = [line for line in raw.decode('utf-8').splitlines() if line.strip()]
    # Narrow term: only the injected failure blocks carry it, so a planner that
    # returns the superset (or ignores the predicate) cannot pass this one.
    narrow_id, narrow, narrow_page = _run_query(client, file_id, 'private-upload-failure')
    elapsed = round(time.perf_counter() - started, 2)
    broad_hits = state['count']['matched']
    narrow_hits = narrow['count']['matched']
    # The page items are references, not records: fetch one hit and check the text
    # really is the narrow term, so a superset planner cannot pass on counts alone.
    sample = None
    if narrow_page['items']:
        first = narrow_page['items'][0]
        detail = client.json('GET', '/api/v2/records/%s:%d'
                             % (file_id, first['ordinal']))
        record = detail.get('record') or {}
        body = detail.get('body') or {}
        text = '\n'.join(body.get('items') or [])
        # The hit is the traceback header record; the term lives in a later line of
        # the same record, so the predicate has to be checked against the full text.
        sample = {'ordinal': first['ordinal'], 'level': record.get('level'),
                  'message': record.get('message'), 'lines': record.get('lines'),
                  'text_has_term': 'private-upload-failure' in text,
                  'text_bytes': len(text)}
    narrow_ok = (narrow['count']['relation'] == 'exact' and 0 < narrow_hits < broad_hits / 10
                 and sample is not None and sample['text_has_term'])
    return {
        'ok': (state['count']['relation'] == 'exact' and paged > 0 and len(lines) == 5
               and narrow_ok),
        'query_id': query_id,
        'state': state['state'],
        'count': state['count'],
        'coverage': state['coverage'],
        'plan': (state.get('plan') or {}).get('mode'),
        'paged': paged,
        'export': {'status': status, 'lines': len(lines),
                   'rows_header': headers.get('X-LogBench-Export-Rows'),
                   'truncated': headers.get('X-LogBench-Export-Truncated')},
        'narrow': {'query_id': narrow_id, 'text': 'private-upload-failure',
                   'count': narrow['count'], 'plan': (narrow.get('plan') or {}).get('mode'),
                   'hit_ordinals': [item['ordinal'] for item in narrow_page['items'][:5]],
                   'sample': sample, 'ok': narrow_ok},
        'seconds': elapsed,
    }


def case_isolation(app, client: Client, file_id: str, query_id: str) -> dict:
    checks = []
    for method, target in (('GET', '/api/v2/files/' + file_id),
                           ('GET', '/api/v2/files/%s/records' % file_id),
                           ('GET', '/api/v2/records/%s:0' % file_id),
                           ('GET', '/api/v2/queries/' + query_id),
                           ('GET', '/api/v2/queries/%s/export' % query_id)):
        headers = {'Host': 'localhost:50004', 'X-LogBench-CSRF': app.csrf(FOREIGN)}
        response = app.handle(method, target, headers, b'', FOREIGN)
        checks.append({'target': target, 'status': response.status,
                       'code': (response.value or {}).get('error', {}).get('code')})
    return {'ok': all(item['status'] == 404 for item in checks), 'checks': checks,
            'peer': FOREIGN,
            'note': '身份由 socket 对端推导；同一进程无法portably 绑定两个源地址，故用 handle() 传入另一个对端'}


def case_ui(base: str, source: Path) -> dict:
    from playwright.sync_api import sync_playwright
    report = {'ok': False}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page()
        errors = []
        page.on('pageerror', lambda exc: errors.append(str(exc)))
        page.goto(base + '/r6', wait_until='load')
        # The predicate must be a function expression: the page is served with
        # `script-src 'self'`, and a bare expression is compiled with eval() inside
        # the page and refused by CSP. Same form as qa/r6/local_vertical.py.
        page.wait_for_function('() => window.__r6Ready === true', timeout=30000)
        report['panel_hidden_initially'] = page.is_hidden('#server-panel')
        page.click('#server-trigger')
        report['panel_visible_after_click'] = page.is_visible('#server-panel')
        report['opt_in_unchecked'] = not page.is_checked('#server-optin')
        report['file_input_disabled_before_opt_in'] = page.is_disabled('#server-file')
        page.check('#server-optin')
        report['file_input_enabled_after_opt_in'] = page.is_enabled('#server-file')
        page.set_input_files('#server-file', str(source))
        page.click('#server-start')
        page.wait_for_selector('#server-status:has-text("已就绪")', timeout=180000)
        report['status_text'] = page.inner_text('#server-status')
        page.click('#server-open')
        page.wait_for_selector('.r6-row', timeout=30000)
        report['rows_rendered'] = page.eval_on_selector_all('.r6-row', 'nodes => nodes.length')
        report['view_label'] = page.inner_text('#file-label')
        report['page_errors'] = errors
        report['ok'] = (report['panel_hidden_initially'] and report['opt_in_unchecked']
                        and report['file_input_disabled_before_opt_in']
                        and report['file_input_enabled_after_opt_in']
                        and report['rows_rendered'] > 0 and not errors)
        browser.close()
    return report


# ------------------------------------------------------------------------- report
ORDER = ['upload', 'query', 'isolation', 'resume', 'ui']


def key_numbers(cases: dict) -> list:
    """The one table a reviewer reads first: what was measured, not what ran."""
    rows = []
    upload = cases.get('upload') or {}
    info = upload.get('input') or {}
    if info:
        rows.append(('输入', '%d B / %d 行，sha256 `%s…`'
                     % (info['bytes'], info['lines'], info['sha256'][:16])))
    session = upload.get('session') or {}
    if session:
        rows.append(('分片上传', '%d 片 / %d B，%.2f s（%.1f MB/s）'
                     % (session['parts'], session['bytes_done'], upload.get('upload_seconds', 0),
                        upload.get('mb_per_s', 0))))
    seal = upload.get('seal') or {}
    if seal:
        rows.append(('封存', 'state=%s，%d B，content_sig `%s…`'
                     % (seal['state'], seal['bytes'], seal['content_sig'])))
    job = upload.get('job') or {}
    if job:
        rows.append(('服务端索引', '%.2f s（%s）'
                     % (job['seconds'], ' → '.join(job.get('states') or []))))
    source = cases.get('query') or {}
    count = source.get('count') or {}
    if count:
        rows.append(('宽查询命中', '%s 条（relation=%s，plan=%s，%.2f s）'
                     % (count['matched'], count['relation'], source.get('plan'),
                        source.get('seconds', 0))))
    coverage = source.get('coverage') or {}
    if coverage:
        rows.append(('扫描覆盖', '%s / %s B，complete=%s，mode=%s'
                     % (coverage.get('scanned_bytes'), coverage.get('total_bytes'),
                        coverage.get('complete'), coverage.get('mode'))))
    narrow = source.get('narrow') or {}
    if narrow.get('count'):
        rows.append(('窄查询命中', '%d 条（ordinal %s）'
                     % (narrow['count']['matched'], narrow.get('hit_ordinals'))))
    export = source.get('export') or {}
    if export:
        rows.append(('导出', 'HTTP %s，%s 行，X-LogBench-Export-Rows=%s，truncated=%s'
                     % (export.get('status'), export.get('lines'), export.get('rows_header'),
                        export.get('truncated'))))
    isolation = cases.get('isolation') or {}
    if isolation.get('checks'):
        denied = sum(1 for item in isolation['checks'] if item['status'] == 404)
        rows.append(('跨身份访问', '%d/%d 条路由返回 404（对端 %s）'
                     % (denied, len(isolation['checks']), isolation.get('peer'))))
    resume = cases.get('resume') or {}
    if 'missing_after_first_part' in resume:
        rows.append(('断点续传', '首片后缺 %s，续传后 %s B（期望 %s）'
                     % (resume['missing_after_first_part'], resume['bytes_done_after_resume'],
                        resume['expected_bytes'])))
    idempotent = resume.get('idempotent_part') or {}
    if idempotent:
        rows.append(('分片幂等', '重传同片 HTTP %s，idempotent=%s，bytes_done 仍为 %s'
                     % (idempotent.get('status'), idempotent.get('idempotent'),
                        idempotent.get('bytes_done'))))
    bad_digest = resume.get('bad_digest') or {}
    if bad_digest:
        rows.append(('错摘要分片', 'HTTP %s，code=%s'
                     % (bad_digest.get('status'), bad_digest.get('code'))))
    retry = resume.get('seal_retry') or {}
    if retry:
        rows.append(('封存重试', 'file_id 相同=%s，job_id 相同=%s，accepted=%s'
                     % (retry.get('file_id') == (resume.get('seal') or {}).get('file_id'),
                        retry.get('job_id') == (resume.get('seal') or {}).get('job_id'),
                        retry.get('accepted'))))
    ui = cases.get('ui') or {}
    if 'rows_rendered' in ui:
        rows.append(('UI', '%d 行渲染，`%s`，页面错误 %d'
                     % (ui['rows_rendered'], ui.get('view_label'), len(ui.get('page_errors') or []))))
    return rows


def write_report(out: Path, cases: dict, meta: dict) -> str:
    blocked = None
    if (cases.get('ui') or {}).get('blocked'):
        blocked = cases['ui']['blocked']
    ordered = {name: cases[name] for name in ORDER if name in cases}
    ordered.update({name: value for name, value in cases.items() if name not in ordered})
    cases = ordered
    failed = [name for name, case in cases.items() if not case.get('ok')]
    verdict = 'PASS' if not failed and not blocked else ('BLOCKED' if blocked else 'FAIL')

    lines = [
        '# M4 Gate · 私有上传到索引查询的闭环（R6-051）',
        '',
        '- 结论：**%s**' % verdict,
        '- 时间：%s（耗时 %.1f s）' % (meta.get('started'), meta.get('total', 0)),
        '- 用例：%d 通过 / %d 失败%s' % (len(cases) - len(failed), len(failed),
                                        ('，阻塞：' + blocked) if blocked else ''),
        '- 输入：%s MiB 生成日志（确定性模板）' % meta.get('size_mb'),
        '',
        '## 环境',
        '',
        '| 项 | 值 |',
        '| --- | --- |',
        '| python | %s |' % meta.get('python'),
        '| platform | %s |' % meta.get('platform'),
        '| base | %s |' % meta.get('base'),
        '| 传输 | server.httpd.Server（真实 socket） |',
        '| 客户端 | urllib + Playwright(system Chrome) |',
        '| 请求数 | %s，响应字节 %s |' % (meta.get('calls'), meta.get('bytes')),
        '',
        '## 命令',
        '',
        '```',
        'env -u PYTHONPATH ~/.local/bin/python3.9 qa/r6/private_acceleration_gate.py',
        'env -u PYTHONPATH ~/.local/bin/python3.9 qa/r6/private_acceleration_gate.py --report-only',
        '```',
        '',
        '## 关键数字',
        '',
    ]
    numbers = key_numbers(cases)
    if numbers:
        lines += ['| 项 | 值 |', '| --- | --- |']
        lines += ['| %s | %s |' % (key, value) for key, value in numbers]
    else:
        lines.append('（本次没有可汇总的数值用例）')
    lines += ['', '## 用例', '']
    for name, case in cases.items():
        lines.append('### %s · %s（%.1f s）' % (name, 'PASS' if case.get('ok') else 'FAIL',
                                              case.get('seconds', 0)))
        lines.append('')
        lines.append('```json')
        lines.append(json.dumps(case, ensure_ascii=False, indent=2)[:3000])
        lines.append('```')
        lines.append('')
    lines += [
        '## 证据',
        '',
        '每个用例的原始 JSON 在 `qa/evidence/M4-gate/`；本文件由脚本生成。',
        '',
        '## 未覆盖 / 已知限制',
        '',
        '- 第二个 principal 通过 `Application.handle(peer=...)` 传入，而不是真的从另一个源地址连接：'
        '同一进程无法可移植地绑定第二个回环地址。HTTP 层其余用例都是真实 socket。',
        '- 上传分片为 4 MiB，未在本机跑到 1 GiB；1 GiB 的索引吞吐见 M3 门禁。',
        '- 导出走 `SpooledTemporaryFile` 落到临时磁盘再流式发送，未做真正的边查边发。',
        '- 页面 CSP 为 `script-src \'self\'`：`wait_for_function` 的谓词必须是函数表达式，'
        '裸表达式会被页面内的 `eval` 拒绝。',
    ]
    Path(out).write_text('\n'.join(lines) + '\n', 'utf-8')
    return verdict


def report_only(case_dir: Path, out: Path) -> int:
    meta_path = case_dir / 'meta.json'
    if not meta_path.is_file():
        print('缺少 %s，先跑一次完整门禁' % meta_path)
        return 1
    meta = json.loads(meta_path.read_text('utf-8'))
    cases = {}
    for name in ORDER:
        path = case_dir / ('%s.json' % name)
        if path.is_file():
            cases[name] = json.loads(path.read_text('utf-8'))
    verdict = write_report(out, cases, meta)
    print('verdict: %s (%s)' % (verdict, out))
    return 0 if verdict == 'PASS' else (2 if verdict == 'BLOCKED' else 1)


# -------------------------------------------------------------------------- driver
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Gate M4 (R6-051)')
    parser.add_argument('--size-mb', type=int, default=6)
    parser.add_argument('--skip-ui', action='store_true')
    parser.add_argument('--out', default=str(EVIDENCE / 'M4-gate.md'))
    parser.add_argument('--report-only', action='store_true',
                        help='用 qa/evidence/M4-gate 里已有的 JSON 重写报告，不重新跑用例')
    args = parser.parse_args(argv)

    case_dir = EVIDENCE / 'M4-gate'
    case_dir.mkdir(parents=True, exist_ok=True)
    if args.report_only:
        return report_only(case_dir, Path(args.out))
    cases = {}
    blocked = None
    temp = Path(tempfile.mkdtemp(prefix='r6-gate-m4-'))
    app, server, thread, base = boot(temp)
    client = Client(base)
    started = time.time()

    def run(name, fn, *extra):
        began = time.perf_counter()
        try:
            cases[name] = fn(*extra)
        except GateError as exc:
            cases[name] = {'ok': False, 'error': '%s: %s' % (exc.code, exc), 'details': exc.details}
        except Exception as exc:  # noqa: BLE001 - a crashed case is a failed case
            cases[name] = {'ok': False, 'error': '%s: %s' % (type(exc).__name__, exc)}
        cases[name]['seconds'] = round(time.perf_counter() - began, 2)
        (case_dir / ('%s.json' % name)).write_text(
            json.dumps(cases[name], ensure_ascii=False, indent=2), 'utf-8')
        print('[%s] %s (%.1fs)' % ('PASS' if cases[name].get('ok') else 'FAIL', name,
                                   cases[name]['seconds']))

    try:
        source = temp / 'private.log'
        info = generate_log(source, args.size_mb * 1024 * 1024)
        info['file'] = 'generated (deterministic templates)'
        client.login()
        run('upload', case_upload, client, source, info)
        file_id = (cases.get('upload') or {}).get('file_id')
        if file_id:
            run('query', case_query, client, file_id)
            query_id = (cases.get('query') or {}).get('query_id') or ''
            run('isolation', case_isolation, app, client, file_id, query_id)
        run('resume', case_resume, client, source)
        if args.skip_ui:
            blocked = 'UI 用例被 --skip-ui 跳过'
            cases['ui'] = {'ok': False, 'blocked': blocked}
        else:
            run('ui', case_ui, base, source)
    finally:
        server.shutdown()
        server.server_close()
        app.index_jobs.close()
        app.queries.close()
        shutil.rmtree(temp, ignore_errors=True)

    meta = {
        'python': sys.version.split()[0],
        'platform': __import__('platform').platform(),
        'base': base,
        'calls': client.calls,
        'bytes': client.bytes,
        'size_mb': args.size_mb,
        'started': time.strftime('%Y-%m-%d %H:%M:%S'),
        'total': round(time.time() - started, 1),
        'skip_ui': args.skip_ui,
    }
    (case_dir / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), 'utf-8')
    verdict = write_report(Path(args.out), cases, meta)
    print('verdict: %s (%s)' % (verdict, args.out))
    return 0 if verdict == 'PASS' else (2 if verdict == 'BLOCKED' else 1)


if __name__ == '__main__':
    raise SystemExit(main())
