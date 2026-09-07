import re
import sys
from pathlib import Path

path = Path("pyproject.toml")
content, count = re.subn(
    r'^version = "[^"]+"$', f'version = "{sys.argv[1]}"', path.read_text(), flags=re.MULTILINE
)
if count != 1:
    raise SystemExit("Expected one project version in pyproject.toml.")
path.write_text(content)
