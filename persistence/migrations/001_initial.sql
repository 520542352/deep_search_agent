CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    title TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'archived')),
    created_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    updated_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    )
);

CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    parent_run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
    request_id TEXT,
    query TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (
            status IN (
                'queued',
                'running',
                'succeeded',
                'failed',
                'interrupted',
                'cancelled'
            )
        ),
    error_code TEXT,
    error_message TEXT,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    UNIQUE (thread_id, request_id)
);

CREATE UNIQUE INDEX one_active_run_per_thread
ON runs(thread_id)
WHERE status IN ('queued', 'running');

CREATE INDEX runs_thread_created_idx
ON runs(thread_id, created_at DESC);

CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    created_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    UNIQUE (thread_id, sequence)
);

CREATE INDEX messages_thread_sequence_idx
ON messages(thread_id, sequence);

CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    )
);

CREATE INDEX events_thread_id_idx
ON events(thread_id, id);

CREATE INDEX events_run_id_idx
ON events(run_id, id);

CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
    kind TEXT NOT NULL CHECK (kind IN ('upload', 'generated')),
    filename TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    media_type TEXT,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (
        strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    ),
    UNIQUE (thread_id, relative_path)
);

CREATE INDEX artifacts_thread_created_idx
ON artifacts(thread_id, created_at DESC);
