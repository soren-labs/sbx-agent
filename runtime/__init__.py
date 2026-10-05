"""sbx-runtime: supervised daemon boundary code.

Runs inside the executor sandbox (or as a local subprocess under the local
backend). RFC 167 §09: ``runtime`` MUST import no ``control`` package, DB
repository or platform vault implementation — only ``protocol/`` data types.
"""
