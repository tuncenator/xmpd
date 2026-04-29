"""Tests for playlist_patcher: M3U/XSPF patching and MPD queue tag updates.

Covers:
- patch_playlist_files: add indicator to M3U (right/left alignment)
- patch_playlist_files: remove indicator from M3U
- patch_playlist_files: idempotency (no double-add, no error on missing)
- patch_playlist_files: skip favorites playlists
- patch_playlist_files: add indicator to XSPF
- patch_playlist_files: remove indicator from XSPF
- patch_playlist_files: skip XSPF favorites
- patch_mpd_queue: updates title tags for matching queue entries
- patch_mpd_queue: skips non-matching entries
- patch_mpd_queue: idempotency (liked=True when already has indicator)
"""

from unittest.mock import MagicMock

from xmpd.playlist_patcher import patch_mpd_queue, patch_playlist_files

PROXY_URL = "http://localhost:8080/proxy/tidal/12345"
LIKE_CONFIG_DEFAULT = {"enabled": True, "tag": "+1", "alignment": "right"}
LIKE_CONFIG_LEFT = {"enabled": True, "tag": "+1", "alignment": "left"}
LIKE_CONFIG_DISABLED = {"enabled": False, "tag": "+1", "alignment": "right"}


# ---------------------------------------------------------------------------
# M3U patching tests
# ---------------------------------------------------------------------------


def _make_m3u(artist: str, title: str, url: str, indicator: str | None = None) -> str:
    display = title if not indicator else f"{title} {indicator}"
    return f"#EXTM3U\n#EXTINF:-1,{artist} - {display}\n{url}\n"


def _make_m3u_left(artist: str, title: str, url: str, indicator: str | None = None) -> str:
    if indicator:
        display = f"{indicator} {artist} - {title}"
    else:
        display = f"{artist} - {title}"
    return f"#EXTM3U\n#EXTINF:-1,{display}\n{url}\n"


class TestM3UPatching:
    def test_add_indicator_right_alignment(self, tmp_path):
        playlist_dir = tmp_path / "playlists"
        playlist_dir.mkdir()
        m3u_file = playlist_dir / "TD: chilax.m3u"
        m3u_file.write_text(_make_m3u("Skinshape", "Metanoia", PROXY_URL), encoding="utf-8")

        patch_playlist_files(PROXY_URL, True, playlist_dir, None, LIKE_CONFIG_DEFAULT, set())

        content = m3u_file.read_text(encoding="utf-8")
        assert "#EXTINF:-1,Skinshape - Metanoia [+1]" in content

    def test_add_indicator_left_alignment(self, tmp_path):
        playlist_dir = tmp_path / "playlists"
        playlist_dir.mkdir()
        m3u_file = playlist_dir / "TD: chilax.m3u"
        m3u_file.write_text(_make_m3u("Skinshape", "Metanoia", PROXY_URL), encoding="utf-8")

        patch_playlist_files(PROXY_URL, True, playlist_dir, None, LIKE_CONFIG_LEFT, set())

        content = m3u_file.read_text(encoding="utf-8")
        assert "#EXTINF:-1,[+1] Skinshape - Metanoia" in content

    def test_remove_indicator_right_alignment(self, tmp_path):
        playlist_dir = tmp_path / "playlists"
        playlist_dir.mkdir()
        m3u_file = playlist_dir / "TD: chilax.m3u"
        m3u_file.write_text(_make_m3u("Skinshape", "Metanoia", PROXY_URL, "[+1]"), encoding="utf-8")

        patch_playlist_files(PROXY_URL, False, playlist_dir, None, LIKE_CONFIG_DEFAULT, set())

        content = m3u_file.read_text(encoding="utf-8")
        assert "[+1]" not in content
        assert "#EXTINF:-1,Skinshape - Metanoia" in content

    def test_remove_indicator_left_alignment(self, tmp_path):
        playlist_dir = tmp_path / "playlists"
        playlist_dir.mkdir()
        m3u_file = playlist_dir / "TD: chilax.m3u"
        # Left-aligned indicator in the title
        m3u_file.write_text(
            "#EXTM3U\n#EXTINF:-1,[+1] Skinshape - Metanoia\n" + PROXY_URL + "\n",
            encoding="utf-8",
        )

        patch_playlist_files(PROXY_URL, False, playlist_dir, None, LIKE_CONFIG_LEFT, set())

        content = m3u_file.read_text(encoding="utf-8")
        assert "[+1]" not in content
        assert "Skinshape - Metanoia" in content

    def test_idempotent_add_already_has_indicator(self, tmp_path):
        playlist_dir = tmp_path / "playlists"
        playlist_dir.mkdir()
        m3u_file = playlist_dir / "TD: chilax.m3u"
        m3u_file.write_text(_make_m3u("Skinshape", "Metanoia", PROXY_URL, "[+1]"), encoding="utf-8")

        patch_playlist_files(PROXY_URL, True, playlist_dir, None, LIKE_CONFIG_DEFAULT, set())

        content = m3u_file.read_text(encoding="utf-8")
        # Should not double-add
        assert content.count("[+1]") == 1

    def test_idempotent_remove_already_no_indicator(self, tmp_path):
        playlist_dir = tmp_path / "playlists"
        playlist_dir.mkdir()
        m3u_file = playlist_dir / "TD: chilax.m3u"
        original = _make_m3u("Skinshape", "Metanoia", PROXY_URL)
        m3u_file.write_text(original, encoding="utf-8")

        patch_playlist_files(PROXY_URL, False, playlist_dir, None, LIKE_CONFIG_DEFAULT, set())

        content = m3u_file.read_text(encoding="utf-8")
        assert content == original

    def test_skip_favorites_playlist(self, tmp_path):
        playlist_dir = tmp_path / "playlists"
        playlist_dir.mkdir()
        m3u_file = playlist_dir / "TD: Favorites.m3u"
        original = _make_m3u("Skinshape", "Metanoia", PROXY_URL)
        m3u_file.write_text(original, encoding="utf-8")

        patch_playlist_files(
            PROXY_URL, True, playlist_dir, None, LIKE_CONFIG_DEFAULT, {"TD: Favorites"}
        )

        content = m3u_file.read_text(encoding="utf-8")
        assert content == original

    def test_does_not_patch_non_matching_url(self, tmp_path):
        playlist_dir = tmp_path / "playlists"
        playlist_dir.mkdir()
        other_url = "http://localhost:8080/proxy/tidal/99999"
        m3u_file = playlist_dir / "TD: chilax.m3u"
        original = _make_m3u("Skinshape", "Metanoia", other_url)
        m3u_file.write_text(original, encoding="utf-8")

        patch_playlist_files(PROXY_URL, True, playlist_dir, None, LIKE_CONFIG_DEFAULT, set())

        content = m3u_file.read_text(encoding="utf-8")
        assert content == original

    def test_disabled_like_indicator_skips_m3u(self, tmp_path):
        playlist_dir = tmp_path / "playlists"
        playlist_dir.mkdir()
        m3u_file = playlist_dir / "TD: chilax.m3u"
        original = _make_m3u("Skinshape", "Metanoia", PROXY_URL)
        m3u_file.write_text(original, encoding="utf-8")

        patch_playlist_files(PROXY_URL, True, playlist_dir, None, LIKE_CONFIG_DISABLED, set())

        content = m3u_file.read_text(encoding="utf-8")
        assert content == original

    def test_nonexistent_playlist_dir_does_not_raise(self, tmp_path):
        nonexistent = tmp_path / "does_not_exist"
        # Should not raise even if directory missing
        patch_playlist_files(PROXY_URL, True, nonexistent, None, LIKE_CONFIG_DEFAULT, set())


