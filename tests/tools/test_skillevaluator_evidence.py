"""Evidence coverage must survive the real optional-scanner adapter path."""

import json
import sys
import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

from tools import skillevaluator_scan as adapter


def run_stub(tmp_path, monkeypatch, payload):
    code = (
        "import json,pathlib,sys\n"
        "out=pathlib.Path(sys.argv[sys.argv.index('-o')+1])\n"
        f"payload=json.loads({json.dumps(json.dumps(payload))})\n"
        "(out / 'skillevaluator-output-stub.json').write_text(json.dumps(payload),encoding='utf-8')\n"
    )
    (tmp_path / "validate").write_text(code, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(adapter, "SCANNER_BIN", sys.executable)
    return adapter.run_tier1_scan(tmp_path)


def test_overall_failure_cannot_look_like_clean_subset(tmp_path, monkeypatch):
    result = run_stub(tmp_path, monkeypatch, {
        "overall_passed": False,
        "results": [{"validator": "PII Scan", "passed": True, "findings": []}],
    })
    assert result.available and not result.passed
    assert "overall" in adapter.format_tier1_report(result).lower()


def test_partial_evidence_and_findings_remain_visible(tmp_path, monkeypatch):
    result = run_stub(tmp_path, monkeypatch, {
        "overall_status": "incomplete", "incomplete_scans": ["SkillSpector"],
        "results": [{"validator": "Security Scan", "passed": True,
                     "incomplete_scans": ["SkillSpector"], "findings": [
                         {"check_name": "hardcoded_secrets", "message": "synthetic"}]}],
    })
    assert result.available and not result.passed and result.secrets_findings
    assert "Security Scan" in result.incomplete_checks
    assert "SkillSpector" in result.incomplete_checks
    assert "SkillSpector" in adapter.format_tier1_report(result)


def test_empty_results_are_not_reported_as_clean(tmp_path, monkeypatch):
    result = run_stub(tmp_path, monkeypatch, {"results": []})
    assert result.available and not result.passed
    assert result.incomplete_checks
    assert "no findings." not in adapter.format_tier1_report(result).lower()


def test_malformed_coverage_is_visible(tmp_path, monkeypatch):
    result = run_stub(tmp_path, monkeypatch, {
        "overall_status": "incomplete", "incomplete_scans": "invalid",
        "results": [{"validator": "PII Scan", "passed": True, "findings": []}],
    })
    assert result.available and result.incomplete_checks
    assert "not run" in adapter.format_tier1_report(result)


def test_unclear_validator_cannot_be_counted_as_a_pass(tmp_path, monkeypatch):
    result = run_stub(tmp_path, monkeypatch, {
        "results": [{"validator": "Security Scan", "status": "failed", "passed": True, "findings": []}],
    })
    assert result.available and not result.passed
    assert "Security Scan" in result.incomplete_checks


def test_failed_check_without_findings_is_not_described_as_clean(tmp_path, monkeypatch):
    result = run_stub(tmp_path, monkeypatch, {
        "results": [{"validator": "Security Scan", "passed": False, "findings": []}],
    })
    assert result.available and not result.passed
    assert "reported failure" in adapter.format_tier1_report(result)


def test_hub_scan_consumer_exposes_aggregate_failure(tmp_path, monkeypatch):
    from hermes_cli import skills_hub as cli_hub
    from hermes_cli.web_routers import skills as router
    from tools import skills_guard, skills_hub

    payload = {"overall_passed": False, "incomplete_scans": ["SkillSpector"],
               "results": [{"validator": "PII Scan", "passed": True, "findings": []}]}
    code = ("import json,pathlib,sys\n"
            "out=pathlib.Path(sys.argv[sys.argv.index('-o')+1])\n"
            f"payload=json.loads({json.dumps(json.dumps(payload))})\n"
            "(out / 'skillevaluator-output-stub.json').write_text(json.dumps(payload),encoding='utf-8')\n")
    (tmp_path / "validate").write_text(code, encoding="utf-8")
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    bundle = SimpleNamespace(source="community", identifier="synthetic")
    scan = SimpleNamespace(findings=[], skill_name="synthetic", source="community", trust_level="test",
                           verdict="allow", summary="synthetic")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(adapter, "SCANNER_BIN", sys.executable)
    monkeypatch.setattr(adapter, "tier1_advisory_enabled", lambda: True)
    monkeypatch.setattr(router, "_config_profile_scope", lambda _: nullcontext())
    monkeypatch.setattr(cli_hub, "_resolve_source_meta_and_bundle", lambda *_: (bundle, bundle, None))
    monkeypatch.setattr(skills_hub, "create_source_router", lambda: None)
    monkeypatch.setattr(skills_hub, "quarantine_bundle", lambda _: quarantine)
    monkeypatch.setattr(skills_guard, "scan_skill", lambda *_args, **_kwargs: scan)
    monkeypatch.setattr(skills_guard, "should_allow_install", lambda *_args, **_kwargs: (True, "test"))
    result = asyncio.run(router.scan_skill_hub("synthetic"))
    assert result["tier1"]["passed"] is False
    assert "SkillSpector" in result["tier1"]["incomplete_checks"]
