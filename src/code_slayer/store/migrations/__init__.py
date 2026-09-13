"""Packaged SQL migration files, applied in order by `code_slayer.store.db`.

Filenames are `<4-digit version>_<name>.sql`. A migration file's SQL must
start with a literal `BEGIN;` and must NOT commit — `db.migrate()` commits
each migration in the same transaction as its `schema_migrations` row.
"""
