/*
 * backup.test.cjs — R6-017 manifest, volume and restore-plan semantics.
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');

const backup = require(path.resolve(__dirname, '..', '..', 'src', 'shared', 'backup.js'));

const MiB = 1024 * 1024;

function files(sizes) {
  return sizes.map((size, index) => ({
    file_id: 'f' + (index + 1),
    name: 'f' + (index + 1) + '.log',
    size,
    digest: 'digest' + (index + 1),
    generation: 1
  }));
}

function sealedManifest(sizes, volumeBytes) {
  const manifest = backup.buildManifest({ namespace: 'ns', files: files(sizes), volumeBytes });
  const digests = manifest.volumes.map((volume, index) => 'v' + index + '-' + volume.bytes);
  return backup.sealManifest(manifest, digests);
}

test('volumes never exceed the 64 MiB cap', () => {
  const manifest = sealedManifest([100 * MiB, 30 * MiB], 64 * MiB);
  assert.ok(manifest.volumes.length >= 3);
  for (const volume of manifest.volumes) assert.ok(volume.bytes <= 64 * MiB, String(volume.bytes));
  const total = manifest.volumes.reduce((sum, volume) => sum + volume.bytes, 0);
  assert.equal(total, 130 * MiB);
});

test('a file is split across volumes without losing a byte', () => {
  const manifest = sealedManifest([130 * MiB], 64 * MiB);
  const parts = manifest.volumes.flatMap(volume => volume.parts);
  assert.equal(parts.length, 3);
  assert.deepEqual(parts.map(p => [p.from, p.to]), [[0, 64 * MiB], [64 * MiB, 128 * MiB], [128 * MiB, 130 * MiB]]);
});

test('an empty file still appears in the manifest', () => {
  const manifest = backup.buildManifest({ namespace: 'ns', files: files([0]), volumeBytes: MiB });
  assert.equal(manifest.volumes.length, 1);
  assert.deepEqual(manifest.volumes[0].parts, [{ file_id: 'f1', from: 0, to: 0 }]);
});

test('an unsealed manifest is refused for restore', () => {
  const manifest = backup.buildManifest({ namespace: 'ns', files: files([10]), volumeBytes: MiB });
  const check = backup.validateManifest(manifest);
  assert.equal(check.ok, false);
  assert.ok(check.problems.some(problem => problem.includes('without digest')));
});

test('a sealed manifest is accepted', () => {
  const manifest = sealedManifest([10, 20], MiB);
  assert.deepEqual(backup.validateManifest(manifest), { ok: true, problems: [] });
});

test('volume verification catches truncation and tampering', () => {
  const manifest = sealedManifest([8], MiB);
  const bytes = new Uint8Array(8);
  manifest.volumes[0].digest = backup.digestBytes(bytes);
  assert.equal(backup.verifyVolume(manifest, 0, bytes).ok, true);
  assert.equal(backup.verifyVolume(manifest, 0, bytes.subarray(0, 4)).code, 'volume_size_mismatch');
  const tampered = new Uint8Array(8);
  tampered[3] = 1;
  assert.equal(backup.verifyVolume(manifest, 0, tampered).code, 'volume_digest_mismatch');
  assert.equal(backup.verifyVolume(manifest, 9, bytes).code, 'volume_unknown');
});

test('restore is refused when a volume is missing, extra or out of order', () => {
  const manifest = sealedManifest([130 * MiB], 64 * MiB);
  assert.equal(backup.planRestore(manifest, [0, 1, 2]).ok, true);
  const missing = backup.planRestore(manifest, [0, 2]);
  assert.equal(missing.code, 'missing_volumes');
  assert.deepEqual(missing.missing, [1]);
  assert.equal(backup.planRestore(manifest, [0, 1, 2, 3]).code, 'unknown_volumes');
  assert.equal(backup.planRestore(manifest, [1, 0, 2]).ok, true, 'input order is normalised');
  const broken = sealedManifest([130 * MiB], 64 * MiB);
  broken.volumes = [broken.volumes[0], { ...broken.volumes[1], index: 5 }, broken.volumes[2]];
  assert.equal(backup.planRestore(broken, [0, 1, 2]).code, 'invalid_manifest');
});

test('partial and trashed files are surfaced, not silently restored', () => {
  const manifest = backup.buildManifest({
    namespace: 'ns',
    files: [
      { file_id: 'a', size: 10, digest: 'd1' },
      { file_id: 'b', size: 10, digest: 'd2', provisional: true, reason: 'truncated-tail' },
      { file_id: 'c', size: 10, digest: 'd3', in_trash: true }
    ],
    volumeBytes: MiB
  });
  backup.sealManifest(manifest, manifest.volumes.map(v => 'x'));
  const plan = backup.planRestore(manifest, [0]);
  assert.equal(plan.ok, true);
  assert.deepEqual(plan.partial_files, ['b']);
  assert.deepEqual(plan.trash_files, ['c']);
});

test('a provisional file without a reason is rejected', () => {
  const manifest = backup.buildManifest({
    namespace: 'ns', files: [{ file_id: 'a', size: 1, digest: 'd', provisional: true }], volumeBytes: MiB
  });
  backup.sealManifest(manifest, ['x']);
  const check = backup.validateManifest(manifest);
  assert.equal(check.ok, false);
  assert.ok(check.problems.some(problem => problem.includes('without reason')));
});

test('legacy R5 backups convert to a manifest with drafts inactive', () => {
  const legacy = {
    namespace: 'old',
    drafts: { rule: { name: '草稿' } },
    files: [{ id: 'f1', name: 'a.log', size: 12, text: 'hello world!' }]
  };
  const manifest = backup.convertLegacyBackup(legacy);
  assert.equal(manifest.format, backup.FORMAT);
  assert.equal(manifest.drafts_active, false);
  assert.equal(manifest.files[0].provisional, true);
  assert.equal(manifest.files[0].reason, 'legacy-backup');
  assert.equal(manifest.files[0].size, 12);
});

test('the manifest never carries log text or credentials', () => {
  const manifest = sealedManifest([10], MiB);
  const serialized = JSON.stringify(manifest);
  assert.ok(!serialized.includes('text'));
  assert.ok(!serialized.includes('password'));
  assert.ok(manifest.note.includes('不含日志正文'));
});
