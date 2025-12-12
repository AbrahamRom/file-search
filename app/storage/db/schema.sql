-- ============================================================================
-- Storage Node Database Schema
-- ============================================================================

-- Main files table with metadata
CREATE TABLE IF NOT EXISTS files (
    file_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    last_modified DATETIME NOT NULL,
    -- Shard identifier for future horizontal partitioning
    -- NULL means this storage node holds all data (no sharding)
    shard_id TEXT DEFAULT NULL,
    -- Content hash for conflict detection (SHA256)
    content_hash TEXT DEFAULT NULL,
    -- Version counter per file_path (monotonic)
    version INTEGER DEFAULT 1,
    -- Origin node that wrote this version
    origin_node TEXT DEFAULT NULL,
    -- Primary epoch when written (for fencing)
    write_epoch INTEGER DEFAULT NULL,
    -- Timestamp when record was created/updated
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Index for name searches (used by search_files)
CREATE INDEX IF NOT EXISTS idx_files_name ON files(name);

-- Index for shard-based queries (future sharding support)
CREATE INDEX IF NOT EXISTS idx_files_shard ON files(shard_id);

-- ============================================================================
-- Full-Text Search Support (Índice Invertido)
-- For future content-based search functionality
-- ============================================================================

-- Keywords extracted from file contents
CREATE TABLE IF NOT EXISTS keywords (
    keyword_id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL UNIQUE,
    -- Total documents containing this keyword (for IDF calculation)
    doc_count INTEGER DEFAULT 1
);

-- Many-to-many relationship: files <-> keywords
-- Stores term frequency per file for relevance ranking
CREATE TABLE IF NOT EXISTS file_keywords (
    keyword TEXT NOT NULL,
    file_id TEXT NOT NULL,
    -- Term frequency in this document
    frequency INTEGER DEFAULT 1,
    -- Position data for phrase queries (JSON array of positions)
    positions TEXT DEFAULT NULL,
    PRIMARY KEY (keyword, file_id),
    FOREIGN KEY (file_id) REFERENCES files(file_id) ON DELETE CASCADE
);

-- Index for reverse lookups (find files by keyword)
CREATE INDEX IF NOT EXISTS idx_file_keywords_keyword ON file_keywords(keyword);

-- Index for forward lookups (find keywords for a file)
CREATE INDEX IF NOT EXISTS idx_file_keywords_file ON file_keywords(file_id);

-- ============================================================================
-- Sync and Replication Metadata
-- Tracks synchronization state between storage nodes
-- ============================================================================

CREATE TABLE IF NOT EXISTS sync_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Trigger to auto-update updated_at on files table
CREATE TRIGGER IF NOT EXISTS update_files_timestamp 
AFTER UPDATE ON files
BEGIN
    UPDATE files SET updated_at = CURRENT_TIMESTAMP WHERE file_id = NEW.file_id;
END;
