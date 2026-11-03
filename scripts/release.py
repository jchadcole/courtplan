"""Cut a release: bump VERSION and turn the changelog's Unreleased section into a dated version.

    python scripts/release.py patch|minor|major      # or an explicit version, e.g. 1.2.0

Then review the diff, commit, tag and push (the script prints the commands). Pushing a tag
named vX.Y.Z runs .github/workflows/release.yml, which tests and publishes the GitHub release.
"""
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "https://github.com/jchadcole/courtplan"
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def next_version(current: str, part: str) -> str:
    m = SEMVER.match(current)
    if not m:
        raise SystemExit(f"VERSION is not X.Y.Z: {current!r}")
    major, minor, patch = map(int, m.groups())
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    if SEMVER.match(part):
        if tuple(map(int, part.split("."))) <= (major, minor, patch):
            raise SystemExit(f"{part} is not newer than {current}")
        return part
    raise SystemExit("usage: python scripts/release.py patch|minor|major|X.Y.Z")


def update_changelog(text: str, old: str, new: str, today: str) -> str:
    head = "## [Unreleased]"
    if head not in text:
        raise SystemExit("CHANGELOG.md has no '## [Unreleased]' section")
    body = text.split(head, 1)[1].split("\n## [", 1)[0]
    if not body.strip():
        raise SystemExit("The Unreleased section is empty; add the changes before releasing")
    text = text.replace(head, f"{head}\n\n## [{new}] - {today}", 1)
    text = re.sub(r"^\[Unreleased\]: .*$",
                  f"[Unreleased]: {REPO}/compare/v{new}...HEAD\n[{new}]: {REPO}/compare/v{old}...v{new}",
                  text, count=1, flags=re.M)
    return text


def main(argv):
    if len(argv) != 1:
        raise SystemExit(__doc__)
    old = (ROOT / "VERSION").read_text().strip()
    new = next_version(old, argv[0])
    log = ROOT / "CHANGELOG.md"
    log.write_text(update_changelog(log.read_text(), old, new, date.today().isoformat()))
    (ROOT / "VERSION").write_text(new + "\n")
    print(f"{old} -> {new}. Review CHANGELOG.md, then:\n"
          f"  git commit -am 'Release v{new}'\n"
          f"  git tag -a v{new} -m 'CourtPlan v{new}'\n"
          f"  git push origin HEAD v{new}")


if __name__ == "__main__":
    main(sys.argv[1:])
