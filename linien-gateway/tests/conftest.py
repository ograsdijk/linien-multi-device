"""Keep the test run away from the operator's real lock-record setup.

app.main builds its PostgreSQL writer at import from the config under
linien_common's USER_DATA_PATH. If that config has logging enabled, a test that
starts the app would spool records next to it and push them into the real
database. Every test gets a writer on a temporary, disabled config instead.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_lock_record_writer(tmp_path_factory, monkeypatch):
    import app.main as main
    from app.manual_lock_postgres import LockResultPostgresService

    folder = tmp_path_factory.mktemp("lock_records")
    service = LockResultPostgresService(
        config_path=folder / "manual_lock_postgres.json",
        spool_path=folder / "lock_acquisition_spool.sqlite3",
    )
    monkeypatch.setattr(main, "lock_result_postgres", service)
    yield
    service.stop()
