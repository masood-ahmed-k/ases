from ases import db


def test_connect_creates_schema_and_is_idempotent(tmp_path):
    path = tmp_path / "sub" / "ases.db"
    conn = db.connect(path)
    assert path.exists()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"schema_migrations", "requests_ledger", "model_registry", "events"} <= tables

    # Reconnecting must not error or duplicate the migration row.
    conn2 = db.connect(path)
    rows = conn2.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    assert rows == 1


def test_connect_uses_wal_mode(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
