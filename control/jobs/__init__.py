"""Durable Job execution — the only path slow effects take (RFC 167 §04).

A Worker claims due/expired rows generationally, runs the registered
handler, and settles: success, retry with bounded backoff, or failure at
the attempt limit. Claim generations fence stale holders — a worker that
loses its lease cannot settle a row another worker reclaimed.
"""
