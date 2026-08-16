from pathlib import Path
import ast
import json
import re
import sys
import tomllib

root = Path(__file__).parents[1]
failures = []
ignored = {"dist", "build", "__pycache__", ".git", ".venv"}
text_suffixes = {".py", ".md", ".toml", ".yml", ".yaml", ".json", ".txt"}
forbidden = "".join(("sky", "om"))
secret_patterns = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
)
required = ("README.md", "SECURITY.md", "AI_ASSISTANCE.md", "LICENSE",
            "examples/basic.json", ".gitignore", ".github/workflows/ci.yml")
for relative in required:
    if not (root / relative).is_file():
        failures.append(f"missing:{relative}")

for path in root.rglob("*"):
    if not path.is_file() or any(part in ignored for part in path.relative_to(root).parts):
        continue
    if path.name == ".env" or path.name.startswith(".env."):
        failures.append(f"secret-file:{path.relative_to(root)}")
    if path.suffix not in text_suffixes and path.name != ".gitignore":
        continue
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        failures.append(f"unreadable:{path.relative_to(root)}:{exc}")
        continue
    if len(text) > 500_000:
        failures.append(f"oversized:{path.relative_to(root)}")
    if forbidden in text.lower():
        failures.append(f"private-boundary:{path.relative_to(root)}")
    if any(pattern.search(text) for pattern in secret_patterns):
        failures.append(f"secret-pattern:{path.relative_to(root)}")
    if path.suffix == ".py":
        try:
            ast.parse(text, filename=str(path))
        except SyntaxError as exc:
            failures.append(f"syntax:{path.relative_to(root)}:{exc.lineno}")

try:
    with (root / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    if not project.get("name") or not project.get("scripts"):
        failures.append("invalid:pyproject.toml")
except (OSError, KeyError, tomllib.TOMLDecodeError) as exc:
    failures.append(f"invalid:pyproject.toml:{exc}")

try:
    json.loads((root / "examples/basic.json").read_text(encoding="utf-8"))
except (OSError, UnicodeError, json.JSONDecodeError) as exc:
    failures.append(f"invalid:examples/basic.json:{exc}")

readme = (root / "README.md").read_text(encoding="utf-8") if (root / "README.md").is_file() else ""
for heading in ("## Purpose", "## Non-goals", "## Install", "## CLI and API", "## Example",
                "## Security and trust model", "## Limitations", "## Tests",
                "## AI disclosure", "## License"):
    if heading not in readme:
        failures.append(f"readme-heading:{heading}")

workflow = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8") if (root / ".github/workflows/ci.yml").is_file() else ""
required_workflow = (
    "actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
    "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",
    "permissions:",
    "contents: read",
    "timeout-minutes:",
    "python -m build --no-isolation",
    "examples/basic.json",
)
for marker in required_workflow:
    if marker not in workflow:
        failures.append(f"workflow-marker:{marker}")
for match in re.findall(r"uses:\s*([^\s#]+)", workflow):
    if not re.fullmatch(r"[^@]+@[0-9a-f]{40}", match):
        failures.append(f"unpinned-action:{match}")

ignore_lines = set((root / ".gitignore").read_text(encoding="utf-8").splitlines()) if (root / ".gitignore").is_file() else set()
for entry in (".venv/", "dist/", "build/", "*.egg-info/", "__pycache__/", "*.py[cod]"):
    if entry not in ignore_lines:
        failures.append(f"gitignore:{entry}")

if failures:
    print(json.dumps({"ok": False, "failures": sorted(set(failures))}, sort_keys=True))
    sys.exit(1)
print(json.dumps({"ok": True, "checks": "syntax, public-boundary, secret-patterns, docs, example, CI, gitignore"}, sort_keys=True))