# ---------------------------------------------------------------------------
# XSPF patching tests
# ---------------------------------------------------------------------------


def _make_xspf(creator: str, title: str, url: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<playlist version="1" xmlns="http://xspf.org/ns/0/">\n'
        "  <trackList>\n"
        "    <track>\n"
        f"      <location>{url}</location>\n"
        f"      <creator>{creator}</creator>\n"
        f"      <title>{title}</title>\n"
        "      <duration>244000</duration>\n"
        "    </track>\n"
        "  </trackList>\n"
        "</playlist>\n"
    )


class TestXSPFPatching:
    def test_add_indicator_to_xspf(self, tmp_path):
        xspf_dir = tmp_path / "xspf"
        xspf_dir.mkdir()
        xspf_file = xspf_dir / "TD: chilax.xspf"
        xspf_file.write_text(_make_xspf("Skinshape", "Metanoia", PROXY_URL), encoding="utf-8")

        patch_playlist_files(
            PROXY_URL, True, tmp_path / "playlists", xspf_dir, LIKE_CONFIG_DEFAULT, set()
        )

        content = xspf_file.read_text(encoding="utf-8")
        assert "<title>Metanoia [+1]</title>" in content

    def test_remove_indicator_from_xspf(self, tmp_path):
        xspf_dir = tmp_path / "xspf"
        xspf_dir.mkdir()
        xspf_file = xspf_dir / "TD: chilax.xspf"
        xspf_file.write_text(_make_xspf("Skinshape", "Metanoia [+1]", PROXY_URL), encoding="utf-8")

        patch_playlist_files(
            PROXY_URL, False, tmp_path / "playlists", xspf_dir, LIKE_CONFIG_DEFAULT, set()
        )

        content = xspf_file.read_text(encoding="utf-8")
        assert "<title>Metanoia</title>" in content
        assert "[+1]" not in content

    def test_idempotent_xspf_add(self, tmp_path):
        xspf_dir = tmp_path / "xspf"
        xspf_dir.mkdir()
        xspf_file = xspf_dir / "TD: chilax.xspf"
        xspf_file.write_text(_make_xspf("Skinshape", "Metanoia [+1]", PROXY_URL), encoding="utf-8")

        patch_playlist_files(
            PROXY_URL, True, tmp_path / "playlists", xspf_dir, LIKE_CONFIG_DEFAULT, set()
        )

        content = xspf_file.read_text(encoding="utf-8")
        assert content.count("[+1]") == 1

    def test_skip_xspf_favorites(self, tmp_path):
        xspf_dir = tmp_path / "xspf"
        xspf_dir.mkdir()
        xspf_file = xspf_dir / "TD: Favorites.xspf"
        original = _make_xspf("Tycho", "Adrift", PROXY_URL)
        xspf_file.write_text(original, encoding="utf-8")

        patch_playlist_files(
            PROXY_URL,
            True,
            tmp_path / "playlists",
            xspf_dir,
            LIKE_CONFIG_DEFAULT,
            {"TD: Favorites"},
        )

        content = xspf_file.read_text(encoding="utf-8")
        assert content == original

    def test_xspf_none_dir_skipped(self, tmp_path):
        # xspf_dir=None should not raise
        patch_playlist_files(
            PROXY_URL, True, tmp_path / "playlists", None, LIKE_CONFIG_DEFAULT, set()
        )

    def test_xspf_no_match_unchanged(self, tmp_path):
        xspf_dir = tmp_path / "xspf"
        xspf_dir.mkdir()
        xspf_file = xspf_dir / "TD: chilax.xspf"
        other_url = "http://localhost:8080/proxy/tidal/99999"
        original = _make_xspf("Skinshape", "Metanoia", other_url)
        xspf_file.write_text(original, encoding="utf-8")

        patch_playlist_files(
            PROXY_URL, True, tmp_path / "playlists", xspf_dir, LIKE_CONFIG_DEFAULT, set()
        )

        content = xspf_file.read_text(encoding="utf-8")
        assert content == original


