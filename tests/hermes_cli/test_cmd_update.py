"""Git trampoline recovery; branch updates use the real target-identity suite."""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli import update_cmd


@pytest.fixture(autouse=True)
def _isolate_venv_holders(monkeypatch):
    """The update flow's venv-holder guard sees the live gateway processes on
    a dev machine and aborts with SystemExit 2 before reaching the branch
    logic under test.  Isolate it so the test exercises the intended path."""
    monkeypatch.setattr("hermes_cli.update_cmd_windows._detect_venv_python_processes", list)


class TestGitTrampolineSelfHeal:
    """Proactive Git-for-Windows trampoline self-heal (#87876).

    A broken bin\\git.exe / cmd\\git.exe shim (~46KB) refuses every git call
    with a "BUG (fork bomb)" guard instead of re-execing the real git-core
    binary. _ensure_non_trampoline_git detects this up front and swaps in a
    real git binary when one can be located, so the normal git update path
    survives instead of degrading to the ZIP fallback.
    """

    @staticmethod
    def _fake_run_healthy(command, **_kwargs):
        return subprocess.CompletedProcess(
            command, 0, stdout="git version 2.50.0.windows.1\n", stderr=""
        )

    @staticmethod
    def _fake_run_trampoline(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="BUG (fork bomb): tried to spawn itself, check your PATH\n",
        )

    @pytest.mark.platforms("windows")
    def test_healthy_git_command_unchanged(self):
        from hermes_cli import update_cmd

        git_cmd = ["git", "-c", "windows.appendAtomically=false"]
        with (
            patch(
                "hermes_cli.update_cmd.subprocess.run",
                side_effect=self._fake_run_healthy,
            ),
            patch("hermes_cli.update_cmd._locate_real_git") as locate,
        ):
            result = update_cmd._ensure_non_trampoline_git(git_cmd)
        assert result == git_cmd
        locate.assert_not_called()

    @pytest.mark.platforms("windows")
    def test_trampoline_swaps_to_real_git(self, capsys):
        from pathlib import Path

        from hermes_cli import update_cmd

        git_cmd = ["git", "-c", "windows.appendAtomically=false"]
        real = Path(r"C:\Program Files\Git\mingw64\libexec\git-core\git.exe")
        with (
            patch(
                "hermes_cli.update_cmd.subprocess.run",
                side_effect=self._fake_run_trampoline,
            ),
            patch(
                "hermes_cli.update_cmd._locate_real_git", return_value=real
            ),
        ):
            result = update_cmd._ensure_non_trampoline_git(git_cmd)
        assert result == [str(real), "-c", "windows.appendAtomically=false"]
        out = capsys.readouterr().out
        assert "switching to real git" in out

    @pytest.mark.platforms("windows")
    def test_access_denied_git_is_not_accepted_as_healthy(self):
        git_cmd = ["git", "-c", "windows.appendAtomically=false"]
        denied = subprocess.CompletedProcess(
            git_cmd, 1, stdout="", stderr="error launching git: Access is denied.\n"
        )
        real = Path(r"C:\Program Files\Git\ucrt64\libexec\git-core\git.exe")
        with (
            patch("hermes_cli.update_cmd.subprocess.run", return_value=denied),
            patch("hermes_cli.update_cmd._locate_real_git", return_value=real),
        ):
            assert update_cmd._ensure_non_trampoline_git(git_cmd)[0] == str(real)

    def test_check_uses_healed_git_for_fetch(self, monkeypatch, tmp_path):
        (tmp_path / ".git").mkdir()
        broken = ["git", "-c", "windows.appendAtomically=false"]
        healed = ["working-git", *broken[1:]]
        observed = []
        monkeypatch.setattr(update_cmd, "_m", lambda: SimpleNamespace(PROJECT_ROOT=tmp_path))
        monkeypatch.setattr(update_cmd, "_base_git_cmd", lambda: broken)
        monkeypatch.setattr(update_cmd, "_ensure_non_trampoline_git", lambda cmd: healed)
        monkeypatch.setattr(update_cmd, "_map_ssl_cert_file_for_git", lambda cmd: None)
        monkeypatch.setattr(update_cmd, "_source_update_channel", lambda **kwargs: "main")
        monkeypatch.setattr(update_cmd._check, "clear_git_debris", lambda root: None)
        monkeypatch.setattr(update_cmd._check, "channel_compare_branch", lambda *args: "main")
        monkeypatch.setattr(update_cmd._check, "is_shallow_repository", lambda *args: False)
        monkeypatch.setattr(update_cmd._check, "fetch_compare_branch",
                            lambda cmd, *args: (observed.append(cmd) or subprocess.CompletedProcess(cmd, 0), "origin/main"))
        monkeypatch.setattr(update_cmd._check, "compare_ref_exists", lambda *args: True)
        monkeypatch.setattr(update_cmd._check, "report_rev_list_verdict", lambda *args: None)
        with patch("hermes_cli.update_contract.evaluate_update_admission", return_value=None):
            update_cmd._cmd_update_check()
        assert observed == [healed]

    @pytest.mark.platforms("windows")
    def test_apply_selects_git_before_ca_probe_and_children(self, monkeypatch, tmp_path):
        (tmp_path / ".git").mkdir()
        broken = ["git", "-c", "windows.appendAtomically=false"]
        real = Path(r"C:\Program Files\Git\ucrt64\libexec\git-core\git.exe")
        healed = [str(real), *broken[1:]]
        events = []
        monkeypatch.setenv("PATH", "broken-git-dir")
        monkeypatch.setattr(update_cmd, "_m", lambda: SimpleNamespace(
            PROJECT_ROOT=tmp_path, _get_origin_url=lambda *args: "https://github.com/NousResearch/hermes-agent.git"))
        monkeypatch.setattr("hermes_cli._subprocess_compat.expose_pm_git", lambda root: None)
        monkeypatch.setattr(update_cmd, "_base_git_cmd", lambda: broken)
        monkeypatch.setattr(update_cmd, "_git_run", lambda cmd, args: events.append(("config", cmd)))
        monkeypatch.setattr(update_cmd, "_map_ssl_cert_file_for_git", lambda cmd: events.append(("ca", cmd)))
        monkeypatch.setattr(update_cmd, "_ensure_non_trampoline_git",
                            lambda cmd: events.append(("select", cmd)) or healed)
        monkeypatch.setattr(update_cmd, "_discard_lockfile_churn", lambda *args, **kwargs: None)
        monkeypatch.setattr(update_cmd, "_normalize_managed_eol", lambda *args, **kwargs: None)
        monkeypatch.setattr(update_cmd, "_is_fork", lambda url: False)

        assert update_cmd._prepare_git_command()[:2] == (False, healed)
        assert events == [("select", broken), ("config", healed), ("ca", healed)]
        assert os.environ["PATH"].split(os.pathsep)[0] == str(real.parent)

    @pytest.mark.platforms("windows")
    def test_source_completion_reselects_git_after_pm_activation(self, monkeypatch, tmp_path):
        from hermes_cli import source_completion, update_cmd_git

        real = update_cmd._locate_real_git()
        assert real is not None
        monkeypatch.setenv("PATH", "broken-managed-git-dir" + os.pathsep + os.environ["PATH"])
        monkeypatch.setattr("hermes_cli._subprocess_compat.expose_pm_git", lambda root: None)
        monkeypatch.setattr(update_cmd_git, "_ensure_non_trampoline_git", lambda cmd: [str(real)])

        source_completion._expose_working_git(tmp_path)
        assert os.environ["PATH"].split(os.pathsep)[0] == str(real.parent)
        probe = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=15, check=False)
        assert probe.returncode == 0, probe.stderr

    @pytest.mark.platforms("windows")
    def test_trampoline_no_real_git_keeps_command(self, capsys):
        from hermes_cli import update_cmd

        git_cmd = ["git", "-c", "windows.appendAtomically=false"]
        with (
            patch(
                "hermes_cli.update_cmd.subprocess.run",
                side_effect=self._fake_run_trampoline,
            ),
            patch("hermes_cli.update_cmd._locate_real_git", return_value=None),
        ):
            result = update_cmd._ensure_non_trampoline_git(git_cmd)
        assert result == git_cmd
        out = capsys.readouterr().out
        assert "ZIP path" in out

    @pytest.mark.platforms("not windows")
    def test_off_windows_noop(self):
        from hermes_cli import update_cmd

        git_cmd = ["git"]
        with patch("hermes_cli.update_cmd.subprocess.run") as run:
            result = update_cmd._ensure_non_trampoline_git(git_cmd)
        assert result == git_cmd
        run.assert_not_called()

    def test_portable_git_candidates_check_shared_root_first(self, tmp_path, monkeypatch):
        # Profile-scoped layout: HERMES_HOME = <root>/profiles/foo, but the
        # PortableGit tree lives under the SHARED root (monerostar review on
        # #88136). The candidate list must check get_default_hermes_root()
        # before the profile home.
        import hermes_constants
        from hermes_cli.update_cmd_git import _portable_git_candidates

        root = tmp_path / "root"
        profile_home = root / "profiles" / "foo"

        monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: root)
        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: profile_home)

        candidates = _portable_git_candidates()
        assert candidates[0] == (
            root / "git" / "mingw64" / "libexec" / "git-core" / "git.exe"
        )
        assert candidates[1] == (
            profile_home / "git" / "mingw64" / "libexec" / "git-core" / "git.exe"
        )
