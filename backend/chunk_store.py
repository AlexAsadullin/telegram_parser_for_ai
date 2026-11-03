"""
Chunked data files for parsed chats.

Channels and private dialogs are stored as *chunks*, not one monolithic file:
every DM_CHUNK_SIZE messages go into their own JSONL part, so a parse run
never holds (or rewrites) the whole chat in memory, and an interrupted run
keeps every chunk it already finished. Parts are named by their first message
id (`chunk_000000042.jsonl`) — that keeps them sortable in chronological order
even when Telegram ids jump around.

After the run finishes, merge_chunks() folds all parts into a single
messages.jsonl, which is what the exporters and telegram_mcp read. The parts
stay on disk under chunks/ as crash insurance; if the merged file is ever lost
or interrupted mid-write, rebuild_merged() recreates it from them.
"""
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

CHUNKS_DIR = "chunks"
MERGED_FILE = "messages.jsonl"

_CHUNK_RE = re.compile(r"^chunk_(\d+)\.jsonl$")


def _chunks_dir(out_dir: Path) -> Path:
    return out_dir / CHUNKS_DIR


def chunk_name(first_id: int | str) -> str:
    return f"chunk_{int(first_id):09d}.jsonl"


def list_chunk_paths(out_dir: Path) -> list[Path]:
    """Chunk files of a dialog dir, oldest messages first."""
    cdir = _chunks_dir(out_dir)
    if not cdir.exists():
        return []
    return sorted(
        (p for p in cdir.iterdir() if _CHUNK_RE.match(p.name)),
        key=lambda p: int(_CHUNK_RE.match(p.name).group(1)),
    )


def iter_entries(paths: list[Path]):
    """Stream entries from several JSONL files, one at a time."""
    for path in paths:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Skipping malformed line in %s", path)


class ChunkWriter:
    """
    Append entries to chunked JSONL files.

    Usage:
        writer = ChunkWriter(out_dir)
        ...
        writer.start_chunk(first_message_id)   # opens a new part
        writer.write(entry)                    # appends one line
        ...
        writer.close()
        merge_chunks(out_dir)                  # -> messages.jsonl
    """

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.cdir = _chunks_dir(out_dir)
        # A fresh run replaces everything the previous run left behind.
        shutil.rmtree(self.cdir, ignore_errors=True)
        self.cdir.mkdir(parents=True, exist_ok=True)
        self._sink = None

    def start_chunk(self, first_id: int | str) -> None:
        self.close()
        self._sink = (self.cdir / chunk_name(first_id)).open("w", encoding="utf-8")

    def write(self, entry: dict) -> None:
        if self._sink is None:  # first entry without an explicit start
            self.start_chunk(entry.get("message_id", 0))
        self._sink.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def flush(self) -> None:
        if self._sink is not None:
            self._sink.flush()

    def close(self) -> None:
        if self._sink is not None:
            self._sink.close()
            self._sink = None


def merge_chunks(out_dir: Path) -> Path:
    """Concatenate all chunk parts into the single merged file. Streams both ways."""
    merged = out_dir / MERGED_FILE
    paths = list_chunk_paths(out_dir)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=out_dir, suffix=".merged.tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as sink:
            for src in paths:
                with src.open(encoding="utf-8") as f:
                    shutil.copyfileobj(f, sink)
        os.replace(tmp_path, merged)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    logger.info("Merged %s chunk(s) into %s", len(paths), merged)
    return merged


def rebuild_merged(out_dir: Path) -> Path:
    """Recreate the merged file from chunks; used when it is missing or stale."""
    return merge_chunks(out_dir)


def remove_chunks(out_dir: Path) -> None:
    """Drop the per-chunk parts once the merged file is confirmed good."""
    shutil.rmtree(_chunks_dir(out_dir), ignore_errors=True)
