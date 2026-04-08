"""Feishu/Lark WebSocket adapter — one instance per app, runs in dedicated thread.

Wraps lark_oapi.ws.Client. Each instance has its own:
- WebSocket connection via client.start()
- lark.Client for API calls (send messages, download media)
- Bot display name (fetched on start for @mention stripping)
"""

import asyncio
import json
import logging
import time
from urllib import request as urllib_request

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    GetMessageResourceRequest,
)

from ws_bridge.adapters.base import BotConfig, BotStatus, ChannelAdapter, InboundMessage

logger = logging.getLogger("ws-bridge.feishu")

MAX_TEXT_LEN = 20000
FEISHU_API_DOMAIN = "https://open.feishu.cn"

# Serialize Feishu bot startup to avoid race condition on module-level loop patch.
# Once start() enters run_until_complete(), the loop is bound to that thread and
# won't be affected by subsequent patches from other threads.
import threading
_feishu_start_lock = threading.Lock()


class FeishuAdapter(ChannelAdapter):
    """Feishu/Lark WebSocket adapter. One instance per app, one thread."""

    def __init__(self, config: BotConfig, core):
        super().__init__(config, core)
        self._app_id = config.credentials["appId"]
        self._app_secret = config.credentials["appSecret"]
        self._bot_name: str = ""
        self._bot_open_id: str = ""
        self._lark_client: lark.Client | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Not used directly for multi-bot — BotManager._run_feishu_group() handles that.

        This fallback exists for single-bot testing: patches the module-level
        event loop and calls ws_client.start() which blocks forever.
        """
        import lark_oapi.ws.client as _ws_mod
        thread_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(thread_loop)
        _ws_mod.loop = thread_loop

        self._build_feishu_client()
        self.status = BotStatus.CONNECTED
        self.connected_at = time.time()
        logger.info("bot=%s starting Feishu WS (appId=%s, botName=%s)",
                     self.config.id, self._app_id, self._bot_name)
        self._ws_client.start()

    def _build_feishu_client(self):
        """Build the lark API client and WS client. Called before connect.

        Must be called AFTER the module-level loop has been patched.
        """
        self._lark_client = (
            lark.Client.builder()
            .app_id(self._app_id)
            .app_secret(self._app_secret)
            .domain(FEISHU_API_DOMAIN)
            .build()
        )
        self._fetch_bot_info()

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message)
            .build()
        )
        self._ws_client = lark.ws.Client(
            self._app_id,
            self._app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
            domain=FEISHU_API_DOMAIN,
        )
        logger.info("bot=%s Feishu client built (appId=%s, botName=%s)",
                     self.config.id, self._app_id, self._bot_name)

    def stop(self):
        self.status = BotStatus.STOPPED

    # ------------------------------------------------------------------
    # Bot info
    # ------------------------------------------------------------------

    def _fetch_bot_info(self):
        """Fetch this bot's display name and open_id for @mention stripping.

        Uses the tenant_access_token + GET /bot/v3/info HTTP API directly,
        since lark_oapi.Client doesn't expose a bot info module.
        """
        try:
            # Get tenant_access_token
            token_url = f"{FEISHU_API_DOMAIN}/open-apis/auth/v3/tenant_access_token/internal"
            token_data = json.dumps({"app_id": self._app_id, "app_secret": self._app_secret}).encode()
            token_req = urllib_request.Request(token_url, data=token_data,
                                               headers={"Content-Type": "application/json"})
            token_resp = urllib_request.urlopen(token_req, timeout=10)
            token_result = json.loads(token_resp.read())
            tenant_token = token_result.get("tenant_access_token", "")
            if not tenant_token:
                logger.warning("bot=%s Failed to get tenant_access_token for bot info", self.config.id)
                return

            # Get bot info
            bot_url = f"{FEISHU_API_DOMAIN}/open-apis/bot/v3/info"
            bot_req = urllib_request.Request(bot_url, headers={
                "Authorization": f"Bearer {tenant_token}",
            })
            bot_resp = urllib_request.urlopen(bot_req, timeout=10)
            bot_result = json.loads(bot_resp.read())
            bot_data = bot_result.get("bot", {})
            self._bot_name = bot_data.get("bot_name", "")
            self._bot_open_id = bot_data.get("open_id", "")
            logger.info("bot=%s Feishu bot info: name=%s open_id=%s",
                         self.config.id, self._bot_name, self._bot_open_id)
        except Exception as e:
            logger.warning("bot=%s Failed to fetch bot info: %s", self.config.id, e)

    # ------------------------------------------------------------------
    # Typing indicator (message reaction)
    # ------------------------------------------------------------------

    def _add_typing_reaction(self, message_id: str) -> str:
        """Add a 'TYPING' emoji reaction to acknowledge message receipt.

        Returns the reaction_id for later removal, or empty string on failure.
        Following the official openclaw-lark plugin pattern.
        """
        if not self._lark_client or not message_id:
            return ""
        try:
            from lark_oapi.api.im.v1 import (
                CreateMessageReactionRequest,
                CreateMessageReactionRequestBody,
            )
            from lark_oapi.api.im.v1.model.emoji import Emoji
            emoji = Emoji.builder().emoji_type("OnIt").build()
            body = (
                CreateMessageReactionRequestBody.builder()
                .reaction_type(emoji)
                .build()
            )
            req = (
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(body)
                .build()
            )
            resp = self._lark_client.im.v1.message_reaction.create(req)
            if resp.success() and resp.data:
                reaction_id = resp.data.reaction_id or ""
                return reaction_id
            else:
                logger.info("bot=%s Typing reaction failed: code=%s msg=%s",
                            self.config.id, resp.code if resp else "?", resp.msg if resp else "?")
        except Exception as e:
            logger.info("bot=%s Failed to add typing reaction: %s", self.config.id, e)
        return ""

    def _remove_typing_reaction(self, message_id: str, reaction_id: str):
        """Remove the typing reaction after processing is complete."""
        if not self._lark_client or not message_id or not reaction_id:
            return
        try:
            from lark_oapi.api.im.v1 import DeleteMessageReactionRequest
            req = (
                DeleteMessageReactionRequest.builder()
                .message_id(message_id)
                .reaction_id(reaction_id)
                .build()
            )
            self._lark_client.im.v1.message_reaction.delete(req)
        except Exception:
            pass  # Best-effort removal

    # ------------------------------------------------------------------
    # Message handler
    # ------------------------------------------------------------------

    def _on_message(self, data):
        """Feishu event callback — runs in SDK's event loop thread."""
        try:
            event = data.event
            message = event.message
            sender = event.sender

            # Parse message content (Feishu sends JSON-encoded content)
            content = {}
            if message.content:
                try:
                    content = json.loads(message.content)
                except (json.JSONDecodeError, TypeError):
                    pass

            text = content.get("text", "")
            msg_type = message.message_type

            # Strip @bot mention in group chats
            is_group = message.chat_type == "group"
            if is_group and message.mentions:
                for mention in message.mentions:
                    if mention.id and mention.id.open_id == self._bot_open_id:
                        # Replace the mention placeholder (@_user_N) with empty string
                        if mention.key:
                            text = text.replace(mention.key, "").strip()

            # Extract media
            image_key = ""
            if msg_type == "image":
                image_key = content.get("image_key", "")

            file_key = ""
            file_name = ""
            if msg_type == "file":
                file_key = content.get("file_key", "")
                file_name = content.get("file_name", "")

            msg = InboundMessage(
                bot_id=self.config.id,
                channel="feishu",
                sender_id=sender.sender_id.open_id if sender.sender_id else "",
                sender_name=sender.sender_id.open_id if sender.sender_id else "",
                text=text,
                message_id=message.message_id or "",
                is_group=is_group,
                conversation_id=message.chat_id if is_group else "",
                raw={},  # Don't store full event to avoid serialization issues
                image_download_code=image_key,
                file_download_code=file_key,
                file_name=file_name,
            )

            logger.info("bot=%s received message: type=%s from=%s group=%s",
                         self.config.id, msg_type,
                         msg.sender_id[:20] if msg.sender_id else "?", is_group)

            # Add "Typing" reaction as instant acknowledgment
            reaction_id = self._add_typing_reaction(message.message_id)

            try:
                # Process synchronously in this thread (boto3 is sync)
                self.core.process_message(self, msg)
            finally:
                # Remove typing reaction after processing
                if reaction_id:
                    self._remove_typing_reaction(message.message_id, reaction_id)

        except Exception:
            logger.error("bot=%s Error handling Feishu message", self.config.id,
                         exc_info=True)

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def send_text(self, receiver_id, text, *, is_group=False, conversation_id=""):
        if not self._lark_client:
            return

        chunks = ([text[i:i + MAX_TEXT_LEN]
                   for i in range(0, len(text), MAX_TEXT_LEN)]
                  if len(text) > MAX_TEXT_LEN else [text])

        for chunk in chunks:
            receive_id = conversation_id if is_group else receiver_id
            receive_id_type = "chat_id" if is_group else "open_id"
            body = (
                CreateMessageRequestBody.builder()
                .receive_id(receive_id)
                .msg_type("text")
                .content(json.dumps({"text": chunk}))
                .build()
            )
            req = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(body)
                .build()
            )
            try:
                resp = self._lark_client.im.v1.message.create(req)
                if not resp.success():
                    logger.error("bot=%s Feishu send_text failed: code=%s msg=%s",
                                 self.config.id, resp.code, resp.msg)
            except Exception as e:
                logger.error("bot=%s Feishu send_text error: %s", self.config.id, e)

    def send_image(self, receiver_id, image_key_or_url, *,
                   is_group=False, conversation_id=""):
        """Send an image message. image_key_or_url should be a Feishu image_key."""
        if not self._lark_client:
            return

        receive_id = conversation_id if is_group else receiver_id
        receive_id_type = "chat_id" if is_group else "open_id"
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type("image")
            .content(json.dumps({"image_key": image_key_or_url}))
            .build()
        )
        req = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(body)
            .build()
        )
        try:
            resp = self._lark_client.im.v1.message.create(req)
            if not resp.success():
                logger.error("bot=%s Feishu send_image failed: code=%s msg=%s",
                             self.config.id, resp.code, resp.msg)
        except Exception as e:
            logger.error("bot=%s Feishu send_image error: %s", self.config.id, e)

    def send_file(self, receiver_id, file_key, filename, file_type, *,
                  is_group=False, conversation_id=""):
        """Send a file message using the correct msg_type per file type.

        Following the official openclaw-lark plugin convention:
        - audio (opus/ogg/mp3/wav): msg_type='audio'
        - video (mp4/mov/avi): msg_type='media'
        - other files: msg_type='file'
        """
        if not self._lark_client:
            return

        # Determine msg_type from file extension (matching official openclaw-lark)
        ext = file_type.lower().lstrip(".")
        if ext in ("opus", "ogg", "mp3", "wav"):
            msg_type = "audio"
        elif ext in ("mp4", "mov", "avi", "mkv", "webm"):
            msg_type = "media"
        else:
            msg_type = "file"

        receive_id = conversation_id if is_group else receiver_id
        receive_id_type = "chat_id" if is_group else "open_id"
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type(msg_type)
            .content(json.dumps({"file_key": file_key}))
            .build()
        )
        req = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(body)
            .build()
        )
        try:
            resp = self._lark_client.im.v1.message.create(req)
            if not resp.success():
                logger.error("bot=%s Feishu send_file failed: code=%s msg=%s body=%s",
                             self.config.id, resp.code, resp.msg,
                             resp.raw.content.decode()[:500] if resp.raw else "")
            else:
                logger.info("bot=%s Sent %s via Feishu: %s (%s)",
                            self.config.id, msg_type, filename, file_key[:20])
        except Exception as e:
            logger.error("bot=%s Feishu send_file error: %s", self.config.id, e)

    def send_link(self, receiver_id, title, text, message_url, *,
                  is_group=False, conversation_id=""):
        """Send a rich text message with a link (Feishu doesn't have a native link card like DingTalk)."""
        if not self._lark_client:
            return

        # Use post (rich text) message type with a link
        content = {
            "zh_cn": {
                "title": title,
                "content": [
                    [{"tag": "text", "text": text + "\n"}],
                    [{"tag": "a", "text": "Download", "href": message_url}],
                ],
            }
        }
        receive_id = conversation_id if is_group else receiver_id
        receive_id_type = "chat_id" if is_group else "open_id"
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type("post")
            .content(json.dumps(content))
            .build()
        )
        req = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(body)
            .build()
        )
        try:
            resp = self._lark_client.im.v1.message.create(req)
            if not resp.success():
                logger.error("bot=%s Feishu send_link failed: code=%s msg=%s",
                             self.config.id, resp.code, resp.msg)
        except Exception as e:
            logger.error("bot=%s Feishu send_link error: %s", self.config.id, e)

    # ------------------------------------------------------------------
    # Media upload/download
    # ------------------------------------------------------------------

    def upload_media(self, file_bytes, filename, media_type="file"):
        """Upload media to Feishu. Returns image_key or file_key, or None.

        For images: POST /im/v1/images with type=message
        For files: POST /im/v1/files with file_type + file_name
        """
        if not self._lark_client:
            return None

        try:
            if media_type == "image":
                from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody
                import io
                body = (
                    CreateImageRequestBody.builder()
                    .image_type("message")
                    .image(io.BytesIO(file_bytes))
                    .build()
                )
                req = CreateImageRequest.builder().request_body(body).build()
                resp = self._lark_client.im.v1.image.create(req)
                if resp.success() and resp.data:
                    image_key = resp.data.image_key
                    logger.info("bot=%s Uploaded image to Feishu: image_key=%s",
                                self.config.id, image_key)
                    return image_key
                logger.error("bot=%s Feishu image upload failed: code=%s msg=%s body=%s",
                             self.config.id,
                             resp.code if resp else "?",
                             resp.msg if resp else "no response",
                             resp.raw.content.decode()[:500] if resp and resp.raw else "")
            else:
                from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody
                import io
                # Determine Feishu file type from extension
                ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
                feishu_file_type = "stream"  # default
                if ext in ("opus", "mp3", "wav", "ogg"):
                    feishu_file_type = "opus"
                elif ext in ("mp4", "mov", "avi"):
                    feishu_file_type = "mp4"
                elif ext in ("pdf",):
                    feishu_file_type = "pdf"
                elif ext in ("doc", "docx"):
                    feishu_file_type = "doc"
                elif ext in ("xls", "xlsx"):
                    feishu_file_type = "xls"
                elif ext in ("ppt", "pptx"):
                    feishu_file_type = "ppt"

                body = (
                    CreateFileRequestBody.builder()
                    .file_type(feishu_file_type)
                    .file_name(filename)
                    .file(io.BytesIO(file_bytes))
                    .build()
                )
                req = CreateFileRequest.builder().request_body(body).build()
                resp = self._lark_client.im.v1.file.create(req)
                if resp.success() and resp.data:
                    file_key = resp.data.file_key
                    logger.info("bot=%s Uploaded file to Feishu: file_key=%s type=%s",
                                self.config.id, file_key, feishu_file_type)
                    return file_key
                logger.error("bot=%s Feishu file upload failed: code=%s msg=%s body=%s",
                             self.config.id,
                             resp.code if resp else "?",
                             resp.msg if resp else "no response",
                             resp.raw.content.decode()[:500] if resp and resp.raw else "")
        except Exception as e:
            logger.error("bot=%s Feishu media upload failed: %s", self.config.id, e,
                         exc_info=True)
        return None

    def download_media(self, download_code, max_bytes, *, message_id=""):
        """Download Feishu image/file by file_key using message_id.

        Feishu requires message_id + file_key for media download.
        """
        if not self._lark_client or not message_id:
            logger.warning("bot=%s Feishu download requires message_id", self.config.id)
            return None, ""

        try:
            req = (
                GetMessageResourceRequest.builder()
                .message_id(message_id)
                .file_key(download_code)
                .type("image")
                .build()
            )
            resp = self._lark_client.im.v1.message_resource.get(req)
            if not resp.success():
                logger.error("bot=%s Feishu download failed: code=%s msg=%s",
                             self.config.id, resp.code, resp.msg)
                return None, ""

            # resp.file is a file-like object
            if resp.file:
                data = resp.file.read(max_bytes + 1)
                if len(data) > max_bytes:
                    logger.warning("bot=%s Downloaded file exceeds size limit", self.config.id)
                    return None, ""
                # Infer content type
                content_type = "image/png"  # Feishu images are typically PNG
                if download_code.endswith((".jpg", ".jpeg")):
                    content_type = "image/jpeg"
                logger.info("bot=%s Downloaded from Feishu: %d bytes",
                            self.config.id, len(data))
                return data, content_type

            return None, ""
        except Exception as e:
            logger.error("bot=%s Feishu download error: %s", self.config.id, e)
            return None, ""
