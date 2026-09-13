CREATE TABLE orders (
    id VARCHAR(36) PRIMARY KEY,
    request_id VARCHAR(128) NOT NULL UNIQUE,
    exchange_id VARCHAR(32) NOT NULL,
    exchange_network VARCHAR(16) NOT NULL,
    symbol VARCHAR(64) NOT NULL,
    side VARCHAR(8) NOT NULL CHECK (side IN ('buy', 'sell')),
    order_type VARCHAR(16) NOT NULL CHECK (order_type IN ('market', 'limit')),
    quantity NUMERIC(36, 18) NOT NULL CHECK (quantity > 0),
    limit_price NUMERIC(36, 18),
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    rejection_reason TEXT,
    reduce_only BOOLEAN NOT NULL DEFAULT FALSE,
    filled_quantity NUMERIC(36, 18) NOT NULL DEFAULT 0,
    average_fill_price NUMERIC(36, 18),
    total_fee NUMERIC(36, 18) NOT NULL DEFAULT 0,
    cancellation_requested BOOLEAN NOT NULL DEFAULT FALSE,
    resting_since TIMESTAMPTZ,
    last_market_data_id VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK ((order_type = 'limit' AND limit_price IS NOT NULL) OR (order_type = 'market' AND limit_price IS NULL))
);

CREATE INDEX ix_orders_exchange_id ON orders(exchange_id);
CREATE INDEX ix_orders_status ON orders(status);
CREATE INDEX ix_orders_status_created ON orders(status, created_at);

CREATE TABLE fills (
    id VARCHAR(36) PRIMARY KEY,
    order_id VARCHAR(36) NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    exchange_id VARCHAR(32) NOT NULL,
    symbol VARCHAR(64) NOT NULL,
    side VARCHAR(8) NOT NULL,
    quantity NUMERIC(36, 18) NOT NULL,
    price NUMERIC(36, 18) NOT NULL,
    fee NUMERIC(36, 18) NOT NULL,
    liquidity_role VARCHAR(8) NOT NULL DEFAULT 'taker' CHECK (liquidity_role IN ('maker', 'taker')),
    market_data_id VARCHAR(64),
    executed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_fill_order_sequence UNIQUE(order_id, sequence)
);

CREATE INDEX ix_fills_order_id ON fills(order_id);

CREATE TABLE positions (
    id VARCHAR(36) PRIMARY KEY,
    exchange_id VARCHAR(32) NOT NULL,
    symbol VARCHAR(64) NOT NULL,
    quantity NUMERIC(36, 18) NOT NULL DEFAULT 0,
    average_entry_price NUMERIC(36, 18) NOT NULL DEFAULT 0,
    realized_pnl NUMERIC(36, 18) NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_position_exchange_symbol UNIQUE(exchange_id, symbol)
);

CREATE TABLE control_flags (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    kill_switch BOOLEAN NOT NULL DEFAULT FALSE,
    reason TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO control_flags(id, kill_switch) VALUES (1, FALSE);

CREATE TABLE service_heartbeats (
    service VARCHAR(64) PRIMARY KEY,
    healthy BOOLEAN NOT NULL DEFAULT TRUE,
    detail TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE daily_pnl (
    id VARCHAR(36) PRIMARY KEY,
    trade_date DATE NOT NULL,
    realized_pnl NUMERIC(36, 18) NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_daily_pnl_date UNIQUE(trade_date)
);
