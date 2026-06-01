from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class NotificationResult:
    ok: bool
    message: str


class DesktopNotifier:
    """Best-effort desktop notification adapter with no third-party dependency."""

    def __init__(self, timeout_seconds: int = 5):
        self.timeout_seconds = timeout_seconds

    def send(self, title: str, message: str) -> NotificationResult:
        system = platform.system().lower()
        if system == "darwin":
            return self._run(["osascript", "-e", self._osascript(title, message)])
        if system == "linux" and shutil.which("notify-send"):
            return self._run(["notify-send", title, message])
        if system == "windows":
            return self._windows_balloon(title, message)
        return NotificationResult(
            ok=False,
            message="Desktop notifications are not available on this platform yet.",
        )

    def _run(self, command: list[str]) -> NotificationResult:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except Exception as exc:
            return NotificationResult(ok=False, message=f"Notification failed: {exc}")
        if completed.returncode == 0:
            return NotificationResult(ok=True, message="Desktop notification sent.")
        detail = (completed.stderr or completed.stdout or "No details returned.").strip()
        return NotificationResult(ok=False, message=f"Notification failed: {detail}")

    def _windows_balloon(self, title: str, message: str) -> NotificationResult:
        escaped_title = title.replace("'", "''")
        escaped_message = message.replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "Add-Type -AssemblyName System.Drawing; "
            "$n=New-Object System.Windows.Forms.NotifyIcon; "
            "$n.Icon=[System.Drawing.SystemIcons]::Information; "
            f"$n.BalloonTipTitle='{escaped_title}'; "
            f"$n.BalloonTipText='{escaped_message}'; "
            "$n.Visible=$true; "
            "$n.ShowBalloonTip(5000); "
            "Start-Sleep -Milliseconds 800; "
            "$n.Dispose()"
        )
        return self._run(["powershell", "-NoProfile", "-Command", script])

    def _osascript(self, title: str, message: str) -> str:
        safe_title = title.replace('"', '\\"')
        safe_message = message.replace('"', '\\"')
        return f'display notification "{safe_message}" with title "{safe_title}"'
