"""One reusable event stream per browser (R6-074).

Design decisions that matter more than the code:

* **one connection per principal, not per file.** A hundred files do not open a
  hundred streams; the client routes by `fileId` inside the event.
* **events are small references.** Watermarks, record versions, gap markers and
  error codes travel; log bodies never do.
* **replay is bounded and honest.** `Last-Event-ID` replays from the persisted log;
  if that id has aged out the answer is `resync_required`, not a silent gap that
  looks like "nothing happened".
* **a slow consumer is dropped, never allowed to backpressure collection.** The
  subscription queue is bounded; overflow closes the subscription and the client is
  told to resync.
* **scope decides visibility.** Publishing happens with `shared` (administrator
  visibility) or `members`; a subscriber only receives the scopes its role allows,
  and revocation closes the stream on the next authorisation check.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

RETENTION = 500
TTL = 900
QUEUE = 200


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def scopes_for(actor: dict) -> list:
    if actor.get('role') == 'admin':
        return ['shared', 'members']
    if actor.get('role') == 'member':
        return ['members']
    return []


@dataclass
class Subscription:
    scopes: list
    queue: 'queue.Queue' = field(default_factory=lambda: queue.Queue(maxsize=QUEUE))
    dropped: bool = False
    closed: bool = False

    def push(self, event: dict) -> bool:
        if self.closed:
            return False
        try:
            self.queue.put_nowait(event)
            return True
        except queue.Full:
            # Do not block the producer: this client is too slow to keep.
            self.dropped = True
            return False

    def close(self) -> None:
        self.closed = True
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            pass

    def next(self, timeout: float) -> Optional[dict]:
        try:
            item = self.queue.get(timeout=timeout)
        except queue.Empty:
            return None
        return item


class EventBroker:
    def __init__(self, store, *, retention: int = RETENTION, ttl: float = TTL,
                 clock=time.time):
        self.store = store
        self.retention = max(20, int(retention))
        self.ttl = float(ttl)
        self.clock = clock
        self._lock = threading.Lock()
        self._subs: list = []
        self.published = 0

    # ------------------------------------------------------------------ publish
    def publish(self, *, kind: str, payload: dict, scope: str = 'shared') -> dict:
        stamp = _now()
        expires = self.clock() + self.ttl
        with self.store.transaction() as db:
            cursor = db.execute('INSERT INTO event_log(scope,kind,payload,created_at,expires_at) '
                                'VALUES(?,?,?,?,?)',
                                (scope, kind, json.dumps(payload, ensure_ascii=False),
                                 stamp, expires))
            event_id = int(cursor.lastrowid)
            # Bounded: keep the newest N per scope so a long-running deployment does
            # not grow this table forever.
            db.execute('DELETE FROM event_log WHERE id IN (SELECT id FROM event_log '
                       'WHERE scope=? ORDER BY id DESC LIMIT -1 OFFSET ?)', (scope, self.retention))
            db.execute('DELETE FROM event_log WHERE expires_at<?', (self.clock(),))
        event = {'id': event_id, 'kind': kind, 'scope': scope, 'payload': payload, 'at': stamp}
        with self._lock:
            self.published += 1
            subs = list(self._subs)
        for subscription in subs:
            if scope in subscription.scopes:
                subscription.push(event)
        return event

    # ------------------------------------------------------------------- replay
    def replay(self, scopes: list, last_event_id: Optional[int] = None,
               *, limit: int = 200) -> dict:
        if not scopes:
            return {'events': [], 'resync_required': False, 'reason': 'no scope'}
        marks = ','.join('?' for _ in scopes)
        params = list(scopes)
        sql = ('SELECT id,scope,kind,payload,created_at FROM event_log WHERE scope IN (%s)'
               % marks)
        if last_event_id is not None:
            sql += ' AND id>?'
            params.append(int(last_event_id))
        sql += ' ORDER BY id DESC LIMIT ?'
        params.append(int(limit))
        with self.store.connect() as db:
            rows = db.execute(sql, params).fetchall()
            oldest = db.execute('SELECT MIN(id) AS first FROM event_log WHERE scope IN (%s)'
                                % marks, scopes).fetchone()
        events = [{'id': int(row['id']), 'kind': row['kind'], 'scope': row['scope'],
                   'payload': json.loads(row['payload']), 'at': row['created_at']}
                  for row in reversed(rows)]
        resync = False
        reason = None
        if last_event_id is not None and oldest and oldest['first'] is not None:
            if int(last_event_id) + 1 < int(oldest['first']) and last_event_id < int(oldest['first']):
                # The requested position has been pruned: the client must take a
                # fresh snapshot instead of assuming nothing happened in between.
                resync = True
                reason = 'event_retention_exceeded'
        return {'events': events, 'resync_required': resync, 'reason': reason,
                'lastEventId': events[-1]['id'] if events else last_event_id}

    def poll(self, actor: dict, last_event_id: Optional[int] = None) -> dict:
        return self.replay(scopes_for(actor), last_event_id)

    # -------------------------------------------------------------- subscribe
    def attach(self, actor: dict) -> Subscription:
        subscription = Subscription(scopes_for(actor))
        with self._lock:
            self._subs.append(subscription)
            # Bound the number of subscribers so idle tabs cannot exhaust the pool.
            while len(self._subs) > 64:
                victim = self._subs.pop(0)
                victim.close()
        return subscription

    def detach(self, subscription: Subscription) -> None:
        with self._lock:
            if subscription in self._subs:
                self._subs.remove(subscription)
        subscription.close()

    def snapshot(self) -> dict:
        with self._lock:
            return {'subscribers': len(self._subs), 'published': self.published,
                    'retention': self.retention}

    # -------------------------------------------------------------------- SSE
    def stream(self, actor: dict, *, last_event_id: Optional[int] = None,
               heartbeat: float = 15.0, lifetime: float = 600.0,
               authorize=None):
        """Yield SSE frames. `authorize` is re-checked on every heartbeat."""
        scopes = scopes_for(actor)
        if not scopes:
            yield 'event: error\ndata: {"code":"forbidden"}\n\n'
            return
        replay = self.replay(scopes, last_event_id)
        if replay['resync_required']:
            yield 'event: resync_required\ndata: %s\n\n' % json.dumps(
                {'reason': replay['reason']}, ensure_ascii=False)
        for event in replay['events']:
            yield _frame(event)
        subscription = self.attach(actor)
        started = self.clock()
        try:
            yield ': connected\n\n'
            while self.clock() - started < lifetime:
                if authorize is not None and not authorize():
                    yield 'event: revocation\ndata: {"code":"acl_changed"}\n\n'
                    return
                event = subscription.next(timeout=heartbeat)
                if subscription.dropped:
                    yield 'event: resync_required\ndata: {"reason":"slow_consumer"}\n\n'
                    return
                if event is None:
                    yield ': keep-alive\n\n'
                    continue
                yield _frame(event)
        finally:
            self.detach(subscription)


def _frame(event: dict) -> str:
    # `id` travels in the body as well as in the `id:` line: the browser exposes the
    # header only as `lastEventId`, and a polling client has no header at all, so the
    # ordering key must be part of the payload too.
    return 'id: %d\nevent: %s\ndata: %s\n\n' % (
        event['id'], event['kind'],
        json.dumps({'id': event['id'], 'kind': event['kind'], 'at': event['at'],
                    **event['payload']}, ensure_ascii=False))
