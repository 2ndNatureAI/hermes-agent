"""Windows elevation findings must obey the doctor check/aggregation contract."""

import pytest

from hermes_cli import doctor_platform
from hermes_cli.doctor_report import Finding


@pytest.mark.platforms("windows")
@pytest.mark.parametrize("elevated", [False, True])
def test_gateway_elevation_check_returns_aggregatable_findings(monkeypatch, elevated):
    from hermes_cli import gateway
    import hermes_dir_acl

    monkeypatch.setattr(gateway, "find_gateway_pids", lambda: [12345])
    monkeypatch.setattr(hermes_dir_acl, "process_elevation", lambda _pid: elevated)

    findings = Finding()
    findings.merge(doctor_platform._check_gateway_elevation(False))

    assert bool(findings.issues) is elevated
    if elevated:
        assert "12345" in findings.issues[0]
        assert "NON-elevated" in findings.issues[0]


@pytest.mark.platforms("windows")
def test_gateway_elevation_check_can_report_a_stopped_gateway(monkeypatch):
    from hermes_cli import gateway

    monkeypatch.setattr(gateway, "find_gateway_pids", list)

    findings = Finding()
    findings.merge(doctor_platform._check_gateway_elevation(False))

    assert not findings.issues
    assert not findings.manual_issues


@pytest.mark.platforms("windows")
def test_unreadable_gateway_token_is_reported_as_unverified(monkeypatch):
    from hermes_cli import gateway
    import hermes_dir_acl

    monkeypatch.setattr(gateway, "find_gateway_pids", lambda: [12345])
    monkeypatch.setattr(hermes_dir_acl, "process_elevation", lambda _pid: None)

    findings = doctor_platform._check_gateway_elevation(False)

    assert not findings.issues
    assert len(findings.manual_issues) == 1
    assert "could not be read" in findings.manual_issues[0]
