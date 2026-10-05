from protocol.manifests import installed_catalog
from protocol.runtime import ProtocolError

from runtime.harnesses.opencode import OpenCodeHarness


def catalog():
    return installed_catalog()


def get_harness(provider, **kwargs):
    if provider == "opencode":
        return OpenCodeHarness(**kwargs)
    raise ProtocolError("unsupported_capability")
