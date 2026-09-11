"""Durable local artifacts and resumable publication; never performs network I/O."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

CAPTURE_LIMIT = 64 * 1024 * 1024


def safe_path(root: Path, relative: str) -> Path:
    part = Path(relative)
    if not relative or part.is_absolute() or any(p in (".", "..") for p in part.parts):
        raise ValueError("path must be relative and confined to the workspace")
    path = root
    for component in part.parts:
        path = path / component
        if path.is_symlink():
            raise ValueError("symlink is not allowed: " + str(path))
    if root.resolve() not in path.resolve().parents:
        raise ValueError("path escapes workspace: " + relative)
    return path


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def atomic_file(path: Path):
    if path.is_symlink():
        raise ValueError("refusing to replace a symlink: " + str(path))
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_write(path: Path, text: str) -> None:
    with atomic_file(path) as handle:
        handle.write(text.encode("utf-8"))


def write_json(path: Path, payload: object) -> None:
    with atomic_file(path) as handle:
        for chunk in json.JSONEncoder(indent=2, ensure_ascii=False, allow_nan=False).iterencode(payload):
            handle.write(chunk.encode("utf-8"))
        handle.write(b"\n")


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(131072), b""):
            result.update(chunk)
    return result.hexdigest()


def _finite(value):
    raise ValueError("nonfinite JSON value: " + value)


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + key)
        result[key] = value
    return result


def read_json(path: Path, limit: int = 4 * 1024 * 1024):
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("JSON file exceeds local size limit: " + str(path))
    return json.loads(data, parse_constant=_finite, object_pairs_hook=_unique_pairs)


def write_capture(run_dir: Path, payload: dict) -> None:
    path = run_dir / "capture.json.gz"
    # Stream rather than retaining a second full uncompressed copy in memory.
    with atomic_file(path) as handle:
        with gzip.GzipFile(fileobj=handle, mode="wb", compresslevel=5, mtime=0) as compressed:
            with io.TextIOWrapper(compressed, encoding="utf-8") as text:
                json.dump(payload, text, ensure_ascii=False, allow_nan=False)
    atomic_write(run_dir / "capture.sha256", digest(path) + "\n")


def read_capture(run_dir: Path) -> dict:
    path = safe_path(run_dir, "capture.json.gz")
    checksum = safe_path(run_dir, "capture.sha256")
    if not path.is_file() or not checksum.is_file():
        raise ValueError("this run has no complete source capture; it cannot be rebuilt without a new daily collection")
    if path.stat().st_size > CAPTURE_LIMIT or checksum.read_text().strip() != digest(path):
        raise ValueError("source capture checksum/size mismatch; do not rebuild altered or damaged evidence")
    with gzip.open(path, "rb") as handle:
        raw = handle.read(CAPTURE_LIMIT + 1)
    if len(raw) > CAPTURE_LIMIT:
        raise ValueError("source capture exceeds decompressed size limit")
    value = json.loads(raw, parse_constant=_finite, object_pairs_hook=_unique_pairs)
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("unsupported source capture version")
    if not all(isinstance(value.get(key), list) for key in ("candidates", "sources")):
        raise ValueError("invalid source capture collections")
    return value


def publish(root: Path, output_dir: Path, day: str, connection: sqlite3.Connection) -> None:
    """Journal intent before advancing the ledger or any latest-file alias."""
    files = {name: {"path": str((output_dir / name).relative_to(root)), "sha256": digest(output_dir / name)}
             for name in ("ranked.csv", "selected.json", "report.json")}
    write_json(root / ".publication.json", {"version": 1, "local_date": day, "files": files})
    recover_publication(root, connection)


def recover_publication(root: Path, connection: sqlite3.Connection) -> bool:
    journal = safe_path(root, ".publication.json")
    if not journal.exists():
        return False
    intent = read_json(journal)
    if not isinstance(intent, dict) or intent.get("version") != 1:
        raise ValueError("invalid publication journal")
    paths = {}
    for name in ("ranked.csv", "selected.json", "report.json"):
        entry = intent["files"][name]
        paths[name] = safe_path(root, entry["path"])
        if not paths[name].is_file() or digest(paths[name]) != entry["sha256"]:
            raise ValueError("pending publication is damaged: " + name)
    report = read_json(paths["report.json"])
    selected = read_json(paths["selected.json"], CAPTURE_LIMIT)["candidates"]
    day = intent["local_date"]
    if report.get("local_date") != day or report.get("candidate_count") != len(selected):
        raise ValueError("publication report/selection identity mismatch")
    if not connection.execute("SELECT 1 FROM runs WHERE local_date=?", (day,)).fetchone():
        raise ValueError("publication has no original daily claim")
    # A failed latest-file write leaves the journal in place. The next guarded
    # invocation completes only these local writes, never another scrape.
    with connection:
        for candidate in selected:
            connection.execute(
                "INSERT OR IGNORE INTO seen(url,company,title,location,job_id,provider,board_token,first_seen,run_date) VALUES (?,?,?,?,?,?,?,?,?)",
                (candidate.get("canonical_application_url") or candidate["url"], candidate["company"], candidate["title"],
                 candidate["location"], candidate.get("job_id", ""), candidate.get("provider", ""),
                 candidate.get("board_token", ""), report["finished_at"], day),
            )
        connection.execute("UPDATE runs SET finished_at=?,status=?,report_path=?,error=NULL WHERE local_date=?",
                           (report["finished_at"], report["status"], str(paths["report.json"]), day))
    atomic_write(root / "latest.csv", paths["ranked.csv"].read_text(encoding="utf-8"))
    write_json(root / "latest.json", report)
    journal.unlink()
    sync_directory(root)
    return True
