"""
Stream URL resolver using yt-dlp to extract direct audio URLs from YouTube video IDs.

This module provides functionality to resolve YouTube video IDs to streamable audio URLs
using yt-dlp, with in-memory caching to avoid repeated extractions. Stream URLs expire
after approximately 6 hours, so caching is limited to 5 hours by default.
"""

import concurrent.futures
import io
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import yt_dlp

logger = logging.getLogger(__name__)

# Consecutive failed extractions (not counting per-video problems such as a
# private or removed video) before on_repeated_failure fires. MPD skips a
# track it can't open in under a second, so a broken resolver empties the
# queue and stops playback without the user ever hearing why.
FAILURE_STREAK_ALERT = 5


@dataclass
class CachedURL:
    """Cached stream URL with expiration tracking.

    Attributes:
        url: The direct audio stream URL
        cached_at: When this URL was cached
        video_id: The YouTube video ID this URL corresponds to
    """
    url: str
    cached_at: datetime
    video_id: str


class StreamResolver:
    """Resolves YouTube video IDs to streamable audio URLs using yt-dlp.

    Features:
    - In-memory caching with configurable expiration
    - Graceful error handling for unavailable videos
    - Batch resolution with parallel processing
    - Automatic retry on network errors

    Example:
        resolver = StreamResolver(cache_hours=5)
        url = resolver.resolve_video_id("dQw4w9WgXcQ")
        if url:
            print(f"Stream URL: {url}")
    """

    def __init__(
        self,
        cache_hours: int = 5,
        should_stop_callback: Callable[[], bool] | None = None,
        cache_file: str | None = None,
        cookie_file: str | Path | None = None,
        on_repeated_failure: Callable[[str], None] | None = None,
    ):
        """Initialize resolver with cache duration.

        Args:
            cache_hours: How long to cache URLs before re-extraction (default: 5 hours)
            should_stop_callback: Callback returning True when resolution should be cancelled.
            cache_file: Optional path to JSON file for persistent cache storage.
            cookie_file: Netscape cookies.txt to give yt-dlp, written by auto-auth.
                While it is missing or unreadable, yt-dlp reads Firefox's
                profile directly instead.
            on_repeated_failure: Called once, with the last error, when
                FAILURE_STREAK_ALERT extractions fail in a row. Re-armed by
                the next success.
        """
        self._cache: dict[str, CachedURL] = {}
        # Guards _cache and the cache file: the proxy and batch resolves run in
        # worker threads.
        self._lock = threading.RLock()
        self._cache_hours = cache_hours
        self._cache_file = Path(cache_file).expanduser() if cache_file else None
        self.should_stop = should_stop_callback or (lambda: False)
        self._cookie_file = Path(cookie_file).expanduser() if cookie_file else None
        self._using_cookie_file: bool | None = None  # last source used, for logging changes
        self._on_repeated_failure = on_repeated_failure
        self._failure_streak = 0

        # Load persistent cache if enabled
        if self._cache_file:
            self._load_cache()

        logger.info(f"StreamResolver initialized with {cache_hours}h cache" +
                   (f", persistent cache at {self._cache_file}" if self._cache_file else ""))

    def resolve_video_id(self, video_id: str) -> str | None:
        """Get streamable audio URL for a YouTube video ID.

        This method first checks the cache for a valid (non-expired) URL. If not found
        or expired, it uses yt-dlp to extract the stream URL from YouTube.

        Args:
            video_id: YouTube video ID (e.g., "dQw4w9WgXcQ")

        Returns:
            Stream URL string if successful, None if video unavailable or extraction fails
        """
        # Check cache first
        with self._lock:
            if self._is_cache_valid(video_id):
                logger.debug(f"Cache hit for {video_id}")
                return self._cache[video_id].url

        logger.debug(f"Cache miss for {video_id}, extracting URL")

        # Extract URL with yt-dlp
        url = self._extract_url(video_id)

        if url:
            # Cache the result
            with self._lock:
                self._cache[video_id] = CachedURL(
                    url=url,
                    cached_at=datetime.now(),
                    video_id=video_id
                )
            logger.debug(f"Cached URL for {video_id}")

            # Persist cache if enabled
            if self._cache_file:
                self._save_cache()

        return url

    def resolve_batch(self, video_ids: list[str]) -> dict[str, str]:
        """Resolve multiple video IDs efficiently with parallel processing.

        This method processes video IDs in parallel with limited concurrency to avoid
        rate limiting. It returns only successful resolutions, logging failures.

        Args:
            video_ids: List of YouTube video IDs to resolve

        Returns:
            Dict mapping video_id -> stream URL for successful resolutions only
        """
        if not video_ids:
            return {}

        logger.info(f"Resolving batch of {len(video_ids)} video IDs")
        results: dict[str, str] = {}

        # Use ThreadPoolExecutor for parallel processing (max 10 concurrent)
        max_workers = min(10, len(video_ids))

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all tasks
            future_to_video_id = {
                executor.submit(self.resolve_video_id, video_id): video_id
                for video_id in video_ids
            }

            # Collect results as they complete
            completed = 0
            pending_futures = set(future_to_video_id)

            while pending_futures:
                # Check if we should stop (e.g., daemon shutting down)
                if self.should_stop():
                    logger.info(
                        f"Stream resolution cancelled after {completed}/{len(video_ids)} videos"
                    )
                    # Cancel all remaining futures
                    for f in pending_futures:
                        f.cancel()
                    break

                # Use timeout so we can check should_stop() regularly
                done, pending_futures = concurrent.futures.wait(
                    pending_futures, timeout=0.5, return_when=concurrent.futures.FIRST_COMPLETED
                )

                for future in done:
                    video_id = future_to_video_id[future]
                    completed += 1

                    try:
                        url = future.result()
                        if url:
                            results[video_id] = url
                        else:
                            logger.warning(f"Failed to resolve {video_id}")
                    except Exception as e:
                        logger.error(f"Exception resolving {video_id}: {e}")

                    # Log progress every 10 videos
                    if completed % 10 == 0:
                        logger.info(f"Progress: {completed}/{len(video_ids)} videos processed")

        success_rate = len(results) / len(video_ids) * 100 if video_ids else 0
        logger.info(
            f"Batch complete: {len(results)}/{len(video_ids)} successful ({success_rate:.1f}%)"
        )

        # Persist cache after batch resolution
        if self._cache_file and results:
            self._save_cache()

        return results

    def _cookie_options(self) -> dict:
        """Return the yt-dlp cookie option for one extraction.

        Prefers the saved cookie file. Its text goes in as a fresh StringIO so
        that yt-dlp, which writes cookies back to its cookie file on close,
        writes into a throwaway buffer: the shared file stays as auto-auth
        wrote it, and parallel extractions can't clobber each other.
        """
        text = None
        if self._cookie_file is not None:
            try:
                text = self._cookie_file.read_text(encoding="utf-8")
            except OSError as e:
                logger.debug(f"Cookie file {self._cookie_file} not readable: {e}")

        using_file = bool(text and text.strip())
        if using_file != self._using_cookie_file and self._cookie_file is not None:
            if using_file:
                logger.info(f"yt-dlp uses cookies from {self._cookie_file}")
            else:
                logger.warning(
                    f"Cookie file {self._cookie_file} missing or empty; "
                    "yt-dlp reads Firefox's profile directly"
                )
        self._using_cookie_file = using_file

        if using_file:
            return {'cookiefile': io.StringIO(text)}
        # Firefox cookies bypass YouTube's "not a bot" wall. yt-dlp's
        # 'firefox' covers Developer Edition too: it takes the most recently
        # used profile.
        return {'cookiesfrombrowser': ('firefox',)}

    def _ydl_opts(self) -> dict:
        """Build yt-dlp options. Called per YoutubeDL instance (see _cookie_options)."""
        return {
            # Prefer direct HTTPS URLs over HLS/DASH for proxy compatibility
            # Format priority: opus in webm (251) > m4a audio (140) > any audio
            # Explicitly exclude HLS/DASH manifests (m3u8_native, dash protocols)
            'format': (
                'bestaudio[protocol^=https][protocol!=m3u8_native]'
                '[protocol!=http_dash_segments][ext=webm]/'
                'bestaudio[protocol^=https][protocol!=m3u8_native]'
                '[protocol!=http_dash_segments]/'
                'bestaudio[protocol!=m3u8_native]/bestaudio/best'
            ),
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'nocheckcertificate': True,
            # Don't download, just extract info
            'skip_download': True,
            # Prefer non-HLS formats for direct streaming
            'prefer_free_formats': True,
            **self._cookie_options(),
            # Allow yt-dlp to fetch the EJS JS-challenge solver on demand;
            # without it YouTube returns only image storyboards.
            'remote_components': ['ejs:github'],
        }

    def _record_outcome(self, ok: bool, error: str = "") -> None:
        """Track the failure streak and fire on_repeated_failure once per streak."""
        with self._lock:
            if ok:
                self._failure_streak = 0
                return
            self._failure_streak += 1
            fire = self._failure_streak == FAILURE_STREAK_ALERT
        if not fire:
            return
        logger.error(
            f"YouTube stream resolution failed {FAILURE_STREAK_ALERT} times in a row; "
            f"last error: {error}"
        )
        if self._on_repeated_failure is not None:
            try:
                self._on_repeated_failure(error)
            except Exception as e:
                logger.warning(f"on_repeated_failure callback failed: {e}")

    def _extract_url(self, video_id: str) -> str | None:
        """Extract stream URL using yt-dlp.

        This method uses yt-dlp to fetch video information and extract the direct
        audio stream URL. It handles various failure modes gracefully.

        Args:
            video_id: YouTube video ID

        Returns:
            Stream URL if successful, None if extraction fails
        """
        video_url = f'https://youtube.com/watch?v={video_id}'

        try:
            with yt_dlp.YoutubeDL(self._ydl_opts()) as ydl:
                info = ydl.extract_info(video_url, download=False)

                if not info:
                    logger.info(f"No info extracted for {video_id}")
                    return None

                # Get the direct URL
                url = info.get('url')
                if not isinstance(url, str) or not url:
                    logger.warning(f"No URL in extracted info for {video_id}")
                    return None

                logger.debug(f"Extracted URL for {video_id}")
                self._record_outcome(True)
                return url

        except yt_dlp.utils.DownloadError as e:
            error_msg = str(e).lower()

            # Handle specific error cases with appropriate log levels
            if 'private video' in error_msg or 'this video is private' in error_msg:
                logger.info(f"Video {video_id} is private")
            elif 'video unavailable' in error_msg or 'not available' in error_msg:
                logger.info(f"Video {video_id} is unavailable")
            elif 'region' in error_msg or 'blocked' in error_msg:
                logger.info(f"Video {video_id} is region locked")
            elif 'removed' in error_msg or 'deleted' in error_msg:
                logger.info(f"Video {video_id} has been removed")
            else:
                # Unknown download error
                logger.warning(f"Download error for {video_id}: {e}")
                self._record_outcome(False, str(e))

            return None

        except yt_dlp.utils.ExtractorError as e:
            logger.warning(f"Extractor error for {video_id}: {e}")
            self._record_outcome(False, str(e))
            return None

        except Exception as e:
            # Network errors or unexpected issues
            # Retry once for network errors
            if 'network' in str(e).lower() or 'timeout' in str(e).lower():
                logger.debug(f"Network error for {video_id}, retrying once")
                time.sleep(1)

                try:
                    with yt_dlp.YoutubeDL(self._ydl_opts()) as ydl:
                        info = ydl.extract_info(video_url, download=False)
                        retry_url = info.get('url') if info else None
                        if isinstance(retry_url, str) and retry_url:
                            logger.debug(f"Retry successful for {video_id}")
                            self._record_outcome(True)
                            return retry_url
                except Exception as retry_error:
                    logger.warning(f"Retry failed for {video_id}: {retry_error}")
                    self._record_outcome(False, str(retry_error))
                    return None

            logger.error(f"Unexpected error extracting {video_id}: {e}")
            self._record_outcome(False, str(e))
            return None

    def _is_cache_valid(self, video_id: str) -> bool:
        """Check if cached URL is still valid (not expired).

        Args:
            video_id: YouTube video ID to check

        Returns:
            True if cache hit and not expired, False otherwise
        """
        with self._lock:
            cached = self._cache.get(video_id)
            if cached is None:
                return False

            if not self._is_fresh(cached):
                age = datetime.now() - cached.cached_at
                logger.debug(f"Cache expired for {video_id} (age: {age.total_seconds()/3600:.1f}h)")
                # Remove expired entry
                del self._cache[video_id]
                return False

            return True

    def _is_fresh(self, cached: CachedURL) -> bool:
        """Return True if ``cached`` is younger than the cache TTL."""
        return datetime.now() - cached.cached_at <= timedelta(hours=self._cache_hours)

    def invalidate(self, video_id: str) -> None:
        """Drop the cached URL for one video so the next resolve re-extracts.

        Used when a cached URL was rejected (e.g. 403), which can happen well
        before the cache TTL runs out.
        """
        with self._lock:
            if self._cache.pop(video_id, None) is None:
                return
        logger.debug(f"Invalidated cached URL for {video_id}")
        if self._cache_file:
            self._save_cache()

    def clear_cache(self) -> None:
        """Clear all cached URLs.

        This can be useful for testing or to force re-extraction of all URLs.
        """
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
        logger.info(f"Cleared cache ({count} entries)")

    def get_cache_stats(self) -> dict[str, int]:
        """Get cache statistics.

        Returns:
            Dict with cache_size and expired_count
        """
        with self._lock:
            total = len(self._cache)
            expired = sum(
                1 for video_id in list(self._cache.keys()) if not self._is_cache_valid(video_id)
            )

        return {
            'cache_size': total,
            'expired_count': expired,
            'valid_count': total - expired
        }

    def _load_cache(self) -> None:
        """Load cache from JSON file.

        Loads previously cached URLs from disk. Expired entries are discarded.
        """
        if not self._cache_file or not self._cache_file.exists():
            logger.debug("No cache file to load")
            return

        try:
            with open(self._cache_file) as f:
                cache_data = json.load(f)

            loaded = 0
            expired = 0
            for video_id, entry in cache_data.items():
                # Parse datetime from ISO format
                cached_at = datetime.fromisoformat(entry['cached_at'])

                # Check if expired
                age = datetime.now() - cached_at
                if age > timedelta(hours=self._cache_hours):
                    expired += 1
                    continue

                self._cache[video_id] = CachedURL(
                    url=entry['url'],
                    cached_at=cached_at,
                    video_id=video_id
                )
                loaded += 1

            logger.info(
                f"Loaded {loaded} cached URLs from {self._cache_file} "
                f"({expired} expired entries discarded)"
            )

        except Exception as e:
            logger.warning(f"Failed to load cache from {self._cache_file}: {e}")

    def _save_cache(self) -> None:
        """Save cache to JSON file.

        Saves current cache state to disk for persistence across restarts.
        Only saves valid (non-expired) entries. The file is replaced atomically
        so a crash mid-write cannot leave it truncated.
        """
        if not self._cache_file:
            return

        try:
            # Ensure directory exists
            self._cache_file.parent.mkdir(parents=True, exist_ok=True)

            # Hold the lock through the write so concurrent saves can't
            # interleave on the temp file.
            with self._lock:
                # Convert cache to JSON-serializable format, skipping expired
                # entries without deleting them mid-iteration
                cache_data = {
                    video_id: {
                        'url': cached_url.url,
                        'cached_at': cached_url.cached_at.isoformat(),
                        'video_id': cached_url.video_id
                    }
                    for video_id, cached_url in self._cache.items()
                    if self._is_fresh(cached_url)
                }

                tmp = self._cache_file.with_suffix(self._cache_file.suffix + '.tmp')
                with open(tmp, 'w') as f:
                    json.dump(cache_data, f, indent=2)
                os.replace(tmp, self._cache_file)

            logger.debug(f"Saved {len(cache_data)} cached URLs to {self._cache_file}")

        except Exception as e:
            logger.warning(f"Failed to save cache to {self._cache_file}: {e}")
