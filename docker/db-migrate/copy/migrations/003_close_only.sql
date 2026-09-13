ALTER TABLE control_flags
    ADD COLUMN close_only BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE control_flags
    ADD CONSTRAINT ck_control_flags_exclusive_modes
    CHECK (NOT (kill_switch AND close_only));
