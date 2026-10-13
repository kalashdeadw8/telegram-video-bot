import asyncio
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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

    def test_rejects_non_http_scheme(self):
        self.assertIsNone(asyncio.run(main.validate_video_url("ftp://example.com/video.mp4")))


class PendingDownloadTests(unittest.TestCase):
    def setUp(self):
        main.PENDING_DOWNLOADS.clear()

    def test_cleanup_expired_downloads(self):
        now = time.monotonic()
        old_id = "old_entry"
        fresh_id = "fresh_entry"
        main.PENDING_DOWNLOADS[old_id] = main.PendingDownload(
            "https://example.com/1", 1, 1, now - main.PENDING_DOWNLOAD_TTL_SECONDS - 10
        )
        main.PENDING_DOWNLOADS[fresh_id] = main.PendingDownload(
            "https://example.com/2", 1, 1, now
        )

        main._cleanup_pending_downloads()

        self.assertNotIn(old_id, main.PENDING_DOWNLOADS)
        self.assertIn(fresh_id, main.PENDING_DOWNLOADS)


class AudioDetectionTests(unittest.TestCase):
    def test_info_with_separate_audio_formats(self):
        info = {
            "requested_formats": [
                {"vcodec": "avc1.64001f", "acodec": "none"},
                {"vcodec": "none", "acodec": "mp4a.40.2"},
            ]
        }
        self.assertTrue(main._info_has_audio(info))

    def test_info_with_no_audio(self):
        info = {
            "requested_formats": [
                {"vcodec": "avc1.64001f", "acodec": "none"},
            ]
        }
        self.assertFalse(main._info_has_audio(info))

    def test_info_single_stream_with_audio(self):
        info = {
            "requested_formats": [
                {"vcodec": "avc1.64001f", "acodec": "aac"},
            ]
        }
        self.assertTrue(main._info_has_audio(info))


class RateLimitCleanupTests(unittest.TestCase):
    def setUp(self):
        main.USER_LAST_REQUEST.clear()

    def test_cleanup_user_requests(self):
        now = time.monotonic()
        main.USER_LAST_REQUEST[101] = now - main.USER_COOLDOWN_SECONDS - 5
        main.USER_LAST_REQUEST[102] = now - 2

        main._cleanup_user_request_times(now)

        self.assertNotIn(101, main.USER_LAST_REQUEST)
        self.assertIn(102, main.USER_LAST_REQUEST)


