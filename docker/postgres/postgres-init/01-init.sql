-- Generated from linien-gateway/app/manual_lock_postgres.py; keep the two in step.
-- One row per lock-acquisition attempt, keyed by lock_acquisition_id.

CREATE TABLE IF NOT EXISTS pdh_lock_results (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lock_acquisition_id UUID NOT NULL UNIQUE,
    record_version INTEGER NOT NULL,
    device_key TEXT NOT NULL,
    laser_name TEXT NOT NULL,
    lock_source TEXT NOT NULL,
    success BOOLEAN,
    attempt_started_at TIMESTAMPTZ NOT NULL,
    lock_command_at TIMESTAMPTZ,
    lock_command_status TEXT NOT NULL CHECK (lock_command_status IN ('not_sent', 'accepted', 'rejected')),
    failure_stage TEXT,
    failure_reason TEXT,
    modulation_frequency_hz DOUBLE PRECISION,
    demod_phase_deg DOUBLE PRECISION,
    signal_offset_volts DOUBLE PRECISION,
    modulation_amplitude DOUBLE PRECISION,
    pid_p DOUBLE PRECISION,
    pid_i DOUBLE PRECISION,
    pid_d DOUBLE PRECISION,
    trace_x DOUBLE PRECISION[],
    trace_y DOUBLE PRECISION[],
    secondary_trace_y DOUBLE PRECISION[],
    trace_x_units TEXT,
    trace_y_units TEXT,
    secondary_trace_y_units TEXT,
    trace_x_source TEXT,
    trace_y_role TEXT,
    secondary_trace_role TEXT,
    trace_unavailable_reason TEXT,
    trace_meta JSONB,
    refinement JSONB,
    linien_config JSONB,
    verification_status TEXT NOT NULL CHECK (verification_status IN ('not_attempted', 'pending', 'verified', 'failed', 'unavailable')),
    verification_reason TEXT,
    verification_completed_at TIMESTAMPTZ,
    characterization JSONB,
    characterization_status TEXT,
    zero_crossing_sweep_v DOUBLE PRECISION,
    slope_error_v_per_sweep_v DOUBLE PRECISION,
    discriminator_slope_v_per_mhz DOUBLE PRECISION,
    peak_to_peak_v DOUBLE PRECISION,
    sweep_center_v DOUBLE PRECISION,
    sweep_amplitude_v DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_pdh_lock_results_created_at
    ON pdh_lock_results (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_pdh_lock_results_laser_created_at
    ON pdh_lock_results (laser_name, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_pdh_lock_results_device_attempt
    ON pdh_lock_results (device_key, attempt_started_at DESC);
