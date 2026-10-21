"""External MCP entry point: `sbx mcp serve` runs SBX tools over stdio.

Codex and Claude Code launch the command as a subprocess. It talks to the SBX API with the
same ClientConfig and public SDK as the CLI; it never reads the database or runtime state.
"""

from sbx.mcp_server.server import serve

__all__ = ["serve"]
