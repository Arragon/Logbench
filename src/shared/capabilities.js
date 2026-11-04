/*
 * capabilities.js — browser capability detection for R6.
 *
 * Pure function over an injected environment so it can be unit tested in Node
 * without a browser. OPFS requires a secure context; when it is missing the
 * local reading path must keep working, so this never reports a fatal error.
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBCapabilities = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  function entry(state, reason, extra) {
    var payload = { state: state, reason: reason || null };
    if (extra) for (var key in extra) payload[key] = extra[key];
    return payload;
  }

  function detect(env) {
    env = env || (typeof window !== 'undefined' ? window : {});
    var secure = env.isSecureContext === true;
    var nav = env.navigator || {};
    var hasIdb = false;
    try {
      hasIdb = !!env.indexedDB && typeof env.indexedDB.open === 'function';
    } catch (error) {
      hasIdb = false;
    }
    var hasStorageManager = !!nav.storage && typeof nav.storage.getDirectory === 'function';
    var hasWorker = typeof env.Worker === 'function';
    var hasBlob = typeof env.Blob === 'function' && typeof env.Blob.prototype.slice === 'function';

    return {
      secure_context: secure,
      indexeddb: entry(
        hasIdb ? 'enabled' : 'disabled',
        hasIdb ? null : 'IndexedDB 不可用：本机导入与缓存将无法保存，阅读仍可进行'
      ),
      opfs: entry(
        hasStorageManager && secure ? 'enabled' : 'unavailable',
        hasStorageManager && secure
          ? null
          : secure
            ? '当前浏览器未提供 OPFS，大文件缓存退回 IndexedDB'
            : '非安全上下文（私网 HTTP），OPFS 按规范不可用；不影响本机阅读'
      ),
      worker: entry(hasWorker ? 'enabled' : 'disabled',
        hasWorker ? null : 'Worker 不可用：搜索将退化为主线程执行，可能卡顿'),
      blob_slice: entry(hasBlob ? 'enabled' : 'disabled',
        hasBlob ? null : 'Blob.slice 不可用：无法按字节范围流式读取大文件'),
      storage_estimate: entry(
        nav.storage && typeof nav.storage.estimate === 'function' ? 'enabled' : 'unavailable',
        null
      )
    };
  }

  /* A feature is blocked only by the capabilities it actually needs. */
  function blockers(caps) {
    var out = [];
    if (caps.indexeddb && caps.indexeddb.state !== 'enabled') out.push('indexeddb');
    if (caps.blob_slice && caps.blob_slice.state !== 'enabled') out.push('blob_slice');
    return out;
  }

  return { detect: detect, blockers: blockers };
});
