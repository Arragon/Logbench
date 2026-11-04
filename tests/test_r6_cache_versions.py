"""R6-038: layered cache versions — reuse, staging, publication and isolation."""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.app import Application, Config
from server.indexing.cache_versions import CacheError, CacheRegistry, deps_for, signature
from server.indexing.layers import build_rules_layer, build_search_layer
from server.indexing.node_pool import NodePool
from server.indexing.pipeline import Pipeline
from server.indexing.shards import ShardReader
from server.models import file_id_for

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / 'src' / 'workers' / 'server-engine.cjs'
GOLDEN = ROOT / 'tests' / 'fixtures' / 'r6' / 'golden'
RUNTIME = 'node-18'


def make_app(root: Path) -> Application:
    (root / 'rules').mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / 'rules' / 'cpqpsgx-default.json', root / 'rules' / 'cpqpsgx-default.json')
    return Application(Config(root, bootstrap_admins=['127.0.0.1']))


class Harness(unittest.TestCase):
    """A real pipeline plus registry, so layer reuse is measured against real work."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = make_app(self.root)
        self.pool = NodePool(ENTRY, workers=1, request_timeout=30)
        self.pool.start()
        self.index_root = self.root / 'index'
        self.pipeline = Pipeline(self.app.store, self.pool, self.index_root, chunk_size=64 * 1024)
        self.registry = CacheRegistry(self.app.store)
        self.source = GOLDEN / 'edge_cases.log'
        self.profile = json.loads((ROOT / 'rules' / 'cpqpsgx-default.json').read_text('utf-8'))
        self.owner = 'admin@127.0.0.1'
        self.generation = self.pipeline.ensure_generation(self.source)
        self.file_id = self.generation.file_id
        self.shard = None
        self.semantics = self.pool.signatures('sig', self.profile)['signatures']
        self.work = {'parses': 0, 'structures': 0, 'rules': 0, 'search': 0, 'source_bytes': 0}

    def tearDown(self):
        self.pool.close(drain=False)
        self.temp.cleanup()

    # ------------------------------------------------------------------ helpers
    def structure_deps(self, profile_semantics=None):
        return deps_for('structure', content_sig=self.generation.content_sig,
                        generation=self.generation.generation, framing='r6-009',
                        runtime=RUNTIME, schema='r6-shard/2', encoding='utf-8')

    def rules_deps(self, semantics, structure_version):
        return deps_for('rules', structure_signature=structure_version['signature'],
                        rule_structure=semantics['structure'], rule_extract=semantics['extract'],
                        engine='rules/1')

    def search_deps(self, rules_version):
        return deps_for('search', rules_signature=rules_version['signature'],
                        projection='grams/v1', gram_size=3)

    def open_layers(self, *, owner=None, semantics=None):
        """The equivalent of "open this file": build only what is not reusable."""
        owner = owner or self.owner
        semantics = semantics or self.semantics
        structure = self.registry.ensure(self.file_id, 'structure', owner=owner,
                                         generation=self.generation.generation,
                                         deps=self.structure_deps())
        if not structure['reused']:
            before = self.pool.snapshot()['stats']['requests']
            result = self.pipeline.run('job-%s' % structure['version_id'], self.source,
                                       file_id=self.file_id, profile=self.profile)
            self.work['parses'] += self.pool.snapshot()['stats']['requests'] - before
            self.work['structures'] += 1
            self.shard = Path(result['shard'])
            reader = ShardReader(self.shard)
            self.registry.publish(
                structure['version_id'], rows=reader.count(),
                digest=hashlib.sha256(result['content_sig'].encode('utf-8')).hexdigest(),
                stats={'parses': 1, 'bytes': result['stats']['bytes'], 'records': reader.count()},
                expected_rows=result['stats']['records'])

        rules = self.registry.ensure(self.file_id, 'rules', owner=owner,
                                     generation=self.generation.generation,
                                     deps=self.rules_deps(semantics, structure))
        if not rules['reused']:
            built = build_rules_layer(self.shard, source_path=self.source, pool=self.pool,
                                      profile=self.profile, version=rules['version_id'])
            self.work['rules'] += 1
            self.work['source_bytes'] += built['stats']['source_bytes']
            rules = self.registry.publish(rules['version_id'], rows=built['rows'],
                                          digest=built['digest'], stats=built['stats'],
                                          expected_rows=built['rows'])

        search = self.registry.ensure(self.file_id, 'search', owner=owner,
                                      generation=self.generation.generation,
                                      deps=self.search_deps(rules))
        if not search['reused']:
            built = build_search_layer(self.shard, version=search['version_id'],
                                       rules_version=rules['version_id'])
            self.work['search'] += 1
            search = self.registry.publish(search['version_id'], rows=built['rows'],
                                           digest=built['digest'], stats=built['stats'])
        return {'structure': structure, 'rules': rules, 'search': search}

    def raw_layer(self, owner=None):
        return self.registry.ensure(self.file_id, 'raw', owner=owner or self.owner,
                                    generation=self.generation.generation,
                                    deps=deps_for('raw', content_sig=self.generation.content_sig))


class SignatureTests(Harness):
    def test_required_dependencies_cannot_be_omitted(self):
        with self.assertRaises(CacheError) as caught:
            deps_for('structure', content_sig='abc')
        self.assertEqual(caught.exception.code, 'missing_dependency')
        self.assertIn('framing', caught.exception.details['missing'])

    def test_mtime_and_size_are_refused_as_identity(self):
        for key in ('mtime', 'mtime_ns', 'size'):
            with self.assertRaises(CacheError) as caught:
                deps_for('structure', content_sig='abc', generation=1, framing='r6-009',
                         runtime=RUNTIME, schema='r6-shard/2', encoding='utf-8', **{key: 1})
            self.assertEqual(caught.exception.code, 'unstable_dependency')

    def test_signature_is_stable_and_layer_scoped(self):
        deps = {'content_sig': 'abc', 'generation': 1}
        self.assertEqual(signature('raw', deps), signature('raw', dict(reversed(list(deps.items())))))
        self.assertNotEqual(signature('raw', deps), signature('query', deps))

    def test_style_changes_do_not_change_rule_semantics(self):
        styled = json.loads(json.dumps(self.profile))
        styled['name'] = 'CPQPSGX 夜间'
        for index, node in enumerate(styled.get('nodes') or []):
            node['color'] = '#ff00aa'
            node['shape'] = 'diamond'
            node['name'] = 'node-%d' % index
        restyled = self.pool.signatures('sig', styled)['signatures']
        self.assertEqual(restyled, self.semantics)

        semantic = json.loads(json.dumps(self.profile))
        semantic['anomalies'][0]['value'] = '^(ERROR|FATAL|CRITICAL)$'
        changed = self.pool.signatures('sig', semantic)['signatures']
        self.assertNotEqual(changed['structure'], self.semantics['structure'])
        self.assertEqual(changed['extract'], self.semantics['extract'])


class ReuseTests(Harness):
    def test_second_open_parses_nothing_and_reads_nothing(self):
        first = self.open_layers()
        self.assertGreater(self.work['parses'], 0, 'the first open really parsed')
        self.assertEqual(self.work['structures'], 1)
        parses, source_bytes = self.work['parses'], self.work['source_bytes']

        before = self.pool.snapshot()['stats']['requests']
        second = self.open_layers()
        self.assertEqual(self.pool.snapshot()['stats']['requests'], before,
                         'a cache hit must not reach the parser')
        for layer in ('structure', 'rules', 'search'):
            self.assertTrue(second[layer]['reused'], layer)
            self.assertEqual(second[layer]['version_id'], first[layer]['version_id'])
        self.assertEqual(self.work['parses'], parses, 'no parse requests on reopen')
        self.assertEqual(self.work['source_bytes'], source_bytes, 'no source bytes read on reopen')
        self.assertEqual(self.work['rules'], 1, 'rules layer is not rebuilt on reopen')
        self.assertEqual(self.work['search'], 1, 'search layer is not rebuilt on reopen')

    def test_style_change_rebuilds_nothing(self):
        first = self.open_layers()
        parses, source_bytes = self.work['parses'], self.work['source_bytes']
        styled = json.loads(json.dumps(self.profile))
        styled['name'] = '夜间配色'
        styled['nodes'][0]['color'] = '#00ff88'
        styled['nodes'][0]['shape'] = 'star'
        self.profile = styled
        self.semantics = self.pool.signatures('sig', styled)['signatures']
        second = self.open_layers()
        for layer in ('structure', 'rules', 'search'):
            self.assertEqual(second[layer]['version_id'], first[layer]['version_id'], layer)
        self.assertEqual(self.work['parses'], parses)
        self.assertEqual(self.work['source_bytes'], source_bytes)
        self.assertEqual((self.work['rules'], self.work['search']), (1, 1))

    def test_anomaly_edit_rebuilds_rules_and_search_but_not_structure(self):
        first = self.open_layers()
        parses = self.work['parses']
        edited = json.loads(json.dumps(self.profile))
        edited['anomalies'][1]['value'] = '^(WARNING|WARN|NOTICE)$'
        self.profile = edited
        self.semantics = self.pool.signatures('sig', edited)['signatures']

        second = self.open_layers()
        self.assertEqual(second['structure']['version_id'], first['structure']['version_id'],
                         'rule edits must not re-frame the file')
        self.assertEqual(self.work['parses'], parses, 'no parse requests at all')
        self.assertNotEqual(second['rules']['version_id'], first['rules']['version_id'])
        self.assertNotEqual(second['search']['version_id'], first['search']['version_id'])
        self.assertEqual((self.work['rules'], self.work['search']), (2, 2))
        # The rules rebuild re-read record bytes, and says so.
        self.assertGreater(self.work['source_bytes'], self.source.stat().st_size)

    def test_extract_edit_changes_rules_but_not_the_structure_version(self):
        first = self.open_layers()
        edited = json.loads(json.dumps(self.profile))
        edited['extractors'][0]['pattern'] = r'(?:^|\b)(?<key>[A-Za-z_][A-Za-z0-9_]*)\s*[:：=]\s*(?<value>\S+)'
        self.profile = edited
        self.semantics = self.pool.signatures('sig', edited)['signatures']
        second = self.open_layers()
        self.assertEqual(second['structure']['version_id'], first['structure']['version_id'])
        self.assertNotEqual(second['rules']['signature'], first['rules']['signature'])
        rows = ShardReader(self.shard).rule_rows(second['rules']['version_id'], 0, 10)
        self.assertTrue(rows, 'the new rules layer is readable')

    def test_a_multi_page_rules_build_keeps_every_page(self):
        """Clearing the version per batch would leave only the last page behind."""
        layers = self.open_layers()
        reader = ShardReader(self.shard)
        total = ShardReader(self.shard).count()
        self.assertGreater(total, 1)
        built = build_rules_layer(self.shard, source_path=self.source, pool=self.pool,
                                  profile=self.profile, version='paged', page=2)
        self.assertEqual(built['rows'], total)
        self.assertEqual(reader.rule_count('paged')['rows'], total,
                         'every page must survive, not just the last one')
        self.assertEqual(reader.rule_count(layers['rules']['version_id'])['rows'], total)

    def test_parallel_rules_layers_keep_every_page(self):
        open_layers = self.open_layers()
        reader = ShardReader(self.shard)
        total = reader.count()
        from server.indexing.parallel import ParallelIndexer
        indexer = ParallelIndexer(self.app.store, self.pool, self.index_root, parse_slots=3)
        report = indexer.rebuild_rules_parallel(self.shard, source_path=self.source,
                                                pool=self.pool, profile=self.profile,
                                                version='par-paged', page=2, lanes=3)
        self.assertEqual(report['rows'], total)
        self.assertEqual(reader.rule_count('par-paged')['rows'], total)

    def test_rules_layer_rows_match_a_fresh_parse(self):
        layers = self.open_layers()
        reader = ShardReader(self.shard)
        rows = reader.rule_rows(layers['rules']['version_id'], 0, 2000)
        # Differential: build the same layer from a clean parse of the same file in
        # a separate shard, then compare row for row.
        clean = Pipeline(self.app.store, self.pool, self.root / 'index-clean', chunk_size=64 * 1024)
        result = clean.run('job-clean', self.source, profile=self.profile)
        clean_shard = Path(result['shard'])
        built = build_rules_layer(clean_shard, source_path=self.source, pool=self.pool,
                                  profile=self.profile, version='compare')
        expected = ShardReader(clean_shard).rule_rows('compare', 0, 2000)
        self.assertEqual(built['rows'], len(expected))
        self.assertEqual([row['ordinal'] for row in rows], [row['ordinal'] for row in expected])
        for row, other in zip(rows, expected):
            self.assertEqual(row['fields'], other['fields'])
            self.assertEqual(row['issue'], other['issue'])
            self.assertEqual(row['payload'], other['payload'])


class PublicationTests(Harness):
    def publish_structure(self, digest=None, rows=None, runtime=RUNTIME):
        staged = self.registry.ensure(self.file_id, 'structure', owner=self.owner,
                                      generation=self.generation.generation,
                                      deps=deps_for('structure', content_sig=self.generation.content_sig,
                                                    generation=self.generation.generation,
                                                    framing='r6-009', runtime=runtime,
                                                    schema='r6-shard/2', encoding='utf-8'))
        result = self.pipeline.run('job-%s' % staged['version_id'], self.source,
                                   file_id=self.file_id, profile=self.profile)
        reader = ShardReader(Path(result['shard']))
        self.shard = Path(result['shard'])
        self.registry.publish(staged['version_id'], rows=reader.count() if rows is None else rows,
                              digest=digest or hashlib.sha256(b'r6').hexdigest(),
                              stats={'parses': 1})
        return staged['version_id'], reader.count()

    def test_coverage_mismatch_does_not_publish(self):
        first, count = self.publish_structure()
        staged = self.registry.ensure(self.file_id, 'structure', owner=self.owner,
                                      generation=self.generation.generation,
                                      deps=deps_for('structure', content_sig=self.generation.content_sig,
                                                    generation=self.generation.generation,
                                                    framing='r6-009', runtime='node-22',
                                                    schema='r6-shard/2', encoding='utf-8'))
        with self.assertRaises(CacheError) as caught:
            self.registry.publish(staged['version_id'], rows=count - 1,
                                  digest=hashlib.sha256(b'x').hexdigest(), expected_rows=count)
        self.assertEqual(caught.exception.code, 'coverage_mismatch')
        pointer = self.registry.pointer(self.file_id, 'structure')
        self.assertEqual(pointer['version_id'], first, 'the old pointer survived')
        self.assertEqual(self.registry.read(self.file_id, 'structure', owner=self.owner)['version_id'], first)
        self.assertEqual(self.registry._row(staged['version_id'])['state'], 'failed')

    def test_a_missing_digest_is_a_failed_publication_not_a_silent_one(self):
        staged = self.registry.ensure(self.file_id, 'structure', owner=self.owner,
                                      generation=self.generation.generation,
                                      deps=self.structure_deps())
        with self.assertRaises(CacheError):
            self.registry.publish(staged['version_id'], rows=1, digest=None)
        self.assertIsNone(self.registry.pointer(self.file_id, 'structure'))

    def test_cancelled_rebuild_leaves_the_previous_version_readable(self):
        first, count = self.publish_structure()
        staged = self.registry.ensure(self.file_id, 'structure', owner=self.owner,
                                      generation=self.generation.generation,
                                      deps=deps_for('structure', content_sig=self.generation.content_sig,
                                                    generation=self.generation.generation,
                                                    framing='r6-010', runtime=RUNTIME,
                                                    schema='r6-shard/2', encoding='utf-8'))
        self.registry.cancel(staged['version_id'])
        self.assertEqual(self.registry.read(self.file_id, 'structure', owner=self.owner)['version_id'], first)
        self.assertEqual(self.registry.pointer(self.file_id, 'structure')['version_id'], first)

    def test_publishing_moves_one_pointer_and_supersedes_the_old_version(self):
        first, count = self.publish_structure()
        pin = self.registry.pin(first, owner=self.owner)
        second, _ = self.publish_structure(digest=hashlib.sha256(b'second').hexdigest(),
                                           runtime='node-20')
        self.assertNotEqual(first, second)
        self.assertEqual(self.registry.pointer(self.file_id, 'structure')['version_id'], second)
        self.assertEqual(self.registry._row(first)['state'], 'superseded')
        # The pinned reader continues on the old version instead of being yanked.
        self.assertEqual(self.registry.read(self.file_id, 'structure', owner=self.owner,
                                            version_id=first)['version_id'], first)
        self.assertTrue(self.registry.unpin(first, pin['token'])['released'])

    def test_a_stale_version_stays_readable_only_while_pinned(self):
        first, count = self.publish_structure()
        pin = self.registry.pin(first, owner=self.owner)
        self.registry.invalidate(self.file_id, 'structure', reason='rules 编辑')
        self.assertIsNone(self.registry.pointer(self.file_id, 'structure'))
        self.assertEqual(self.registry.read(self.file_id, 'structure', owner=self.owner,
                                            version_id=first)['version_id'], first)
        self.registry.unpin(first, pin['token'])
        with self.assertRaises(CacheError) as caught:
            self.registry.read(self.file_id, 'structure', owner=self.owner, version_id=first)
        self.assertEqual(caught.exception.code, 'stale_version')

    def test_gc_collects_only_unpinned_rebuildable_versions(self):
        first, count = self.publish_structure()
        old_raw = self.raw_layer()['version_id']
        self.registry.publish(old_raw, rows=1, digest=hashlib.sha256(b'raw').hexdigest())
        pin = self.registry.pin(first, owner=self.owner)
        # A newer version takes the pointer; `first` becomes superseded but pinned.
        self.publish_structure(digest=hashlib.sha256(b'newer').hexdigest(), runtime='node-20')
        staged = self.registry.ensure(self.file_id, 'structure', owner=self.owner,
                                      generation=self.generation.generation,
                                      deps=deps_for('structure', content_sig='other',
                                                    generation=self.generation.generation,
                                                    framing='r6-009', runtime=RUNTIME,
                                                    schema='r6-shard/2', encoding='utf-8'))
        self.registry.cancel(staged['version_id'])
        report = self.registry.gc()
        self.assertIn(staged['version_id'], report['collected'])
        kept = {item['version']: item['reason'] for item in report['kept']}
        self.assertEqual(kept.get(first), 'pinned')
        self.assertEqual(kept.get(old_raw), 'raw')
        self.assertIn(old_raw, [row['version_id'] for row in self.registry.versions(layer='raw')])
        self.assertTrue(self.registry.unpin(first, pin['token'])['released'])
        after = self.registry.gc()
        self.assertIn(first, after['collected'], 'an unpinned superseded version is collectable')
        self.assertEqual([row['version_id'] for row in self.registry.versions(layer='raw')], [old_raw])


class IsolationTests(Harness):
    def test_another_principal_gets_the_same_not_found_as_a_missing_file(self):
        self.open_layers()
        with self.assertRaises(CacheError) as foreign:
            self.registry.read(self.file_id, 'structure', owner='viewer@10.0.0.9')
        with self.assertRaises(CacheError) as missing:
            self.registry.read('file-does-not-exist', 'structure', owner='viewer@10.0.0.9')
        self.assertEqual(foreign.exception.code, missing.exception.code)
        self.assertEqual(foreign.exception.message, missing.exception.message)
        # Same shape, so the response cannot be used to tell "exists but not yours"
        # from "does not exist".
        self.assertEqual(set(foreign.exception.details), set(missing.exception.details))
        self.assertEqual(self.registry.counters['leaks_blocked'], 1)

    def test_a_second_principal_cannot_reuse_the_first_one_s_cache_entry(self):
        first = self.open_layers()
        other = 'viewer@10.0.0.9'
        staged = self.registry.ensure(self.file_id, 'structure', owner=other,
                                      generation=self.generation.generation,
                                      deps=self.structure_deps())
        self.assertFalse(staged['reused'], 'a cache entry is per principal')
        self.assertNotEqual(staged['version_id'], first['structure']['version_id'])
        with self.assertRaises(CacheError):
            self.registry.pin(first['structure']['version_id'], owner=other)
        self.assertEqual([row['owner'] for row in self.registry.versions(
            file_id=self.file_id, owner=other)], [other])

    def test_revocation_terminates_reads(self):
        layers = self.open_layers()
        pin = self.registry.pin(layers['structure']['version_id'], owner=self.owner)
        report = self.registry.revoke(self.owner)
        self.assertGreaterEqual(report['pins'], 1)
        for layer in ('structure', 'rules', 'search'):
            with self.assertRaises(CacheError) as caught:
                self.registry.read(self.file_id, layer, owner=self.owner)
            self.assertEqual(caught.exception.code, 'not_found')
        with self.assertRaises(CacheError):
            self.registry.read(self.file_id, 'structure', owner=self.owner,
                               version_id=layers['structure']['version_id'])
        self.assertIsNone(self.registry.pointer(self.file_id, 'structure'))
        self.assertTrue(pin['token'])


class MetricsTests(Harness):
    def test_metrics_report_versions_and_reuse(self):
        self.open_layers()
        self.open_layers()
        metrics = self.registry.metrics()
        self.assertGreaterEqual(metrics['counters']['reused'], 3)
        self.assertEqual(metrics['layers']['structure']['states'].get('complete'), 1)
        self.assertEqual(metrics['layers']['rules']['states'].get('complete'), 1)

    def test_events_record_the_publication_trail(self):
        layers = self.open_layers()
        events = [item['event'] for item in self.registry.events(layers['structure']['version_id'])]
        self.assertEqual(events[:2], ['staged', 'published'])
        self.assertIn('published', events)


if __name__ == '__main__':
    unittest.main()
