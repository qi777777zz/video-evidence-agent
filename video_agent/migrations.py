"""Small, repeatable SQL migrations for deployments without Alembic."""

from sqlalchemy import text


MIGRATIONS = [
    ("0001-vector-search", [
        "ALTER TABLE videos ALTER COLUMN parser_version TYPE varchar(255)",
        "ALTER TABLE evidences ALTER COLUMN parser_version TYPE varchar(255)",
        "ALTER TABLE evidences ADD COLUMN IF NOT EXISTS embedding vector(512)",
        "ALTER TABLE evidences ADD COLUMN IF NOT EXISTS embedding_model varchar(160)",
        "ALTER TABLE evidences ADD COLUMN IF NOT EXISTS index_status varchar(20) NOT NULL DEFAULT 'pending'",
        "CREATE INDEX IF NOT EXISTS ix_evidence_embedding_hnsw ON evidences USING hnsw (embedding vector_cosine_ops) WHERE embedding IS NOT NULL",
    ]),
]


def run_migrations(engine) -> None:
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version varchar(100) PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        applied = set(connection.execute(text("SELECT version FROM schema_migrations")).scalars())
        for version, statements in MIGRATIONS:
            if version in applied:
                continue
            for statement in statements:
                connection.exec_driver_sql(statement)
            connection.execute(text("INSERT INTO schema_migrations(version) VALUES (:version)"), {"version": version})
