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
    @patch("main.is_already_iphone_compatible", return_value=None)
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

    @patch("main.is_already_iphone_compatible", return_value=None)
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

    @patch("main.is_already_iphone_compatible", return_value=True)
    @patch("subprocess.run")
    @patch("main._get_duration_seconds", return_value=60.0)
    def test_compress_to_fit_args_with_audio(self, mock_dur, mock_run, mock_compat):
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

    @patch("main.is_already_iphone_compatible", return_value=None)
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
        self.assertTrue(args[args.index("-vf") + 1].startswith(
            "scale=1280:1280:force_original_aspect_ratio=decrease:force_divisible_by=2"))
        # Явно выбираем основной видеопоток (не обложку) и тег avc1 для «Фото» на iOS
        self.assertEqual(args[args.index("-map") + 1], "0:V:0")
        self.assertEqual(args[args.index("-tag:v") + 1], "avc1")

    @patch("main.is_already_iphone_compatible", return_value=True)
    @patch("subprocess.run")
    def test_ensure_iphone_compatible_always_transcodes(self, mock_run, mock_compat):
        """Даже «совместимый на вид» файл перекодируется — никаких пропусков по эвристике."""
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        test_file = Path("dummy.mp4")
        with patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "stat", return_value=MagicMock(st_size=1000)), \
             patch.object(Path, "unlink"):
            out_file, success = main.ensure_iphone_compatible(test_file, has_audio=True)
        self.assertTrue(success)
        self.assertNotEqual(out_file, test_file)
        self.assertTrue(any(call[0][0][0] == "ffmpeg" for call in mock_run.call_args_list))

    @patch("main.is_already_iphone_compatible", return_value=None)
    @patch("subprocess.run")
    def test_ensure_iphone_compatible_retries_with_lighter_settings(self, mock_run, mock_compat):
        def fake_run(cmd, **kwargs):
            if cmd[0] == "ffmpeg" and "veryfast" in cmd:
                return MagicMock(returncode=-9, stderr="Killed")
            return MagicMock(returncode=0, stderr="", stdout="{}")
        mock_run.side_effect = fake_run
        with patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "stat", return_value=MagicMock(st_size=1000)), \
             patch.object(Path, "unlink"):
            out_file, success = main.ensure_iphone_compatible(Path("dummy.mkv"), has_audio=True)
        self.assertTrue(success)
        presets = [c[0][0][c[0][0].index("-preset") + 1] for c in mock_run.call_args_list if c[0][0][0] == "ffmpeg"]
        self.assertEqual(presets, ["veryfast", "ultrafast"])

    @patch("subprocess.run")
    def test_is_already_iphone_compatible(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout='{"streams": [{"codec_type": "video", "codec_name": "h264", "codec_tag_string": "avc1", "profile": "High", "pix_fmt": "yuv420p", "width": 1920, "height": 1080}, {"codec_type": "audio", "codec_name": "aac"}]}')
        self.assertTrue(main.is_already_iphone_compatible(Path("video.mp4")))

    def test_is_already_iphone_compatible_rejects_non_mp4(self):
        self.assertFalse(main.is_already_iphone_compatible(Path("video.mkv")))

    @patch("subprocess.run")
    def test_rejects_hevc_hev1_and_opus(self, mock_run):
        """Регрессия: HEVC(hev1)+Opus (TikTok) не сохраняется в «Фото», в «Файлах» нет видеоряда."""
        mock_run.return_value = MagicMock(returncode=0, stdout=(
            '{"streams": [{"codec_type": "video", "codec_name": "hevc", "codec_tag_string": "hev1", '
            '"profile": "Main", "pix_fmt": "yuv420p", "width": 720, "height": 1280}, '
            '{"codec_type": "audio", "codec_name": "opus"}]}'))
        self.assertFalse(main.is_already_iphone_compatible(Path("video.mp4")))

    @patch("subprocess.run")
    def test_rejects_opus_audio_with_h264(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=(
            '{"streams": [{"codec_type": "video", "codec_name": "h264", "codec_tag_string": "avc1", "profile": "High", "pix_fmt": "yuv420p", "width": 1920, "height": 1080}, {"codec_type": "audio", "codec_name": "opus"}]}'))
        self.assertFalse(main.is_already_iphone_compatible(Path("video.mp4")))

    @patch("subprocess.run")
    def test_rejects_high10_profile(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=(
            '{"streams": [{"codec_type": "video", "codec_name": "h264", "codec_tag_string": "avc1", '
            '"profile": "High 10", "pix_fmt": "yuv420p10le", "width": 1920, "height": 1080}]}'))
        self.assertFalse(main.is_already_iphone_compatible(Path("video.mp4")))

    @patch("subprocess.run")
    def test_is_already_iphone_compatible_rejects_odd_dimensions(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=(
            '{"streams": [{"codec_type": "video", "codec_name": "h264", "codec_tag_string": "avc1", '
            '"profile": "High", "pix_fmt": "yuv420p", "width": 719, "height": 1280}]}'))
        self.assertFalse(main.is_already_iphone_compatible(Path("video.mp4")))

    @patch("subprocess.run")
    def test_returns_none_when_ffprobe_fails(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        self.assertIsNone(main.is_already_iphone_compatible(Path("video.mp4")))

    @patch("main.is_already_iphone_compatible", return_value=True)
    @patch("subprocess.run")
    def test_fallback_to_source_only_when_strictly_compatible(self, mock_run, mock_compat):
        mock_run.return_value = MagicMock(returncode=1, stderr="transcode failed")
        test_file = Path("dummy.mp4")
        with patch.object(Path, "exists", return_value=False), patch.object(Path, "unlink"):
            out_file, success = main.ensure_iphone_compatible(test_file, has_audio=True)
        self.assertTrue(success)
        self.assertEqual(out_file, test_file)

    @patch("main.is_already_iphone_compatible", return_value=False)
    @patch("subprocess.run")
    def test_reports_failure_when_source_incompatible(self, mock_run, mock_compat):
        mock_run.return_value = MagicMock(returncode=1, stderr="transcode failed")
        with patch.object(Path, "exists", return_value=False), patch.object(Path, "unlink"):
            out_file, success = main.ensure_iphone_compatible(Path("dummy.mp4"), has_audio=True)
        self.assertFalse(success)


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