class iPhoneAudioSyncTests(unittest.TestCase):
    @patch("main.is_already_iphone_compatible", return_value=False)
    @patch("subprocess.run")
    def test_ensure_iphone_compatible_args_with_audio(self, mock_run, mock_compat):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        test_file = Path("dummy.mkv")

        with patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "stat", return_value=MagicMock(st_size=1000)), \
             patch.object(Path, "unlink"):
            out_file, success = main.ensure_iphone_compatible(test_file, has_audio=True)
            self.assertTrue(success)

        # Проверяем, что в команду переданы параметры стерео и асинхронного ресэмплинга
        args = mock_run.call_args[0][0]
        self.assertIn("-ac", args)
        self.assertEqual(args[args.index("-ac") + 1], "2")
        self.assertIn("-af", args)
        self.assertEqual(args[args.index("-af") + 1], "aresample=async=1000")
        self.assertIn("+faststart", args)

    @patch("main.is_already_iphone_compatible", return_value=False)
    @patch("subprocess.run")
    def test_ensure_iphone_compatible_args_without_audio(self, mock_run, mock_compat):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        test_file = Path("dummy.mkv")

        with patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "stat", return_value=MagicMock(st_size=1000)), \
             patch.object(Path, "unlink"):
            out_file, success = main.ensure_iphone_compatible(test_file, has_audio=False)
            self.assertTrue(success)

        args = mock_run.call_args[0][0]
        self.assertIn("-an", args)
        self.assertNotIn("-af", args)

    @patch("subprocess.run")
    @patch("main._get_duration_seconds", return_value=60.0)
    def test_compress_to_fit_args_with_audio(self, mock_dur, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        test_file = Path("dummy.mp4")

        with patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "stat", return_value=MagicMock(st_size=1000)), \
             patch.object(Path, "unlink"):
            out_file, success = main.compress_to_fit(test_file, 40 * 1024 * 1024, has_audio=True)
            self.assertTrue(success)

        args = mock_run.call_args[0][0]
        self.assertIn("-ac", args)
        self.assertEqual(args[args.index("-ac") + 1], "2")
        self.assertIn("-af", args)
        self.assertEqual(args[args.index("-af") + 1], "aresample=async=1000")
        self.assertIn("+faststart", args)

    @patch("main.is_already_iphone_compatible", return_value=False)
    @patch("subprocess.run")
    def test_ensure_iphone_compatible_includes_scale_filter(self, mock_run, mock_compat):
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        test_file = Path("dummy.mkv")

        with patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "stat", return_value=MagicMock(st_size=1000)), \
             patch.object(Path, "unlink"):
            out_file, success = main.ensure_iphone_compatible(test_file, has_audio=True)
            self.assertTrue(success)

        args = mock_run.call_args[0][0]
        self.assertIn("-vf", args)
        self.assertEqual(args[args.index("-vf") + 1], "scale=1280:1280:force_original_aspect_ratio=decrease:force_divisible_by=2")

    @patch("main.is_already_iphone_compatible", return_value=True)
    @patch("subprocess.run")
    def test_ensure_iphone_compatible_skips_when_already_compatible(self, mock_run, mock_compat):
        test_file = Path("dummy.mp4")
        out_file, success = main.ensure_iphone_compatible(test_file, has_audio=True)
        self.assertTrue(success)
        self.assertEqual(out_file, test_file)
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_is_already_iphone_compatible(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"streams": [{"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p", "width": 1920, "height": 1080}, {"codec_type": "audio", "codec_name": "aac"}]}',
        )
        self.assertTrue(main.is_already_iphone_compatible(Path("video.mp4")))

    def test_is_already_iphone_compatible_rejects_non_mp4(self):
        self.assertFalse(main.is_already_iphone_compatible(Path("video.mkv")))

    @patch("subprocess.run")
    def test_is_already_iphone_compatible_rejects_odd_dimensions(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"streams": [{"codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p", "width": 719, "height": 1280}]}',
        )
        self.assertFalse(main.is_already_iphone_compatible(Path("video.mp4")))

    @patch("main.is_already_iphone_compatible", side_effect=[False, True])
    @patch("subprocess.run")
    def test_fallback_when_ffmpeg_fails_but_file_is_compatible(self, mock_run, mock_compat):
        mock_run.return_value = MagicMock(returncode=1, stderr="transcode failed")
        test_file = Path("dummy.mp4")
        with patch.object(Path, "exists", return_value=False), \
             patch.object(Path, "unlink"):
            out_file, success = main.ensure_iphone_compatible(test_file, has_audio=True)
            self.assertTrue(success)
            self.assertEqual(out_file, test_file)


class AudioTaskTests(unittest.TestCase):
    def setUp(self):
        main.AUDIO_TASKS.clear()

    def test_cleanup_audio_tasks(self):
        now = time.monotonic()
        old_id = "old_task"
        fresh_id = "fresh_task"
        main.AUDIO_TASKS[old_id] = main.AudioTask(
            url="https://example.com/1",
            user_id=1,
            chat_id=1,
            created_at=now - main.AUDIO_TASK_TTL_SECONDS - 100,
        )
        main.AUDIO_TASKS[fresh_id] = main.AudioTask(
            url="https://example.com/2",
            user_id=1,
            chat_id=1,
            created_at=now,
        )

        main._cleanup_audio_tasks()
        self.assertNotIn(old_id, main.AUDIO_TASKS)
        self.assertIn(fresh_id, main.AUDIO_TASKS)


if __name__ == "__main__":
    unittest.main()
