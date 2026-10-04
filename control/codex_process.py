"""One runtime authentication retry using the broker's serialized rotation."""

from runtime.runner.constants import EXIT_AUTH_INVALID


class AccessOnlyProcess:
    """Remove the native access cache as soon as the scoped operation ends."""

    def __init__(self, process, cleanup):
        self.process, self.cleanup = process, cleanup
        self.stdout = process.stdout
        self.cleaned = False

    def _clean(self):
        if not self.cleaned:
            self.cleanup()
            self.cleaned = True

    def wait(self):
        try:
            return self.process.wait()
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
