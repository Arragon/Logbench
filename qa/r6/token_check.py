#!/usr/bin/env python3
"""Compare the shipped R5 token block against the unify-design v1.0.1 kit source.

The kit states "CSS remains authoritative", so the check reads the real
`.ud-app { ... }` declaration block from src/style.css and diffs it against
tokens.json. Output is a machine-readable delta list plus a text table.

Usage:
  python3.9 qa/r6/token_check.py --kit ~/.workbuddy/skills/unify-design/assets/ui-kit/tokens.json \
      --out qa/evidence/R6-003/token-diff.json
"""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STYLE = ROOT / 'src' / 'style.css'
DEFAULT_KIT = Path.home() / '.workbuddy/skills/unify-design/assets/ui-kit/tokens.json'


def read_token_block(path: Path) -> dict:
    text = path.read_text('utf-8')
    match = re.search(r'\.ud-app\s*\{(.*?)\}', text, re.S)
    if not match:
        raise SystemExit('could not find the .ud-app token block in %s' % path)
    block = match.group(1)
    tokens = {}
    for name, value in re.findall(r'(--[a-z0-9-]+)\s*:\s*([^;]+);', block):
        tokens[name] = value.strip()
    return tokens


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='R5 token block vs unify-design kit')
    parser.add_argument('--kit', default=str(DEFAULT_KIT))
    parser.add_argument('--out', default=str(ROOT / 'qa/evidence/R6-003/token-diff.json'))
    args = parser.parse_args(argv)

    kit = json.loads(Path(args.kit).read_text('utf-8'))
    expected = kit.get('tokens', {})
    actual = read_token_block(STYLE)

    rows = []
    missing, extra, mismatch = [], [], []
    for name, value in expected.items():
        if name not in actual:
            missing.append(name)
            rows.append({'token': name, 'kit': value, 'r5': None, 'status': 'missing'})
            continue
        same = actual[name].replace(' ', '') == value.replace(' ', '')
        if not same:
            mismatch.append(name)
        rows.append({'token': name, 'kit': value, 'r5': actual[name], 'status': 'match' if same else 'differs'})
    for name in actual:
        if name not in expected:
            extra.append(name)

    report = {
        'schema': 'logbench.r6.token-diff/1',
        'kit': {'path': args.kit, 'name': kit.get('name'), 'version': kit.get('version')},
        'r5': {'path': 'src/style.css', 'block': '.ud-app'},
        'summary': {'expected': len(expected), 'matched': len(expected) - len(missing) - len(mismatch),
                    'mismatched': len(mismatch), 'missing': len(missing), 'r5_only': len(extra)},
        'mismatched': mismatch, 'missing': missing, 'r5_only': extra,
        'rows': rows,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    print('kit %s v%s · %d tokens' % (kit.get('name'), kit.get('version'), len(expected)))
    print('matched %d · differs %d · missing %d · r5-only %d'
          % (report['summary']['matched'], len(mismatch), len(missing), len(extra)))
    for row in rows:
        if row['status'] != 'match':
            print('  %-22s kit=%-28s r5=%s' % (row['token'], row['kit'], row['r5']))
    print('wrote %s' % out)
    return 0 if not missing else 1


if __name__ == '__main__':
    raise SystemExit(main())
