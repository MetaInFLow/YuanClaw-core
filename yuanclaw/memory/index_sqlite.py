"""SQLite-backed line index for YuanClaw memory recall."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable


class SQLiteMemoryIndex:
    """Maintain a lightweight SQLite/FTS index over memory markdown files."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._fts_enabled = False
        self._ensure_schema()

    @property
    def fts_enabled(self) -> bool:
        """Return whether the runtime SQLite build supports FTS5."""
        return self._fts_enabled

    def index_paths(self, entries: Iterable[tuple[str, Path]]) -> None:
        """Index the provided display-path -> file-path mappings."""
        current: dict[str, tuple[Path, int]] = {}
        for display_path, file_path in entries:
            path = Path(file_path).expanduser().resolve()
            try:
                mtime_ns = path.stat().st_mtime_ns
            except FileNotFoundError:
                continue
            current[display_path] = (path, mtime_ns)

        with self._connect() as conn:
            existing = {
                row["path"]: int(row["mtime_ns"])
                for row in conn.execute("SELECT path, mtime_ns FROM indexed_files")
            }

            for display_path in sorted(set(existing) - set(current)):
                self._delete_path(conn, display_path)
                conn.execute("DELETE FROM indexed_files WHERE path = ?", (display_path,))

            for display_path, (path, mtime_ns) in current.items():
                if existing.get(display_path) == mtime_ns:
                    continue

                self._delete_path(conn, display_path)
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
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
                    INSERT INTO indexed_files(path, actual_path, mtime_ns)
                    VALUES(?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        actual_path = excluded.actual_path,
                        mtime_ns = excluded.mtime_ns
                    """,
                    (display_path, path.as_posix(), mtime_ns),
                )

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Run an indexed memory search and return compact line records."""
        tokens = self._tokenize(query)
        if not tokens or limit <= 0:
            return []

        with self._connect() as conn:
            if self._fts_enabled:
                rows = self._search_fts(conn, tokens, limit)
                if rows:
                    return rows
            return self._search_fallback(conn, tokens, limit)

    def status(self) -> dict[str, Any]:
        """Return index diagnostics."""
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM indexed_files").fetchone()
        return {
            "db_path": self.db_path.as_posix(),
            "fts_enabled": self._fts_enabled,
            "indexed_files": int(row["count"]) if row else 0,
        }

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS indexed_files(
                    path TEXT PRIMARY KEY,
                    actual_path TEXT NOT NULL,
                    mtime_ns INTEGER NOT NULL
                )
                """
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
