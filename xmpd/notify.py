"""Desktop notification helper for xmpd.

Sends notifications via notify-send (osascript on macOS) when auto-auth
refresh fails, with rate limiting to prevent spam.
"""

import logging
import subprocess
import sys
import time

logger = logging.getLogger(__name__)

# Module-level rate limiting state
_last_notification_time: float = 0.0
_notification_cooldown: float = 3600.0  # 1 hour

_MACOS = sys.platform == "darwin"


def send_notification(
    title: str,
    message: str,
    urgency: str = "normal",
    icon: str = "dialog-warning",
) -> bool:
    """Send desktop notification via notify-send, or osascript on macOS.

    Notifications are rate-limited to a maximum of one per hour to avoid spam.

    Args:
        title: Notification title.
        message: Notification body text.
        urgency: Urgency level ("low", "normal", or "critical").
        icon: Icon name for the notification. urgency and icon have no
            macOS equivalent and are ignored there.

    Returns:
        True if notification was sent, False if rate-limited, failed, or
        notify-send is not available.
    """
    global _last_notification_time

    # Rate limiting
    now = time.time()
    if now - _last_notification_time < _notification_cooldown:
        logger.debug("Notification rate-limited (max 1 per hour)")
        return False

    if _MACOS:
        # Passed as arguments, not spliced into the script, so quotes in the
        # text can't break it
        cmd = [
            "osascript",
            "-e",
            "on run argv",
            "-e",
            "display notification (item 2 of argv) with title (item 1 of argv)",
            "-e",
            "end run",
            title,
            message,
        ]
    else:
        cmd = [
            "notify-send",
            "--urgency",
            urgency,
            "--icon",
            icon,
            "--app-name",
            "xmpd",
            title,
            message,
        ]
    try:
        subprocess.run(cmd, timeout=5, capture_output=True)
        _last_notification_time = now
        logger.info("Desktop notification sent: %s", title)
        return True
    except FileNotFoundError:
        logger.warning("%s not found; desktop notifications unavailable", cmd[0])
        return False
    except subprocess.TimeoutExpired:
        logger.warning("%s timed out", cmd[0])
        return False
    except Exception as e:
        logger.warning("Failed to send notification: %s", e)
        return False
