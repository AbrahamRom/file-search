CREATE TABLE IF NOT EXISTS files (
    file_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    last_modified DATETIME NOT NULL
);