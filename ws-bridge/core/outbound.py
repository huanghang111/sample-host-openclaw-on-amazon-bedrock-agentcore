"""Outbound file delivery — screenshots, [SEND_FILE:] markers, S3 URL conversion.

Extracted from dingtalk-bridge/bridge.py. Delegates actual sending to the
ChannelAdapter (adapter.send_image, adapter.send_file, adapter.send_link).
"""

import logging
import os
import re

from ws_bridge.core.s3 import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS

logger = logging.getLogger("ws-bridge.outbound")

SCREENSHOT_MARKER_RE = re.compile(r"\[SCREENSHOT:([^\]]+)\]")
SEND_FILE_MARKER_RE = re.compile(r"\[SEND_FILE:([^\]]+)\]")
# Matches S3 URLs the model may generate despite instructions
S3_URL_RE = re.compile(
    r"https?://(?:openclaw-user-files[^/]*\.s3[^/]*\.amazonaws\.com|s3[^/]*\.amazonaws\.com/openclaw-user-files[^/]*)"
    r"/([^\s?\")]+)"
)
MEDIA_MAX_BYTES = 10_000_000  # 10 MB — platform media upload limit


def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def extract_screenshots(text: str) -> tuple[str, list[str]]:
    """Extract [SCREENSHOT:key] markers from text. Returns (clean_text, [s3_keys])."""
    keys = SCREENSHOT_MARKER_RE.findall(text)
    clean = SCREENSHOT_MARKER_RE.sub("", text).strip()
    return clean, keys


def extract_send_files(text: str) -> tuple[str, list[str]]:
    """Extract [SEND_FILE:path] markers from text. Returns (clean_text, [relative_paths])."""
    paths = SEND_FILE_MARKER_RE.findall(text)
    clean = SEND_FILE_MARKER_RE.sub("", text).strip()
    return clean, paths


def convert_s3_urls_to_markers(text: str, namespace: str) -> str:
    """Convert S3 URLs in text to [SEND_FILE:path] markers.

    The model sometimes generates presigned S3 URLs despite instructions to use
    [SEND_FILE:path]. This intercepts those URLs and converts them.
    """
    prefix = f"{namespace}/"

    def _replace(match):
        s3_key = match.group(1)
        s3_key = s3_key.replace("%2F", "/").replace("%20", " ")
        if not s3_key.startswith(prefix):
            return match.group(0)
        relative_path = s3_key[len(prefix):]
        if not relative_path:
            return match.group(0)
        logger.info("Converted S3 URL to SEND_FILE marker: %s", relative_path)
        return f"[SEND_FILE:{relative_path}]"

    # Replace markdown-wrapped S3 URLs: [text](S3_URL)
    result = re.sub(
        r"\[[^\]]*\]\(\s*" + S3_URL_RE.pattern + r"[^\)]*\)",
        lambda m: f"[SEND_FILE:{m.group(1)[len(prefix):]!s}]"
        if m.group(1).startswith(prefix) and m.group(1)[len(prefix):]
        else m.group(0),
        text,
    )
    # Also handle bare URLs not in markdown links
    result = S3_URL_RE.sub(_replace, result)
    return result


def deliver_screenshot(s3_key: str, namespace: str, adapter, msg,
                       s3_service) -> None:
    """Fetch a screenshot from S3 and send it via the adapter."""
    image_bytes = s3_service.fetch_file(s3_key, namespace,
                                         required_prefix="_screenshots/")
    if not image_bytes:
        return

    receiver_id = msg.sender_id if not msg.is_group else msg.conversation_id
    is_group = msg.is_group
    conv_id = msg.conversation_id

    # Try native image upload (≤1MB for image type)
    if len(image_bytes) <= 1_000_000:
        media_id = adapter.upload_media(image_bytes, "screenshot.png", media_type="image")
        if media_id:
            adapter.send_image(receiver_id, media_id,
                               is_group=is_group, conversation_id=conv_id)
            return

    # Fallback: native file upload
    media_id = adapter.upload_media(image_bytes, "screenshot.png", media_type="file")
    if media_id:
        adapter.send_file(receiver_id, media_id, "screenshot.png", "png",
                          is_group=is_group, conversation_id=conv_id)
        return

    # Last resort: presigned URL
    logger.warning("Native screenshot upload failed, falling back to presigned URL")
    presigned_url = s3_service.generate_presigned_url(s3_key)
    if presigned_url:
        adapter.send_image(receiver_id, presigned_url,
                           is_group=is_group, conversation_id=conv_id)


def deliver_file(relative_path: str, namespace: str, adapter, msg,
                 s3_service) -> None:
    """Deliver a user file from S3 via the adapter (native upload or presigned URL)."""
    if ".." in relative_path:
        logger.error("Rejected SEND_FILE with path traversal: %s", relative_path)
        return

    s3_key = f"{namespace}/{relative_path}"

    # Verify the file exists
    head = s3_service.head_object(s3_key)
    if not head:
        logger.error("SEND_FILE target not found in S3: %s", s3_key)
        return

    file_size = head.get("ContentLength", 0)
    filename = relative_path.rsplit("/", 1)[-1] if "/" in relative_path else relative_path
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""

    receiver_id = msg.sender_id if not msg.is_group else msg.conversation_id
    is_group = msg.is_group
    conv_id = msg.conversation_id

    # Try native platform upload for files ≤10MB
    if file_size <= MEDIA_MAX_BYTES:
        file_bytes = s3_service.get_object_bytes(s3_key)
        if file_bytes:
            # Images ≤1MB: send as image for inline preview
            if ext in IMAGE_EXTENSIONS and len(file_bytes) <= 1_000_000:
                media_id = adapter.upload_media(file_bytes, filename, media_type="image")
                if media_id:
                    adapter.send_image(receiver_id, media_id,
                                       is_group=is_group, conversation_id=conv_id)
                    logger.info("Delivered image natively: %s (%s)",
                                filename, format_size(file_size))
                    return

            # All other files: send as file
            file_type = ext.lstrip(".") if ext else "file"
            media_id = adapter.upload_media(file_bytes, filename, media_type="file")
            if media_id:
                adapter.send_file(receiver_id, media_id, filename, file_type,
                                  is_group=is_group, conversation_id=conv_id)
                logger.info("Delivered file natively: %s (%s, %s)",
                            filename, ext, format_size(file_size))
                return
        logger.warning("Native upload failed for %s, falling back to presigned URL", filename)

    # Fallback: presigned URL link card
    presigned_url = s3_service.generate_presigned_url(s3_key)
    if not presigned_url:
        return
    size_str = format_size(file_size)
    if ext in VIDEO_EXTENSIONS:
        desc = f"Video · {size_str} · Click to download"
    else:
        desc = f"File · {size_str} · Click to download"
    adapter.send_link(receiver_id, filename, desc, presigned_url,
                      is_group=is_group, conversation_id=conv_id)
    logger.info("Delivered file via link card: %s (%s, %s)",
                filename, ext, format_size(file_size))
