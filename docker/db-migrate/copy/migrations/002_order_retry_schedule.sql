ALTER TABLE orders
    ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX ix_orders_status_next_attempt
    ON orders(status, next_attempt_at, created_at);
