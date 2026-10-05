"""Runtime-safe wire/data contracts shared by the control plane and sbx-runtime.

RFC 167 §09: ``protocol/`` holds data/schema compatibility only — no business
reducers, scheduling, DB access or secret handling. Both ``control`` (the
runtime client/ingress) and ``runtime`` (the daemon and Harness adapters)
import these types; ``runtime`` never imports ``control``.
"""
