import re
from urllib.parse import urlparse

from control.domain.errors import require


def repository_identity(url):
    parsed = urlparse(url)
    require(
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment,
        "forbidden",
    )
    path = parsed.path.removesuffix(".git").strip("/")
    require(bool(re.fullmatch(r"[\w.-]+/[\w.-]+", path)), "forbidden")
    return "https://github.com/" + path


def validate_environment(spec):
    require(isinstance(spec, dict), "output_contract_invalid")
    for name, value in spec.get("env", {}).items():
        require(
            not re.search(r"token|password|secret|key|auth|^HOME$|^PATH$|^XDG_|^SBX_", name, re.I),
            "forbidden",
        )
        require(isinstance(value, str), "output_contract_invalid")
    for service in spec.get("services", []):
        require(
            bool(service.get("name")) and isinstance(service.get("argv"), list),
            "output_contract_invalid",
        )
        require(
            ".." not in service.get("cwd", ".") and not service.get("cwd", ".").startswith("/"),
            "forbidden",
        )
    return spec
