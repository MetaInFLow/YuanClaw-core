"""SQLite-backed line index for YuanClaw memory recall."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable


class SQLiteMemoryIndex:
    """Maintain a lightweight SQLite/FTS index over memory markdown files."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._fts_enabled = False
        self._lock = threading.RLock()
        self._ensure_schema()

    @property
    def fts_enabled(self) -> bool:
        """Return whether the runtime SQLite build supports FTS5."""
        return self._fts_enabled

    def index_paths(self, entries: Iterable[tuple[str, Path]]) -> None:
        """Index the provided display-path -> file-path mappings."""
        current: dict[str, tuple[Path, int, int]] = {}
        for display_path, file_path in entries:
            path = Path(file_path).expanduser().resolve()
            try:
                file_stat = path.stat()
            except FileNotFoundError:
                continue
            current[display_path] = (path, file_stat.st_mtime_ns, file_stat.st_size)

        with self._lock, closing(self._connect()) as conn, conn:
            existing = {
                row["path"]: (
                    int(row["mtime_ns"]),
                    int(row["size_bytes"]),
                    str(row["content_sha256"]),
                )
                for row in conn.execute(
                    "SELECT path, mtime_ns, size_bytes, content_sha256 FROM indexed_files"
                )
            }

            for display_path in sorted(set(existing) - set(current)):
                self._delete_path(conn, display_path)
                conn.execute("DELETE FROM indexed_files WHERE path = ?", (display_path,))

            for display_path, (path, mtime_ns, size_bytes) in current.items():
                prior = existing.get(display_path)
                if prior and prior[0] == mtime_ns and prior[1] == size_bytes and prior[2]:
                    continue

                raw = path.read_bytes()
                content_sha256 = hashlib.sha256(raw).hexdigest()
                if prior and prior[2] == content_sha256:
                    conn.execute(
                        "UPDATE indexed_files SET actual_path = ?, mtime_ns = ?, size_bytes = ? "
                        "WHERE path = ?",
                        (path.as_posix(), mtime_ns, size_bytes, display_path),
                    )
                    continue
                self._delete_path(conn, display_path)
                lines = raw.decode("utf-8", errors="replace").splitlines()
                rows = [
                    (display_path, line_no, line)
                    for line_no, line in enumerate(lines, start=1)
                    if line.strip()
                ]
                if rows:
                    conn.executemany(
                        "INSERT INTO memory_lines_fallback(path, line_no, content) VALUES(?, ?, ?)",
                        rows,
                    )
                    if self._fts_enabled:
                        conn.executemany(
                            "INSERT INTO memory_lines(path, line_no, content) VALUES(?, ?, ?)",
                            rows,
                        )

                conn.execute(
                    """
                    INSERT INTO indexed_files(
                        path, actual_path, mtime_ns, size_bytes, content_sha256
                    )
                    VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        actual_path = excluded.actual_path,
                        mtime_ns = excluded.mtime_ns,
                        size_bytes = excluded.size_bytes,
                        content_sha256 = excluded.content_sha256
                    """,
                    (display_path, path.as_posix(), mtime_ns, size_bytes, content_sha256),
                )

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Run an indexed memory search and return compact line records."""
        tokens = self._tokenize(query)
        if not tokens or limit <= 0:
            return []

        with self._lock, closing(self._connect()) as conn, conn:
            if self._fts_enabled:
                rows = self._search_fts(conn, tokens, limit)
                if rows:
                    return rows
            return self._search_fallback(conn, tokens, limit)

    def status(self) -> dict[str, Any]:
        """Return index diagnostics."""
        with self._lock, closing(self._connect()) as conn, conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM indexed_files").fetchone()
        return {
            "db_path": self.db_path.as_posix(),
            "fts_enabled": self._fts_enabled,
            "indexed_files": int(row["count"]) if row else 0,
        }

    def _ensure_schema(self) -> None:
        try:
            self._ensure_schema_once()
        except sqlite3.DatabaseError as exc:
            if not self._is_corrupt_database_error(exc):
                raise
            self._preserve_corrupt_database()
            self._ensure_schema_once()

    def _ensure_schema_once(self) -> None:
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS indexed_files(
                    path TEXT PRIMARY KEY,
                    actual_path TEXT NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    content_sha256 TEXT NOT NULL DEFAULT ''
                )
                """
            )
            columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(indexed_files)")
            }
            if "size_bytes" not in columns:
                conn.execute(
                    "ALTER TABLE indexed_files ADD COLUMN size_bytes INTEGER NOT NULL DEFAULT 0"
                )
            if "content_sha256" not in columns:
                conn.execute(
                    "ALTER TABLE indexed_files "
                    "ADD COLUMN content_sha256 TEXT NOT NULL DEFAULT ''"
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_lines_fallback(
                    path TEXT NOT NULL,
                    line_no INTEGER NOT NULL,
                    content TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_lines_fallback_path "
                "ON memory_lines_fallback(path)"
            )
            if not self._table_exists(conn, "memory_lines"):
                try:
                    conn.execute(
                        """
                        CREATE VIRTUAL TABLE memory_lines
                        USING fts5(path UNINDEXED, line_no UNINDEXED, content)
                        """
                    )
                except sqlite3.OperationalError:
                    pass
            self._fts_enabled = self._table_exists(conn, "memory_lines")

    @staticmethod
    def _is_corrupt_database_error(error: sqlite3.DatabaseError) -> bool:
        message = str(error).lower()
        return any(
            marker in message
            for marker in ("malformed", "not a database", "database disk image is malformed")
        )

    def _preserve_corrupt_database(self) -> None:
        if not self.db_path.exists():
            return
        backup = self.db_path.with_name(
            f"{self.db_path.name}.corrupt-{time.time_ns()}"
        )
        self.db_path.replace(backup)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{self.db_path}{suffix}")
            if sidecar.exists():
                sidecar.unlink()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'virtual table') AND name = ?",
            (name,),
        ).fetchone()
        return row is not None

    def _delete_path(self, conn: sqlite3.Connection, display_path: str) -> None:
        conn.execute("DELETE FROM memory_lines_fallback WHERE path = ?", (display_path,))
        if self._fts_enabled:
            conn.execute("DELETE FROM memory_lines WHERE path = ?", (display_path,))

    def _search_fts(
        self,
        conn: sqlite3.Connection,
        tokens: list[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        match_expr = " OR ".join(f'"{token}"' for token in tokens)
        try:
            rows = conn.execute(
                """
                SELECT
                    path,
                    CAST(line_no AS INTEGER) AS line_no,
                    content,
                    bm25(memory_lines) AS rank
                FROM memory_lines
                WHERE memory_lines MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (match_expr, max(limit, 1)),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [dict(row) for row in rows]

    def _search_fallback(
        self,
        conn: sqlite3.Connection,
        tokens: list[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        clauses = " OR ".join("LOWER(content) LIKE ?" for _ in tokens)
        params = [f"%{token.lower()}%" for token in tokens] + [max(limit, 1)]
        rows = conn.execute(
            f"""
            SELECT path, line_no, content, 0.0 AS rank
            FROM memory_lines_fallback
            WHERE {clauses}
            ORDER BY path, line_no
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _tokenize(query: str) -> list[str]:
        tokens: list[str] = []
        current = []
        for char in query.lower():
            if char.isalnum() or char == "_":
                current.append(char)
                continue
            if current:
                token = "".join(current)
                if token not in tokens:
                    tokens.append(token)
                current = []
        if current:
            token = "".join(current)
            if token not in tokens:
                tokens.append(token)
        return tokens
