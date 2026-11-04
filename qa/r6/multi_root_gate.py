#!/usr/bin/env python3
"""R6-058 · Gate M5: multiple physical roots, directory inheritance, offline recovery.

Every checklist line from the Linear ticket is measured here against a real
deployment directory plus two roots outside it, on a real filesystem, through the
real HTTP handler. Results are `PASS`, `FAIL` or `BLOCKED`; nothing is inferred
from the source.

    python3 qa/r6/multi_root_gate.py --out qa/evidence

What this script does NOT prove, and says so in the report:

  * a real unmounted device or a second physical disk. The offline check removes
    the sentinel and injects a different volume id, which is what the guard sees
    when a mount is replaced — but the kernel behaviour of a real unplug is only
    verifiable on the target machine (`mount_fault` is therefore BLOCKED).
  * the native-browser layout. That is `qa/r6/library_storage_ui.py`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

LINE = '2026-01-01 10:00:00.000 [INFO] gate line\n'
MANY = 320
PAGE = 50


def make_app(deploy: Path):
    from server.app import Application, Config
    (deploy / 'rules').mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / 'rules' / 'cpqpsgx-default.json', deploy / 'rules' / 'cpqpsgx-default.json')
    return Application(Config(deploy, bootstrap_admins=['127.0.0.1']))


class Gate:
    def __init__(self):
        self.items = []

    def check(self, item, result, detail, evidence=None):
        self.items.append({'item': item, 'result': result, 'detail': detail,
                           'evidence': evidence or []})

    def passed(self, item, detail, evidence=None):
        self.check(item, 'PASS', detail, evidence)

    def failed(self, item, detail, evidence=None):
        self.check(item, 'FAIL', detail, evidence)

    def blocked(self, item, detail, evidence=None):
        self.check(item, 'BLOCKED', detail, evidence)


def request(app, method, target, payload=None, ip='127.0.0.1', headers=None):
    base = {'Host': 'localhost:50004', 'Content-Type': 'application/json',
            'Origin': 'http://localhost:50004', 'X-LogBench-CSRF': app.csrf(ip)}
    base.update(headers or {})
    body = json.dumps(payload).encode() if payload is not None else b''
    return app.handle(method, target, base, body, ip)


def run(out: Path) -> int:
    temp = Path(tempfile.mkdtemp(prefix='lb-m5-gate-'))
    gate = Gate()
    report = {'started_at': time.strftime('%Y-%m-%dT%H:%M:%S'), 'items': gate.items,
              'environment': {'python': sys.version.split()[0], 'platform': sys.platform,
                              'filesystem_root': str(temp)}}
    app = make_app(temp / 'deploy')
    data = app.config.data_dir
    actor = app.actor('127.0.0.1')
    try:
        # ---------------------------------------------------------------- setup
        legacy_dir = data / 'logs'
        legacy_dir.mkdir(parents=True)
        (legacy_dir / 'app.log').write_text(LINE * 4, 'utf-8')
        shared = temp / 'physical' / 'archive'
        (shared / 'logs').mkdir(parents=True)
        (shared / 'logs' / 'app.log').write_text(LINE * 7, 'utf-8')
        private = temp / 'physical' / 'private'
        private.mkdir(parents=True)
        (private / 'mine.log').write_text(LINE, 'utf-8')
        shared_id = request(app, 'POST', '/api/v2/storage-roots', {
            'label': '归档盘', 'path': str(shared), 'kind': 'shared_raw'}).value['item']['root_id']
        private_id = request(app, 'POST', '/api/v2/storage-roots', {
            'label': '私有区', 'path': str(private), 'kind': 'private_raw'}).value['item']['root_id']

        # 1 ------------------------------------------------ roots coexist, no overwrite
        app.storage.catalog.sync_directory(app.storage.roots.get('legacy'), 'logs')
        app.storage.catalog.sync_directory(app.storage.roots.get(shared_id), 'logs')
        listing = request(app, 'GET', '/api/v2/library?root=all&limit=50').value['items']
        same = [item for item in listing if item['relPath'] == 'logs/app.log']
        ids = {item['fileId'] for item in same}
        legacy_size = (legacy_dir / 'app.log').stat().st_size
        shared_size = (shared / 'logs' / 'app.log').stat().st_size
        if len(same) == 2 and len(ids) == 2 and legacy_size != shared_size:
            gate.passed('1. legacy 与两个部署目录外根并存，同名文件不覆盖',
                        {'roots': 3, 'same_path_rows': len(same), 'distinct_ids': len(ids),
                         'sizes': [legacy_size, shared_size]})
        else:
            gate.failed('1. legacy 与两个部署目录外根并存，同名文件不覆盖',
                        {'rows': len(same), 'ids': sorted(ids)})

        rule_ids = [row['id'] for row in request(app, 'GET', '/api/v1/rules').value['items']]
        legacy_binding = request(app, 'GET', '/api/v1/bindings/resolve?path=logs/app.log').value
        if rule_ids and legacy_binding['source']['scope'] == 'default':
            gate.passed('1b. 规则 ID 与 legacy 绑定映射保持不变',
                        {'rules': rule_ids, 'default_binding': legacy_binding['source']})
        else:
            gate.failed('1b. 规则 ID 与 legacy 绑定映射保持不变', legacy_binding)

        # 2 ------------------------------------------------------- safety refusals
        traversal = request(app, 'GET', '/api/v2/library/tree?root=legacy&path=../')
        symlink_target = temp / 'outside-secret.txt'
        symlink_target.write_text('secret', 'utf-8')
        (legacy_dir / 'link.log').symlink_to(symlink_target)
        symlink_listing = request(app, 'GET', '/api/v2/library/tree?root=legacy&path=logs').value
        names = [item['name'] for item in symlink_listing.get('items', [])]
        metadata = request(app, 'GET', '/api/v2/library/tree?root=legacy&path=').value
        safe = (traversal.status in (400, 403) and 'link.log' not in names
                and metadata['state'] == 'online')
        if safe:
            gate.passed('2a. 越根路径、符号链接被拒绝，读取仍正常',
                        {'traversal_status': traversal.status, 'symlink_hidden': True})
        else:
            gate.failed('2a. 越根路径、符号链接被拒绝，读取仍正常',
                        {'traversal_status': traversal.status, 'names': names})

        sentinel = data / '.logbench-root.json'
        stored = json.loads(sentinel.read_text('utf-8'))
        sentinel.unlink()
        offline_write = request(app, 'POST', '/api/v2/library/scan',
                                {'rootId': 'legacy', 'path': ''})
        offline_tree = request(app, 'GET', '/api/v2/library/tree?root=legacy&path=').value
        rows_before = request(app, 'GET', '/api/v2/library?root=legacy').value['total']
        if (offline_write.status == 409 and offline_tree['state'] == 'offline'
                and offline_tree.get('registrationKept') and rows_before >= 1):
            gate.passed('2b. 挂载掉线后停止写入，登记与元数据保留（非空库）',
                        {'write_status': offline_write.status, 'reason': offline_tree['reason'],
                         'rows_kept': rows_before})
        else:
            gate.failed('2b. 挂载掉线后停止写入，登记与元数据保留（非空库）',
                        {'write_status': offline_write.status, 'tree': offline_tree})
        sentinel.write_text(json.dumps(stored), 'utf-8')

        with app.store.transaction() as db:
            db.execute("UPDATE storage_roots SET volume_id='other-device' WHERE id=?", (shared_id,))
        remount = request(app, 'POST', '/api/v2/storage-roots/%s/probe' % shared_id, {})
        with app.store.transaction() as db:
            db.execute("UPDATE storage_roots SET volume_id=? WHERE id=?",
                       (str(shared.stat().st_dev), shared_id))
        if remount.status == 409 and remount.value['error']['details']['reason'] == 'volume_changed':
            gate.passed('2c. 不同盘重挂被识别为 volume_changed 并拒绝写入（注入身份）',
                        {'status': remount.status})
        else:
            gate.failed('2c. 不同盘重挂被识别为 volume_changed 并拒绝写入（注入身份）', remount.value)
        gate.blocked('2d. 真实卸载 / 换盘挂载的内核行为',
                     '本机没有可安全卸载的独立挂载点；本项需在目标机执行，未验证。')

        # 3 -------------------------------------------------- hundreds of files, paged
        opens = {'count': 0}
        original_open = app.storage.guard.open_regular
        def counting_open(*args, **kwargs):
            opens['count'] += 1
            return original_open(*args, **kwargs)
        app.storage.guard.open_regular = counting_open
        for index in range(MANY):
            (legacy_dir / ('run-%03d.log' % index)).write_text(LINE, 'utf-8')
        # The tree call is the scan: it registers metadata for the directory. Paging
        # before it would measure a database that has not seen these files yet.
        request(app, 'GET', '/api/v2/library/tree?root=legacy&path=logs')
        started = time.time()
        pages, cursor, rows = 0, None, 0
        while True:
            target = '/api/v2/library?directory=logs&limit=%d' % PAGE
            if cursor:
                target += '&cursor=' + cursor
            page = request(app, 'GET', target).value
            pages += 1
            rows += len(page['items'])
            cursor = page['nextCursor']
            if not cursor:
                break
            if pages > 40:
                break
        elapsed = round(time.time() - started, 3)
        if opens['count'] == 0 and rows >= MANY:
            gate.passed('3a. 数百文件按需分页浏览，全程未读取原文',
                        {'files': MANY, 'page': PAGE, 'pages': pages, 'rows': rows,
                         'seconds': elapsed, 'content_opens': 0})
        else:
            gate.failed('3a. 数百文件按需分页浏览，全程未读取原文',
                        {'rows': rows, 'content_opens': opens['count']})
        app.storage.guard.open_regular = original_open

        viewer = '10.44.1.2'
        viewer_private = request(app, 'GET', '/api/v2/library?scope=private', ip=viewer)
        viewer_roots = request(app, 'GET', '/api/v2/storage-roots', ip=viewer)
        admin_roots = request(app, 'GET', '/api/v2/storage-roots')
        leak = 'physical' in json.dumps(request(app, 'GET', '/api/v2/library/sources',
                                               ip=viewer).value, ensure_ascii=False)
        if (viewer_private.value['items'] == [] and viewer_roots.status == 403
                and admin_roots.status == 200 and not leak):
            gate.passed('3b. 私有范围与物理路径不向普通身份泄露',
                        {'private_items': 0, 'root_table': 403, 'path_leak': False})
        else:
            gate.failed('3b. 私有范围与物理路径不向普通身份泄露',
                        {'private': viewer_private.value, 'status': viewer_roots.status})

        # 4 ------------------------------------------------------- inheritance chain
        rules = request(app, 'GET', '/api/v1/rules').value['items']
        alt = request(app, 'POST', '/api/v1/rules', {
            'name': '归档规则',
            'profile': {'headerPattern': '^(?<message>.*)$', 'moduleRules': [],
                        'stepRules': [], 'errorRules': []}}).value['item']
        request(app, 'PUT', '/api/v2/bindings', {'scope': 'mapping', 'rootId': shared_id,
                                                'path': 'logs', 'ruleId': alt['id'],
                                                'expectedRevision': 0})
        effective = request(app, 'GET', '/api/v2/bindings/effective?rootId=%s&path=logs/app.log'
                            % shared_id).value
        personal = request(app, 'PUT', '/api/v2/bindings/personal', {
            'scope': 'directory', 'rootId': shared_id, 'path': 'logs',
            'ruleId': rules[0]['id'], 'expectedRevision': 0})
        personal_effective = request(app, 'GET', '/api/v2/bindings/effective?rootId=%s&path=logs/app.log'
                                     % shared_id).value
        cleared = request(app, 'DELETE', '/api/v2/bindings/personal', {
            'scope': 'directory', 'rootId': shared_id, 'path': 'logs', 'expectedRevision': 1})
        after = request(app, 'GET', '/api/v2/bindings/effective?rootId=%s&path=logs/app.log'
                        % shared_id).value
        ok = (effective['layer'] == 'mapping_default'
              and personal_effective['layer'] == 'personal_directory'
              and cleared.status == 200 and after['layer'] == 'mapping_default'
              and [row['layer'] for row in after['chain']] ==
              ['personal_file', 'personal_directory', 'shared_file', 'shared_directory',
               'mapping_default', 'service_default'])
        if ok:
            gate.passed('4. 映射默认可用，个人覆盖优先并可恢复继承，解释链完整',
                        {'mapping': effective['layer'], 'personal': personal_effective['layer'],
                         'after_clear': after['layer']})
        else:
            gate.failed('4. 映射默认可用，个人覆盖优先并可恢复继承，解释链完整',
                        {'mapping': effective, 'personal': personal_effective, 'after': after})

        # 5 ------------------------------------------------- delete / restore safety
        target_file = legacy_dir / 'run-000.log'
        digest = hashlib.sha256(target_file.read_bytes()).hexdigest()
        file_id = request(app, 'GET', '/api/v2/library?q=run-000&root=legacy').value['items'][0]['fileId']
        job = app.index_jobs.create(actor, {'fileId': file_id})
        blocked = request(app, 'DELETE', '/api/v2/library/files/' + file_id)
        app.jobs.action(actor, job['item']['id'], 'cancel', {'actionId': 'gate-cancel'})
        deleted = request(app, 'DELETE', '/api/v2/library/files/' + file_id)
        trash_item = deleted.value.get('item', {})
        target_file.write_text('conflict\n', 'utf-8')
        conflict = request(app, 'POST', '/api/v2/library/trash/%s/restore'
                           % trash_item.get('id', 'missing'))
        target_file.unlink()
        restored = request(app, 'POST', '/api/v2/library/trash/%s/restore' % trash_item.get('id'))
        same_bytes = target_file.is_file() and hashlib.sha256(target_file.read_bytes()).hexdigest() == digest
        if (blocked.status == 409 and deleted.status == 200 and conflict.status == 409
                and restored.status == 200 and same_bytes):
            gate.passed('5a. 活动引用阻止删除；同名冲突不覆盖；恢复后字节一致',
                        {'blocked': blocked.value['error']['code'],
                         'conflict': conflict.value['error']['code'],
                         'digest_kept': same_bytes})
        else:
            gate.failed('5a. 活动引用阻止删除；同名冲突不覆盖；恢复后字节一致',
                        {'blocked': blocked.status, 'deleted': deleted.status,
                         'conflict': conflict.status, 'restored': restored.status})

        crash_file = legacy_dir / 'run-001.log'
        crash_id = request(app, 'GET', '/api/v2/library?q=run-001&root=legacy').value['items'][0]['fileId']
        crash_digest = hashlib.sha256(crash_file.read_bytes()).hexdigest()
        bin_dir = data / '.logbench-trash'
        if not bin_dir.is_dir():
            bin_dir.mkdir(mode=0o700, parents=True)
        moved = bin_dir / 't-gate-crash'
        os.replace(str(crash_file), str(moved))
        with app.store.transaction() as db:
            db.execute(
                'INSERT INTO trash_entries(id,file_id,root_id,rel_path,generation,kind,size,owner,'
                'scope,digest,state,deleted_at,deleted_by,restored_at,restore_path,detail) '
                "VALUES('t-gate-crash',?,'legacy','logs/run-001.log',1,'file',?,'','shared',?,'pending',"
                "datetime('now'),'gate',NULL,NULL,?)",
                (crash_id, len(LINE), crash_digest, json.dumps({'trashPath': str(moved)})))
        repaired = app.storage.trash.repair()['repaired']
        recovered = request(app, 'POST', '/api/v2/library/trash/t-gate-crash/restore')
        if repaired and recovered.status == 200 and crash_file.is_file():
            gate.passed('5b. 移动中崩溃可由回收清单修复，原文不丢',
                        {'repaired': repaired, 'restore': recovered.status})
        else:
            gate.failed('5b. 移动中崩溃可由回收清单修复，原文不丢',
                        {'repaired': repaired, 'restore': recovered.status})

        # 6 -------------------------------------------------------------------- UI
        # The browser evidence is produced by its own script; this gate only reads it
        # and refuses to pass the item when the report is missing or failed.
        ui_report = ROOT / 'qa' / 'evidence' / 'R6-056' / 'library-storage.json'
        if ui_report.is_file():
            ui = json.loads(ui_report.read_text('utf-8'))
            failed = [name for name, value in (ui.get('checks') or {}).items() if not value['ok']]
            if ui.get('passed') and not failed:
                gate.passed('6. 文件库 / 存储位置页面的视觉与键盘证据',
                            {'checks': len(ui['checks']), 'screenshots': ui.get('screenshots'),
                             'viewports': [key for key in ui.get('states', {})
                                           if key.startswith('size_')]},
                            ['qa/evidence/R6-056/library-storage.json'])
            else:
                gate.failed('6. 文件库 / 存储位置页面的视觉与键盘证据', {'failed': failed})
        else:
            gate.blocked('6. 文件库 / 存储位置页面的视觉与键盘证据',
                         '未找到 qa/evidence/R6-056/library-storage.json；请先运行 '
                         'qa/r6/library_storage_ui.py。')

        report['summary'] = {
            'pass': sum(1 for item in gate.items if item['result'] == 'PASS'),
            'fail': sum(1 for item in gate.items if item['result'] == 'FAIL'),
            'blocked': sum(1 for item in gate.items if item['result'] == 'BLOCKED'),
        }
        # A blocked item is never turned into a pass: the gate reports PARTIAL and
        # the evidence file names what has to run on the target machine.
        report['verdict'] = ('FAIL' if report['summary']['fail']
                             else ('PARTIAL' if report['summary']['blocked'] else 'PASS'))
        report['finished_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    finally:
        app.index_jobs.close()
        app.queries.close()

    out.mkdir(parents=True, exist_ok=True)
    (out / 'multi_root_gate.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    shutil.rmtree(temp, ignore_errors=True)
    print(json.dumps({'summary': report['summary'], 'verdict': report['verdict'],
                      'out': str(out / 'multi_root_gate.json')}, ensure_ascii=False))
    return 0 if report['summary']['fail'] == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', default=str(ROOT / 'qa' / 'evidence'))
    return run(Path(parser.parse_args().out))


if __name__ == '__main__':
    raise SystemExit(main())
