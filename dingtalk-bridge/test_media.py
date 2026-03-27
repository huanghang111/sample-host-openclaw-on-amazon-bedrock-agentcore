"""Unit tests for DingTalk bridge file/video/screenshot handling."""

import json
import os
import re
import unittest
from unittest.mock import MagicMock, patch, ANY

# Set required env vars before importing bridge
os.environ.setdefault("AGENTCORE_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/test")
os.environ.setdefault("AGENTCORE_QUALIFIER", "DEFAULT")
os.environ.setdefault("IDENTITY_TABLE_NAME", "openclaw-identity")
os.environ.setdefault("AWS_REGION", "us-west-2")

# Patch modules before importing bridge
import sys
sys.modules["dingtalk_stream"] = MagicMock()
sys.modules["dingtalk_stream.chatbot"] = MagicMock()

import bridge


class TestFormatSize(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(bridge._format_size(500), "500 B")

    def test_kilobytes(self):
        self.assertEqual(bridge._format_size(2048), "2.0 KB")

    def test_megabytes(self):
        self.assertEqual(bridge._format_size(3_500_000), "3.3 MB")


class TestExtractScreenshots(unittest.TestCase):
    def test_no_markers(self):
        clean, keys = bridge._extract_screenshots("Hello world")
        self.assertEqual(clean, "Hello world")
        self.assertEqual(keys, [])

    def test_single_marker(self):
        text = "Here is the screenshot [SCREENSHOT:ns/_screenshots/img.png]"
        clean, keys = bridge._extract_screenshots(text)
        self.assertEqual(clean, "Here is the screenshot")
        self.assertEqual(keys, ["ns/_screenshots/img.png"])

    def test_multiple_markers(self):
        text = "A [SCREENSHOT:a/_screenshots/1.png] and [SCREENSHOT:b/_screenshots/2.png] done"
        clean, keys = bridge._extract_screenshots(text)
        self.assertEqual(keys, ["a/_screenshots/1.png", "b/_screenshots/2.png"])
        self.assertNotIn("[SCREENSHOT:", clean)

    def test_marker_only(self):
        text = "[SCREENSHOT:ns/_screenshots/test.png]"
        clean, keys = bridge._extract_screenshots(text)
        self.assertEqual(clean, "")
        self.assertEqual(keys, ["ns/_screenshots/test.png"])


class TestFetchS3Image(unittest.TestCase):
    def test_rejects_path_traversal(self):
        result = bridge._fetch_s3_image("ns/../other/_screenshots/img.png", "ns")
        self.assertIsNone(result)

    def test_rejects_wrong_namespace(self):
        result = bridge._fetch_s3_image("other/_screenshots/img.png", "ns")
        self.assertIsNone(result)

    @patch.object(bridge, "s3_client")
    @patch.object(bridge, "USER_FILES_BUCKET", "test-bucket")
    def test_fetches_valid_key(self, mock_s3):
        mock_body = MagicMock()
        mock_body.read.return_value = b"image-data"
        mock_s3.get_object.return_value = {"Body": mock_body}

        result = bridge._fetch_s3_image("ns/_screenshots/img.png", "ns")
        self.assertEqual(result, b"image-data")
        mock_s3.get_object.assert_called_once_with(Bucket="test-bucket", Key="ns/_screenshots/img.png")


class TestGetDingtalkDownloadUrl(unittest.TestCase):
    @patch.object(bridge, "_get_dingtalk_credentials", return_value=("clientId", "secret"))
    @patch.object(bridge, "_get_dingtalk_access_token", return_value="token123")
    @patch("bridge.urllib_request.urlopen")
    def test_gets_download_url(self, mock_urlopen, mock_token, mock_creds):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"downloadUrl": "https://oss.example.com/img.jpg"}).encode()
        mock_urlopen.return_value = mock_resp

        url = bridge._get_dingtalk_download_url("code123")
        self.assertEqual(url, "https://oss.example.com/img.jpg")

    @patch.object(bridge, "_get_dingtalk_credentials", return_value=("", ""))
    @patch.object(bridge, "_get_dingtalk_access_token", return_value="")
    def test_no_credentials(self, mock_token, mock_creds):
        url = bridge._get_dingtalk_download_url("code123")
        self.assertEqual(url, "")


