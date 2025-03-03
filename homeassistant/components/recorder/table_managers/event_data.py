"""Support managing EventData."""
from __future__ import annotations

from collections.abc import Iterable
import logging
from typing import TYPE_CHECKING, cast

from lru import LRU  # pylint: disable=no-name-in-module
from sqlalchemy.orm.session import Session

from homeassistant.core import Event
from homeassistant.util.json import JSON_ENCODE_EXCEPTIONS

from . import BaseTableManager
from ..const import SQLITE_MAX_BIND_VARS
from ..db_schema import EventData
from ..queries import get_shared_event_datas
from ..util import chunked

if TYPE_CHECKING:
    from ..core import Recorder


CACHE_SIZE = 2048

_LOGGER = logging.getLogger(__name__)


class EventDataManager(BaseTableManager):
    """Manage the EventData table."""

    def __init__(self, recorder: Recorder) -> None:
        """Initialize the event type manager."""
        self._id_map: dict[str, int] = LRU(CACHE_SIZE)
        self._pending: dict[str, EventData] = {}
        super().__init__(recorder)
        self.active = True  # always active

    def serialize_from_event(self, event: Event) -> bytes | None:
        """Serialize event data."""
        try:
            return EventData.shared_data_bytes_from_event(
                event, self.recorder.dialect_name
            )
        except JSON_ENCODE_EXCEPTIONS as ex:
            _LOGGER.warning("Event is not JSON serializable: %s: %s", event, ex)
            return None

    def load(self, events: list[Event], session: Session) -> None:
        """Load the shared_datas to data_ids mapping into memory from events."""
        if hashes := {
            EventData.hash_shared_data_bytes(shared_event_bytes)
            for event in events
            if (shared_event_bytes := self.serialize_from_event(event))
        }:
            self._load_from_hashes(hashes, session)

    def get(self, shared_data: str, data_hash: int, session: Session) -> int | None:
        """Resolve shared_datas to the data_id."""
        return self.get_many(((shared_data, data_hash),), session)[shared_data]

    def get_from_cache(self, shared_data: str) -> int | None:
        """Resolve shared_data to the data_id without accessing the underlying database."""
        return self._id_map.get(shared_data)

    def get_many(
        self, shared_data_data_hashs: Iterable[tuple[str, int]], session: Session
    ) -> dict[str, int | None]:
        """Resolve shared_datas to data_ids."""
        results: dict[str, int | None] = {}
        missing_hashes: set[int] = set()
        for shared_data, data_hash in shared_data_data_hashs:
            if (data_id := self._id_map.get(shared_data)) is None:
                missing_hashes.add(data_hash)

            results[shared_data] = data_id

        if not missing_hashes:
            return results

        return results | self._load_from_hashes(missing_hashes, session)

    def _load_from_hashes(
        self, hashes: Iterable[int], session: Session
    ) -> dict[str, int | None]:
        """Load the shared_datas to data_ids mapping into memory from a list of hashes."""
        results: dict[str, int | None] = {}
        with session.no_autoflush:
            for hashs_chunk in chunked(hashes, SQLITE_MAX_BIND_VARS):
                for data_id, shared_data in session.execute(
                    get_shared_event_datas(hashs_chunk)
                ):
                    results[shared_data] = self._id_map[shared_data] = cast(
                        int, data_id
                    )

        return results

    def get_pending(self, shared_data: str) -> EventData | None:
        """Get pending EventData that have not be assigned ids yet."""
        return self._pending.get(shared_data)

    def add_pending(self, db_event_data: EventData) -> None:
        """Add a pending EventData that will be committed at the next interval."""
        assert db_event_data.shared_data is not None
        shared_data: str = db_event_data.shared_data
        self._pending[shared_data] = db_event_data

    def post_commit_pending(self) -> None:
        """Call after commit to load the data_ids of the new EventData into the LRU."""
        for shared_data, db_event_data in self._pending.items():
            self._id_map[shared_data] = db_event_data.data_id
        self._pending.clear()

    def reset(self) -> None:
        """Reset the event manager after the database has been reset or changed."""
        self._id_map.clear()
        self._pending.clear()

    def evict_purged(self, data_ids: set[int]) -> None:
        """Evict purged data_ids from the cache when they are no longer used."""
        id_map = self._id_map
        event_data_ids_reversed = {
            data_id: shared_data for shared_data, data_id in id_map.items()
        }
        # Evict any purged data from the cache
        for purged_data_id in data_ids.intersection(event_data_ids_reversed):
            id_map.pop(event_data_ids_reversed[purged_data_id], None)
