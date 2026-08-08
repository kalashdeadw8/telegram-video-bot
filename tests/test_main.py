import asyncio
import unittest
from unittest.mock import patch

import main


class UrlValidationTests(unittest.TestCase):
    def test_rejects_localhost(self):
        self.assertIsNone(asyncio.run(main.validate_video_url("http://127.0.0.1/private")))

    def test_rejects_credentials_in_url(self):
        self.assertIsNone(asyncio.run(main.validate_video_url("https://user:pass@example.com/video")))

    @patch("main._is_public_hostname", return_value=True)
    def test_accepts_public_http_url(self, _is_public_hostname):
        url = asyncio.run(main.validate_video_url("https://example.com/video.mp4,"))
        self.assertEqual(url, "https://example.com/video.mp4")


class PendingDownloadTests(unittest.TestCase):
    def test_expired_confirmation_is_detectable(self):
        pending = main.PendingDownload("https://example.com/video", 1, 1, 10.0)
        self.assertGreater(10.0 + main.PENDING_DOWNLOAD_TTL_SECONDS + 1 - pending.created_at,
                           main.PENDING_DOWNLOAD_TTL_SECONDS)


if __name__ == "__main__":
    unittest.main()
