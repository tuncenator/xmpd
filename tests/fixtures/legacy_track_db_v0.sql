-- Legacy v0 track_store schema fixture.
-- Mirrors the exact schema from the user's production database at
-- ~/.config/xmpd/track_mapping.db before the Phase 5 compound-key migration.
--
-- PRAGMA user_version is intentionally NOT set (SQLite default = 0).

CREATE TABLE tracks (
    video_id TEXT PRIMARY KEY,
    stream_url TEXT,
    artist TEXT,
    title TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX idx_tracks_updated_at ON tracks(updated_at);

-- Sample rows mimicking real data shape:
-- - 11-char video IDs
-- - Mix of NULL and populated stream_url
-- - All rows have artist populated (matches observed production data)
-- - Realistic updated_at timestamps (Unix epoch, mid-2025 range)

INSERT INTO tracks (video_id, stream_url, artist, title, updated_at) VALUES
    ('2xOPkdtFeHM', NULL, 'Tommy Guerrero', 'Thin Brown Layer', 1761148106.611),
    ('5li6QC5NuLM', NULL, 'WITCH', 'Home Town', 1761148106.625),
    ('I5FT9J3w3EI', NULL, 'All Them Witches', 'Blood and Sand / Milk and Endless Waters', 1761148106.632),
    ('aAb3j9rcCrE', NULL, 'Wayra', 'Vertigo (feat. Sethe)', 1761148106.639),
    ('jofDfEI2m_o', NULL, 'John Cameron', 'Liquid Sunshine', 1761148106.647),
    ('dQWGCUnImWs', 'https://example.com/stream/dQWGCUnImWs', 'Bonobo', 'Stay The Same (feat. Andreya Triana)', 1761148341.582),
    ('DJCB1ZlseJ8', 'https://example.com/stream/DJCB1ZlseJ8', 'Men I Trust', 'Show Me How', 1761149327.914),
    ('kR0gIEGaiSE', NULL, 'Khruangbin', 'Time (You and I)', 1761148106.655),
    ('Qr4igYPMSS8', 'https://example.com/stream/Qr4igYPMSS8', 'Radiohead', 'Everything In Its Right Place', 1761150012.301),
    ('xN0FFK8JSYE', NULL, 'Tame Impala', 'Let It Happen', 1761148106.671);
