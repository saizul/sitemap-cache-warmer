"""A lock file so that overlapping cron or systemd runs do not warm the same site twice."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path


class LockError(RuntimeError):
    """Another run holds the lock."""


class LockFile:
    """An exclusive, non-blocking lock on ``path``.

    On POSIX systems this uses ``fcntl.flock``: the kernel releases the lock when the process exits, even after a
    crash or ``kill -9``, so a stale file never blocks the next run. Elsewhere it falls back to creating the file
    exclusively, and treats a file whose process no longer runs as stale.
    The file contains the PID and start time of the holder.
    """

    def __init__(self, path: str | os.PathLike[str], *, wait: float = 0.0, poll: float = 0.2) -> None:
        self.path = Path(path).expanduser()
        self.wait = wait
        self.poll = poll
        self._fd: int | None = None

    def holder(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def acquire(self) -> None:
        deadline = time.monotonic() + self.wait
        while True:
            if self._try_acquire():
                return
            if time.monotonic() >= deadline:
                holder = self.holder()
                raise LockError(f"{self.path} is locked" + (f" by {holder}" if holder else ""))
            time.sleep(self.poll)

    def _try_acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if sys.platform != "win32":
            import fcntl

            # O_NOFOLLOW: never follow a symlink planted in a shared directory such as /tmp.
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                return False
            os.ftruncate(fd, 0)
            os.write(fd, self._stamp())
            self._fd = fd
            return True
        try:  # pragma: no cover - Windows only
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:  # pragma: no cover
            if self._is_stale():
                self.path.unlink(missing_ok=True)
                return self._try_acquire()
            return False
        os.write(fd, self._stamp())  # pragma: no cover
        self._fd = fd  # pragma: no cover
        return True  # pragma: no cover

    def _stamp(self) -> bytes:
        return f"pid={os.getpid()} started={time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n".encode()

    def _is_stale(self) -> bool:  # pragma: no cover - Windows only
        holder = self.holder()
        try:
            pid = int(holder.split()[0].removeprefix("pid="))
        except (ValueError, IndexError):
            return True
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        return False

    def release(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            if sys.platform != "win32":
                import fcntl

                os.ftruncate(fd, 0)
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
            if sys.platform == "win32":  # pragma: no cover
                self.path.unlink(missing_ok=True)

    def __enter__(self) -> LockFile:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
