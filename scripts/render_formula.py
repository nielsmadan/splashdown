import re
import sys
from pathlib import Path

VERSION_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
EXPECTED_ARGUMENT_COUNT = 5
FORMULA_VERSION_PATTERN = re.compile(
    r'^\s*url "https://github\.com/nielsmadan/splashdown/archive/refs/tags/v([^"/]+)\.tar\.gz"$',
    re.MULTILINE,
)


def render(version: str, sha256: str, formula: Path) -> str:
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError(f"Invalid release version: {version!r}")
    if not SHA256_PATTERN.fullmatch(sha256):
        raise ValueError("Source SHA256 must be 64 lowercase hexadecimal characters.")
    content = formula.read_text(encoding="utf-8")
    if installed_version(content) != tuple(int(part) for part in version.split(".")):
        raise ValueError("Verified formula version differs from the release version.")
    source_checksum = re.search(r'^  sha256 "([0-9a-f]{64})"$', content, re.MULTILINE)
    if source_checksum is None or source_checksum[1] != sha256:
        raise ValueError("Verified formula source checksum differs from the release checksum.")
    return content


def installed_version(content: str) -> tuple[int, int, int]:
    matches = FORMULA_VERSION_PATTERN.findall(content)
    if len(matches) != 1 or VERSION_PATTERN.fullmatch(matches[0]) is None:
        raise ValueError("Existing Homebrew formula has no unique trusted Splashdown version.")
    major, minor, patch = matches[0].split(".")
    return int(major), int(minor), int(patch)


def render_file(version: str, sha256: str, formula: Path, output: Path) -> bool:
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError(f"Invalid release version: {version!r}")
    if not SHA256_PATTERN.fullmatch(sha256):
        raise ValueError("Source SHA256 must be 64 lowercase hexadecimal characters.")
    release_version = tuple(int(part) for part in version.split("."))
    if output.exists() and installed_version(output.read_text()) > release_version:
        return False
    content = render(version, sha256, formula)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content)
    return True


def main() -> None:
    if len(sys.argv) != EXPECTED_ARGUMENT_COUNT:
        raise SystemExit("usage: render_formula.py VERSION SHA256 VERIFIED_FORMULA OUTPUT")
    output = Path(sys.argv[4])
    rendered = render_file(
        sys.argv[1],
        sys.argv[2],
        Path(sys.argv[3]),
        output,
    )
    print("rendered" if rendered else "skipped-newer")


if __name__ == "__main__":
    main()
