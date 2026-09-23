"""An in-memory Redis covering exactly the commands the seeder writes with and the service's
`CalibrationStore` reads with.

One `FakeRedis` holds the data; `.sync()` is the seeder's synchronous client and `.aio()` the
service's `redis.asyncio` client, so a test can seed through one and load through the other. Reads
return `bytes`, as a real client without `decode_responses` does, so the reader's decoding is
exercised too. Pipelines queue commands and apply them in one step on `execute()`, which is also
what `round_trips` counts.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterator, Mapping
from typing import Any, Self


class FakeRedis:
    def __init__(self) -> None:
        self.strings: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.sets: dict[str, set[str]] = {}
        self.round_trips = 0

    # -- state helpers ---------------------------------------------------------------------

    def all_keys(self) -> set[str]:
        return set(self.strings) | set(self.hashes) | set(self.sets)

    def snapshot(self) -> dict[str, Any]:
        """Every key's value, comparable across seeding runs."""
        return {
            "strings": dict(self.strings),
            "hashes": {k: dict(v) for k, v in self.hashes.items()},
            "sets": {k: set(v) for k, v in self.sets.items()},
        }

    def _delete(self, *names: str) -> None:
        for name in names:
            self.strings.pop(name, None)
            self.hashes.pop(name, None)
            self.sets.pop(name, None)

    def _apply(self, op: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        if op == "delete":
            self._delete(*args)
            return None
        if op == "set":
            name, value = args
            self.strings[name] = value
            return True
        if op == "hset":
            (name,) = args
            self.hashes.setdefault(name, {}).update(kwargs["mapping"])
            return None
        if op == "sadd":
            name, *values = args
            self.sets.setdefault(name, set()).update(values)
            return None
        if op == "get":
            (name,) = args
            value = self.strings.get(name)
            return None if value is None else value.encode()
        if op == "hgetall":
            (name,) = args
            return {k.encode(): v.encode() for k, v in self.hashes.get(name, {}).items()}
        if op == "smembers":
            (name,) = args
            return {v.encode() for v in self.sets.get(name, set())}
        raise NotImplementedError(op)

    def sync(self) -> _SyncClient:
        return _SyncClient(self)

    def aio(self) -> _AsyncClient:
        return _AsyncClient(self)


class _Queue:
    def __init__(self, store: FakeRedis) -> None:
        self._store = store
        self._ops: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _queue(self, op: str, *args: Any, **kwargs: Any) -> _Queue:
        self._ops.append((op, args, kwargs))
        return self

    def delete(self, *names: str) -> _Queue:
        return self._queue("delete", *names)

    def set(self, name: str, value: str) -> _Queue:
        return self._queue("set", name, value)

    def hset(self, name: str, *, mapping: Mapping[str, str]) -> _Queue:
        return self._queue("hset", name, mapping=dict(mapping))

    def sadd(self, name: str, *values: str) -> _Queue:
        return self._queue("sadd", name, *values)

    def get(self, name: str) -> _Queue:
        return self._queue("get", name)

    def hgetall(self, name: str) -> _Queue:
        return self._queue("hgetall", name)

    def smembers(self, name: str) -> _Queue:
        return self._queue("smembers", name)

    def _run(self) -> list[Any]:
        self._store.round_trips += 1
        ops, self._ops = self._ops, []
        return [self._store._apply(op, args, kwargs) for op, args, kwargs in ops]


class _SyncPipeline(_Queue):
    def execute(self) -> list[Any]:
        return self._run()


class _AsyncPipeline(_Queue):
    async def execute(self) -> list[Any]:
        return self._run()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _SyncClient:
    def __init__(self, store: FakeRedis) -> None:
        self._store = store

    def scan_iter(self, match: str) -> Iterator[bytes]:
        for key in sorted(self._store.all_keys()):
            if fnmatch.fnmatchcase(key, match):
                yield key.encode()

    def pipeline(self, transaction: bool = True) -> _SyncPipeline:
        return _SyncPipeline(self._store)


class _AsyncClient:
    def __init__(self, store: FakeRedis) -> None:
        self._store = store

    def pipeline(self, transaction: bool = True) -> _AsyncPipeline:
        return _AsyncPipeline(self._store)
