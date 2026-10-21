"""Self-hosted operator tooling around the one control plane (``sbx server ...``).

Nothing here is a second core: the server it starts is ``control.composition serve``,
PostgreSQL stays the business authority and credentials stay in the vault. This package
only owns the operator's private config file, an optional private PostgreSQL cluster,
start-up checks and the background process lifecycle.
"""
