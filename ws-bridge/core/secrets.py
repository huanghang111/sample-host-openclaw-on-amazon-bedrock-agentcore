"""Secret fetching + caching (15 min TTL). Thread-safe."""

import json
import logging
import threading
import time

logger = logging.getLogger("ws-bridge.secrets")

_SECRET_CACHE_TTL = 900  # 15 min
_cache: dict[str, tuple[str, float]] = {}
_lock = threading.Lock()


def get_secret(secrets_client, secret_id: str) -> str:
    """Fetch a secret from Secrets Manager with caching."""
    with _lock:
        cached = _cache.get(secret_id)
        if cached:
            value, fetched_at = cached
            if time.time() - fetched_at < _SECRET_CACHE_TTL:
                return value

    if not secret_id:
        return ""
    try:
        resp = secrets_client.get_secret_value(SecretId=secret_id)
        value = resp["SecretString"]
        with _lock:
            _cache[secret_id] = (value, time.time())
        return value
    except Exception as e:
        logger.warning("Failed to fetch secret %s: %s", secret_id, e)
        return ""


def get_bot_configs(secrets_client, secret_id: str):
    """Load bot configs from the ws-bridge/bots secret."""
    from ws_bridge.adapters.base import BotConfig

    raw = get_secret(secrets_client, secret_id)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.error("Failed to parse bot configs from %s", secret_id)
        return []

    bots = data.get("bots", [])
    configs = []
    for b in bots:
        try:
            cfg = BotConfig(
                id=b["id"],
                channel=b["channel"],
                enabled=b.get("enabled", True),
                credentials=b.get("credentials", {}),
            )
            cfg.validate()
            configs.append(cfg)
        except (KeyError, ValueError) as e:
            logger.error("Invalid bot config: %s — %s", b.get("id", "?"), e)
    return configs
