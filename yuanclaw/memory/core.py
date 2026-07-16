"""Core memory backend for YuanClaw."""

from __future__ import annotations

import re
from pathlib import Path

from yuanclaw.memory.base import MemoryDoc, MemoryHit
from yuanclaw.memory.index_sqlite import SQLiteMemoryIndex

_DATE_PAGE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")


class CoreMemoryBackend:
    """Workspace-backed memory store with cited search and document reads."""

    backend_name = "core"

    def __init__(
        self,
        workspace: Path,
        *,
        daily_pages: bool = True,
        recent_days: int = 2,
        extra_paths: list[str] | None = None,
        search_max_results: int = 8,
    ):
        self.workspace = Path(workspace).expanduser().resolve()
        self.memory_dir = self.workspace / "memory"
        self.daily_pages = daily_pages
        self.recent_days = max(0, recent_days)
        self.extra_paths = list(extra_paths or [])
        self.search_max_results = max(1, search_max_results)
        self._index = SQLiteMemoryIndex(self.memory_dir / ".memory-index.sqlite3")

    def build_context(
        self,
        session_key: str,
        channel: str,
        chat_id: str,
        is_group: bool = False,
    ) -> str:
        """Build the memory context injected into the agent prompt."""
        if is_group:
            return ""

        sections: list[str] = []
        memory_doc = self._primary_memory_doc()
        if memory_doc is not None:
            sections.append(self._format_section("Long-term Memory", memory_doc))

        is_main_session = session_key == f"{channel}:{chat_id}"
        recent_docs = self._recent_daily_docs(limit=self.recent_days) if is_main_session else []
        if recent_docs:
            daily_sections = [self._format_section(doc.path, doc) for doc in recent_docs]
            sections.append("## Recent Memory\n\n" + "\n\n".join(daily_sections))

        return "\n\n---\n\n".join(sections)

    def search(self, query: str, max_results: int = 8) -> list[MemoryHit]:
        """Search memory documents and return cited hits."""
        self._refresh_index_if_needed()
        tokens = self._tokenize(query)
        limit = max(1, min(max_results or self.search_max_results, self.search_max_results))
        if not tokens or limit <= 0:
            return []

        hits: list[MemoryHit] = []
        rows = self._index.search(query, max(limit * 6, limit))
        for row in rows:
            path = str(row["path"])
            line_no = int(row["line_no"])
            snippet = str(row["content"]).strip()
            score = self._score_line(snippet, tokens, self._path_weight(Path(path)))
            score += self._rank_bonus(float(row.get("rank") or 0.0))
            if score <= 0:
                continue
            hits.append(
                MemoryHit(
                    path=path,
                    line=line_no,
                    snippet=snippet,
                    score=score,
                )
            )

        hits.sort(key=lambda hit: (-hit.score, hit.path, hit.line))
        return hits[:limit]

    def get(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> MemoryDoc:
        """Return a cited slice from a memory document."""
        doc_path = self._resolve_path(path)
        if not doc_path.exists():
            raise FileNotFoundError(f"memory document not found: {path}")

        lines = doc_path.read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(lines)
        if total == 0:
            start = 1
            end = 1
            selected: list[str] = []
        else:
            start = 1 if start_line is None else max(1, start_line)
            end = total if end_line is None else min(total, end_line)
            if end < start:
                raise ValueError("end_line must be >= start_line")
            selected = lines[start - 1 : end]

        return MemoryDoc(
            path=self._display_path(doc_path),
            start_line=start,
            end_line=end,
            lines=selected,
        )

    def status(self) -> dict[str, object]:
        """Return a compact backend status snapshot."""
        self._refresh_index_if_needed()
        status = self._index.status()
        return {
            "backend": self.backend_name,
            "workspace": str(self.workspace),
            "memory_dir": str(self.memory_dir),
            "daily_pages": self.daily_pages,
            "recent_days": self.recent_days,
            "extra_paths": list(self.extra_paths),
            **status,
        }

    def _primary_memory_doc(self) -> MemoryDoc | None:
        for candidate in (
            self.workspace / "MEMORY.md",
            self.memory_dir / "MEMORY.md",
        ):
            if candidate.exists():
                return self.get(candidate.as_posix())
        return None

    def _recent_daily_docs(self, limit: int = 2) -> list[MemoryDoc]:
        if not self.daily_pages or limit <= 0:
            return []
        docs = []
        for path in self._indexed_paths():
            if path.name in {"MEMORY.md", "HISTORY.md"}:
                continue
            if not _DATE_PAGE_RE.match(path.name):
                continue
            docs.append(path)
        docs.sort(key=lambda p: p.name, reverse=True)
        return [self.get(path.as_posix()) for path in docs[:limit]]

    def _format_section(self, title: str, doc: MemoryDoc) -> str:
        body = doc.content.strip()
        if not body:
            return f"## {title}\n\n(empty)"
        return f"## {title}\n\n{body}"

    def _path_weight(self, path: Path) -> float:
        name = path.name.upper()
        if name == "MEMORY.MD":
            return 0.75
        if name == "HISTORY.MD":
            return 0.25
        if _DATE_PAGE_RE.match(path.name):
            return 0.1
        return 0.0

    def _score_line(self, line: str, tokens: list[str], path_score: float) -> float:
        line_lower = line.lower()
        matched_terms = 0
        score = path_score
        for token in tokens:
            count = line_lower.count(token)
            if count:
                matched_terms += 1
                score += count
        if matched_terms == 0:
            return 0.0
        if matched_terms == len(tokens):
            score += 1.0
        return score

    def _refresh_index_if_needed(self) -> None:
        entries = [(self._display_path(path), path) for path in self._indexed_paths()]
        self._index.index_paths(entries)

    def _indexed_paths(self) -> list[Path]:
        paths: list[Path] = []
        for candidate in (
            self.workspace / "MEMORY.md",
            self.workspace / "HISTORY.md",
            self.memory_dir / "MEMORY.md",
            self.memory_dir / "HISTORY.md",
        ):
            if candidate.exists():
                paths.append(candidate.resolve())

        if self.memory_dir.exists():
            for path in self.memory_dir.rglob("*.md"):
                if path.is_file():
                    paths.append(path.resolve())

        paths.extend(self._extra_index_paths())

        unique: list[Path] = []
        seen: set[str] = set()
        for path in paths:
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            unique.append(path)
        return unique

    def _resolve_path(self, path: str) -> Path:
        raw = Path(path).expanduser()
        candidates = [raw if raw.is_absolute() else (self.workspace / raw)]
        if not raw.is_absolute():
            candidates.append(self.memory_dir / raw)
        for candidate in candidates:
            if candidate.exists():
                return candidate.resolve()
        return candidates[0].resolve()

    def _display_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.workspace).as_posix()
        except Exception:
            return path.resolve().as_posix()

    def _extra_index_paths(self) -> list[Path]:
        resolved: list[Path] = []
        for item in self.extra_paths:
            raw = Path(item).expanduser()
            path = raw if raw.is_absolute() else (self.workspace / raw)
            if path.is_file() and path.suffix.lower() == ".md":
                resolved.append(path.resolve())
                continue
            if path.is_dir():
                for child in path.rglob("*.md"):
                    if child.is_file():
                        resolved.append(child.resolve())
        return resolved

    @staticmethod
    def _rank_bonus(rank: float) -> float:
        if rank <= 0:
            return 1.0
        return 1.0 / (1.0 + rank)

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
