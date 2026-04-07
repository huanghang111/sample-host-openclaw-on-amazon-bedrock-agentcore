"""Base adapter interface, data classes, and shared types for WS Bridge."""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class BotStatus(Enum):
    STARTING = "starting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    ERROR = "error"
    STOPPED = "stopped"


@dataclass
class BotConfig:
    """Configuration for a single bot instance."""
    id: str                     # Unique bot identifier (e.g., "dingtalk-main")
    channel: str                # "dingtalk" | "feishu"
    enabled: bool
    credentials: dict           # Channel-specific credentials

    def validate(self):
        """Validate bot config fields."""
        if not self.id or not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,47}$', self.id):
            raise ValueError(f"Invalid bot id: {self.id!r} (must be alphanumeric/hyphens/underscores, 1-48 chars)")
        if self.channel not in ("dingtalk", "feishu"):
            raise ValueError(f"Unknown channel: {self.channel!r}")
        if not self.credentials:
            raise ValueError(f"Bot {self.id}: credentials required")


@dataclass
class InboundMessage:
    """Normalized inbound message from any channel."""
    bot_id: str                 # Which bot received this
    channel: str                # "dingtalk" | "feishu"
    sender_id: str              # Platform user ID (staffId / open_id)
    sender_name: str            # Display name
    text: str                   # Message text
    message_id: str             # Platform message ID (for dedup + Feishu media download)
    is_group: bool              # DM or group chat
    conversation_id: str        # Group conversation ID (if group)
    raw: dict                   # Original platform event data
    # Media attachments
    image_download_code: str = ""   # DingTalk downloadCode or Feishu image_key
    file_download_code: str = ""
    file_name: str = ""
    video_download_code: str = ""


class ChannelAdapter(ABC):
    """Base class for WebSocket channel adapters.

    Each adapter instance manages ONE bot (one credential set, one WebSocket).
    The adapter's start() method is BLOCKING — it runs in a dedicated thread
    managed by BotManager. Message callbacks dispatch to the shared core via
    the core reference.
    """

    def __init__(self, config: BotConfig, core):
        self.config = config
        self.core = core        # Reference to shared core services
        self.status = BotStatus.STARTING
        self.connected_at: float = 0  # timestamp

    @abstractmethod
    def start(self):
        """Start WebSocket connection. BLOCKS until stopped or crashed.

        This method runs in a dedicated thread. SDK reconnection logic
        (start_forever / auto-reconnect) should be used here.
        """
        ...

    @abstractmethod
    def stop(self):
        """Gracefully disconnect. Called from the main thread on SIGTERM."""
        ...

    @abstractmethod
    def send_text(self, receiver_id: str, text: str,
                  *, is_group: bool = False,
                  conversation_id: str = "") -> None:
        """Send a text message to a user or group."""
        ...

    @abstractmethod
    def send_image(self, receiver_id: str, image_url_or_media_id: str,
                   *, is_group: bool = False,
                   conversation_id: str = "") -> None:
        """Send an image to a user or group."""
        ...

    @abstractmethod
    def send_file(self, receiver_id: str, media_id: str,
                  filename: str, file_type: str,
                  *, is_group: bool = False,
                  conversation_id: str = "") -> None:
        """Send a file to a user or group."""
        ...

    @abstractmethod
    def send_link(self, receiver_id: str, title: str, text: str,
                  message_url: str,
                  *, is_group: bool = False,
                  conversation_id: str = "") -> None:
        """Send a link card to a user or group."""
        ...

    @abstractmethod
    def upload_media(self, file_bytes: bytes, filename: str,
                     media_type: str = "file") -> str | None:
        """Upload media to the platform's storage. Returns media_id or None."""
        ...

    @abstractmethod
    def download_media(self, download_code: str,
                       max_bytes: int,
                       *, message_id: str = "") -> tuple[bytes | None, str]:
        """Download media from platform. Returns (bytes, content_type).

        message_id is required for Feishu (image/file download needs it).
        """
        ...
