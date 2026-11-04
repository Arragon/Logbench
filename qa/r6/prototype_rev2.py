#!/usr/bin/env python3
"""R6-004: verify the Rev2 prototype as a reviewable target.

Checks that are contracts, not opinions:
* the toolbar stays one row and the data area keeps most of the viewport
* no horizontal overflow at 1366 and 375
* every state renders deterministically and can be reached by keyboard/selector
* Escape closes the overlay and returns focus to the selector

Usage::

    env -u PYTHONPATH python3.9 qa/r6/prototype_rev2.py --out qa/evidence/R6-004
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PROTOTYPE = ROOT / 'docs' / 'prototypes' / 'r6-rev2.html'

MEASURE = r"""
() => {
  const rect = (selector) => {
    const node = document.querySelector(selector);
    if (!node) return null;
    const box = node.getBoundingClientRect();
    return { x: Math.round(box.x), y: Math.round(box.y), w: Math.round(box.width), h: Math.round(box.height) };
  };
  const toolbar = document.querySelector('.toolbar');
  let toolbarRows = 0;
  if (toolbar) {
    const tops = new Set();
    for (const child of toolbar.children) {
      const box = child.getBoundingClientRect();
      if (box.height > 0) tops.add(Math.round(box.top));
    }
    toolbarRows = tops.size;
  }
  return {
    viewport: { w: innerWidth, h: innerHeight },
    toolbarRows,
    toolbar: rect('.toolbar'),
    timeline: rect('.timeline'),
    table: rect('.tablewrap'),
    foot: rect('.foot'),
    status: rect('.status'),
    overlayVisible: !document.getElementById('overlay').hidden,
    overlayHeight: rect('#panel') ? rect('#panel').h : 0,
    scrollWidth: document.documentElement.scrollWidth,
    innerWidth,
    rows: document.querySelectorAll('#rows tr').length,
    resultHead: document.getElementById('result-head').hidden ? null : document.getElementById('result-head').innerText.replace(/\s+/g, ' '),
    activeElement: document.activeElement ? (document.activeElement.id || document.activeElement.tagName) : null
  };
}
"""


def run(args) -> int:
    from playwright.sync_api import sync_playwright

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    url = PROTOTYPE.as_uri()
    report = {
        'schema': 'logbench.prototype-check/1',
        'prototype': str(PROTOTYPE.relative_to(ROOT)),
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'states': {},
        'checks': [],
        'screenshots': [],
    }

    def check(name, ok, detail):
        report['checks'].append({'name': name, 'ok': bool(ok), 'detail': detail})

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=args.channel, headless=True)
        page = browser.new_page(viewport={'width': 1366, 'height': 768})
        page.goto(url + '?state=idle', wait_until='load')
        page.wait_for_selector('#rows tr')

        states = page.evaluate('() => window.__r6.states')
        report['state_list'] = states

        # Desktop pass over every state.
        for state in states:
            page.select_option('#state', state)
            page.wait_for_timeout(120)
            metrics = page.evaluate(MEASURE)
            report['states'][state] = metrics
            shot = out / ('desktop-%s.png' % state)
            page.screenshot(path=str(shot))
            report['screenshots'].append(shot.name)

        idle = report['states']['idle']
        check('toolbar stays one row', idle['toolbarRows'] == 1, 'rows=%d' % idle['toolbarRows'])
        ratio = idle['table']['h'] / idle['viewport']['h']
        check('data area keeps >= 60% of viewport', ratio >= 0.6, 'ratio=%.3f' % ratio)
        check('no horizontal overflow', idle['scrollWidth'] <= idle['innerWidth'],
              'scrollWidth=%d innerWidth=%d' % (idle['scrollWidth'], idle['innerWidth']))
        check('table renders deterministic rows', idle['rows'] == 10, 'rows=%d' % idle['rows'])

        # Result-head semantics: partial must never look like an exact zero.
        page.select_option('#state', 'search-partial')
        page.wait_for_timeout(120)
        partial_text = page.evaluate("() => document.getElementById('result-head').innerText")
        check('partial result states a floor, not a count',
              '至少' in partial_text and '0 条' not in partial_text, partial_text.replace('\n', ' '))
        check('partial result offers resume', page.locator('#resume').count() == 1, 'resume button present')

        page.select_option('#state', 'search-exact')
        page.wait_for_timeout(120)
        exact_text = page.evaluate("() => document.getElementById('result-head').innerText")
        check('exact result states coverage', '100%' in exact_text, exact_text.replace('\n', ' '))

        page.select_option('#state', 'search-unknown')
        page.wait_for_timeout(120)
        unknown_text = page.evaluate("() => document.getElementById('result-head').innerText")
        check('unknown result withholds a number', '未知' in unknown_text, unknown_text.replace('\n', ' '))

        # Upload completion must not read as searchable.
        page.select_option('#state', 'upload-uploaded')
        page.wait_for_timeout(120)
        upload_text = page.evaluate("() => document.getElementById('panel').innerText")
        check('uploaded shows 未索引 instead of searchable',
              ('未索引' in upload_text) and ('可检索。' not in upload_text),
              upload_text.replace('\n', ' ')[:120])

        # Keyboard path: selector -> open -> search -> tasks.
        page.select_option('#state', 'idle')
        page.wait_for_timeout(80)
        page.focus('#state')
        order = []
        for _ in range(9):
            page.keyboard.press('Tab')
            order.append(page.evaluate('() => document.activeElement && document.activeElement.id'))
        report['keyboard_order'] = order
        check('keyboard reaches open, search and tasks in order',
              all(name in order for name in ('open', 'q', 'tasks')), str(order))

        page.focus('#open')
        page.keyboard.press('Enter')
        page.wait_for_timeout(150)
        check('Enter opens the choice dialog', not page.evaluate("() => document.getElementById('overlay').hidden"), '')
        page.keyboard.press('Escape')
        page.wait_for_timeout(150)
        closed = page.evaluate("() => document.getElementById('overlay').hidden")
        focus_back = page.evaluate("() => document.activeElement.id")
        check('Escape closes and restores focus to the selector', closed and focus_back == 'state',
              'closed=%s focus=%s' % (closed, focus_back))

        # Narrow viewport: the table must still be usable, nav collapses.
        narrow = browser.new_page(viewport={'width': 375, 'height': 812})
        narrow.goto(url + '?state=idle', wait_until='load')
        narrow.wait_for_selector('#rows tr')
        narrow_metrics = narrow.evaluate(MEASURE)
        report['states']['narrow-375'] = narrow_metrics
        shot = out / 'narrow-375-idle.png'
        narrow.screenshot(path=str(shot))
        report['screenshots'].append(shot.name)
        check('narrow: no horizontal overflow', narrow_metrics['scrollWidth'] <= narrow_metrics['innerWidth'],
              'scrollWidth=%d innerWidth=%d' % (narrow_metrics['scrollWidth'], narrow_metrics['innerWidth']))
        check('narrow: single toolbar row', narrow_metrics['toolbarRows'] == 1,
              'rows=%d' % narrow_metrics['toolbarRows'])
        narrow.select_option('#state', 'reading')
        narrow.wait_for_timeout(150)
        narrow_reading = narrow.evaluate(MEASURE)
        report['states']['narrow-375-reading'] = narrow_reading
        shot = out / 'narrow-375-reading.png'
        narrow.screenshot(path=str(shot))
        report['screenshots'].append(shot.name)
        check('narrow: overlay fits the viewport', narrow_reading['overlayHeight'] <= narrow_metrics['viewport']['h'],
              'panel=%d viewport=%d' % (narrow_reading['overlayHeight'], narrow_metrics['viewport']['h']))

        browser.close()

    report['failures'] = [item for item in report['checks'] if not item['ok']]
    (out / 'prototype-check.json').write_text(json.dumps(report, ensure_ascii=False, indent=1) + '\n', 'utf-8')
    for item in report['checks']:
        print('%-6s %-52s %s' % ('PASS' if item['ok'] else 'FAIL', item['name'], item['detail']))
    print('screenshots:', len(report['screenshots']), '->', out)
    return 1 if report['failures'] else 0


def main() -> int:
    parser = argparse.ArgumentParser(description='R6-004 prototype verification')
    parser.add_argument('--out', default=str(ROOT / 'qa/evidence/R6-004'))
    parser.add_argument('--channel', default='chrome')
    return run(parser.parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
