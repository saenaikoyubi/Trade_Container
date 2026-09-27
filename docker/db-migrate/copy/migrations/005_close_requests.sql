CREATE TABLE request_keys (
    request_id VARCHAR(128) PRIMARY KEY,
    operation_kind VARCHAR(20) NOT NULL CHECK (operation_kind IN ('order', 'close_request')),
    target_id VARCHAR(36) NOT NULL
);

INSERT INTO request_keys(request_id, operation_kind, target_id)
SELECT request_id, 'order', id FROM orders;

CREATE TABLE close_requests (
    id VARCHAR(36) PRIMARY KEY,
    request_id VARCHAR(128) NOT NULL UNIQUE,
    exchange_id VARCHAR(32) NOT NULL,
    symbol VARCHAR(64),
    submitted_symbol VARCHAR(64),
    strategy_id VARCHAR(64),
    status VARCHAR(20) NOT NULL,
    reason_code VARCHAR(64),
    detail TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE close_request_positions (
    id VARCHAR(36) PRIMARY KEY,
    close_request_id VARCHAR(36) NOT NULL REFERENCES close_requests(id),
    position_id VARCHAR(36) NOT NULL REFERENCES positions(id),
    symbol VARCHAR(64) NOT NULL,
    initial_position_quantity NUMERIC(36, 18) NOT NULL,
    remaining_position_quantity NUMERIC(36, 18) NOT NULL,
    status VARCHAR(20) NOT NULL,
    reason_code VARCHAR(64),
    detail TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_close_request_position UNIQUE(close_request_id, position_id)
);

CREATE INDEX ix_close_request_positions_parent ON close_request_positions(close_request_id);
CREATE INDEX ix_close_request_positions_position ON close_request_positions(position_id);
CREATE UNIQUE INDEX uq_close_active_position ON close_request_positions(position_id)
    WHERE status IN ('queued', 'running', 'waiting', 'canceling');

ALTER TABLE orders
    ADD COLUMN close_request_id VARCHAR(36) REFERENCES close_requests(id),
    ADD COLUMN close_position_id VARCHAR(36) REFERENCES close_request_positions(id),
    ADD COLUMN close_sequence INTEGER;

CREATE INDEX ix_orders_close_request_id ON orders(close_request_id);
CREATE UNIQUE INDEX uq_orders_close_position_sequence ON orders(close_position_id, close_sequence)
    WHERE close_position_id IS NOT NULL;
