"""Typed relational persistence for the unified architecture (RFC 167 §04).

PostgreSQL is the only production business authority. Repositories are thin
typed row mappers; all mutations happen inside ``SqlUnitOfWork``.
"""
