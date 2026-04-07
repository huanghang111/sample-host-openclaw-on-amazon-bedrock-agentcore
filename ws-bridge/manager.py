"""Bot Manager — orchestrates lifecycle of all bot adapters.

DingTalk: thread-per-bot (dingtalk_stream manages its own event loop).
Feishu: ALL Feishu bots share ONE thread with ONE asyncio event loop,
because lark-oapi uses a module-level event loop variable that can't
be isolated per-thread.
"""

import asyncio
import logging
import threading
import time

import lark_oapi as lark

from ws_bridge.adapters.base import BotConfig, BotStatus, ChannelAdapter

logger = logging.getLogger("ws-bridge.manager")


class BotManager:
    """Manages lifecycle of all bot adapters."""

    def __init__(self, core):
        self.core = core
        self.bots: dict[str, ChannelAdapter] = {}
        self.threads: dict[str, threading.Thread] = {}
        self._shutdown = threading.Event()

    def load_and_start(self):
        """Load bot configs from Secrets Manager, start all enabled bots.
        Blocks until shutdown signal received.
        """
        configs = self.core.get_bot_configs()
        if not configs:
            logger.error("No bot configs found — bridge will idle")

        feishu_configs = []
        for cfg in configs:
            if not cfg.enabled:
                logger.info("Skipping disabled bot: %s", cfg.id)
                continue

            if cfg.channel == "feishu":
                # Collect Feishu bots — they'll share one thread
                feishu_configs.append(cfg)
                continue

            # DingTalk: one thread per bot
            adapter = self._create_adapter(cfg)
            self.bots[cfg.id] = adapter
            thread = threading.Thread(
                target=self._run_bot,
                args=(adapter,),
                name=f"bot-{cfg.id}",
                daemon=True,
            )
            self.threads[cfg.id] = thread
            thread.start()

        # Start all Feishu bots in a single shared thread
        if feishu_configs:
            feishu_adapters = []
            for cfg in feishu_configs:
                adapter = self._create_adapter(cfg)
                self.bots[cfg.id] = adapter
                feishu_adapters.append(adapter)

            thread = threading.Thread(
                target=self._run_feishu_group,
                args=(feishu_adapters,),
                name="feishu-group",
                daemon=True,
            )
            # All Feishu bots share this thread reference
            for a in feishu_adapters:
                self.threads[a.config.id] = thread
            thread.start()

        logger.info("Started %d bot(s): %s",
                     len(self.bots), ", ".join(self.bots.keys()) or "(none)")

        # Block main thread until shutdown
        self._shutdown.wait()

    def _run_bot(self, adapter: ChannelAdapter):
        """Run a single bot with crash-restart logic. Runs in dedicated thread."""
        backoff = 5
        while not self._shutdown.is_set():
            try:
                adapter.start()  # Blocks until disconnect/crash
            except SystemExit:
                break
            except Exception as e:
                adapter.status = BotStatus.ERROR
                logger.error("bot=%s crashed: %s", adapter.config.id, e,
                             exc_info=True)
                if self._shutdown.wait(timeout=backoff):
                    break
                backoff = min(backoff * 2, 60)
            else:
                if not self._shutdown.is_set():
                    logger.warning("bot=%s exited unexpectedly, restarting in %ds",
                                    adapter.config.id, backoff)
                    if self._shutdown.wait(timeout=backoff):
                        break

    def _run_feishu_group(self, adapters: list[ChannelAdapter]):
        """Run ALL Feishu bots on a single shared asyncio event loop.

        lark-oapi ws.Client uses a module-level `loop` variable for all async
        operations. Multiple Feishu clients MUST share the same event loop.
        We patch the module-level loop once and run all clients as concurrent
        asyncio tasks on it.
        """
        import lark_oapi.ws.client as _ws_mod

        feishu_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(feishu_loop)
        _ws_mod.loop = feishu_loop

        backoff = 5
        while not self._shutdown.is_set():
            try:
                feishu_loop.run_until_complete(
                    self._start_feishu_bots(adapters, feishu_loop))
            except SystemExit:
                break
            except Exception as e:
                for a in adapters:
                    a.status = BotStatus.ERROR
                logger.error("Feishu group crashed: %s", e, exc_info=True)
                if self._shutdown.wait(timeout=backoff):
                    break
                backoff = min(backoff * 2, 60)
                # Reset the loop for retry
                feishu_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(feishu_loop)
                _ws_mod.loop = feishu_loop

    async def _start_feishu_bots(self, adapters, loop):
        """Connect all Feishu bots and run them concurrently."""
        from lark_oapi.ws.client import _select

        tasks = []
        for adapter in adapters:
            try:
                adapter._build_feishu_client()
                await adapter._ws_client._connect()
                adapter.status = BotStatus.CONNECTED
                adapter.connected_at = time.time()
                logger.info("bot=%s Feishu connected", adapter.config.id)
                # Start ping and receive loops as background tasks
                loop.create_task(adapter._ws_client._ping_loop())
            except Exception as e:
                adapter.status = BotStatus.ERROR
                logger.error("bot=%s Feishu connect failed: %s",
                             adapter.config.id, e)

        # Block until something breaks (_select is an infinite sleep)
        await _select()

    def shutdown(self):
        """Graceful shutdown: signal all bots to stop."""
        logger.info("Shutting down %d bot(s)...", len(self.bots))
        self._shutdown.set()
        for bot_id, adapter in self.bots.items():
            try:
                adapter.stop()
            except Exception as e:
                logger.warning("bot=%s stop failed: %s", bot_id, e)

    def _create_adapter(self, cfg: BotConfig) -> ChannelAdapter:
        if cfg.channel == "dingtalk":
            from ws_bridge.adapters.dingtalk import DingTalkAdapter
            return DingTalkAdapter(cfg, self.core)
        elif cfg.channel == "feishu":
            from ws_bridge.adapters.feishu import FeishuAdapter
            return FeishuAdapter(cfg, self.core)
        else:
            raise ValueError(f"Unknown channel: {cfg.channel}")

    def get_adapter(self, bot_id: str) -> ChannelAdapter | None:
        return self.bots.get(bot_id)

    def get_health(self) -> dict:
        result = {}
        for bot_id, adapter in self.bots.items():
            uptime = time.time() - adapter.connected_at if adapter.connected_at else 0
            result[bot_id] = {
                "status": adapter.status.value,
                "channel": adapter.config.channel,
                "uptime_s": int(uptime) if adapter.status == BotStatus.CONNECTED else 0,
                "thread_alive": self.threads.get(bot_id, threading.Thread()).is_alive(),
            }
        return result
