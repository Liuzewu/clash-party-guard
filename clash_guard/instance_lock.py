"""Non-blocking per-state-directory process lock."""
import errno
import os
from pathlib import Path
import stat


class InstanceRunning(RuntimeError):
    """Another guard already owns this state directory."""


class InstanceLock:
    """Hold an exclusive OS lock until this context exits or the process dies."""

    def __init__(self, state_dir):
        self.path = Path(state_dir).expanduser() / 'guard.lock'
        self._fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(str(self.path), flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError('guard lock is not a regular file')
            if os.name == 'nt':
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b'0')
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                except OSError as error:
                    if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) or getattr(error, 'winerror', None) in (33, 36):
                        raise InstanceRunning('another guard is using this state directory') from error
                    raise
            else:
                import fcntl
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    if error.errno in (errno.EACCES, errno.EAGAIN):
                        raise InstanceRunning('another guard is using this state directory') from error
                    raise
            self._fd = fd
            return self
        except BaseException:
            os.close(fd)
            raise

    def __exit__(self, exc_type, exc, traceback):
        fd, self._fd = self._fd, None
        if fd is None:
            return False
        try:
            if os.name == 'nt':
                import msvcrt
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
        return False
