"""Write real favorites playlists for daemon search and rating tests."""

from pathlib import Path


def write_favorites(daemon, tracks):
    directory = Path(daemon.config["mpd_playlist_directory"])
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "YT: Liked Songs.m3u"
    path.write_text("#EXTM3U\n" + "".join(
        f"http://localhost:8080/proxy/{track.provider}/{track.track_id}\n"
        for track in tracks
    ))
    return path
