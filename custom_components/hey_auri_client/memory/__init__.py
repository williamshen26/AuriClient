"""Long-term memory for owner mode, stored locally in PostgreSQL (pgvector).

Modules here that don't need Home Assistant (config, recall, redaction, db,
migrations, store, transcript) avoid importing it, so they can be tested on
their own.
"""
