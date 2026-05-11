"""Redis-backed event bus (Day 8, Phase 2).

Production publish path. The ingestion handler still calls a `publish`
method on an `EventBus` Protocol — only the implementation changes when
`REDIS_URL` is set in the environment. The wire payload is the same
envelope shape `envelope_for(event)` produces (see `event_bus.py`),
JSON-encoded for Redis Pub/Sub.

Why Pub/Sub and not Streams: for Day 8 the orchestrator only needs
fire-and-forget delivery of newly-created events (replays don't republish,
per the audit invariant). Streams give us replayability and consumer-group
ordering — those become valuable in Phase 4 hardening when we want
at-least-once delivery and offset tracking. Pub/Sub keeps the Day-8 wire
contract dead simple; switching to Streams later is one method on this
class plus a parallel listener path.

The `redis` SDK import is lazy — the test path that uses
`InMemoryEventBus` doesn't need the package at all, and the orchestrator
unit tests inject a fake client that satisfies the small surface we use
(`publish(channel, payload) -> int`).
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable

# Sentinel so docker-compose's empty-string default doesn't get mistaken
# for a configured URL. The dispatch in `orchestrator/api.py` and the
# context-engine ingestion wiring both treat empty string as "unset".
DEFAULT_REDIS_URL = "redis://localhost:6379/0"


@runtime_checkable
class RedisClientLike(Protocol):
    """The Pub/Sub publish surface we depend on.

    `redis.Redis` satisfies this; tests inject a fake. Keeping the surface
    this narrow means a future swap to a different client (e.g. an
    aioredis facade behind a sync shim) is a one-class change.
    """

    def publish(self, channel: str, message: str) -> int: ...

    def ping(self) -> bool: ...

    def close(self) -> None: ...


class RedisEventBus:
    """`EventBus` implementation backed by Redis Pub/Sub.

    Holds a single `redis.Redis` connection (the SDK pools internally) and
    JSON-encodes every envelope before publish. The envelope contract lives
    in `event_bus.envelope_for` — keep both sides in sync if you change it.
    """

    def __init__(self, client: RedisClientLike) -> None:
        self._client = client

    def publish(self, channel: str, payload: dict[str, Any]) -> int:
        """Publish a JSON-encoded envelope. Returns the Redis-reported
        subscriber count (0 if no orchestrator is listening — which is
        legal: the event is still durably stored in Postgres and the
        Day-23 outbox pattern will replay missed events).
        """
        message = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        return self._client.publish(channel, message)

    def close(self) -> None:
        self._client.close()


def make_redis_event_bus(url: str) -> RedisEventBus:
    """Construct a `RedisEventBus` from a connection URL.

    Lazy import keeps the unit-test path psycopg-style — only paths that
    explicitly opt into Redis pay the import cost.
    """
    import redis  # noqa: PLC0415 — intentionally lazy

    client = redis.Redis.from_url(url, decode_responses=True)
    return RedisEventBus(client)
