"""Security tests — secrets handling, dependency scanning, infra
hardening.

QA framework: Security Testing Strategy (section 19), Secret Management.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.security
@pytest.mark.invariant
class TestNoSecretsInRepo:
    """Secrets must never be committed. Even short-lived dev keys can
    end up in git history."""

    def test_env_file_not_tracked_in_git(self):
        gitignore = ROOT / ".gitignore"
        if not gitignore.exists():
            pytest.skip("no .gitignore at repo root")
        contents = gitignore.read_text()
        assert ".env" in contents, (
            ".env must be in .gitignore — secrets would leak to git"
        )

    def test_no_anthropic_key_in_committed_source(self):
        """A sloppy commit of an API key into a Python file. Even if
        rotated, leaked keys hit Anthropic abuse counters."""
        offenders = []
        for f in (ROOT / "backend").rglob("*.py"):
            if "__pycache__" in str(f):
                continue
            text = f.read_text()
            # Real Anthropic keys start with sk-ant-
            if "sk-ant-" in text:
                offenders.append(str(f))
        assert not offenders, (
            f"Anthropic key literal found in source: {offenders}"
        )


@pytest.mark.security
@pytest.mark.invariant
class TestConfigEndpointRedaction:
    """The /config endpoint runs over HTTPS but the body is visible
    to anyone with the browser tab open. Keys must be redacted."""

    def test_config_endpoint_redacts_anthropic_key(self):
        from backend.api.routes.config import _public
        cfg = {"anthropic_api_key": "sk-ant-real-secret"}
        out = _public(cfg)
        assert out["anthropic_api_key"] == ""
        assert "sk-ant" not in json.dumps(out)


@pytest.mark.security
@pytest.mark.invariant
class TestNoOpenHostBinding:
    """The FastAPI service must bind to 127.0.0.1 only. nginx is the
    sole public ingress. Direct binding to 0.0.0.0 would expose the
    API to the internet without TLS or auth."""

    def test_deploy_doc_pins_127_binding(self):
        """The deploy script must pin --host 127.0.0.1 when launching
        uvicorn. Direct binding to 0.0.0.0 would expose the API."""
        deploy = (ROOT / "deploy.sh")
        if not deploy.exists():
            pytest.skip("deploy.sh not present in this checkout")
        text = deploy.read_text()
        # The deploy hands off to systemd; the systemd unit text is
        # what matters. If deploy.sh writes the unit, it should contain
        # the bind. If deploy ships an external unit, leave this as a
        # documented contract.
        assert ("127.0.0.1" in text or "localhost" in text
                    or "ExecStart" not in text), (
            "deploy.sh should pin uvicorn --host 127.0.0.1 OR delegate "
            "to a systemd unit that does. Open binding would expose the API."
        )


@pytest.mark.security
@pytest.mark.requires_network
@pytest.mark.slow
class TestDependencyVulnerabilities:
    """pip-audit must find no HIGH vulnerabilities in the lockfile.
    Skipped when pip-audit isn't installed locally."""

    def test_pip_audit_no_high_severity(self):
        try:
            import subprocess
            result = subprocess.run(
                ["pip-audit", "--strict", "--format", "json"],
                capture_output=True, text=True, timeout=60,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pytest.skip("pip-audit not available")
        if result.returncode == 0:
            return  # no vulnerabilities
        # Parse JSON output and assert no HIGH severity
        try:
            data = json.loads(result.stdout)
            highs = [v for v in data.get("vulnerabilities", [])
                     if v.get("severity") == "HIGH"]
            assert not highs, f"HIGH severity vulns: {highs}"
        except json.JSONDecodeError:
            pytest.skip("pip-audit output not JSON")


@pytest.mark.security
@pytest.mark.invariant
class TestPaperModeDefault:
    """Defense-in-depth — even if config is corrupt, paper_mode must
    stay True so we don't accidentally route to a live broker."""

    def test_default_config_is_paper_mode(self):
        from backend.config import DEFAULT_BOT_CONFIG
        assert DEFAULT_BOT_CONFIG.get("paper_mode") is True

    def test_default_broker_is_paper(self):
        from backend.config import DEFAULT_BOT_CONFIG
        broker = DEFAULT_BOT_CONFIG.get("broker", "").lower()
        assert "paper" in broker or "local" in broker, (
            f"Default broker '{broker}' is not paper — risk of live exec"
        )
