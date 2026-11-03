import importlib.util
import re
from pathlib import Path

from app import __version__

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("release", ROOT / "scripts" / "release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_version_is_semver_and_in_changelog():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)
    assert f"## [{__version__}]" in (ROOT / "CHANGELOG.md").read_text()


def test_version_endpoint():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/api/version").json() == {"name": "CourtPlan", "version": __version__}
        assert c.get("/openapi.json").json()["info"]["version"] == __version__


def test_next_version():
    assert release.next_version("1.2.3", "patch") == "1.2.4"
    assert release.next_version("1.2.3", "minor") == "1.3.0"
    assert release.next_version("1.2.3", "major") == "2.0.0"
    assert release.next_version("1.2.3", "1.4.0") == "1.4.0"


def test_update_changelog():
    text = ("# Changelog\n\n## [Unreleased]\n\n### Added\n- Brackets\n\n## [1.0.0] - 2026-11-03\n\n"
            "[Unreleased]: https://github.com/jchadcole/courtplan/compare/v1.0.0...HEAD\n")
    out = release.update_changelog(text, "1.0.0", "1.1.0", "2026-11-10")
    assert "## [Unreleased]\n\n## [1.1.0] - 2026-11-10\n\n### Added\n- Brackets" in out
    assert "[Unreleased]: https://github.com/jchadcole/courtplan/compare/v1.1.0...HEAD" in out
    assert "[1.1.0]: https://github.com/jchadcole/courtplan/compare/v1.0.0...v1.1.0" in out
