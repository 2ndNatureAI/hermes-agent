"""Codex image-only messages survive foreign-session import as text markers."""
import json

from hermes_cli.foreign_sessions import import_foreign_session
from hermes_state import SessionDB


def test_codex_image_messages_are_kept_without_image_data(tmp_path):
    def message(role, content):
        return {"type": "response_item", "payload": {"type": "message", "role": role, "content": content}}

    image = {"type": "input_image", "image_url": "data:image/png;base64,DO_NOT_IMPORT", "detail": "auto"}
    records = [
        message("user", [image]),
        message("assistant", [{"type": "output_text", "text": "What should I inspect?"}]),
        message("user", [{"type": "input_text", "text": "Look here"}, image]),
        message("developer", [image]),
    ]
    path = tmp_path / "rollout-image.jsonl"
    original = "\n".join(json.dumps(record) for record in records) + "\n"
    path.write_text(original, encoding="utf-8")
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        session_id = import_foreign_session("codex", path, db=db)
        messages = db.get_messages(session_id)
        assert [m["role"] for m in messages] == ["user", "assistant", "user"]
        assert messages[0]["content"] == "[image attachment not imported]"
        assert messages[2]["content"] == "Look here\n\n[image attachment not imported]"
        assert "DO_NOT_IMPORT" not in repr(messages)
        assert path.read_text(encoding="utf-8") == original
    finally:
        db.close()
