#!/usr/bin/env python3
"""R6-015: two real pages compete for the same lease.

Checks that a single writer wins, that a stale fencing token cannot commit, and
that correctness does not depend on BroadcastChannel.

    env -u PYTHONPATH python3.9 qa/r6/leases.py --lan --out qa/evidence/R6-015
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from idb_store import lan_ip, serve, ROOT  # noqa: E402  (same directory)


def run(args) -> int:
    from playwright.sync_api import sync_playwright

    server = serve(ROOT)
    port = server.server_address[1]
    host = lan_ip() if args.lan else '127.0.0.1'
    url = 'http://%s:%d/qa/r6/lease_harness.html' % (host, port)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    checks = []

    def check(name, ok, detail=""):
        checks.append({'name': name, 'ok': bool(ok), 'detail': str(detail)})

    def call(page, expression):
        return page.evaluate('async () => { const r = await (%s); return r; }' % expression)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=args.channel, headless=True,
                                               args=['--no-proxy-server'])
            context = browser.new_context(viewport={'width': 900, 'height': 600})
            page_a = context.new_page()
            page_a.goto(url, wait_until='load')
            page_a.wait_for_function('() => window.__ready === true', timeout=30000)
            page_b = context.new_page()
            page_b.goto(url, wait_until='load')
            page_b.wait_for_function('() => window.__ready === true', timeout=30000)

            # Only the first tab resets the database; the second joins the same one.
            page_a.evaluate('() => window.__lease.open("lease-ns", true)')
            page_b.evaluate('() => window.__lease.open("lease-ns", false)')

            first = call(page_a, 'window.__lease.acquire("A", 5000)')
            check('A 获得租约并拿到 fencing=1', first['ok'] and first['value']['fencing'] == 1, first)

            blocked = call(page_b, 'window.__lease.acquire("B", 5000)')
            check('B 被拒绝（lease_held）', not blocked['ok'] and blocked['code'] == 'lease_held', blocked)

            committed = call(page_a, 'window.__lease.commit("A", 1, "from-A")')
            check('A 用自己的 token 提交成功', committed['ok'] and committed['value']['committed'] == 1, committed)

            call(page_a, 'window.__lease.release("A", 1)')
            second = call(page_b, 'window.__lease.acquire("B", 5000)')
            check('A 释放后 B 接手，fencing 递增到 2', second['ok'] and second['value']['fencing'] == 2, second)

            stale_commit = call(page_a, 'window.__lease.commit("A", 1, "from-stale-A")')
            check('旧 token 提交被拒（stale_fencing）',
                  not stale_commit['ok'] and stale_commit['code'] == 'stale_fencing', stale_commit)

            stale_renew = call(page_a, 'window.__lease.renew("A", 1, 5000)')
            check('旧 token 续租被拒', not stale_renew['ok'] and stale_renew['code'] == 'stale_fencing', stale_renew)

            payload = call(page_b, 'window.__lease.readPayload()')
            check('旧 token 的写入没有落地', payload['value'] == 'from-A', payload)

            fresh = call(page_b, 'window.__lease.commit("B", 2, "from-B")')
            check('新持有者提交成功', fresh['ok'], fresh)
            payload = call(page_b, 'window.__lease.readPayload()')
            check('新写入生效', payload['value'] == 'from-B', payload)

            call(page_b, 'window.__lease.release("B", 2)')

            # TTL expiry hands the lease over without an explicit release.
            expiring = call(page_a, 'window.__lease.acquire("A", 200)')
            time.sleep(0.5)
            taken = call(page_b, 'window.__lease.acquire("B", 5000)')
            expiring_fencing = (expiring.get('value') or {}).get('fencing') or 0
            taken_fencing_now = (taken.get('value') or {}).get('fencing') or 0
            check('租约过期后可被接管', expiring['ok'] and taken['ok'] and taken_fencing_now > expiring_fencing,
                  'expiring=%s taken=%s' % (expiring, taken))

            # Correctness must not depend on BroadcastChannel.
            dropped = page_b.evaluate('() => window.__lease.dropBroadcastChannel()')
            taken_fencing = (taken.get('value') or {}).get('fencing')
            if taken_fencing is not None:
                call(page_b, 'window.__lease.release("B", %d)' % taken_fencing)
            without = call(page_a, 'window.__lease.acquire("A", 5000)')
            check('无 BroadcastChannel 仍可正确获取', dropped == 'undefined' and without['ok'],
                  'BroadcastChannel=%s fencing=%s' % (dropped, (without.get('value') or {}).get('fencing')))

            page_a.screenshot(path=str(out / 'lease-page-a.png'))
            page_b.screenshot(path=str(out / 'lease-page-b.png'))
            secure = page_a.evaluate('() => window.isSecureContext')
            browser.close()
    finally:
        server.shutdown()

    report = {
        'schema': 'logbench.lease-evidence/1',
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'url': url,
        'secure_context': secure,
        'checks': checks,
        'ok': all(item['ok'] for item in checks),
    }
    (out / 'leases.json').write_text(json.dumps(report, ensure_ascii=False, indent=1) + '\n', 'utf-8')
    for item in checks:
        print('%-6s %-34s %s' % ('PASS' if item['ok'] else 'FAIL', item['name'], item['detail']))
    print('secure_context=%s pages=2' % secure)
    return 0 if report['ok'] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description='R6-015 lease evidence')
    parser.add_argument('--out', default=str(ROOT / 'qa/evidence/R6-015'))
    parser.add_argument('--channel', default='chrome')
    parser.add_argument('--lan', action='store_true')
    return run(parser.parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
