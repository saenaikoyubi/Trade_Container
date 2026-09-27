ALTER TABLE close_requests
    ADD COLUMN generation_mode VARCHAR(16) NOT NULL DEFAULT 'eager'
    CHECK (generation_mode IN ('eager', 'incremental'));
