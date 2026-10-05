"""sbx-runtime daemon — supervised infrastructure inside one ExecutorLease.

RFC 167 §03: durable operation acceptance/dedupe, fencing, process
supervision, event spool/ack, Worktree/files/services work. It owns no
business database, no global scheduling, no agent reasoning.
"""
