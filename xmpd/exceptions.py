"""Custom exceptions for xmpd.

This module defines custom exception classes used throughout the xmpd application
for better error handling and debugging.
"""


class XMPDError(Exception):
    """Base exception for all xmpd errors."""

    pass


class YTMusicAuthError(XMPDError):
    """Raised when YouTube Music authentication fails."""

    pass


class YTMusicAPIError(XMPDError):
    """Raised when a YouTube Music API call fails."""

    pass


class YTMusicNotFoundError(XMPDError):
    """Raised when a requested resource is not found in YouTube Music."""

    pass


class ConfigError(XMPDError):
    """Raised when configuration is invalid or cannot be loaded."""

    pass


class PlayerError(XMPDError):
    """Raised when player operations fail."""

    pass


class ServerError(XMPDError):
    """Raised when socket server operations fail."""

    pass


class MPDConnectionError(XMPDError):
    """Raised when connection to MPD fails."""

    pass


class MPDPlaylistError(XMPDError):
    """Raised when MPD playlist operations fail."""

    pass


class ProxyError(XMPDError):
    """Base exception for proxy errors."""

    pass


class YouTubeStreamError(ProxyError):
    """Raised when YouTube stream fetch fails."""

    pass


class TrackNotFoundError(ProxyError):
    """Raised when track not found in store."""

    pass


class URLRefreshError(ProxyError):
    """Raised when URL refresh fails."""

    pass


class CookieExtractionError(XMPDError):
    """Raised when browser cookie extraction fails."""

    pass


class TidalAuthRequired(XMPDError):  # noqa: N818
    """Raised when the Tidal session cannot be loaded or has expired without a refresh path.

    The daemon catches this at the provider boundary and converts it to a
    warn-and-skip per the spec; the CLI surfaces it to the user with a hint
    to run ``xmpctl auth tidal``.
    """

    pass
