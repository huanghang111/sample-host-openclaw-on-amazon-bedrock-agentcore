"""In-memory message deduplication. Thread-safe, per-bot keyed."""

import logging
import threading
import time

logger = logging.getLogger("ws-bridge.dedup")

_DEDUP_TTL = 300  # 5 min


class DedupService:
    """Per-bot in-memory message dedup cache."""

    def __init__(self, ttl: int = _DEDUP_TTL):
        self.ttl = ttl
        self._cache: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_duplicate(self, bot_id: str, message_id: str) -> bool:
        """Returns True if this message was already seen."""
        if not message_id:
            return False
        key = f"{bot_id}:{message_id}"
        now = time.time()
        with self._lock:
            if key in self._cache:
                logger.info("Dedup: skipping already-processed message %s", key)
                return True
            self._cache[key] = now
            # Cleanup when cache grows large
            if len(self._cache) > 200:
                expired = [k for k, v in self._cache.items() if now - v > self.ttl]
                for k in expired:
                    del self._cache[k]
        return False
