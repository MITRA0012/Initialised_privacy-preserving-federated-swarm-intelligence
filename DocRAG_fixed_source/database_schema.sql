-- SQLite database: ~/docrag.sqlite3
-- ChromaDB stores vectorized text chunks separately in ~/docrag_chroma.
CREATE TABLE groups (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    owner_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE users (
    id TEXT PRIMARY KEY,
    full_name TEXT NOT NULL,
    phone TEXT NOT NULL,
    unique_id TEXT NOT NULL COLLATE NOCASE UNIQUE,
    group_id TEXT NOT NULL REFERENCES groups(id),
    role TEXT NOT NULL CHECK(role IN ('host', 'subhost', 'user')),
    token TEXT UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE documents (
    id          TEXT PRIMARY KEY,             -- UUID; also the ChromaDB doc_id
    filename    TEXT NOT NULL,                -- original client filename
    file_type   TEXT NOT NULL,                -- pdf or docx
    mime_type   TEXT NOT NULL,
    file_size   INTEGER NOT NULL CHECK (file_size >= 0),
    file_data   BLOB NOT NULL,                -- original uploaded bytes
    chunks      INTEGER NOT NULL DEFAULT 0,
    chars       INTEGER NOT NULL DEFAULT 0,
    index_status TEXT NOT NULL DEFAULT 'stored', -- stored, indexed, failed
    index_error TEXT,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    group_id    TEXT REFERENCES groups(id)
);

CREATE INDEX idx_documents_created_at ON documents(created_at);
CREATE INDEX idx_documents_group ON documents(group_id);

-- Each DocRAG group is an isolated cluster. Kin remains in its own local store.
CREATE TABLE cluster_settings (
    group_id TEXT PRIMARY KEY REFERENCES groups(id),
    instructions TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Conversation transcripts are isolated by both cluster and signed-in member.
CREATE TABLE cluster_chat_messages (
    id TEXT PRIMARY KEY,
    group_id TEXT NOT NULL REFERENCES groups(id),
    user_id TEXT NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_cluster_chat_owner ON cluster_chat_messages(group_id, user_id, created_at);

-- SQLite maintains a keyword-search mirror of the active Chroma collection.
CREATE TABLE doc_chunks (
    chunk_id  TEXT PRIMARY KEY,
    doc_id    TEXT NOT NULL,
    filename  TEXT NOT NULL,
    chunk_num INTEGER NOT NULL,
    content   TEXT NOT NULL
);
CREATE INDEX idx_doc_chunks_doc ON doc_chunks(doc_id);

CREATE VIRTUAL TABLE doc_chunks_fts USING fts5(
    content,
    doc_id UNINDEXED,
    chunk_id UNINDEXED,
    filename UNINDEXED,
    chunk_num UNINDEXED,
    tokenize='unicode61 remove_diacritics 2'
);

-- Tracks which vector collection the keyword index currently mirrors.
CREATE TABLE app_metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
