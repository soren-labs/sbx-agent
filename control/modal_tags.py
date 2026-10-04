"""Modal's bounded external identity tags; execution metadata stays durable."""

# Modal supports ten sandbox tags. These eight describe authority/binding;
# compute, capability surfaces, resource refs and recovery claims belong to
# the control record, not the provider's identity/filter namespace.
MODAL_TAG_LIMIT = 10
MODAL_IDENTITY_TAGS = frozenset(
    {
        "owner",
        "session_id",
        "provider",
        "account_id",
        "hosted",
        "modal_connection",
        "modal_workspace",
        "runtime_image",
    }
)


def modal_tags(tags):
    return {key: value for key, value in tags.items() if key in MODAL_IDENTITY_TAGS}