class TestDownloadFromUrl(unittest.TestCase):
    @patch("bridge.urllib_request.urlopen")
    def test_downloads_file(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.headers.get.return_value = "application/pdf"
        mock_resp.read.return_value = b"pdf-content"
        mock_urlopen.return_value = mock_resp

        data, ct = bridge._download_from_url("https://example.com/file.pdf")
        self.assertEqual(data, b"pdf-content")
        self.assertEqual(ct, "application/pdf")

    @patch("bridge.urllib_request.urlopen")
    def test_rejects_oversized_file(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.headers.get.return_value = "application/pdf"
        mock_resp.read.return_value = b"x" * 102
        mock_urlopen.return_value = mock_resp

        data, ct = bridge._download_from_url("https://example.com/file.pdf", max_bytes=100)
        self.assertIsNone(data)


class TestDownloadDingtalkMedia(unittest.TestCase):
    @patch.object(bridge, "_download_from_url", return_value=(b"pdf-content", "application/pdf"))
    @patch.object(bridge, "_get_dingtalk_download_url", return_value="https://oss.example.com/file.pdf")
    def test_two_step_download(self, mock_url, mock_download):
        data, ct = bridge._download_dingtalk_media("code123")
        self.assertEqual(data, b"pdf-content")
        mock_url.assert_called_once_with("code123")
        mock_download.assert_called_once()

    @patch.object(bridge, "_get_dingtalk_download_url", return_value="")
    def test_no_download_url(self, mock_url):
        data, ct = bridge._download_dingtalk_media("code123")
        self.assertIsNone(data)


class TestDownloadDingtalkImage(unittest.TestCase):
    @patch.object(bridge, "_download_from_url", return_value=(b"img-bytes", "image/jpeg"))
    @patch.object(bridge, "_get_dingtalk_download_url", return_value="https://oss.example.com/img.jpg")
    def test_downloads_image(self, mock_url, mock_download):
        data, ct = bridge._download_dingtalk_image("code123")
        self.assertEqual(data, b"img-bytes")
        self.assertEqual(ct, "image/jpeg")

    @patch.object(bridge, "_download_from_url", return_value=(b"img-bytes", "application/octet-stream"))
    @patch.object(bridge, "_get_dingtalk_download_url", return_value="https://oss.example.com/photo.png")
    def test_infers_type_from_url(self, mock_url, mock_download):
        data, ct = bridge._download_dingtalk_image("code123")
        self.assertEqual(ct, "image/png")

    @patch.object(bridge, "_download_from_url", return_value=(b"img-bytes", "application/octet-stream"))
    @patch.object(bridge, "_get_dingtalk_download_url", return_value="https://oss.example.com/unknown?x=1")
    def test_defaults_to_jpeg(self, mock_url, mock_download):
        data, ct = bridge._download_dingtalk_image("code123")
        self.assertEqual(ct, "image/jpeg")


class TestUploadFileToS3(unittest.TestCase):
    @patch.object(bridge, "s3_client")
    @patch.object(bridge, "USER_FILES_BUCKET", "test-bucket")
    def test_uploads_file(self, mock_s3):
        key = bridge._upload_file_to_s3(b"content", "ns", "application/pdf", prefix="file", ext="pdf")
        self.assertIsNotNone(key)
        self.assertTrue(key.startswith("ns/_uploads/file_"))
        self.assertTrue(key.endswith(".pdf"))
        mock_s3.put_object.assert_called_once()

    @patch.object(bridge, "USER_FILES_BUCKET", "")
    def test_no_bucket(self):
        key = bridge._upload_file_to_s3(b"content", "ns", "application/pdf")
        self.assertIsNone(key)

    @patch.object(bridge, "s3_client")
    @patch.object(bridge, "USER_FILES_BUCKET", "test-bucket")
    def test_ext_from_map(self, mock_s3):
        key = bridge._upload_file_to_s3(b"content", "ns", "video/mp4", prefix="vid")
        self.assertIsNotNone(key)
        self.assertTrue(key.endswith(".mp4"))


class TestSendDingtalkImage(unittest.TestCase):
    @patch.object(bridge, "_get_dingtalk_credentials", return_value=("clientId", "secret"))
    @patch.object(bridge, "_get_dingtalk_access_token", return_value="token123")
    @patch("bridge.urllib_request.urlopen")
    def test_sends_dm_image(self, mock_urlopen, mock_token, mock_creds):
        bridge._send_dingtalk_image("user123", "https://example.com/img.png")
        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        self.assertEqual(body["msgKey"], "sampleImageMsg")
        self.assertEqual(body["userIds"], ["user123"])
        params = json.loads(body["msgParam"])
        self.assertEqual(params["photoURL"], "https://example.com/img.png")

    @patch.object(bridge, "_get_dingtalk_credentials", return_value=("clientId", "secret"))
    @patch.object(bridge, "_get_dingtalk_access_token", return_value="token123")
    @patch("bridge.urllib_request.urlopen")
    def test_sends_group_image(self, mock_urlopen, mock_token, mock_creds):
        bridge._send_dingtalk_image("conv123", "https://example.com/img.png",
                                     is_group=True, conversation_id="conv123")
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        self.assertEqual(body["msgKey"], "sampleImageMsg")
        self.assertEqual(body["openConversationId"], "conv123")


class TestProcessMessageFile(unittest.TestCase):
    """Test file message type processing."""

    def setUp(self):
        bridge._processed_messages.clear()

    def _make_file_data(self, download_code="fc123", file_name="report.pdf", msg_id=None):
        return {
            "msgtype": "file",
            "content": json.dumps({"downloadCode": download_code, "fileName": file_name}),
            "senderStaffId": "staff001",
            "senderId": "sender001",
            "senderNick": "Test User",
            "conversationType": "1",
            "conversationId": "cid001",
            "msgId": msg_id or f"msg_file_{id(self)}_{download_code}",
        }

    @patch.object(bridge, "invoke_agent_runtime", return_value={"response": "Got it."})
    @patch.object(bridge, "send_dingtalk_message")
    @patch.object(bridge, "_upload_file_to_s3", return_value="dingtalk_staff001/_uploads/file_123_abc.pdf")
    @patch.object(bridge, "_download_dingtalk_media", return_value=(b"pdf-content", "application/pdf"))
    @patch.object(bridge, "get_or_create_session", return_value="ses_test")
    @patch.object(bridge, "resolve_user", return_value=("user_test", False))
    def test_file_message_flow(self, mock_resolve, mock_session, mock_download,
                                mock_upload, mock_send, mock_invoke):
        data = self._make_file_data()
        bridge._process_message_inner(data)

        # Verify download was called
        mock_download.assert_called_once_with("fc123")

        # Verify upload was called
        mock_upload.assert_called_once()
        args, kwargs = mock_upload.call_args
        self.assertEqual(args[0], b"pdf-content")
        self.assertEqual(args[1], "dingtalk_staff001")
        self.assertEqual(kwargs["prefix"], "file")

        # Verify agent was invoked with file info text
        mock_invoke.assert_called_once()
        agent_msg = mock_invoke.call_args[0][3]
        self.assertIn("report.pdf", agent_msg)
        self.assertIn("application/pdf", agent_msg)
        self.assertIn("_uploads/file_123_abc.pdf", agent_msg)

    @patch.object(bridge, "send_dingtalk_message")
    @patch.object(bridge, "_get_dingtalk_download_url", return_value="")
    @patch.object(bridge, "resolve_user", return_value=("user_test", False))
    def test_file_download_failure(self, mock_resolve, mock_url, mock_send):
        data = self._make_file_data()
        bridge._process_message_inner(data)
        mock_send.assert_called_once()
        self.assertIn("couldn't download", mock_send.call_args[0][1])


class TestProcessMessageVideo(unittest.TestCase):
    """Test video message type processing."""

    def setUp(self):
        bridge._processed_messages.clear()

    def _make_video_data(self, download_code="vc456", duration="30"):
        return {
            "msgtype": "video",
            "content": json.dumps({"downloadCode": download_code, "duration": duration}),
            "senderStaffId": "staff002",
            "senderId": "sender002",
            "senderNick": "Video User",
            "conversationType": "1",
            "conversationId": "cid002",
            "msgId": f"msg_{download_code}",
        }

    @patch.object(bridge, "invoke_agent_runtime", return_value={"response": "Noted."})
    @patch.object(bridge, "send_dingtalk_message")
    @patch.object(bridge, "_upload_file_to_s3", return_value="dingtalk_staff002/_uploads/vid_123_abc.mp4")
    @patch.object(bridge, "_download_dingtalk_media", return_value=(b"video-data", "video/mp4"))
    @patch.object(bridge, "get_or_create_session", return_value="ses_test2")
    @patch.object(bridge, "resolve_user", return_value=("user_test2", False))
    def test_video_message_flow(self, mock_resolve, mock_session, mock_download,
                                 mock_upload, mock_send, mock_invoke):
        data = self._make_video_data()
        bridge._process_message_inner(data)

        mock_download.assert_called_once_with("vc456")
        mock_upload.assert_called_once()
        args, kwargs = mock_upload.call_args
        self.assertEqual(args[0], b"video-data")
        self.assertEqual(kwargs["prefix"], "vid")
        self.assertEqual(kwargs["ext"], "mp4")

        agent_msg = mock_invoke.call_args[0][3]
        self.assertIn("video", agent_msg.lower())
        self.assertIn("30s", agent_msg)
        self.assertIn("_uploads/vid_123_abc.mp4", agent_msg)


class TestScreenshotDelivery(unittest.TestCase):
    """Test screenshot extraction and delivery in response handling."""

    def setUp(self):
        bridge._processed_messages.clear()

    @patch.object(bridge, "send_dingtalk_message")
    @patch.object(bridge, "_deliver_screenshot")
    @patch.object(bridge, "invoke_agent_runtime",
                  return_value={"response": "Here [SCREENSHOT:ns/_screenshots/shot.png] done"})
    @patch.object(bridge, "get_or_create_session", return_value="ses_test3")
    @patch.object(bridge, "resolve_user", return_value=("user_test3", False))
    def test_screenshot_extracted_from_response(self, mock_resolve, mock_session,
                                                 mock_invoke, mock_deliver, mock_send):
        data = {
            "msgtype": "text",
            "text": {"content": "take a screenshot"},
            "senderStaffId": "staff003",
            "senderId": "sender003",
            "senderNick": "SS User",
            "conversationType": "1",
            "conversationId": "cid003",
            "msgId": "msg_ss_001",
        }
        bridge._process_message_inner(data)

        # Screenshot should be delivered
        mock_deliver.assert_called_once_with(
            "ns/_screenshots/shot.png", "dingtalk_staff003",
            "staff003", "cid003", True
        )
        # Text reply should have marker removed
        mock_send.assert_called_once()
        reply_text = mock_send.call_args[0][1]
        self.assertNotIn("[SCREENSHOT:", reply_text)
        self.assertIn("Here", reply_text)


class TestExtractSendFiles(unittest.TestCase):
    def test_no_markers(self):
        clean, paths = bridge._extract_send_files("Hello world")
        self.assertEqual(clean, "Hello world")
        self.assertEqual(paths, [])

    def test_single_file(self):
        text = "Here is your file [SEND_FILE:documents/report.pdf] enjoy"
        clean, paths = bridge._extract_send_files(text)
        self.assertEqual(paths, ["documents/report.pdf"])
        self.assertNotIn("[SEND_FILE:", clean)
        self.assertIn("enjoy", clean)

    def test_multiple_files(self):
        text = "[SEND_FILE:a.pdf] and [SEND_FILE:b.png]"
        clean, paths = bridge._extract_send_files(text)
        self.assertEqual(paths, ["a.pdf", "b.png"])

    def test_upload_path(self):
        text = "Done [SEND_FILE:_uploads/file_123_abc.xlsx]"
        clean, paths = bridge._extract_send_files(text)
        self.assertEqual(paths, ["_uploads/file_123_abc.xlsx"])


class TestSendDingtalkLink(unittest.TestCase):
    @patch.object(bridge, "_get_dingtalk_credentials", return_value=("clientId", "secret"))
    @patch.object(bridge, "_get_dingtalk_access_token", return_value="token123")
    @patch("bridge.urllib_request.urlopen")
    def test_sends_dm_link(self, mock_urlopen, mock_token, mock_creds):
        bridge._send_dingtalk_link("user123", "report.pdf", "File · 1.2 MB",
                                    "https://s3.example.com/file")
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        self.assertEqual(body["msgKey"], "sampleLink")
        params = json.loads(body["msgParam"])
        self.assertEqual(params["title"], "report.pdf")
        self.assertEqual(params["messageUrl"], "https://s3.example.com/file")

    @patch.object(bridge, "_get_dingtalk_credentials", return_value=("clientId", "secret"))
    @patch.object(bridge, "_get_dingtalk_access_token", return_value="token123")
    @patch("bridge.urllib_request.urlopen")
    def test_sends_group_link(self, mock_urlopen, mock_token, mock_creds):
        bridge._send_dingtalk_link("conv123", "video.mp4", "Video · 5 MB",
                                    "https://s3.example.com/vid",
                                    is_group=True, conversation_id="conv123")
        req = mock_urlopen.call_args[0][0]
        body = json.loads(req.data)
        self.assertEqual(body["openConversationId"], "conv123")


class TestDeliverFile(unittest.TestCase):
    def test_rejects_path_traversal(self):
        bridge._deliver_file("../other/secret.txt", "ns", "u1", "c1", True)
        # Should log error and return without sending

    @patch.object(bridge, "_send_dingtalk_image")
    @patch.object(bridge, "s3_client")
    @patch.object(bridge, "USER_FILES_BUCKET", "test-bucket")
    def test_delivers_image_inline(self, mock_s3, mock_send_img):
        mock_s3.head_object.return_value = {"ContentLength": 50000}
        mock_s3.generate_presigned_url.return_value = "https://presigned/img.jpg"

        bridge._deliver_file("_uploads/img_123.jpg", "ns", "user1", "cid1", True)
        mock_send_img.assert_called_once_with("user1", "https://presigned/img.jpg")

    @patch.object(bridge, "_send_dingtalk_link")
    @patch.object(bridge, "s3_client")
    @patch.object(bridge, "USER_FILES_BUCKET", "test-bucket")
    def test_delivers_file_as_link(self, mock_s3, mock_send_link):
        mock_s3.head_object.return_value = {"ContentLength": 1_200_000}
        mock_s3.generate_presigned_url.return_value = "https://presigned/report.pdf"

        bridge._deliver_file("documents/report.pdf", "ns", "user1", "cid1", True)
        mock_send_link.assert_called_once()
        args = mock_send_link.call_args[0]
        self.assertEqual(args[0], "user1")
        self.assertEqual(args[1], "report.pdf")
        self.assertIn("1.1 MB", args[2])
        self.assertEqual(args[3], "https://presigned/report.pdf")

    @patch.object(bridge, "_send_dingtalk_link")
    @patch.object(bridge, "s3_client")
    @patch.object(bridge, "USER_FILES_BUCKET", "test-bucket")
    def test_delivers_video_as_link(self, mock_s3, mock_send_link):
        mock_s3.head_object.return_value = {"ContentLength": 5_000_000}
        mock_s3.generate_presigned_url.return_value = "https://presigned/clip.mp4"

        bridge._deliver_file("_uploads/vid_123.mp4", "ns", "user1", "cid1", True)
        mock_send_link.assert_called_once()
        args = mock_send_link.call_args[0]
        self.assertIn("Video", args[2])

    @patch.object(bridge, "s3_client")
    @patch.object(bridge, "USER_FILES_BUCKET", "test-bucket")
    def test_file_not_found(self, mock_s3):
        mock_s3.head_object.side_effect = Exception("NoSuchKey")
        bridge._deliver_file("nonexistent.txt", "ns", "u1", "c1", True)
        # Should log error, not raise


class TestOutboundFileInResponse(unittest.TestCase):
    """Test SEND_FILE marker extraction and delivery in response handling."""

    def setUp(self):
        bridge._processed_messages.clear()

    @patch.object(bridge, "send_dingtalk_message")
    @patch.object(bridge, "_deliver_file")
    @patch.object(bridge, "invoke_agent_runtime",
                  return_value={"response": "Here is your report [SEND_FILE:documents/report.pdf] enjoy"})
    @patch.object(bridge, "get_or_create_session", return_value="ses_test4")
    @patch.object(bridge, "resolve_user", return_value=("user_test4", False))
    def test_file_marker_extracted_and_delivered(self, mock_resolve, mock_session,
                                                  mock_invoke, mock_deliver, mock_send):
        data = {
            "msgtype": "text",
            "text": {"content": "send me the report"},
            "senderStaffId": "staff004",
            "senderId": "sender004",
            "senderNick": "File User",
            "conversationType": "1",
            "conversationId": "cid004",
            "msgId": "msg_file_out_001",
        }
        bridge._process_message_inner(data)

        mock_deliver.assert_called_once_with(
            "documents/report.pdf", "dingtalk_staff004",
            "staff004", "cid004", True
        )
        # Text reply should have marker removed
        mock_send.assert_called_once()
        reply_text = mock_send.call_args[0][1]
        self.assertNotIn("[SEND_FILE:", reply_text)
        self.assertIn("report", reply_text)


if __name__ == "__main__":
    unittest.main()
