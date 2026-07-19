-- pgvector extension for the LightRAG Postgres backend.
-- Required by PGVectorStorage; HNSW_HALFVEC (used for 2000+ dim embeddings)
-- needs pgvector >= 0.7.0, which the pgvector/pgvector:pg16 image ships.
-- Idempotent: safe to run on every (re-)init of the data volume.
CREATE EXTENSION IF NOT EXISTS vector;
