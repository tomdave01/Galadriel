"""Cosmos DB snapshot and narration-cache persistence interfaces."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import uuid
import zlib
from typing import Any, Protocol

from azure.cosmos.exceptions import CosmosResourceNotFoundError
from azure.identity.aio import DefaultAzureCredential


logger = logging.getLogger(__name__)
_SNAPSHOT_CHUNK_SIZE = 900_000


class SnapshotStore(Protocol):
    async def load_snapshot(self, match_id: str) -> dict[str, Any] | None: ...

    async def save_snapshot(self, match_id: str, snapshot: dict[str, Any]) -> None: ...


class NarrationCache(Protocol):
    async def get(
        self, fact_id: str, mode: str, language: str, favourite: str | None
    ) -> str | None: ...

    async def put(
        self,
        fact_id: str,
        mode: str,
        language: str,
        favourite: str | None,
        narration: str,
    ) -> None: ...


def narration_cache_key(
    fact_id: str, mode: str, language: str, favourite: str | None
) -> str:
    identity = json.dumps(
        [fact_id, mode, language, favourite],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


class InMemoryNarrationCache:
    def __init__(self) -> None:
        self._entries: dict[tuple[str, str, str, str | None], str] = {}

    @staticmethod
    def _key(
        fact_id: str,
        mode: str,
        language: str,
        favourite: str | None,
    ) -> tuple[str, str, str, str | None]:
        return fact_id, mode, language, favourite

    async def get(
        self, fact_id: str, mode: str, language: str, favourite: str | None
    ) -> str | None:
        return self._entries.get(self._key(fact_id, mode, language, favourite))

    async def put(
        self,
        fact_id: str,
        mode: str,
        language: str,
        favourite: str | None,
        narration: str,
    ) -> None:
        self._entries[self._key(fact_id, mode, language, favourite)] = narration


class CosmosNarrationCache(InMemoryNarrationCache):
    def __init__(self, container: Any) -> None:
        super().__init__()
        self._container = container

    async def get(
        self, fact_id: str, mode: str, language: str, favourite: str | None
    ) -> str | None:
        key = narration_cache_key(fact_id, mode, language, favourite)
        cached = self._entries.get(self._key(fact_id, mode, language, favourite))
        if cached is not None:
            return cached
        try:
            item = await self._container.read_item(item=key, partition_key=key)
        except CosmosResourceNotFoundError:
            return None
        narration = item.get("narration")
        if not isinstance(narration, str):
            raise ValueError(f"Cosmos narration cache item {key} has no narration text")
        self._entries[self._key(fact_id, mode, language, favourite)] = narration
        return narration

    async def put(
        self,
        fact_id: str,
        mode: str,
        language: str,
        favourite: str | None,
        narration: str,
    ) -> None:
        key = narration_cache_key(fact_id, mode, language, favourite)
        await self._container.upsert_item(
            body={
                "id": key,
                "cache_key": key,
                "fact_id": fact_id,
                "mode": mode,
                "language": language,
                "favourite": favourite,
                "narration": narration,
            }
        )
        self._entries[self._key(fact_id, mode, language, favourite)] = narration


class CosmosPersistence:
    def __init__(
        self,
        *,
        client: Any,
        credential: Any,
        snapshot_container: Any,
        narration_cache: CosmosNarrationCache,
    ) -> None:
        self._client = client
        self._credential = credential
        self._snapshot_container = snapshot_container
        self.narration_cache = narration_cache

    @classmethod
    def from_environment(cls) -> CosmosPersistence | None:
        endpoint = os.getenv("COSMOS_ENDPOINT", "").strip()
        if not endpoint:
            logger.warning(
                "COSMOS_ENDPOINT is unset: match snapshots and narration cache "
                "will not persist outside this process."
            )
            return None

        database_name = os.getenv("COSMOS_DATABASE", "overseer").strip()
        snapshot_name = os.getenv(
            "COSMOS_MATCH_STATE_CONTAINER", "match-state"
        ).strip()
        cache_name = os.getenv(
            "COSMOS_NARRATION_CACHE_CONTAINER", "narration-cache"
        ).strip()
        if not database_name or not snapshot_name or not cache_name:
            raise ValueError("Cosmos database and container names must not be blank")

        from azure.cosmos.aio import CosmosClient

        credential = DefaultAzureCredential()
        client = CosmosClient(url=endpoint, credential=credential)
        database = client.get_database_client(database_name)
        return cls(
            client=client,
            credential=credential,
            snapshot_container=database.get_container_client(snapshot_name),
            narration_cache=CosmosNarrationCache(
                database.get_container_client(cache_name)
            ),
        )

    async def load_snapshot(self, match_id: str) -> dict[str, Any] | None:
        manifest_id = _cosmos_id(f"match-snapshot:{match_id}")
        try:
            item = await self._snapshot_container.read_item(
                item=manifest_id, partition_key=match_id
            )
        except CosmosResourceNotFoundError:
            return None
        last_seq = item.get("last_seq")
        snapshot_id = item.get("snapshot_id")
        chunk_count = item.get("chunk_count")
        if (
            not isinstance(last_seq, int)
            or not isinstance(snapshot_id, str)
            or not isinstance(chunk_count, int)
            or chunk_count < 1
        ):
            raise ValueError(f"Cosmos match snapshot {match_id} is malformed")
        chunks: list[str] = []
        for index in range(chunk_count):
            chunk_id = _cosmos_id(f"{snapshot_id}:{index}")
            chunk = await self._snapshot_container.read_item(
                item=chunk_id, partition_key=match_id
            )
            if chunk.get("chunk_index") != index:
                raise ValueError(
                    f"Cosmos match snapshot {match_id} has a malformed chunk"
                )
            data = chunk.get("compressed_state")
            if not isinstance(data, str):
                raise ValueError(
                    f"Cosmos match snapshot {match_id} has a malformed chunk"
                )
            chunks.append(data)
        try:
            serialized_state = zlib.decompress(
                base64.b64decode("".join(chunks), validate=True)
            )
            state = json.loads(serialized_state)
        except (ValueError, zlib.error, json.JSONDecodeError) as error:
            raise ValueError(
                f"Cosmos match snapshot {match_id} could not be decoded"
            ) from error
        if not isinstance(state, dict):
            raise ValueError(f"Cosmos match snapshot {match_id} is malformed")
        return {"match_id": match_id, "state": state, "last_seq": last_seq}

    async def save_snapshot(self, match_id: str, snapshot: dict[str, Any]) -> None:
        manifest_id = _cosmos_id(f"match-snapshot:{match_id}")
        previous: dict[str, Any] | None
        try:
            previous = await self._snapshot_container.read_item(
                item=manifest_id, partition_key=match_id
            )
        except CosmosResourceNotFoundError:
            previous = None

        snapshot_id = _cosmos_id(f"{match_id}:{snapshot['last_seq']}:{uuid.uuid4()}")
        serialized_state = json.dumps(
            snapshot["state"], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        compressed = base64.b64encode(zlib.compress(serialized_state)).decode("ascii")
        chunks = [
            compressed[index:index + _SNAPSHOT_CHUNK_SIZE]
            for index in range(0, len(compressed), _SNAPSHOT_CHUNK_SIZE)
        ]
        if not chunks:
            raise ValueError("A match snapshot must contain state data")
        for index, chunk_data in enumerate(chunks):
            await self._snapshot_container.upsert_item(
                body={
                    "id": _cosmos_id(f"{snapshot_id}:{index}"),
                    "match_id": match_id,
                    "snapshot_id": snapshot_id,
                    "chunk_index": index,
                    "compressed_state": chunk_data,
                }
            )
        await self._snapshot_container.upsert_item(
            body={
                "id": manifest_id,
                "match_id": match_id,
                "last_seq": snapshot["last_seq"],
                "snapshot_id": snapshot_id,
                "chunk_count": len(chunks),
            }
        )

        if previous is not None:
            previous_snapshot_id = previous.get("snapshot_id")
            previous_chunk_count = previous.get("chunk_count")
            if isinstance(previous_snapshot_id, str) and isinstance(
                previous_chunk_count, int
            ):
                for index in range(previous_chunk_count):
                    await self._snapshot_container.delete_item(
                        item=_cosmos_id(f"{previous_snapshot_id}:{index}"),
                        partition_key=match_id,
                    )

    async def close(self) -> None:
        try:
            await self._client.close()
        finally:
            await self._credential.close()


def _cosmos_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
