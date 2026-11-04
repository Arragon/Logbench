#!/usr/bin/env python3
"""R6-003 visual baseline: capture real geometry, screenshots and design tokens.

Runs against a live LogBench instance with Playwright + the system Chrome channel
(no bundled Chromium download). Produces:
  <out>/geometry.json     numbers a reviewer can diff
  <out>/<view>-<theme>.png screenshots of the real rendered page
and exits non-zero when the floating detail drawer shifts the reading workspace,
which is the one geometry promise R5 v3.0.3 must not lose.

Usage:
  python3.9 qa/r6/visual_baseline.py --base-url http://127.0.0.1:50901 --out qa/evidence/R6-003
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - environment guard
    print('playwright is required: python3.9 -m pip install --user playwright', file=sys.stderr)
    raise SystemExit(2)

VIEWS = ('1366x768', '1920x1080', '768x1024', '1024x768', '375x812')
THEMES = ('ink', 'paper', 'graphite', 'nord')
TOKENS = ('--ud-bg', '--ud-surface', '--ud-surface-alt', '--ud-surface-strong', '--ud-ink',
          '--ud-ink-soft', '--ud-muted', '--ud-subtle', '--ud-line', '--ud-line-strong',
          '--ud-control', '--ud-primary', '--ud-primary-hover', '--ud-primary-ink',
          '--ud-primary-tint', '--ud-positive', '--ud-negative', '--ud-amber', '--ud-purple',
          '--ud-sky', '--ud-radius', '--ud-radius-lg', '--ud-radius-xl', '--ud-control-h',
          '--ud-table-row-h', '--ud-topbar-h', '--ud-sidebar-w', '--ud-tabbar-h',
          '--ud-md', '--ud-sm', '--ud-xs')

MEASURE_JS = """
(selectors) => {
  const box = (selector) => {
    const el = document.querySelector(selector);
    if (!el) return null;
    const rect = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return {
      x: Math.round(rect.x), y: Math.round(rect.y),
      w: Math.round(rect.width), h: Math.round(rect.height),
      font: style.fontSize, lineHeight: style.lineHeight,
      visible: rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden'
        && style.display !== 'none',
    };
  };
  const out = {};
  for (const key of Object.keys(selectors)) out[key] = box(selectors[key]);

  const toolbar = document.querySelector('.toolbar');
  let toolbarRows = 0;
  if (toolbar) {
    const tops = [];
    for (const child of toolbar.children) {
      const rect = child.getBoundingClientRect();
      if (rect.height > 0 && rect.width > 0) tops.push(Math.round(rect.top));
    }
    tops.sort((a, b) => a - b);
    let rows = 0;
    let last = null;
    for (const top of tops) {
      if (last === null || top - last > 10) rows++;
      last = top;
    }
    toolbarRows = rows;
  }
  out.toolbarRows = toolbarRows;

  const items = [...document.querySelectorAll('.record-item')];
  const virtual = document.querySelector('.virtual-region');
  out.records = {
    mounted: items.length,
    firstTop: items.length ? Math.round(items[0].getBoundingClientRect().top) : null,
    lastBottom: items.length ? Math.round(items[items.length - 1].getBoundingClientRect().bottom) : null,
    scrollHeight: virtual ? Math.round(virtual.getBoundingClientRect().height) : null,
  };
  const root = document.querySelector('.ud-app');
  out.rootClass = root ? root.className : null;
  out.theme = document.body.dataset.theme || null;
  out.viewport = { w: window.innerWidth, h: window.innerHeight, dpr: window.devicePixelRatio };
  out.themeName = document.body.getAttribute('data-theme');
  return out;
}
"""

SELECTORS = {
    'header': '.lb-header',
    'sidebar': '.sidebar',
    'main': '.main',
    'toolbar': '.toolbar',
    'workpanel': '.workpanel',
    'timeline': '.timeline',
    'reader': '.log-virtual',
    'pager': '.page-foot',
    'status': '.app-status',
    'inspector': '.inspector',
}

TAB_LABELS = ('日志浏览', '文件库', '规则管理')


def collect_tokens(page):
    return page.evaluate("""(names) => {
      const el = document.querySelector('.ud-app') || document.body;
      const style = getComputedStyle(el);
      const out = {};
      for (const name of names) out[name] = style.getPropertyValue(name).trim();
      out['body.font-size'] = getComputedStyle(document.body).fontSize;
      out['body.font-family'] = getComputedStyle(document.body).fontFamily;
      return out;
    }""", list(TOKENS))


def open_server_file(page, timeout_ms=25000):
    """Open the first server log through the real library view, surviving collapsed navigation."""
    selector = '[data-act="nav"][data-value="files"]:visible'
    if page.locator(selector).count() == 0:
        page.click('[data-act="toggle-nav"]')
        page.wait_for_timeout(350)
    page.locator(selector).first.click()
    page.wait_for_selector('[data-act="open-file"]', state='visible', timeout=timeout_ms)
    page.locator('[data-act="open-file"]').first.click()
    page.wait_for_selector('.record-item', timeout=timeout_ms)
    page.wait_for_timeout(600)


def measure(page):
    data = page.evaluate(MEASURE_JS, SELECTORS)
    data['tokens'] = collect_tokens(page)
    return data


def drawer_probe(page, out: Path, view: str):
    """Open the floating detail with a double click, measure the workspace, close with Escape.

    R5 v3.0.3 promises the drawer overlays the reading workspace without moving it.
    """
    before = measure(page)
    page.dblclick('.record-item')
    try:
        page.wait_for_selector('.inspector', timeout=8000)
    except Exception:
        return {'opened': False, 'note': 'inspector did not appear after double click'}
    page.wait_for_timeout(450)
    shot = out / ('%s-drawer.png' % view)
    page.screenshot(path=str(shot), full_page=False)
    after = measure(page)
    anchor_before = before['records']['firstTop']
    anchor_after = after['records']['firstTop']
    deltas = {}
    for key in ('reader', 'workpanel', 'timeline'):
        a, b = before.get(key), after.get(key)
        if a and b:
            deltas[key] = {'dx': b['x'] - a['x'], 'dw': b['w'] - a['w'], 'dh': b['h'] - a['h']}
    page.keyboard.press('Escape')
    page.wait_for_timeout(250)
    closed = page.evaluate("""() => {
      const el = document.querySelector('.inspector');
      if (!el) return true;
      const style = getComputedStyle(el);
      return style.display === 'none' || style.visibility === 'hidden' || el.getBoundingClientRect().width === 0;
    }""")
    return {
        'opened': True,
        'inspector': after.get('inspector'),
        'deltas': deltas,
        'anchorShift': None if anchor_before is None or anchor_after is None else abs(anchor_after - anchor_before),
        'closed': closed,
        'screenshot': shot.name,
    }


def apply_theme(page, theme):
    page.evaluate("(name) => { document.body.dataset.theme = name; }", theme)
    page.wait_for_timeout(180)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='LogBench R6 visual baseline')
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--views', default=','.join(VIEWS))
    parser.add_argument('--themes', default='ink')
    parser.add_argument('--open-file', action='store_true', help='open the first server log before measuring')
    parser.add_argument('--skip-drawer', action='store_true')
    parser.add_argument('--headless', default='1')
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    views = [v.strip() for v in args.views.split(',') if v.strip()]
    themes = [t.strip() for t in args.themes.split(',') if t.strip()]

    report = {'schema': 'logbench.r6.visual-baseline/1', 'base_url': args.base_url,
              'tour': [], 'tokens': {}, 'drawer': {}, 'failures': []}

    with sync_playwright() as play:
        browser = play.chromium.launch(channel='chrome', headless=args.headless != '0')
        for view in views:
            width, height = (int(part) for part in view.split('x'))
            context = browser.new_context(viewport={'width': width, 'height': height},
                                          device_scale_factor=1)
            page = context.new_page()
            page.goto(args.base_url, wait_until='networkidle', timeout=45000)
            page.wait_for_selector('.lb-app', timeout=30000)
            page.wait_for_timeout(900)
            entry = {'view': view, 'themes': {}}
            if args.open_file:
                open_server_file(page)
                if not report['tokens']:
                    report['tokens'] = collect_tokens(page)
            for theme in themes:
                apply_theme(page, theme)
                metrics = measure(page)
                shot = out / ('%s-%s.png' % (view, theme))
                page.screenshot(path=str(shot), full_page=False)
                metrics['screenshot'] = shot.name
                entry['themes'][theme] = metrics
                if metrics['toolbarRows'] > 1 and width >= 768:
                    report['failures'].append('%s/%s: toolbar wrapped into %d rows'
                                              % (view, theme, metrics['toolbarRows']))
                elif metrics['toolbarRows'] > 1:
                    entry.setdefault('notes', []).append(
                        'toolbar wraps into %d rows at %dpx (accepted for narrow screens; '
                        'must not push the data area below 50%% of the viewport)' % (metrics['toolbarRows'], width))
            if args.open_file and not args.skip_drawer:
                drawer = drawer_probe(page, out, view)
                if drawer.get('opened'):
                    shift = drawer.get('anchorShift')
                    if shift is not None and shift > 2:
                        report['failures'].append('%s: reading anchor moved %dpx when the drawer opened'
                                                  % (view, shift))
                    for key, delta in drawer['deltas'].items():
                        if abs(delta['dx']) > 1 or abs(delta['dw']) > 1:
                            report['failures'].append(
                                '%s: %s geometry changed with drawer (dx=%d dw=%d)'
                                % (view, key, delta['dx'], delta['dw']))
                    if not drawer.get('closed'):
                        report['failures'].append('%s: Escape did not close the drawer' % view)
                report['drawer'][view] = drawer
            report['tour'].append(entry)
            context.close()
        browser.close()

    (out / 'geometry.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n',
                                       encoding='utf-8')
    print('wrote %s' % (out / 'geometry.json'))
    for failure in report['failures']:
        print('FAIL ' + failure, file=sys.stderr)
    return 1 if report['failures'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
