"""One runtime authentication retry using the broker's serialized rotation."""

import logging
import threading

from runtime.runner.constants import EXIT_AUTH_INVALID

log = logging.getLogger(__name__)


class AccessOnlyProcess:
    """Remove the native access cache as soon as the scoped operation ends."""

    def __init__(self, process, cleanup):
        self.process, self.cleanup = process, cleanup
        self.stdout = self._stream()
        self.cleaned = False
        self._cleanup_lock = threading.Lock()

    def _stream(self):
        try:
            yield from self.process.stdout
            self.process.wait()
        except BaseException:
            # A broken transport leaves the remote process alive. Close it
            # before removing its cache; sandbox-side finally is the backstop.
            self.kill()
            raise
        finally:
            self._clean()

    def _clean(self):
        with self._cleanup_lock:
            if self.cleaned:
                return
            try:
                self.cleanup()
            except Exception:
                # No provider exception text: it may contain credentials.
                log.warning("native operation cleanup failed")
                return
            self.cleaned = True

    def wait(self):
        try:
            return self.process.wait()
        except BaseException:
            self.kill()
            raise
        finally:
            self._clean()

    def kill(self):
        try:
            self.process.kill()
        finally:
            self._clean()

    def stderr_text(self, limit=2000):
        return self.process.stderr_text(limit)


class RetryCodexProcess:
    def __init__(self, process, version, refresh, reject):
        self.process, self.version = process, version
        self.refresh, self.reject = refresh, reject
        self.cancelled, self.code = False, None
        self.stdout = self._stream()

    def _stream(self):
        for attempt in range(2):
            yield from self.process.stdout
            code = self.process.wait()
            if code != EXIT_AUTH_INVALID or self.cancelled:
                self.code = code
                return
            if attempt == 0:
                self.process, self.version = self.refresh(self.version)
                if self.cancelled:
                    self.process.kill()
            else:
                self.reject(self.version)
                self.code = code

    def wait(self):
        if self.code is None:
            for _ in self.stdout:
                pass
        return self.code

    def kill(self):
        self.cancelled = True
        self.process.kill()

    def stderr_text(self, limit=2000):
        return self.process.stderr_text(limit)
