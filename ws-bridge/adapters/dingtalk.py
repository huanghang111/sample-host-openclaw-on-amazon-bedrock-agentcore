"""DingTalk Stream mode adapter — one instance per bot, runs in dedicated thread.

Wraps dingtalk_stream.DingTalkStreamClient. Each instance has its own:
- WebSocket connection via start_forever()
- Access token cache (per clientId)
- robotCode for API calls
"""

import asyncio
import json
import logging
import time
import uuid
from urllib import request as urllib_request

import dingtalk_stream
from dingtalk_stream import AckMessage

from ws_bridge.adapters.base import BotConfig, BotStatus, ChannelAdapter, InboundMessage
from ws_bridge.core.s3 import ALLOWED_IMAGE_TYPES

logger = logging.getLogger("ws-bridge.dingtalk")

DINGTALK_API = "https://api.dingtalk.com"
DINGTALK_OAPI = "https://oapi.dingtalk.com"
MAX_TEXT_LEN = 20000


class DingTalkAdapter(ChannelAdapter):
    """DingTalk Stream mode adapter. One instance per bot, one thread."""

    def __init__(self, config: BotConfig, core):
        super().__init__(config, core)
        self._client_id = config.credentials["clientId"]
        self._client_secret = config.credentials["clientSecret"]
        # Per-bot access token cache
        self._token_cache = {"token": "", "expires_at": 0}
        self._stream_client = None

    @property
    def robot_code(self) -> str:
        return self._client_id

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Blocking: runs start_forever() in this thread."""
        credential = dingtalk_stream.Credential(self._client_id, self._client_secret)
        self._stream_client = dingtalk_stream.DingTalkStreamClient(credential)
        self._stream_client.register_callback_handler(
            dingtalk_stream.chatbot.ChatbotMessage.TOPIC,
            self._make_handler(),
        )

        # Pre-warm access token
        token = self._get_access_token()
        if token:
            logger.info("bot=%s DingTalk access token acquired", self.config.id)

        self.status = BotStatus.CONNECTED
        self.connected_at = time.time()
        logger.info("bot=%s starting DingTalk Stream (robotCode=%s)",
                     self.config.id, self.robot_code)
        self._stream_client.start_forever()

    def stop(self):
        self.status = BotStatus.STOPPED

    # ------------------------------------------------------------------
    # Access token (per-bot)
    # ------------------------------------------------------------------

    def _get_access_token(self) -> str:
        if self._token_cache["token"] and time.time() < self._token_cache["expires_at"] - 100:
            return self._token_cache["token"]

        url = f"{DINGTALK_API}/v1.0/oauth2/accessToken"
        data = json.dumps({"appKey": self._client_id,
                           "appSecret": self._client_secret}).encode()
        req = urllib_request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            resp = urllib_request.urlopen(req, timeout=10)
            result = json.loads(resp.read())
            token = result.get("accessToken", "")
            expire_in = result.get("expireIn", 7200)
            if token:
                self._token_cache["token"] = token
                self._token_cache["expires_at"] = time.time() + expire_in
                return token
            logger.error("bot=%s DingTalk access token response missing accessToken", self.config.id)
        except Exception as e:
            logger.error("bot=%s Failed to get DingTalk access token: %s", self.config.id, e)
        return ""

    def _api_headers(self) -> dict:
        token = self._get_access_token()
        return {
            "Content-Type": "application/json",
            "x-acs-dingtalk-access-token": token,
        }

    # ------------------------------------------------------------------
    # Message handler
    # ------------------------------------------------------------------

    def _make_handler(self):
        adapter = self

        class Handler(dingtalk_stream.ChatbotHandler):
            async def process(self, callback: dingtalk_stream.CallbackMessage):
                data = callback.data
                logger.info("bot=%s received message: msgtype=%s from=%s",
                             adapter.config.id,
                             data.get("msgtype"), data.get("senderNick", "unknown"))

                msg = adapter._parse_message(data)
                # Process in thread pool (boto3 calls are synchronous)
                asyncio.get_event_loop().run_in_executor(
                    None, adapter.core.process_message, adapter, msg)

                return AckMessage.STATUS_OK, "OK"

        return Handler()

    def _parse_message(self, data: dict) -> InboundMessage:
        """Convert raw DingTalk callback data to InboundMessage."""
        msg_type = data.get("msgtype", "text")

        # Extract text
        text = ""
        if msg_type == "text":
            text_obj = data.get("text", {})
            text = (text_obj.get("content", "") if isinstance(text_obj, dict)
                    else str(text_obj)).strip()
        elif msg_type == "richText":
            rich_text = data.get("content", {}).get("richText", [])
            for item in rich_text:
                if "text" in item:
                    text += item["text"]
            text = text.strip()

        # Extract image download code
        image_download_code = ""
        if msg_type == "picture":
            content = data.get("content", {})
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except json.JSONDecodeError:
                    content = {}
            image_download_code = content.get("downloadCode", "") or content.get("pictureDownloadCode", "")

        # Extract file info
        file_download_code = ""
        file_name = ""
        if msg_type == "file":
            content = data.get("content", {})
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except json.JSONDecodeError:
                    content = {}
            file_download_code = content.get("downloadCode", "")
            file_name = content.get("fileName", "unknown_file")

        # Extract video info
        video_download_code = ""
        if msg_type == "video":
            content = data.get("content", {})
            if isinstance(content, str):
                try:
                    content = json.loads(content)
                except json.JSONDecodeError:
                    content = {}
            video_download_code = content.get("downloadCode", "")

        return InboundMessage(
            bot_id=self.config.id,
            channel="dingtalk",
            sender_id=data.get("senderStaffId") or data.get("senderId", ""),
            sender_name=data.get("senderNick", ""),
            text=text,
            message_id=data.get("msgId", ""),
            is_group=data.get("conversationType") == "2",
            conversation_id=data.get("conversationId", "") if data.get("conversationType") == "2" else "",
            raw=data,
            image_download_code=image_download_code,
            file_download_code=file_download_code,
            file_name=file_name,
            video_download_code=video_download_code,
        )

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def _send_robot_message(self, receiver_id: str, msg_key: str, msg_param: str,
                             *, is_group: bool = False, conversation_id: str = ""):
        """Generic DingTalk robot message sender."""
        headers = self._api_headers()
        if is_group:
            url = f"{DINGTALK_API}/v1.0/robot/groupMessages/send"
            body = json.dumps({
                "robotCode": self.robot_code,
                "openConversationId": conversation_id or receiver_id,
                "msgKey": msg_key,
                "msgParam": msg_param,
            }).encode()
        else:
            url = f"{DINGTALK_API}/v1.0/robot/oToMessages/batchSend"
            body = json.dumps({
                "robotCode": self.robot_code,
                "userIds": [receiver_id],
                "msgKey": msg_key,
                "msgParam": msg_param,
            }).encode()
        req = urllib_request.Request(url, data=body, headers=headers)
        try:
            urllib_request.urlopen(req, timeout=15)
        except Exception as e:
            logger.error("bot=%s Failed to send %s to %s: %s",
                         self.config.id, msg_key, receiver_id, e)

    def send_text(self, receiver_id, text, *, is_group=False, conversation_id=""):
        chunks = ([text[i:i + MAX_TEXT_LEN]
                   for i in range(0, len(text), MAX_TEXT_LEN)]
                  if len(text) > MAX_TEXT_LEN else [text])
        for chunk in chunks:
            self._send_robot_message(
                receiver_id, "sampleText",
                json.dumps({"content": chunk}),
                is_group=is_group, conversation_id=conversation_id)

    def send_image(self, receiver_id, image_url_or_media_id, *,
                   is_group=False, conversation_id=""):
        self._send_robot_message(
            receiver_id, "sampleImageMsg",
            json.dumps({"photoURL": image_url_or_media_id}),
            is_group=is_group, conversation_id=conversation_id)

    def send_file(self, receiver_id, media_id, filename, file_type, *,
                  is_group=False, conversation_id=""):
        self._send_robot_message(
            receiver_id, "sampleFile",
            json.dumps({"mediaId": media_id, "fileName": filename, "fileType": file_type}),
            is_group=is_group, conversation_id=conversation_id)

    def send_link(self, receiver_id, title, text, message_url, *,
                  is_group=False, conversation_id=""):
        self._send_robot_message(
            receiver_id, "sampleLink",
            json.dumps({"title": title, "text": text, "messageUrl": message_url, "picUrl": ""}),
            is_group=is_group, conversation_id=conversation_id)

    # ------------------------------------------------------------------
    # Media upload/download
    # ------------------------------------------------------------------

    def upload_media(self, file_bytes, filename, media_type="file"):
        """Upload to DingTalk OAPI media storage. Returns media_id or None."""
        token = self._get_access_token()
        if not token:
            return None

        boundary = f"----DingTalkMedia{uuid.uuid4().hex[:16]}"
        parts = []
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="media"; filename="{filename}"\r\n'.encode())
        parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
        parts.append(file_bytes)
        parts.append(f"\r\n--{boundary}--\r\n".encode())
        body = b"".join(parts)

        url = f"{DINGTALK_OAPI}/media/upload?access_token={token}&type={media_type}"
        req = urllib_request.Request(url, data=body, headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        })
        try:
            resp = urllib_request.urlopen(req, timeout=60)
            result = json.loads(resp.read())
            if result.get("errcode", 0) != 0:
                logger.error("bot=%s DingTalk media upload error: errcode=%s errmsg=%s",
                             self.config.id, result.get("errcode"), result.get("errmsg"))
                return None
            media_id = result.get("media_id", "")
            if media_id:
                logger.info("bot=%s Uploaded media: media_id=%s type=%s size=%d",
                             self.config.id, media_id[:30], media_type, len(file_bytes))
                return media_id
            logger.error("bot=%s DingTalk media upload missing media_id: %s",
                         self.config.id, result)
        except Exception as e:
            logger.error("bot=%s DingTalk media upload failed: %s", self.config.id, e)
        return None

    def download_media(self, download_code, max_bytes, *, message_id=""):
        """DingTalk two-step download: get downloadUrl, then fetch bytes."""
        # Step 1: Get download URL
        token = self._get_access_token()
        if not token:
            return None, ""

        url = f"{DINGTALK_API}/v1.0/robot/messageFiles/download"
        data = json.dumps({"downloadCode": download_code, "robotCode": self.robot_code}).encode()
        logger.info("bot=%s Requesting DingTalk download URL: downloadCode=%s",
                     self.config.id, download_code[:30])
        req = urllib_request.Request(url, data=data, headers={
            "Content-Type": "application/json",
            "x-acs-dingtalk-access-token": token,
        })
        try:
            resp = urllib_request.urlopen(req, timeout=30)
            body = resp.read(1_000_000)
            result = json.loads(body.decode("utf-8", errors="replace"))
            download_url = result.get("downloadUrl", "")
            if not download_url:
                logger.error("bot=%s DingTalk download API missing downloadUrl: %s",
                             self.config.id, json.dumps(result, ensure_ascii=False)[:500])
                return None, ""
            # DingTalk OSS URLs may use HTTP — upgrade to HTTPS
            if download_url.startswith("http://"):
                download_url = "https://" + download_url[7:]
        except Exception as e:
            logger.error("bot=%s Failed to get DingTalk download URL: %s", self.config.id, e)
            return None, ""

        # Step 2: Download bytes from the OSS URL
        try:
            req2 = urllib_request.Request(download_url)
            resp2 = urllib_request.urlopen(req2, timeout=60)
            content_type = resp2.headers.get("Content-Type", "application/octet-stream").split(";")[0].strip()
            file_bytes = resp2.read(max_bytes + 1)
            if len(file_bytes) > max_bytes:
                logger.warning("bot=%s Downloaded file exceeds size limit: %d > %d",
                               self.config.id, len(file_bytes), max_bytes)
                return None, ""

            # Infer content type from URL extension if generic
            if content_type in ("application/octet-stream", "binary/octet-stream", ""):
                url_lower = download_url.split("?")[0].lower()
                if url_lower.endswith((".jpg", ".jpeg")):
                    content_type = "image/jpeg"
                elif url_lower.endswith(".png"):
                    content_type = "image/png"
                elif url_lower.endswith(".gif"):
                    content_type = "image/gif"
                elif url_lower.endswith(".webp"):
                    content_type = "image/webp"
                elif max_bytes <= 4_000_000:
                    content_type = "image/jpeg"  # safe default for images

            logger.info("bot=%s Downloaded: %d bytes, type=%s",
                        self.config.id, len(file_bytes), content_type)
            return file_bytes, content_type
        except Exception as e:
            logger.error("bot=%s Failed to download from URL: %s", self.config.id, e)
            return None, ""
