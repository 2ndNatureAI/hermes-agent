import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tools import tts_tool as tts


@pytest.mark.parametrize("path,expected", [
    ("voice.OGG", "opus"), ("voice.WAV", "wav"), ("voice.FLAC", "flac"), ("voice.MP3", "mp3")
])
def test_openai_format_matches_uppercase_suffix(path, expected):
    assert tts._tts_response_format_from_path(path) == expected


def test_elevenlabs_requests_opus_for_uppercase_ogg(tmp_path, monkeypatch):
    client = MagicMock()
    client.text_to_speech.convert.return_value = iter([b"OggS-synthetic-audio"])
    monkeypatch.setattr(tts, "_resolve_provider_key", lambda *args: "synthetic-key")
    monkeypatch.setattr(tts, "_import_elevenlabs", lambda: lambda **kwargs: client)
    output = tmp_path / "voice.OGG"

    assert tts._generate_elevenlabs("hello", str(output), {}) == str(output)
    assert client.text_to_speech.convert.call_args.kwargs["output_format"] == "opus_48000_64"
    assert output.read_bytes().startswith(b"OggS")


def test_mistral_requests_wav_for_uppercase_suffix(tmp_path, monkeypatch):
    client = MagicMock()
    client.audio.speech.complete.return_value = SimpleNamespace(audio_data="UklGRi1zeW50aGV0aWM=")
    manager = MagicMock()
    manager.__enter__.return_value = client
    monkeypatch.setattr(tts, "_resolve_provider_key", lambda *args: "synthetic-key")
    monkeypatch.setattr(tts, "_import_mistral_client", lambda: lambda **kwargs: manager)
    output = tmp_path / "voice.WAV"

    assert tts._generate_mistral_tts("hello", str(output), {}) == str(output)
    assert client.audio.speech.complete.call_args.kwargs["response_format"] == "wav"
    assert output.read_bytes().startswith(b"RIFF")


def test_repair_checks_uppercase_ogg_and_keeps_honest_extension(tmp_path, monkeypatch):
    output = tmp_path / "voice.OGG"
    output.write_bytes(b"ID3" + b"\0" * 20)
    monkeypatch.setattr(tts, "_ffmpeg_transcode_to_opus", lambda *args: None)

    repaired = tts._repair_ogg_container(str(output))
    assert repaired == str(tmp_path / "voice.mp3")
    assert (tmp_path / "voice.mp3").read_bytes().startswith(b"ID3")
    assert not output.exists()


def test_native_voice_delivery_accepts_uppercase_ogg(tmp_path, monkeypatch):
    import gateway.session_context

    monkeypatch.setattr(gateway.session_context, "get_session_env", lambda key, default="": "telegram")
    monkeypatch.setattr(tts, "_load_tts_config", lambda: {"provider": "elevenlabs"})
    monkeypatch.setattr(tts, "_import_elevenlabs", lambda: object())
    def synthesize(text, path, config):
        with open(path, "wb") as audio:
            audio.write(b"OggS" + b"\0" * 20)
        return path
    monkeypatch.setattr(tts, "_generate_elevenlabs", synthesize)

    output = tmp_path / "voice.OGG"
    result = json.loads(tts._text_to_speech_single("hello", output_path=str(output), provider="elevenlabs"))
    assert result["success"]
    assert result["file_path"] == str(output)
    assert result["media_tag"].startswith("[[audio_as_voice]]")


def test_local_neutts_writes_uppercase_wav_without_conversion(tmp_path, monkeypatch):
    output = tmp_path / "voice.WAV"

    def synthesize(cmd, **kwargs):
        assert cmd[cmd.index("--out") + 1] == str(output)
        output.write_bytes(b"RIFF-synthetic-audio")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(tts.subprocess, "run", synthesize)
    monkeypatch.setattr(tts.shutil, "which", lambda name: pytest.fail("unexpected conversion"))
    assert tts._generate_neutts("hello", str(output), {}) == str(output)
    assert output.read_bytes().startswith(b"RIFF")
