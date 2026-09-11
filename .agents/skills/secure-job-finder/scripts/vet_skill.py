#!/usr/bin/env python3
"""Fail-closed structural and static-risk vetting for this skill."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

MAX_LINES = 500
MAX_SKILL_BYTES = 64_000
MAX_CONTEXT_BYTES = 20_000
ALLOWED_ROOT_FILES = {"SKILL.md", "manifest.json"}
FORBIDDEN_CODE = {
    "dynamic evaluation": re.compile(r"\b(?:eval|exec)\s*\("),
    "process execution": re.compile(r"\b(?:subprocess|os\.system|os\.popen|pty\.)\b"),
    "dynamic import": re.compile(r"\b(?:importlib|__import__)\b"),
    "unsafe serialization": re.compile(r"\b(?:pickle|marshal|shelve)\b"),
    "native memory access": re.compile(r"\bctypes\b"),
    "encoded payload handling": re.compile(r"\bbase64\b|b64decode"),
}
FRONTMATTER_RE = re.compile(r"\A---\n(?P<meta>.*?)\n---\n", re.S)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(131_072), b""):
            digest.update(chunk)
    return digest.hexdigest()


def relative_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if "__pycache__" in path.parts or path.name == ".DS_Store":
            continue
        if path.is_symlink():
            raise ValueError(f"symlink is not allowed: {path.relative_to(root)}")
        if path.is_file():
            files.append(path)
    return sorted(files, key=lambda item: item.relative_to(root).as_posix())


def check_layout(root: Path, files: list[Path]) -> list[str]:
    errors: list[str] = []
    required = [root / "SKILL.md", root / "references", root / "scripts"]
    for path in required:
        if not path.exists():
            errors.append(f"missing required path: {path.relative_to(root)}")
    for path in files:
        rel = path.relative_to(root)
        if len(rel.parts) == 1 and rel.name not in ALLOWED_ROOT_FILES:
            errors.append(f"unexpected root file: {rel}")
        if rel.parts[0] == "references" and path.suffix != ".md":
            errors.append(f"reference must be Markdown: {rel}")
        if rel.parts[0] == "scripts" and path.suffix != ".py":
            errors.append(f"script must be Python: {rel}")
    return errors


def check_markdown(root: Path, files: list[Path]) -> list[str]:
    errors: list[str] = []
    skill = root / "SKILL.md"
    if not skill.is_file():
        return errors
    raw = skill.read_text(encoding="utf-8")
    if len(raw.encode("utf-8")) > MAX_SKILL_BYTES:
        errors.append(f"SKILL.md exceeds {MAX_SKILL_BYTES} bytes")
    if len(raw.splitlines()) > MAX_LINES:
        errors.append(f"SKILL.md exceeds {MAX_LINES} lines")
    match = FRONTMATTER_RE.match(raw)
    if not match:
        errors.append("SKILL.md has no valid frontmatter")
    else:
        meta: dict[str, str] = {}
        for line in match.group("meta").splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                meta[key.strip()] = value.strip()
        if meta.get("name") != root.name:
            errors.append("frontmatter name must equal skill directory name")
        description = meta.get("description", "")
        if not description:
            errors.append("frontmatter description is required")
        if "\n" in description or len(description) > 500:
            errors.append("description must be one line and at most 500 characters")
    for path in files:
        rel = path.relative_to(root)
        if path.suffix != ".md":
            continue
        content = path.read_text(encoding="utf-8")
        if len(content.splitlines()) > MAX_LINES:
            errors.append(f"Markdown file exceeds {MAX_LINES} lines: {rel}")
        if len(content.encode("utf-8")) > MAX_CONTEXT_BYTES:
            errors.append(f"Markdown file exceeds approximate 5,000-token ceiling ({MAX_CONTEXT_BYTES} bytes): {rel}")
    return errors


def check_python(root: Path, files: list[Path]) -> list[str]:
    errors: list[str] = []
    for path in files:
        rel = path.relative_to(root)
        if path.suffix != ".py":
            continue
        source = path.read_text(encoding="utf-8")
        try:
            compile(source, str(rel), "exec")
        except SyntaxError as exc:
            errors.append(f"syntax error in {rel}: {exc.msg} line {exc.lineno}")
            continue
        if path.name == "vet_skill.py":
            continue
        for label, pattern in FORBIDDEN_CODE.items():
            if pattern.search(source):
                errors.append(f"{label} found in {rel}")
    return errors


def check_manifest(root: Path, files: list[Path], manifest_path: Path) -> list[str]:
    errors: list[str] = []
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return ["manifest is missing or unsafe"]
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"invalid manifest: {exc}"]
    expected = payload.get("files")
    if not isinstance(expected, dict):
        return ["manifest files must be an object"]
    actual_paths = {
        path.relative_to(root).as_posix()
        for path in files
        if path.resolve() != manifest_path.resolve()
    }
    if actual_paths != set(expected):
        missing = sorted(set(expected) - actual_paths)
        extra = sorted(actual_paths - set(expected))
        if missing:
            errors.append(f"manifest paths missing from disk: {missing}")
        if extra:
            errors.append(f"unmanifested files: {extra}")
    for rel, digest in expected.items():
        path = root / rel
        if not path.is_file() or path.is_symlink():
            continue
        if not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
            errors.append(f"invalid manifest digest: {rel}")
        elif sha256(path) != digest:
            errors.append(f"hash mismatch: {rel}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skill-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--manifest", type=Path, default=Path("manifest.json"))
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)

    root = args.skill_root.expanduser().resolve()
    manifest = args.manifest if args.manifest.is_absolute() else root / args.manifest
    errors: list[str] = []
    try:
        files = relative_files(root)
        errors.extend(check_layout(root, files))
        errors.extend(check_markdown(root, files))
        errors.extend(check_python(root, files))
        errors.extend(check_manifest(root, files, manifest.resolve()))
    except (OSError, UnicodeError, ValueError) as exc:
        errors.append(str(exc))

    report = {
        "skill_root": str(root),
        "status": "pass" if not errors else "fail",
        "errors": errors,
    }
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.json:
        args.json.write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
