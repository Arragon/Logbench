/* Record golden expectations from the real R5 parser.
   Run after regenerating fixtures: node tests/r6/record-expected.cjs
   Review the diff before committing - a changed expectation is a semantic change. */
'use strict';
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const harness = require('./parser-harness.cjs');

const OUT = path.join(harness.GOLDEN_DIR, 'expected.json');

const KNOWN_DEFECTS = [
  {
    id: 'orphan-absorbed',
    area: 'parseLocal continuation',
    detail: '一条既无方括号头部、也不在空行之后的孤立文本行，会被当成上一条记录的 body 续行，'
      + '而不是独立 UNPARSED 记录。R5 现有行为如此，R6 需保持兼容或显式改造，不得静默改变。',
  },
  {
    id: 'prefixed-traceback-split',
    area: 'parseLocal per-line stamped stack',
    detail: '每行都带方括号头部的 `| Traceback` 形式，只有首行获得 hasTrace，'
      + '后续 `|   File ...` / `| KeyError ...` 行被切成独立记录，未合并为同一条堆栈记录。',
  },
];

function sha256(buffer) {
  return crypto.createHash('sha256').update(buffer).digest('hex');
}

function main() {
  const source = fs.readFileSync(harness.APP);
  const fixtures = {};
  for (const name of harness.listFixtures()) {
    const raw = fs.readFileSync(path.join(harness.GOLDEN_DIR, name));
    const records = harness.parseFixture(name).map(harness.project);
    fixtures[name] = { sha256: sha256(raw), bytes: raw.length, records };
  }
  const expected = {
    schema: 'logbench.r6.golden-expected/1',
    recorded_from: 'src/app.js::parseLocal (shipped R5 parser, extracted by tests/r6/parser-harness.cjs)',
    app_sha256: sha256(source),
    fixtures,
    known_defects: KNOWN_DEFECTS,
  };
  fs.writeFileSync(OUT, JSON.stringify(expected, null, 2) + '\n');
  const total = Object.values(fixtures).reduce((n, item) => n + item.records.length, 0);
  console.log('wrote %s (%d fixtures, %d records)', OUT, Object.keys(fixtures).length, total);
}

main();
