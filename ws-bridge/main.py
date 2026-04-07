"""WS Bridge entry point — load config, start bot manager + health server."""

import logging
import signal
import sys

logger = logging.getLogger("ws-bridge")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
logger.addHandler(handler)
# Ensure all sub-loggers inherit
for name in ("ws-bridge.core", "ws-bridge.identity", "ws-bridge.agentcore",
             "ws-bridge.s3", "ws-bridge.outbound", "ws-bridge.dedup",
             "ws-bridge.secrets", "ws-bridge.dingtalk", "ws-bridge.feishu",
             "ws-bridge.manager", "ws-bridge.health"):
    sub = logging.getLogger(name)
    sub.setLevel(logging.INFO)


def main():
    from ws_bridge.core.shared import SharedCore
    from ws_bridge.manager import BotManager
    from ws_bridge.health import start_health_server

    logger.info("Starting WS Bridge...")

    core = SharedCore()
    manager = BotManager(core)
    health_server = start_health_server(manager)

    def _on_signal(signum, frame):
        logger.info("Received signal %d, shutting down...", signum)
        manager.shutdown()
        health_server.shutdown()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        manager.load_and_start()  # Blocks until shutdown
    except SystemExit:
        pass
    except Exception:
        logger.error("WS Bridge crashed", exc_info=True)
        sys.exit(1)

    logger.info("WS Bridge stopped")


if __name__ == "__main__":
    main()
