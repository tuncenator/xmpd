"""
Tests for xmpd/stream_resolver.py - Stream URL resolver with yt-dlp integration.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import yt_dlp

from xmpd.stream_resolver import FAILURE_STREAK_ALERT, CachedURL, StreamResolver


class TestCachedURL:
    """Tests for CachedURL dataclass."""

    def test_cached_url_creation(self):
        """Test creating a CachedURL instance."""
        now = datetime.now()
        cached = CachedURL(
            url="https://example.com/audio.m4a",
            cached_at=now,
            video_id="testvideoid"
        )

        assert cached.url == "https://example.com/audio.m4a"
        assert cached.cached_at == now
        assert cached.video_id == "testvideoid"


class TestStreamResolverInit:
    """Tests for StreamResolver initialization."""

    def test_init_default_cache_hours(self):
        """Test initialization with default cache hours."""
        resolver = StreamResolver()
        assert resolver._cache_hours == 5
        assert resolver._cache == {}

    def test_init_custom_cache_hours(self):
        """Test initialization with custom cache hours."""
        resolver = StreamResolver(cache_hours=3)
        assert resolver._cache_hours == 3
        assert resolver._cache == {}


class TestStreamResolverCaching:
    """Tests for caching functionality."""

    def test_is_cache_valid_miss(self):
        """Test cache miss for non-existent video."""
        resolver = StreamResolver()
        assert not resolver._is_cache_valid("nonexistent")

    def test_is_cache_valid_hit(self):
        """Test cache hit for recently cached video."""
        resolver = StreamResolver(cache_hours=5)
        resolver._cache["vid123"] = CachedURL(
            url="https://example.com/audio.m4a",
            cached_at=datetime.now(),
            video_id="vid123"
        )

        assert resolver._is_cache_valid("vid123")

    def test_is_cache_valid_expired(self):
        """Test cache miss for expired entry."""
        resolver = StreamResolver(cache_hours=5)
        # Cache entry from 6 hours ago (expired)
        old_time = datetime.now() - timedelta(hours=6)
        resolver._cache["vid123"] = CachedURL(
            url="https://example.com/audio.m4a",
            cached_at=old_time,
            video_id="vid123"
        )

        assert not resolver._is_cache_valid("vid123")
        # Verify expired entry was removed
        assert "vid123" not in resolver._cache

    def test_clear_cache(self):
        """Test clearing the cache."""
        resolver = StreamResolver()
        resolver._cache["vid1"] = CachedURL("url1", datetime.now(), "vid1")
        resolver._cache["vid2"] = CachedURL("url2", datetime.now(), "vid2")

        assert len(resolver._cache) == 2

        resolver.clear_cache()

        assert len(resolver._cache) == 0

    def test_invalidate(self, tmp_path):
        """Invalidating one video drops it from memory and the cache file."""
        cache_file = tmp_path / "stream_cache.json"
        resolver = StreamResolver(cache_file=str(cache_file))
        resolver._cache["vid1"] = CachedURL("url1", datetime.now(), "vid1")
        resolver._cache["vid2"] = CachedURL("url2", datetime.now(), "vid2")
        resolver._save_cache()

        resolver.invalidate("vid1")
        resolver.invalidate("missing")

        assert "vid1" not in resolver._cache
        assert "vid2" in resolver._cache
        reloaded = StreamResolver(cache_file=str(cache_file))
        assert "vid1" not in reloaded._cache
        assert "vid2" in reloaded._cache

    def test_save_cache_with_expired_entry(self, tmp_path):
        """An expired entry is left out of the file instead of aborting the save."""
        cache_file = tmp_path / "stream_cache.json"
        resolver = StreamResolver(cache_hours=5, cache_file=str(cache_file))
        resolver._cache["old"] = CachedURL("url-old", datetime.now() - timedelta(hours=6), "old")
        resolver._cache["new"] = CachedURL("url-new", datetime.now(), "new")

        resolver._save_cache()

        reloaded = StreamResolver(cache_file=str(cache_file))
        assert set(reloaded._cache) == {"new"}

    def test_save_cache_during_concurrent_writes(self, tmp_path):
        """Saves racing with cache writes in other threads neither fail nor corrupt the file."""
        cache_file = tmp_path / "stream_cache.json"
        resolver = StreamResolver(cache_file=str(cache_file))
        ids = [f"vid{i}" for i in range(2000)]

        with patch.object(resolver, '_extract_url', side_effect=lambda v: f"url-{v}"), \
                patch('xmpd.stream_resolver.logger') as mock_logger, \
                ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(resolver.resolve_video_id, ids))

        mock_logger.warning.assert_not_called()
        reloaded = StreamResolver(cache_file=str(cache_file))
        assert set(reloaded._cache) == set(ids)

    def test_get_cache_stats(self):
        """Test getting cache statistics."""
        resolver = StreamResolver(cache_hours=5)

        # Add fresh entry
        resolver._cache["vid1"] = CachedURL(
            url="url1",
            cached_at=datetime.now(),
            video_id="vid1"
        )

        # Add expired entry
        old_time = datetime.now() - timedelta(hours=6)
        resolver._cache["vid2"] = CachedURL(
            url="url2",
            cached_at=old_time,
            video_id="vid2"
        )

        stats = resolver.get_cache_stats()

        assert stats['cache_size'] == 2
        assert stats['valid_count'] == 1
        assert stats['expired_count'] == 1


class TestStreamResolverExtraction:
    """Tests for URL extraction with yt-dlp."""

    @patch('xmpd.stream_resolver.yt_dlp.YoutubeDL')
    def test_extract_url_success(self, mock_ydl_class):
        """Test successful URL extraction."""
        # Mock yt-dlp context manager
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = {
            'url': 'https://example.com/audio.m4a'
        }
        mock_ydl_class.return_value.__enter__.return_value = mock_ydl

        resolver = StreamResolver()
        url = resolver._extract_url('testvideoid')

        assert url == 'https://example.com/audio.m4a'
        mock_ydl.extract_info.assert_called_once_with(
            'https://youtube.com/watch?v=testvideoid',
            download=False
        )

    @patch('xmpd.stream_resolver.yt_dlp.YoutubeDL')
    def test_extract_url_no_info(self, mock_ydl_class):
        """Test extraction when no info is returned."""
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = None
        mock_ydl_class.return_value.__enter__.return_value = mock_ydl

        resolver = StreamResolver()
        url = resolver._extract_url('bad_video')

        assert url is None

    @patch('xmpd.stream_resolver.yt_dlp.YoutubeDL')
    def test_extract_url_no_url_in_info(self, mock_ydl_class):
        """Test extraction when info lacks URL field."""
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = {'title': 'Some Title'}
        mock_ydl_class.return_value.__enter__.return_value = mock_ydl

        resolver = StreamResolver()
        url = resolver._extract_url('bad_video')

        assert url is None

    @patch('xmpd.stream_resolver.yt_dlp.YoutubeDL')
    def test_extract_url_private_video(self, mock_ydl_class):
        """Test extraction of private video."""
        mock_ydl = MagicMock()
        mock_ydl.extract_info.side_effect = yt_dlp.utils.DownloadError('This video is private')
        mock_ydl_class.return_value.__enter__.return_value = mock_ydl

        resolver = StreamResolver()
        url = resolver._extract_url('private_vid')

        assert url is None

    @patch('xmpd.stream_resolver.yt_dlp.YoutubeDL')
    def test_extract_url_unavailable_video(self, mock_ydl_class):
        """Test extraction of unavailable video."""
        mock_ydl = MagicMock()
        mock_ydl.extract_info.side_effect = yt_dlp.utils.DownloadError('Video unavailable')
        mock_ydl_class.return_value.__enter__.return_value = mock_ydl

        resolver = StreamResolver()
        url = resolver._extract_url('unavailable_vid')

        assert url is None

    @patch('xmpd.stream_resolver.yt_dlp.YoutubeDL')
    def test_extract_url_region_locked(self, mock_ydl_class):
        """Test extraction of region-locked video."""
        mock_ydl = MagicMock()
        mock_ydl.extract_info.side_effect = yt_dlp.utils.DownloadError(
            'Video blocked in your region'
        )
        mock_ydl_class.return_value.__enter__.return_value = mock_ydl

        resolver = StreamResolver()
        url = resolver._extract_url('blocked_vid')

        assert url is None

    @patch('xmpd.stream_resolver.yt_dlp.YoutubeDL')
    def test_extract_url_removed_video(self, mock_ydl_class):
        """Test extraction of removed video."""
        mock_ydl = MagicMock()
        mock_ydl.extract_info.side_effect = yt_dlp.utils.DownloadError(
            'This video has been removed'
        )
        mock_ydl_class.return_value.__enter__.return_value = mock_ydl

        resolver = StreamResolver()
        url = resolver._extract_url('removed_vid')

        assert url is None

    @patch('xmpd.stream_resolver.yt_dlp.YoutubeDL')
    def test_extract_url_extractor_error(self, mock_ydl_class):
        """Test extraction with extractor error."""
        mock_ydl = MagicMock()
        mock_ydl.extract_info.side_effect = yt_dlp.utils.ExtractorError('Extractor failed')
        mock_ydl_class.return_value.__enter__.return_value = mock_ydl

        resolver = StreamResolver()
        url = resolver._extract_url('error_vid')

        assert url is None

    @patch('xmpd.stream_resolver.time.sleep')
    @patch('xmpd.stream_resolver.yt_dlp.YoutubeDL')
    def test_extract_url_network_error_retry_success(self, mock_ydl_class, mock_sleep):
        """Test extraction retries on network error and succeeds."""
        mock_ydl = MagicMock()
        # First call fails with network error, second succeeds
        mock_ydl.extract_info.side_effect = [
            Exception('Network timeout'),
            {'url': 'https://example.com/audio.m4a'}
        ]
        mock_ydl_class.return_value.__enter__.return_value = mock_ydl

        resolver = StreamResolver()
        url = resolver._extract_url('network_fail_vid')

        assert url == 'https://example.com/audio.m4a'
        assert mock_ydl.extract_info.call_count == 2
        mock_sleep.assert_called_once_with(1)

    @patch('xmpd.stream_resolver.time.sleep')
    @patch('xmpd.stream_resolver.yt_dlp.YoutubeDL')
    def test_extract_url_network_error_retry_fails(self, mock_ydl_class, mock_sleep):
        """Test extraction retries on network error and fails."""
        mock_ydl = MagicMock()
        # Both attempts fail
        mock_ydl.extract_info.side_effect = Exception('Network timeout')
        mock_ydl_class.return_value.__enter__.return_value = mock_ydl

        resolver = StreamResolver()
        url = resolver._extract_url('network_fail_vid')

        assert url is None
        assert mock_ydl.extract_info.call_count == 2
        mock_sleep.assert_called_once_with(1)

    @patch('xmpd.stream_resolver.yt_dlp.YoutubeDL')
    def test_extract_url_unexpected_error(self, mock_ydl_class):
        """Test extraction with unexpected error."""
        mock_ydl = MagicMock()
        mock_ydl.extract_info.side_effect = RuntimeError('Unexpected error')
        mock_ydl_class.return_value.__enter__.return_value = mock_ydl

        resolver = StreamResolver()
        url = resolver._extract_url('error_vid')

        assert url is None


class TestStreamResolverResolveVideoId:
    """Tests for resolve_video_id() method."""

    @patch('xmpd.stream_resolver.yt_dlp.YoutubeDL')
    def test_resolve_video_id_cache_miss(self, mock_ydl_class):
        """Test resolving video ID with cache miss."""
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = {
            'url': 'https://example.com/audio.m4a'
        }
        mock_ydl_class.return_value.__enter__.return_value = mock_ydl

        resolver = StreamResolver()
        url = resolver.resolve_video_id('testvideoid')

        assert url == 'https://example.com/audio.m4a'
        # Verify it was cached
        assert 'testvideoid' in resolver._cache
        assert resolver._cache['testvideoid'].url == 'https://example.com/audio.m4a'

    @patch('xmpd.stream_resolver.yt_dlp.YoutubeDL')
    def test_resolve_video_id_cache_hit(self, mock_ydl_class):
        """Test resolving video ID with cache hit."""
        resolver = StreamResolver()
        # Pre-populate cache
        resolver._cache['cached_vid'] = CachedURL(
            url='https://cached.com/audio.m4a',
            cached_at=datetime.now(),
            video_id='cached_vid'
        )

        url = resolver.resolve_video_id('cached_vid')

        assert url == 'https://cached.com/audio.m4a'
        # Verify yt-dlp was not called
        mock_ydl_class.assert_not_called()

    @patch('xmpd.stream_resolver.yt_dlp.YoutubeDL')
    def test_resolve_video_id_extraction_fails(self, mock_ydl_class):
        """Test resolving video ID when extraction fails."""
        mock_ydl = MagicMock()
        mock_ydl.extract_info.side_effect = yt_dlp.utils.DownloadError('Video unavailable')
        mock_ydl_class.return_value.__enter__.return_value = mock_ydl

        resolver = StreamResolver()
        url = resolver.resolve_video_id('bad_vid')

        assert url is None
        # Verify it was not cached
        assert 'bad_vid' not in resolver._cache

    @patch('xmpd.stream_resolver.yt_dlp.YoutubeDL')
    def test_resolve_video_id_expired_cache(self, mock_ydl_class):
        """Test resolving video ID with expired cache entry."""
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = {
            'url': 'https://new.com/audio.m4a'
        }
        mock_ydl_class.return_value.__enter__.return_value = mock_ydl

        resolver = StreamResolver(cache_hours=5)
        # Add expired entry
        old_time = datetime.now() - timedelta(hours=6)
        resolver._cache['expired_vid'] = CachedURL(
            url='https://old.com/audio.m4a',
            cached_at=old_time,
            video_id='expired_vid'
        )

        url = resolver.resolve_video_id('expired_vid')

        # Should extract new URL
        assert url == 'https://new.com/audio.m4a'
        # Should have new cached entry
        assert resolver._cache['expired_vid'].url == 'https://new.com/audio.m4a'
        # Cached_at should be recent
        age = datetime.now() - resolver._cache['expired_vid'].cached_at
        assert age.total_seconds() < 2  # Within last 2 seconds


class TestStreamResolverBatchResolution:
    """Tests for resolve_batch() method."""

    @patch('xmpd.stream_resolver.yt_dlp.YoutubeDL')
    def test_resolve_batch_empty_list(self, mock_ydl_class):
        """Test batch resolution with empty list."""
        resolver = StreamResolver()
        results = resolver.resolve_batch([])

        assert results == {}
        mock_ydl_class.assert_not_called()

    @patch('xmpd.stream_resolver.yt_dlp.YoutubeDL')
    def test_resolve_batch_all_success(self, mock_ydl_class):
        """Test batch resolution where all videos succeed."""
        mock_ydl = MagicMock()

        def mock_extract(url, download):
            video_id = url.split('=')[1]
            return {'url': f'https://example.com/{video_id}.m4a'}

        mock_ydl.extract_info.side_effect = mock_extract
        mock_ydl_class.return_value.__enter__.return_value = mock_ydl

        resolver = StreamResolver()
        video_ids = ['vid1', 'vid2', 'vid3']
        results = resolver.resolve_batch(video_ids)

        assert len(results) == 3
        assert results['vid1'] == 'https://example.com/vid1.m4a'
        assert results['vid2'] == 'https://example.com/vid2.m4a'
        assert results['vid3'] == 'https://example.com/vid3.m4a'

    @patch('xmpd.stream_resolver.yt_dlp.YoutubeDL')
    def test_resolve_batch_partial_success(self, mock_ydl_class):
        """Test batch resolution where some videos fail."""
        mock_ydl = MagicMock()

        def mock_extract(url, download):
            video_id = url.split('=')[1]
            if video_id == 'bad_vid':
                raise yt_dlp.utils.DownloadError('Video unavailable')
            return {'url': f'https://example.com/{video_id}.m4a'}

        mock_ydl.extract_info.side_effect = mock_extract
        mock_ydl_class.return_value.__enter__.return_value = mock_ydl

        resolver = StreamResolver()
        video_ids = ['vid1', 'bad_vid', 'vid3']
        results = resolver.resolve_batch(video_ids)

        assert len(results) == 2
        assert 'vid1' in results
        assert 'bad_vid' not in results
        assert 'vid3' in results

    @patch('xmpd.stream_resolver.yt_dlp.YoutubeDL')
    def test_resolve_batch_uses_cache(self, mock_ydl_class):
        """Test batch resolution uses cached entries."""
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = {'url': 'https://new.com/audio.m4a'}
        mock_ydl_class.return_value.__enter__.return_value = mock_ydl

        resolver = StreamResolver()
        # Pre-populate cache for one video
        resolver._cache['cached_vid'] = CachedURL(
            url='https://cached.com/audio.m4a',
            cached_at=datetime.now(),
            video_id='cached_vid'
        )

        video_ids = ['cached_vid', 'new_vid']
        results = resolver.resolve_batch(video_ids)

        assert len(results) == 2
        assert results['cached_vid'] == 'https://cached.com/audio.m4a'
        assert results['new_vid'] == 'https://new.com/audio.m4a'
        # Verify yt-dlp was only called once (for new_vid)
        assert mock_ydl.extract_info.call_count == 1

    @patch('xmpd.stream_resolver.yt_dlp.YoutubeDL')
    def test_resolve_batch_handles_exceptions(self, mock_ydl_class):
        """Test batch resolution handles exceptions gracefully."""
        mock_ydl = MagicMock()

        def mock_extract(url, download):
            video_id = url.split('=')[1]
            if video_id == 'exception_vid':
                raise RuntimeError('Unexpected error')
            return {'url': f'https://example.com/{video_id}.m4a'}

        mock_ydl.extract_info.side_effect = mock_extract
        mock_ydl_class.return_value.__enter__.return_value = mock_ydl

        resolver = StreamResolver()
        video_ids = ['vid1', 'exception_vid', 'vid3']
        results = resolver.resolve_batch(video_ids)

        # Should get results for vid1 and vid3, not exception_vid
        assert len(results) == 2
        assert 'vid1' in results
        assert 'exception_vid' not in results
        assert 'vid3' in results

    @patch('xmpd.stream_resolver.yt_dlp.YoutubeDL')
    def test_resolve_batch_parallel_processing(self, mock_ydl_class):
        """Test batch resolution processes videos in parallel."""
        mock_ydl = MagicMock()
        mock_ydl.extract_info.return_value = {'url': 'https://example.com/audio.m4a'}
        mock_ydl_class.return_value.__enter__.return_value = mock_ydl

        resolver = StreamResolver()
        # Test with 15 videos to verify parallel processing (should use max 10 workers)
        video_ids = [f'vid{i}' for i in range(15)]
        results = resolver.resolve_batch(video_ids)

        assert len(results) == 15
        assert mock_ydl.extract_info.call_count == 15


class TestCookieSource:
    """yt-dlp gets the saved cookie file when there is one, Firefox otherwise."""

    COOKIES = "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2000000000\tSID\tabc\n"

    def test_uses_cookie_file(self, tmp_path):
        cookie_file = tmp_path / "yt_cookies.txt"
        cookie_file.write_text(self.COOKIES)
        opts = StreamResolver(cookie_file=cookie_file)._ydl_opts()
        assert "cookiesfrombrowser" not in opts
        assert opts["cookiefile"].getvalue() == self.COOKIES

    def test_falls_back_to_firefox_when_file_missing(self, tmp_path):
        opts = StreamResolver(cookie_file=tmp_path / "missing.txt")._ydl_opts()
        assert opts["cookiesfrombrowser"] == ("firefox",)
        assert "cookiefile" not in opts

    def test_falls_back_to_firefox_when_file_empty(self, tmp_path):
        cookie_file = tmp_path / "yt_cookies.txt"
        cookie_file.write_text("")
        opts = StreamResolver(cookie_file=cookie_file)._ydl_opts()
        assert opts["cookiesfrombrowser"] == ("firefox",)

    def test_no_cookie_file_configured(self):
        assert StreamResolver()._ydl_opts()["cookiesfrombrowser"] == ("firefox",)

    def test_fresh_buffer_per_extraction(self, tmp_path):
        cookie_file = tmp_path / "yt_cookies.txt"
        cookie_file.write_text(self.COOKIES)
        resolver = StreamResolver(cookie_file=cookie_file)
        assert resolver._ydl_opts()["cookiefile"] is not resolver._ydl_opts()["cookiefile"]

    def test_yt_dlp_loads_cookies_and_leaves_file_alone(self, tmp_path):
        """A real YoutubeDL loads the cookies and, on close, saves into the
        throwaway buffer rather than the shared file."""
        cookie_file = tmp_path / "yt_cookies.txt"
        cookie_file.write_text(self.COOKIES)
        resolver = StreamResolver(cookie_file=cookie_file)
        with yt_dlp.YoutubeDL(resolver._ydl_opts()) as ydl:
            assert {c.name: c.value for c in ydl.cookiejar} == {"SID": "abc"}
            ydl.cookiejar.set_cookie(
                next(iter(ydl.cookiejar)).__class__(
                    0, "NEW", "x", None, False, ".youtube.com", True, True,
                    "/", True, True, 2000000000, False, None, None, {},
                )
            )
        assert cookie_file.read_text() == self.COOKIES


class TestFailureStreak:
    def _resolver_with_error(self, mock_ydl_class, message, callback):
        mock_ydl = MagicMock()
        mock_ydl.extract_info.side_effect = yt_dlp.utils.DownloadError(message)
        mock_ydl_class.return_value.__enter__.return_value = mock_ydl
        return StreamResolver(on_repeated_failure=callback), mock_ydl

    @patch('xmpd.stream_resolver.yt_dlp.YoutubeDL')
    def test_alerts_once_per_streak(self, mock_ydl_class):
        callback = MagicMock()
        resolver, mock_ydl = self._resolver_with_error(
            mock_ydl_class, "ERROR: could not find firefox cookies database", callback
        )
        for i in range(FAILURE_STREAK_ALERT * 2):
            assert resolver._extract_url(f"vid{i}") is None
        callback.assert_called_once()
        assert "could not find firefox cookies database" in callback.call_args.args[0]

        # A success re-arms the alert.
        mock_ydl.extract_info.side_effect = None
        mock_ydl.extract_info.return_value = {"url": "https://example.com/a"}
        assert resolver._extract_url("ok") == "https://example.com/a"
        mock_ydl.extract_info.side_effect = yt_dlp.utils.DownloadError("ERROR: boom")
        for i in range(FAILURE_STREAK_ALERT):
            resolver._extract_url(f"again{i}")
        assert callback.call_count == 2

    @patch('xmpd.stream_resolver.yt_dlp.YoutubeDL')
    def test_per_video_errors_do_not_count(self, mock_ydl_class):
        callback = MagicMock()
        resolver, _ = self._resolver_with_error(
            mock_ydl_class, "ERROR: Private video", callback
        )
        for i in range(FAILURE_STREAK_ALERT * 2):
            resolver._extract_url(f"vid{i}")
        callback.assert_not_called()

    @patch('xmpd.stream_resolver.yt_dlp.YoutubeDL')
    def test_callback_error_is_contained(self, mock_ydl_class):
        resolver, _ = self._resolver_with_error(
            mock_ydl_class, "ERROR: boom", MagicMock(side_effect=RuntimeError("no notify"))
        )
        for i in range(FAILURE_STREAK_ALERT):
            assert resolver._extract_url(f"vid{i}") is None
