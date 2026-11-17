"""Lifetime OS lock with diagnostic PID metadata, on Linux and Windows."""

import errno
import os
from pathlib import Path


class AlreadyRunningError(RuntimeError):
    def __init__(self, pid):
        self.pid = pid
        super().__init__(f"Another instance owns the lock (PID {pid or 'unknown'})")


class ProcessInstanceLock:
    def __init__(self, path):
        self.path = Path(path)
        self._handle = None

    @staticmethod
    def _lock(handle):
        if os.name == "nt":
            import msvcrt

            # Keep the lock outside the short PID record, so contenders can read it.
            handle.seek(64)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(handle):
        if os.name == "nt":
            import msvcrt

            handle.seek(64)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def acquire(self):
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        handle = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            try:
                self._lock(handle)
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                handle.seek(0)
                contents = handle.read(64).decode("ascii", errors="replace").strip()
                raise AlreadyRunningError(int(contents) if contents.isdigit() else None) from error
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()).encode("ascii"))
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            handle.close()
            raise
        self._handle = handle

    def release(self):
        handle = self._handle
        self._handle = None
        if handle is not None:
            try:
                self._unlock(handle)
            finally:
                handle.close()
        # The inode must remain in place: unlinking can split ownership between files.

