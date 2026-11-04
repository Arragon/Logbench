/*
 * leases.js — cross-tab write lease with fencing tokens (R6-015).
 *
 * Two tabs must not both write. The lease lives in IndexedDB, and every check
 * happens inside the same transaction as the write it guards — checking at task
 * start and trusting it later is exactly the bug this prevents.
 *
 *   acquire  compare owner + expiry inside one readwrite transaction, then bump
 *            `fencing`; the previous owner's token becomes stale immediately
 *   renew    only the current owner with the current token can extend
 *   assert   called by every commit; a stale token is refused with `stale_fencing`
 *   commit   verify + write in one transaction, so a takeover cannot interleave
 *
 * BroadcastChannel is only a notification convenience: correctness never depends
 * on it, nor on Web Locks (unavailable outside secure contexts).
 */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LBLeases = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var KEY_PREFIX = 'lease:';
  var DEFAULT_TTL = 15000;

  function errorOf(code, message, extra) {
    var error = new Error(message || code);
    error.code = code;
    if (extra) for (var key in extra) error[key] = extra[key];
    return error;
  }

  function leaseKey(taskId) { return KEY_PREFIX + taskId; }

  /* Pure: is this lease still usable by `owner` with `fencing`? */
  function inspect(record, owner, fencing, now) {
    var time = now == null ? Date.now() : now;
    if (!record || !record.value) return { state: 'free', usable: false };
    var value = record.value;
    if (value.released || value.expires_at <= time) {
      return { state: 'expired', usable: false, holder: value.owner, fencing: value.fencing };
    }
    if (fencing != null && value.fencing !== fencing) {
      return { state: 'stale', usable: false, holder: value.owner, current_fencing: value.fencing };
    }
    if (owner != null && value.owner !== owner) {
      return { state: 'held', usable: false, holder: value.owner };
    }
    return { state: 'held-by-me', usable: true, holder: value.owner, fencing: value.fencing };
  }

  function createLeaseClient(store) {
    if (!store || !store.database) throw errorOf('no_store', '租约需要原生 IndexedDB 连接');
    var db = store.database;

    function readInTx(taskId) {
      var tx = db.transaction('task_checkpoints', 'readonly');
      var request = tx.objectStore('task_checkpoints').get([store.namespace, leaseKey(taskId)]);
      return new Promise(function (resolve, reject) {
        request.onsuccess = function () { resolve(request.result || null); };
        request.onerror = function () { reject(errorOf('storage_error', '读取租约失败')); };
      });
    }

    /**
     * Atomic compare-and-set. `apply` runs inside the same transaction and can
     * add writes; if it throws the transaction aborts and nothing changes.
     */
    function transact(taskId, work) {
      var tx = db.transaction('task_checkpoints', 'readwrite');
      var store2 = tx.objectStore('task_checkpoints');
      var done = new Promise(function (resolve, reject) {
        tx.oncomplete = function () { resolve(true); };
        tx.onabort = function () { reject(tx.error && tx.error.code ? tx.error : errorOf('transaction_aborted', '租约事务被中止')); };
        tx.onerror = function () { reject(errorOf('storage_error', '租约事务失败')); };
      });
      var outcome;
      var failure = null;
      var request = store2.get([store.namespace, leaseKey(taskId)]);
      request.onsuccess = function () {
        try {
          outcome = work(request.result || null, store2, taskId);
        } catch (error) {
          failure = error;
          try { tx.abort(); } catch (ignored) { /* already done */ }
        }
      };
      request.onerror = function () {
        failure = errorOf('storage_error', '读取租约失败');
        try { tx.abort(); } catch (ignored) { /* noop */ }
      };
      return done.then(function () {
        if (failure) throw failure;
        return outcome;
      }, function (error) {
        throw failure || error;
      });
    }

    function putLease(objectStore, taskId, value) {
      objectStore.put({ namespace: store.namespace, task_id: leaseKey(taskId), value: value, updated_at: Date.now() });
    }

    function acquire(taskId, owner, ttlMs) {
      var ttl = ttlMs || DEFAULT_TTL;
      return transact(taskId, function (record, objectStore) {
        var now = Date.now();
        var state = inspect(record, null, null, now);
        if (state.state === 'held' || state.state === 'held-by-me') {
          throw errorOf('lease_held', '另一个页面正在写入该任务', { holder: state.holder });
        }
        var previous = record && record.value ? record.value : null;
        var value = {
          owner: owner,
          fencing: (previous ? previous.fencing : 0) + 1,
          expires_at: now + ttl,
          acquired_at: now
        };
        putLease(objectStore, taskId, value);
        return { owner: owner, fencing: value.fencing, expires_at: value.expires_at };
      });
    }

    function renew(taskId, owner, fencing, ttlMs) {
      var ttl = ttlMs || DEFAULT_TTL;
      return transact(taskId, function (record, objectStore) {
        var state = inspect(record, owner, fencing, Date.now());
        if (!state.usable) {
          throw errorOf(state.state === 'stale' ? 'stale_fencing' : 'lease_lost',
            '租约已失效或被接管', { state: state.state, holder: state.holder });
        }
        var value = record.value;
        value.expires_at = Date.now() + ttl;
        putLease(objectStore, taskId, value);
        return { owner: owner, fencing: value.fencing, expires_at: value.expires_at };
      });
    }

    function release(taskId, owner, fencing) {
      return transact(taskId, function (record, objectStore) {
        if (!record || !record.value) return { released: false, reason: 'free' };
        var state = inspect(record, owner, fencing, Date.now());
        if (state.state !== 'held-by-me' && state.state !== 'expired') {
          throw errorOf('stale_fencing', '当前租约不属于你，无法释放', { holder: state.holder });
        }
        // Keep the record so the fencing counter stays monotonic: releasing is not
        // a reset, otherwise a fresh tab could reuse a token that already wrote.
        var value = record.value;
        value.owner = null;
        value.released = true;
        value.expires_at = 0;
        putLease(objectStore, taskId, value);
        return { released: true, fencing: value.fencing };
      });
    }

    /** Verify the token and apply writes in one transaction. */
    function commit(taskId, owner, fencing, operations) {
      return transact(taskId, function (record, objectStore, id) {
        var state = inspect(record, owner, fencing, Date.now());
        if (!state.usable) {
          throw errorOf(state.state === 'stale' ? 'stale_fencing' : 'lease_lost',
            '写入者已被接管，本次提交被拒绝', { state: state.state, holder: state.holder });
        }
        (operations || []).forEach(function (operation) {
          objectStore.put({
            namespace: store.namespace,
            task_id: operation.task_id,
            value: operation.value,
            updated_at: Date.now()
          });
        });
        return { committed: (operations || []).length, fencing: state.fencing };
      });
    }

    function status(taskId) {
      return readInTx(taskId).then(function (record) {
        if (!record || !record.value || record.value.released) return { state: 'free' };
        var value = record.value;
        return {
          state: value.expires_at <= Date.now() ? 'expired' : 'held',
          owner: value.owner,
          fencing: value.fencing,
          expires_at: value.expires_at
        };
      });
    }

    /**
     * Advisory notification only: waiting tabs still poll status()/retry acquire().
     * Absence of BroadcastChannel must not change correctness.
     */
    function announce(channelFactory, taskId, message) {
      try {
        if (!channelFactory) return null;
        var channel = channelFactory('logbench-lease-' + taskId);
        channel.postMessage(message);
        return channel;
      } catch (error) {
        return null;
      }
    }

    return {
      acquire: acquire,
      renew: renew,
      release: release,
      commit: commit,
      status: status,
      announce: announce
    };
  }

  return {
    KEY_PREFIX: KEY_PREFIX,
    DEFAULT_TTL: DEFAULT_TTL,
    leaseKey: leaseKey,
    inspect: inspect,
    createLeaseClient: createLeaseClient
  };
});
