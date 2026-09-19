from ases import db, events


def test_redacts_secret_shaped_keys():
    payload = {"model": "glm-5.3-thinking:free", "api_key": "sk-should-not-appear", "nested": {"token": "x"}}
    safe = events.redact(payload)
    assert safe["api_key"] == "[redacted]"
    assert safe["nested"]["token"] == "[redacted]"
    assert safe["model"] == "glm-5.3-thinking:free"


def test_task_key_survives_redaction_but_credential_keys_do_not():
    """Real bug (2026-09-19): the broad "key" pattern also matched task_key, so every merged / merge_failed /
    fix_card_created event stored "[redacted]" where the plan task's key belonged."""
    payload = {
        "task_key": "T1", "idempotency_key": "ases-work-p-T1",
        "key": "sk-abcdefghijklmnop", "api_key": "x", "access_key": "y", "task_key_secret": "z",
        "nested": {"task_key": "T2", "private_key": "w"},
    }
    safe = events.redact(payload)
    assert safe["task_key"] == "T1"
    assert safe["idempotency_key"] == "ases-work-p-T1"
    assert safe["key"] == safe["api_key"] == safe["access_key"] == safe["task_key_secret"] == "[redacted]"
    assert safe["nested"] == {"task_key": "T2", "private_key": "[redacted]"}


def test_a_secret_shaped_value_under_task_key_is_still_scrubbed():
    """The allowlist exempts the field NAME from wholesale redaction, not its value from the value scan."""
    safe = events.redact({"task_key": "sk-abcdefghijklmnopqrstuvwx"})
    assert "sk-abcdefghijklmnopqrstuvwx" not in safe["task_key"]


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
