# Service architecture

HTTP is grouped by capability: Upload, Video, Conversation, Analysis, and Admin.
Handlers translate HTTP input/output only. Business orchestration belongs in
application services, persistence behind repositories/unit-of-work, and vendor
SDK calls behind adapters defined by ports.

Runtime topology:

- `api`: stateless HTTP replicas; never starts recurring jobs.
- `worker`: Temporal activity/workflow workers.
- `scheduler`: exactly one deployment replica retries `dispatch_pending` jobs.
- `lifecycle`: exactly one deployment replica runs retention and consistency repair.
- `db-migrate` and `minio-init`: one-shot startup jobs.

The database remains PostgreSQL/pgvector in this project. `SqlAlchemyUnitOfWork`
is the SQL boundary, `MinioAdapter`, `RedisAdapter`, `TemporalAdapter`,
`QdrantAdapter` (vector-index port), and `ModelAdapter` are replaceable at the
composition root. Tests may construct `Services` with fakes and need no network.

Production configuration is validated while loading `Settings`; default MinIO
credentials, the default webhook secret, an unauthenticated Redis URL, or an
empty metrics token prevent process startup.
