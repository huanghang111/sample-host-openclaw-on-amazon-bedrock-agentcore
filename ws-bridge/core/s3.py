"""S3 file upload/download and presigned URL generation. Thread-safe."""

import logging
import time
import uuid

logger = logging.getLogger("ws-bridge.s3")

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
MAX_IMAGE_BYTES = 3_750_000
CONTENT_TYPE_TO_EXT = {
    "image/jpeg": "jpeg", "image/png": "png",
    "image/gif": "gif", "image/webp": "webp",
}
FILE_EXT_MAP = {
    "video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm",
    "video/avi": "avi", "video/x-msvideo": "avi",
    "application/pdf": "pdf",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/zip": "zip", "application/x-zip-compressed": "zip",
    "application/x-rar-compressed": "rar",
    "text/plain": "txt", "text/csv": "csv",
    "audio/mpeg": "mp3", "audio/wav": "wav", "audio/ogg": "ogg",
}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".avi"}


class S3Service:
    """S3 operations for user file uploads and outbound delivery. Thread-safe."""

    def __init__(self, s3_client, bucket_name: str):
        self.client = s3_client
        self.bucket = bucket_name

    def upload_image(self, image_bytes: bytes, namespace: str,
                     content_type: str) -> str | None:
        """Upload an image to S3. Returns the S3 key or None."""
        if not self.bucket or content_type not in ALLOWED_IMAGE_TYPES:
            return None
        if len(image_bytes) > MAX_IMAGE_BYTES:
            logger.warning("Image too large: %d bytes", len(image_bytes))
            return None
        ext = CONTENT_TYPE_TO_EXT.get(content_type, "bin")
        s3_key = f"{namespace}/_uploads/img_{int(time.time())}_{uuid.uuid4().hex[:8]}.{ext}"
        try:
            self.client.put_object(Bucket=self.bucket, Key=s3_key,
                                   Body=image_bytes, ContentType=content_type)
            logger.info("Uploaded image to s3://%s/%s", self.bucket, s3_key)
            return s3_key
        except Exception as e:
            logger.error("S3 image upload failed: %s", e)
            return None

    def upload_file(self, file_bytes: bytes, namespace: str, content_type: str,
                    prefix: str = "file", ext: str = "") -> str | None:
        """Upload a file to S3. Returns the S3 key or None."""
        if not self.bucket:
            return None
        if not ext:
            ext = FILE_EXT_MAP.get(content_type, "bin")
        s3_key = f"{namespace}/_uploads/{prefix}_{int(time.time())}_{uuid.uuid4().hex[:8]}.{ext}"
        try:
            self.client.put_object(Bucket=self.bucket, Key=s3_key,
                                   Body=file_bytes, ContentType=content_type)
            logger.info("Uploaded file to s3://%s/%s (%d bytes)",
                        self.bucket, s3_key, len(file_bytes))
            return s3_key
        except Exception as e:
            logger.error("S3 file upload failed: %s", e)
            return None

    def fetch_file(self, s3_key: str, namespace: str,
                   *, required_prefix: str = "") -> bytes | None:
        """Fetch file bytes from S3 with namespace validation."""
        if ".." in s3_key:
            logger.error("Rejected S3 key with path traversal: %s", s3_key)
            return None
        if required_prefix and not s3_key.startswith(f"{namespace}/{required_prefix}"):
            logger.error("Rejected S3 key outside expected prefix: %s", s3_key)
            return None
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=s3_key)
            return resp["Body"].read()
        except Exception as e:
            logger.error("Failed to fetch from S3: %s — %s", s3_key, e)
            return None

    def head_object(self, s3_key: str) -> dict | None:
        """Check if an S3 object exists. Returns metadata dict or None."""
        try:
            return self.client.head_object(Bucket=self.bucket, Key=s3_key)
        except Exception:
            return None

    def get_object_bytes(self, s3_key: str) -> bytes | None:
        """Download file bytes from S3."""
        try:
            resp = self.client.get_object(Bucket=self.bucket, Key=s3_key)
            return resp["Body"].read()
        except Exception as e:
            logger.error("Failed to download from S3: %s — %s", s3_key, e)
            return None

    def generate_presigned_url(self, s3_key: str, expires_in: int = 3600) -> str | None:
        """Generate a presigned GET URL for an S3 key."""
        try:
            return self.client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": s3_key},
                ExpiresIn=expires_in,
            )
        except Exception as e:
            logger.error("Failed to generate presigned URL for %s: %s", s3_key, e)
            return None
