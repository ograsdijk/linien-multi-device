CREATE TABLE IF NOT EXISTS pdh_lock_results (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    laser_name TEXT NOT NULL,
    lock_source TEXT NOT NULL DEFAULT 'manual_lock',
    success BOOLEAN,
    modulation_frequency_hz DOUBLE PRECISION,
    demod_phase_deg DOUBLE PRECISION,
    signal_offset_volts DOUBLE PRECISION,
    modulation_amplitude DOUBLE PRECISION,
    pid_p DOUBLE PRECISION,
    pid_i DOUBLE PRECISION,
    pid_d DOUBLE PRECISION,
    trace_x DOUBLE PRECISION[] NOT NULL,
    trace_y DOUBLE PRECISION[] NOT NULL,
    monitor_trace_y DOUBLE PRECISION[] NOT NULL,
    trace_x_units TEXT NOT NULL DEFAULT 'V',
    trace_y_units TEXT NOT NULL DEFAULT 'V',
    monitor_trace_y_units TEXT NOT NULL DEFAULT 'V'
);

ALTER TABLE pdh_lock_results
    ADD COLUMN IF NOT EXISTS lock_source TEXT NOT NULL DEFAULT 'manual_lock';

CREATE INDEX IF NOT EXISTS idx_pdh_lock_results_created_at
    ON pdh_lock_results (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_pdh_lock_results_laser_created_at
    ON pdh_lock_results (laser_name, created_at DESC);
