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


def get_connection(register_pgvector_types: bool = True):
    """
    Returns a new psycopg2 connection with pgvector types registered
    (unless disabled), so Python lists/np arrays convert to/from the
    VECTOR column type automatically.

    register_pgvector_types=False is only for bootstrap paths (e.g. the
    AUTO_SETUP schema apply in app.main.lifespan) that must connect to
    a fresh database BEFORE the `vector` extension exists — otherwise
    registration itself raises "vector type not found in the database".
    """
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    if register_pgvector_types:
        register_vector(conn)
    return conn