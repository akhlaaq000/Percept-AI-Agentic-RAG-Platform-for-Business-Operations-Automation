"""
Shared database connection (Section 2 — PostgreSQL + pgvector,
single Postgres instance for both relational and vector data).
"""

import os
import psycopg2
from psycopg2.extras import RealDictCursor
from pgvector.psycopg2 import register_vector

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://rag_user:rag_password@postgres:5432/rag_platform",
)


def get_connection(register_pgvector_types: bool = True, autocommit: bool = False):
    """
    Returns a new psycopg2 connection with pgvector types registered
    (unless disabled), so Python lists/np arrays convert to/from the
    VECTOR column type automatically.

    register_pgvector_types=False is only for bootstrap paths (e.g. the
    AUTO_SETUP schema apply in app.main.lifespan) that must connect to
    a fresh database BEFORE the `vector` extension exists — otherwise
    registration itself raises "vector type not found in the database".

    autocommit=True is for the same bootstrap paths: register_vector()
    runs a query, and psycopg2 lazily opens a transaction on the first
    statement, after which flipping autocommit raises
    "set_session cannot be used inside a transaction". Setting it at
    connect time avoids that and lets DDL (CREATE EXTENSION / schema.sql)
    apply without an explicit transaction.
    """
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor, autocommit=autocommit)
    if register_pgvector_types:
        register_vector(conn)
    return conn