import pytest

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


def test_redacts_the_more_recent_provider_key_shapes_inside_strings():
    shapes = [
        "nvapi-" + "a1B2c3D4" * 5,
        "sk_live_" + "abcdefghij1234567890",
        "sk-ant-api03-" + "abcdefghijklmnop",
        "github_pat_" + "11ABCDEFG0" + "abcdefghijklmnop",
        "hf_" + "A" * 34,
        "AKIA" + "ABCDEFGHIJKLMNOP",
        "AIza" + "B" * 35,
        "Bearer " + "abcdefghijklmnopqrstuvwxyz0123456789",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghijklmnopqrstuvwxyz",
        "-----BEGIN RSA PRIVATE KEY-----",
    ]
    for shape in shapes:
        safe = events.redact({"log": f"the tool printed {shape} and then went on"})
        assert shape not in safe["log"], shape
        assert "[redacted]" in safe["log"], shape
        assert "the tool printed" in safe["log"] and "and then went on" in safe["log"]


def test_ordinary_text_is_not_mistaken_for_a_secret():
    text = ("commit 0123456789abcdef0123456789abcdef01234567 fixes hf_model_name and sk-1 in "
            "550e8400-e29b-41d4-a716-446655440000, see the AKIA prefix in the docs")
    assert events.redact({"t": text})["t"] == text


def test_a_count_or_a_flag_under_a_credential_shaped_key_is_kept_but_a_string_is_not():
    """input_tokens and max_tokens are numbers. Only a string (or a container) under such a key can be a credential."""
    safe = events.redact({
        "input_tokens": 1200, "max_tokens": 4096, "token_ok": True, "token": None, "ratio_key": 0.5,
        "password": "hunter2", "credentials": {"a": 1}, "token_list": ["x"],
    })
    assert safe["input_tokens"] == 1200 and safe["max_tokens"] == 4096 and safe["token_ok"] is True
    assert safe["token"] is None and safe["ratio_key"] == 0.5
    assert safe["password"] == safe["credentials"] == safe["token_list"] == "[redacted]"


def test_redact_text_scans_one_string():
    assert events.redact_text("used nvapi-" + "z" * 30 + " ok") == "used [redacted] ok"
    assert events.redact_text("nothing to hide") == "nothing to hide"


# --- events.py package (round 9): the schema v7 `project` column -----------------------------------------------


def _project_column(conn, kind):
    row = conn.execute("SELECT project FROM events WHERE kind = ?", (kind,)).fetchone()
    return row["project"]


def test_record_writes_the_project_column_from_the_keyword_argument(tmp_path):
    """ASES-ARC-03: a call site with a project already in scope but not in the payload passes it explicitly."""
    conn = db.connect(tmp_path / "ases.db")
    events.record(conn, "k1", {"task_key": "T1"}, project="p1")
    assert _project_column(conn, "k1") == "p1"
    # The payload on disk is untouched: project= only fills the column, it does not rewrite the JSON.
    assert "project" not in events.recent(conn)[0]["payload"]


def test_record_writes_the_project_column_from_the_payload_with_no_call_site_change(tmp_path):
    """Every existing call site whose payload already names its project (bounds.record_final_gate,
    controller's project_paused, ...) needs no change at all: omitting project= falls back to payload["project"]."""
    conn = db.connect(tmp_path / "ases.db")
    events.record(conn, "k2", {"project": "p1", "task_key": "T1"})
    assert _project_column(conn, "k2") == "p1"


def test_record_with_neither_a_project_argument_nor_a_payload_project_leaves_the_column_null(tmp_path):
    """A genuinely cross-project event (an eval run, credential health, a doctor check): the column stays NULL,
    never a sentinel and never guessed."""
    conn = db.connect(tmp_path / "ases.db")
    events.record(conn, "eval_run", {"task": "E1"})
    assert _project_column(conn, "eval_run") is None


def test_record_project_argument_agreeing_with_the_payload_is_fine(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    events.record(conn, "k3", {"project": "p1", "task_key": "T1"}, project="p1")
    assert _project_column(conn, "k3") == "p1"


def test_record_project_argument_disagreeing_with_the_payload_raises(tmp_path):
    """A project passed that is not the one the payload itself describes is a bug at the call site, not something
    to silently resolve either way (ASES-ARC-03: every record is keyed consistently, or it is an error)."""
    conn = db.connect(tmp_path / "ases.db")
    with pytest.raises(ValueError, match="p1.*p2|p2.*p1"):
        events.record(conn, "k4", {"project": "p2", "task_key": "T1"}, project="p1")
    # Nothing was written: the row does not exist under either name.
    assert conn.execute("SELECT COUNT(*) AS n FROM events WHERE kind = 'k4'").fetchone()["n"] == 0


def test_record_project_none_explicitly_is_the_same_as_omitting_it(tmp_path):
    conn = db.connect(tmp_path / "ases.db")
    events.record(conn, "k5", {"project": "p1"}, project=None)
    assert _project_column(conn, "k5") == "p1"
