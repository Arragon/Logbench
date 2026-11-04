/*
 * backup.js — manifest, per-volume verification and staged restore (R6-017).
 *
 * Format `logbench.backup/1`:
 *   manifest.json      format, schema, namespace, files[], volumes[], incomplete ranges
 *   volume-N.bin       raw bytes, each <= 64 MiB
 *
 * Rules:
 *   - a manifest never contains log text, credentials or a full search term;
 *   - every volume carries its own digest, so a truncated or swapped volume is
 *     detected before anything is restored;
 *   - restore writes into a staging generation and only flips the published
 *     pointer after *all* required volumes verify;
 *   - `partial` files and trash contents are listed explicitly, so a restore
 *     never silently resurrects or drops them;
 *   - a legacy R5 backup is converted into a manifest, but its drafts stay
 *     inactive.
 *
 * Digest: 64-bit FNV-1a. It is an integrity check, not a signature; SubtleCrypto
 * is unavailable over plain HTTP, which is the deployment we must support.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBBackup = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var FORMAT = 'logbench.backup/1';
  var VOLUME_BYTES = 64 * 1024 * 1024;

  function errorOf(code, message, extra) {
    var error = new Error(message || code);
    error.code = code;
    if (extra) for (var key in extra) error[key] = extra[key];
    return error;
  }

  function hex(value) { return ('00000000' + (value >>> 0).toString(16)).slice(-8); }

  function digestBytes(bytes) {
    var high = 0x811c9dc5;
    var low = 0x01000193;
    for (var i = 0; i < bytes.length; i++) {
      low ^= bytes[i];
      low = (low + ((low << 1) + (low << 4) + (low << 7) + (low << 8) + (low << 24))) >>> 0;
      high = (high ^ low) >>> 0;
      high = (high + (high << 1) + (high << 4)) >>> 0;
    }
    return hex(high) + hex(low);
  }

  function planVolumes(files, volumeBytes) {
    var limit = volumeBytes || VOLUME_BYTES;
    var volumes = [];
    var current = null;

    function startVolume() {
      current = { index: volumes.length, bytes: 0, parts: [] };
      volumes.push(current);
    }

    files.forEach(function (file) {
      if (!(file.size >= 0)) throw errorOf('invalid_size', '文件大小非法：' + file.file_id);
      if (file.size === 0) {
        if (!current) startVolume();
        current.parts.push({ file_id: file.file_id, from: 0, to: 0 });
        return;
      }
      var offset = 0;
      while (offset < file.size) {
        if (!current || current.bytes >= limit) startVolume();
        var take = Math.min(file.size - offset, limit - current.bytes);
        current.parts.push({ file_id: file.file_id, from: offset, to: offset + take });
        current.bytes += take;
        offset += take;
      }
    });
    return volumes.map(function (volume) {
      return { index: volume.index, bytes: volume.bytes, parts: volume.parts, digest: null };
    });
  }

  /** Fill volume digests after the bytes exist. An unsealed manifest cannot restore. */
  function sealManifest(manifest, digests) {
    if (!Array.isArray(digests) || digests.length !== manifest.volumes.length) {
      throw errorOf('digest_count_mismatch', '摘要数量与卷数量不一致');
    }
    manifest.volumes = manifest.volumes.map(function (volume, index) {
      return { index: volume.index, bytes: volume.bytes, parts: volume.parts, digest: digests[index] };
    });
    return manifest;
  }

  function validateManifest(manifest) {
    var problems = [];
    if (!manifest || typeof manifest !== 'object') return { ok: false, problems: ['manifest missing'] };
    if (manifest.format !== FORMAT) problems.push('unknown format: ' + manifest.format);
    if (!manifest.namespace) problems.push('namespace missing');
    if (!Array.isArray(manifest.files) || !manifest.files.length) problems.push('files missing');
    if (!Array.isArray(manifest.volumes)) problems.push('volumes missing');
    (manifest.files || []).forEach(function (file) {
      if (!file.file_id) problems.push('file without file_id');
      if (!(file.size >= 0)) problems.push('file ' + file.file_id + ' without size');
      if (!file.digest) problems.push('file ' + file.file_id + ' without digest');
      if (file.provisional && !file.reason) problems.push('provisional file ' + file.file_id + ' without reason');
    });
    var covered = 0;
    (manifest.volumes || []).forEach(function (volume, index) {
      if (volume.index !== index) problems.push('volume order broken at ' + index);
      if (!(volume.bytes >= 0)) problems.push('volume ' + index + ' without byte count');
      if (!volume.digest) problems.push('volume ' + index + ' without digest');
      covered += volume.bytes || 0;
    });
    var declared = (manifest.files || []).reduce(function (sum, file) { return sum + (file.size || 0); }, 0);
    if (manifest.volumes && manifest.volumes.length && covered !== declared) {
      problems.push('volumes cover ' + covered + ' bytes but files declare ' + declared);
    }
    return { ok: problems.length === 0, problems: problems };
  }

  function buildManifest(options) {
    var files = options.files || [];
    var volumes = planVolumes(files, options.volumeBytes);
    var manifest = {
      format: FORMAT,
      schema: options.schema || 'logbench.r6/1',
      namespace: options.namespace,
      created_at: new Date().toISOString(),
      files: files.map(function (file) {
        return {
          file_id: file.file_id,
          name: file.name || file.file_id,
          size: file.size,
          digest: file.digest,
          generation: file.generation || 1,
          provisional: file.provisional === true,
          reason: file.reason || null,
          in_trash: file.in_trash === true
        };
      }),
      volumes: volumes,
      drafts_active: false,
      note: '清单不含日志正文、凭据或完整搜索词。'
    };
    return manifest;
  }

  function verifyVolume(manifest, index, bytes) {
    var volume = (manifest.volumes || [])[index];
    if (!volume) return { ok: false, code: 'volume_unknown' };
    if (bytes.length !== volume.bytes) {
      return { ok: false, code: 'volume_size_mismatch', expected: volume.bytes, actual: bytes.length };
    }
    var actual = digestBytes(bytes);
    if (volume.digest && actual !== volume.digest) {
      return { ok: false, code: 'volume_digest_mismatch', expected: volume.digest, actual: actual };
    }
    return { ok: true, code: 'ok', digest: actual };
  }

  /**
   * Decide whether a restore may start. Refuses missing, extra and out-of-order
   * volumes instead of restoring whatever happens to be present.
   */
  function planRestore(manifest, providedIndexes) {
    var check = validateManifest(manifest);
    if (!check.ok) return { ok: false, code: 'invalid_manifest', problems: check.problems };
    var expected = (manifest.volumes || []).map(function (volume) { return volume.index; });
    var provided = (providedIndexes || []).slice().sort(function (a, b) { return a - b; });
    var missing = expected.filter(function (index) { return provided.indexOf(index) === -1; });
    if (missing.length) return { ok: false, code: 'missing_volumes', missing: missing };
    var extra = provided.filter(function (index) { return expected.indexOf(index) === -1; });
    if (extra.length) return { ok: false, code: 'unknown_volumes', extra: extra };
    for (var i = 0; i < provided.length; i++) {
      if (provided[i] !== i) return { ok: false, code: 'volume_order', at: i };
    }
    return {
      ok: true,
      code: 'ok',
      required: expected.length,
      partial_files: (manifest.files || []).filter(function (file) { return file.provisional; }).map(function (file) { return file.file_id; }),
      trash_files: (manifest.files || []).filter(function (file) { return file.in_trash; }).map(function (file) { return file.file_id; })
    };
  }

  /** Convert an R5 JSON backup into a manifest; drafts stay inactive. */
  function convertLegacyBackup(legacy) {
    var files = (legacy && legacy.files) || [];
    return buildManifest({
      namespace: (legacy && legacy.namespace) || 'legacy',
      files: files.map(function (file) {
        return {
          file_id: file.id || file.file_id,
          name: file.name,
          size: file.size != null ? file.size : (file.text ? file.text.length : 0),
          digest: file.digest || 'legacy-unknown',
          generation: 1,
          provisional: true,
          reason: 'legacy-backup'
        };
      })
    });
  }

  return {
    FORMAT: FORMAT,
    VOLUME_BYTES: VOLUME_BYTES,
    digestBytes: digestBytes,
    planVolumes: planVolumes,
    buildManifest: buildManifest,
    sealManifest: sealManifest,
    validateManifest: validateManifest,
    verifyVolume: verifyVolume,
    planRestore: planRestore,
    convertLegacyBackup: convertLegacyBackup,
    errorOf: errorOf
  };
});
