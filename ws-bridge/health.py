"""HTTP health check server for ECS Fargate health probes."""

import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

logger = logging.getLogger("ws-bridge.health")

HEALTH_PORT = 8080


class HealthHandler(BaseHTTPRequestHandler):
    """HTTP handler for /health endpoint."""

    manager = None  # Set before starting the server

    def do_GET(self):
        if self.path == "/health":
            bot_health = self.manager.get_health() if self.manager else {}
            # Service is healthy if at least one bot thread is alive and connected
            any_connected = any(
                b.get("status") == "connected" and b.get("thread_alive")
                for b in bot_health.values()
            )
            status_code = 200 if any_connected or not bot_health else 503
            body = json.dumps({
                "status": "ok" if any_connected else "degraded",
                "service": "openclaw-ws-bridge",
                "bots": bot_health,
            })
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress request logs


def start_health_server(manager, port: int = HEALTH_PORT) -> HTTPServer:
    """Start the health check server in a daemon thread."""
    HealthHandler.manager = manager
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health check server started on port %d", port)
    return server
