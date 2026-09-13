"""Durable state: SQLite schema, migrations, task persistence, the
content-addressed blob store, and the tool-operation journal.

Nothing in this package ever lives inside a target repository's working
tree (see adr/0002-external-durable-state.md) and nothing in this package
names a specific worker or model provider.
"""
