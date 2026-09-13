ALTER TABLE orders ADD COLUMN IF NOT EXISTS strategy_id VARCHAR(64);
CREATE INDEX IF NOT EXISTS ix_orders_strategy_id ON orders(strategy_id);
