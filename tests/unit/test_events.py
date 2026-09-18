from ases import db, events


def test_redacts_secret_shaped_keys():
    payload = {"model": "glm-5.3-thinking:free", "api_key": "sk-should-not-appear", "nested": {"token": "x"}}
    safe = events.redact(payload)
    assert safe["api_key"] == "[redacted]"
    assert safe["nested"]["token"] == "[redacted]"
    assert safe["model"] == "glm-5.3-thinking:free"


def test_redacts_provider_key_shapes_inside_strings():
    payload = {"log": "request used ghp_1234567890abcdefghij successfully"}
    safe = events.redact(payload)
    assert "ghp_1234567890abcdefghij" not in safe["log"]
    assert "[redacted]" in safe["log"]


def test_record_and_recent_round_trip(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    events.record(conn, "test_event", {"provider": "unorouter", "count": 3})
    rows = events.recent(conn, limit=10)
    assert len(rows) == 1
    assert rows[0]["kind"] == "test_event"
    assert "unorouter" in rows[0]["payload"]
