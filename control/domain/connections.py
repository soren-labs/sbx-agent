from control.domain.errors import require

KINDS = {"modal": {"token_id", "token_secret"}, "github": {"token"}, "opencode_zen": {"api_key"}}
PURPOSES = {
    "modal": {"compute", "validation", "teardown"},
    "github": {"clone", "delivery", "validation"},
    "opencode_zen": {"inference", "validation"},
}


def validate_material(kind, material):
    require(
        kind in KINDS and isinstance(material, dict) and set(material) == KINDS[kind],
        "credential_invalid",
    )
    require(
        all(isinstance(v, str) and 0 < len(v) <= 8192 for v in material.values()),
        "credential_invalid",
    )
    return material