# ---------------------------------------------------------------------------
# MPD queue patching tests
# ---------------------------------------------------------------------------


class TestMPDQueuePatching:
    def _make_mpd_client(self, queue_entries: list[dict]) -> MagicMock:
        client = MagicMock()
        client.playlistinfo.return_value = queue_entries
        return client

    def test_updates_matching_queue_entry(self):
        client = self._make_mpd_client([
            {"id": "42", "file": PROXY_URL, "title": "Radiohead - Creep"},
        ])

        patch_mpd_queue(client, PROXY_URL, "Radiohead - Creep", True, LIKE_CONFIG_DEFAULT)

        client.cleartagid.assert_called_once_with("42", "Title")
        client.addtagid.assert_called_once_with("42", "Title", "Radiohead - Creep [+1]")

    def test_unlike_removes_indicator(self):
        client = self._make_mpd_client([
            {"id": "42", "file": PROXY_URL, "title": "Radiohead - Creep [+1]"},
        ])

        patch_mpd_queue(client, PROXY_URL, "Radiohead - Creep", False, LIKE_CONFIG_DEFAULT)

        client.cleartagid.assert_called_once_with("42", "Title")
        client.addtagid.assert_called_once_with("42", "Title", "Radiohead - Creep")

    def test_skips_non_matching_entry(self):
        other_url = "http://localhost:8080/proxy/tidal/99999"
        client = self._make_mpd_client([
            {"id": "10", "file": other_url, "title": "Other Artist - Other Track"},
        ])

        patch_mpd_queue(client, PROXY_URL, "Radiohead - Creep", True, LIKE_CONFIG_DEFAULT)

        client.cleartagid.assert_not_called()
        client.addtagid.assert_not_called()

    def test_updates_multiple_matching_entries(self):
        client = self._make_mpd_client([
            {"id": "42", "file": PROXY_URL, "title": "Radiohead - Creep"},
            {"id": "43", "file": PROXY_URL, "title": "Radiohead - Creep"},
        ])

        patch_mpd_queue(client, PROXY_URL, "Radiohead - Creep", True, LIKE_CONFIG_DEFAULT)

        assert client.cleartagid.call_count == 2
        assert client.addtagid.call_count == 2

    def test_left_alignment_in_queue(self):
        client = self._make_mpd_client([
            {"id": "42", "file": PROXY_URL, "title": "Radiohead - Creep"},
        ])

        patch_mpd_queue(client, PROXY_URL, "Radiohead - Creep", True, LIKE_CONFIG_LEFT)

        client.addtagid.assert_called_once_with("42", "Title", "[+1] Radiohead - Creep")

    def test_disabled_like_indicator_skips_queue(self):
        client = self._make_mpd_client([
            {"id": "42", "file": PROXY_URL, "title": "Radiohead - Creep"},
        ])

        patch_mpd_queue(client, PROXY_URL, "Radiohead - Creep", True, LIKE_CONFIG_DISABLED)

        client.cleartagid.assert_not_called()
        client.addtagid.assert_not_called()

    def test_empty_queue_does_not_raise(self):
        client = self._make_mpd_client([])

        # Should not raise
        patch_mpd_queue(client, PROXY_URL, "Radiohead - Creep", True, LIKE_CONFIG_DEFAULT)

        client.cleartagid.assert_not_called()

    def test_mpd_error_does_not_propagate(self):
        client = MagicMock()
        client.playlistinfo.side_effect = Exception("MPD connection lost")

        # Should not raise
        patch_mpd_queue(client, PROXY_URL, "Radiohead - Creep", True, LIKE_CONFIG_DEFAULT)
