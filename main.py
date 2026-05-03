#!/usr/bin/env python3
from __future__ import annotations

import ast
import dataclasses
import difflib
import enum
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BASE_DIR = Path(os.getenv("BOT_WORKDIR", "bot_workspace")).resolve()
DB_PATH = BASE_DIR / "sessions.sqlite3"
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "25"))
ALLOWED_EXTENSIONS = {".py", ".php"}
STYLE_OPTIONS = ["success", "danger", "primary", "warning", "secondary"]

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("inline_styler")

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class SessionStatus(str, enum.Enum):
    AWAITING_FILE = "awaiting_file"
    ANALYZING = "analyzing"
    CHOOSING = "choosing"
    FINISHED = "finished"
    CANCELLED = "cancelled"


@dataclass
class Issue:
    severity: str
    message: str


@dataclass
class ButtonCandidate:
    candidate_id: int
    kind: str
    label: str
    start: int
    end: int
    line: int
    column: int
    source_hint: str
    insert_mode: str
    occurrence: int
    existing_style: Optional[str] = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_json(data: dict[str, Any]) -> "ButtonCandidate":
        return ButtonCandidate(**data)


@dataclass
class SessionRecord:
    session_id: int
    user_id: int
    chat_id: int
    original_name: str
    file_type: str
    original_path: str
    backup_path: str
    status: str
    current_index: int
    selected_styles: dict[str, str]
    candidates: list[ButtonCandidate]
    issues: list[Issue]
    created_at: float
    updated_at: float

    def to_db_row(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "chat_id": self.chat_id,
            "original_name": self.original_name,
            "file_type": self.file_type,
            "original_path": self.original_path,
            "backup_path": self.backup_path,
            "status": self.status,
            "current_index": self.current_index,
            "selected_styles": json.dumps(self.selected_styles, ensure_ascii=False),
            "candidates": json.dumps([c.to_json() for c in self.candidates], ensure_ascii=False),
            "issues": json.dumps([asdict(i) for i in self.issues], ensure_ascii=False),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_row(row: sqlite3.Row) -> "SessionRecord":
        return SessionRecord(
            session_id=row["session_id"],
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            original_name=row["original_name"],
            file_type=row["file_type"],
            original_path=row["original_path"],
            backup_path=row["backup_path"],
            status=row["status"],
            current_index=row["current_index"],
            selected_styles=json.loads(row["selected_styles"] or "{}"),
            candidates=[ButtonCandidate.from_json(item) for item in json.loads(row["candidates"] or "[]")],
            issues=[Issue(**item) for item in json.loads(row["issues"] or "[]")],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


# ---------------------------------------------------------------------------
# Database / persistence
# ---------------------------------------------------------------------------

class SessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    original_name TEXT NOT NULL,
                    file_type TEXT NOT NULL,
                    original_path TEXT NOT NULL,
                    backup_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_index INTEGER NOT NULL,
                    selected_styles TEXT NOT NULL,
                    candidates TEXT NOT NULL,
                    issues TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user_status ON sessions(user_id, status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user_updated ON sessions(user_id, updated_at DESC)")

    def create_session(self, record: SessionRecord) -> int:
        row = record.to_db_row()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO sessions (
                    user_id, chat_id, original_name, file_type, original_path, backup_path,
                    status, current_index, selected_styles, candidates, issues, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["user_id"], row["chat_id"], row["original_name"], row["file_type"],
                    row["original_path"], row["backup_path"], row["status"], row["current_index"],
                    row["selected_styles"], row["candidates"], row["issues"], row["created_at"], row["updated_at"],
                ),
            )
            return int(cur.lastrowid)

    def update_session(self, record: SessionRecord) -> None:
        record.updated_at = time.time()
        row = record.to_db_row()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE sessions SET
                    user_id=?, chat_id=?, original_name=?, file_type=?, original_path=?, backup_path=?,
                    status=?, current_index=?, selected_styles=?, candidates=?, issues=?, created_at=?, updated_at=?
                WHERE session_id=?
                """,
                (
                    row["user_id"], row["chat_id"], row["original_name"], row["file_type"],
                    row["original_path"], row["backup_path"], row["status"], row["current_index"],
                    row["selected_styles"], row["candidates"], row["issues"], row["created_at"], row["updated_at"],
                    row["session_id"],
                ),
            )

    def get_session(self, session_id: int) -> Optional[SessionRecord]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            return SessionRecord.from_row(row) if row else None

    def get_active_session_for_user(self, user_id: int) -> Optional[SessionRecord]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM sessions
                WHERE user_id=? AND status IN (?, ?)
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (user_id, SessionStatus.ANALYZING.value, SessionStatus.CHOOSING.value),
            ).fetchone()
            return SessionRecord.from_row(row) if row else None

    def get_latest_session_for_user(self, user_id: int) -> Optional[SessionRecord]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE user_id=? ORDER BY updated_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
            return SessionRecord.from_row(row) if row else None

    def list_user_sessions(self, user_id: int, limit: int = 5) -> list[SessionRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions WHERE user_id=? ORDER BY updated_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
            return [SessionRecord.from_row(row) for row in rows]


session_store = SessionStore(DB_PATH)
SESSION_LOCKS: dict[int, "asyncio.Lock"] = {}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

import asyncio

def get_lock(session_id: int) -> asyncio.Lock:
    lock = SESSION_LOCKS.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        SESSION_LOCKS[session_id] = lock
    return lock


def now() -> float:
    return time.time()


def ensure_workspace(user_id: int, session_id: int) -> Path:
    path = BASE_DIR / f"user_{user_id}" / f"session_{session_id}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def normalize_ext(name: str) -> str:
    return Path(name).suffix.lower().strip()


def sniff_file_type(name: str, content: str) -> Optional[str]:
    ext = normalize_ext(name)
    if ext in ALLOWED_EXTENSIONS:
        return ext
    stripped = content.lstrip()
    if stripped.startswith("<?php") or "function" in stripped and "array(" in stripped:
        return ".php"
    if "import " in stripped or "def " in stripped or "InlineKeyboardButton" in stripped:
        return ".py"
    return None


def count_balances(text: str) -> dict[str, int]:
    return {
        "(": text.count("(") - text.count(")"),
        "{": text.count("{") - text.count("}"),
        "[": text.count("[") - text.count("]"),
    }


def detect_quote_style(text: str) -> str:
    single = text.count("'")
    double = text.count('"')
    return '"' if double >= single else "'"


def escape_for_quote(value: str, quote: str) -> str:
    if quote == '"':
        return value.replace("\\", "\\\\").replace('"', '\\"')
    return value.replace("\\", "\\\\").replace("'", "\\'")


def read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="surrogateescape")


def write_text_file(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", errors="surrogateescape")


def make_preview(text: str, start: int, end: int, radius: int = 120) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    snippet = text[left:right]
    return snippet.replace("\r", "")


def line_col_from_index(text: str, idx: int) -> tuple[int, int]:
    line = text.count("\n", 0, idx) + 1
    last_nl = text.rfind("\n", 0, idx)
    col = idx + 1 if last_nl == -1 else idx - last_nl
    return line, col


def sanitize_callback_payload(*parts: str) -> str:
    return "|".join(parts)


def parse_callback_payload(data: str) -> list[str]:
    return data.split("|")


def format_issues(issues: list[Issue]) -> str:
    if not issues:
        return "لا توجد أخطاء بنيوية واضحة."
    lines = []
    for issue in issues[:20]:
        lines.append(f"- [{issue.severity}] {issue.message}")
    if len(issues) > 20:
        lines.append(f"- ... ({len(issues) - 20} أكثر)")
    return "\n".join(lines)


def format_progress(session: SessionRecord) -> str:
    total = len(session.candidates)
    done = sum(1 for c in session.candidates if str(c.candidate_id) in session.selected_styles)
    return f"{done}/{total}"


def build_session_summary(session: SessionRecord) -> str:
    issues = format_issues(session.issues)
    return (
        f"الملف: {session.original_name}\n"
        f"النوع: {session.file_type}\n"
        f"الحالة: {session.status}\n"
        f"التقدم: {format_progress(session)}\n\n"
        f"الأخطاء/الملاحظات:\n{issues}"
    )


# ---------------------------------------------------------------------------
# Analysis engine
# ---------------------------------------------------------------------------

class SourceAnalyzer:
    BUTTON_KEYS = {"text", "callback_data", "url", "switch_inline_query", "switch_inline_query_current_chat", "web_app", "pay"}
    STYLE_KEY_RE = re.compile(r"""(?P<key>['"]style['"]\s*:\s*['"])(?P<val>[^'"]*)(?P<tail>['"])""")
    STYLE_KEY_PHP_RE = re.compile(r"""(?P<key>['"]style['"]\s*=>\s*['"])(?P<val>[^'"]*)(?P<tail>['"])""")
    STYLE_KW_RE = re.compile(r"""(?P<key>\bstyle\s*=\s*)(?P<val>['"]?)(?P<style>[A-Za-z_][A-Za-z0-9_]*)?(?P<tail>['"]?)""")

    def analyze(self, file_type: str, source: str) -> tuple[list[ButtonCandidate], list[Issue]]:
        issues = []
        balances = count_balances(source)
        if file_type == ".py":
            return self._analyze_python(source, balances)
        if file_type == ".php":
            return self._analyze_php(source, balances)
        return [], [Issue("error", "نوع الملف غير مدعوم.")]

    def _analyze_python(self, source: str, balances: dict[str, int]) -> tuple[list[ButtonCandidate], list[Issue]]:
        issues: list[Issue] = []
        candidates: list[ButtonCandidate] = []

        for sym, diff in balances.items():
            if diff != 0:
                issues.append(Issue("warning", f"عدم توازن محتمل في الرمز {sym} بمقدار {diff}."))

        tree = None
        syntax_ok = True
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            syntax_ok = False
            issues.append(Issue("error", f"SyntaxError في Python: {exc.msg} عند السطر {exc.lineno}."))

        if tree is not None:
            candidates.extend(self._python_from_ast(source, tree))

        if not candidates:
            candidates.extend(self._python_regex_fallback(source, ".py"))

        candidates = self._deduplicate_candidates(source, candidates)
        self._assign_occurrences(candidates)
        return candidates, issues

    def _analyze_php(self, source: str, balances: dict[str, int]) -> tuple[list[ButtonCandidate], list[Issue]]:
        issues: list[Issue] = []
        candidates: list[ButtonCandidate] = []

        for sym, diff in balances.items():
            if diff != 0:
                issues.append(Issue("warning", f"عدم توازن محتمل في الرمز {sym} بمقدار {diff}."))

        if "<?php" not in source and "?>" not in source:
            issues.append(Issue("warning", "الملف لا يبدو كملف PHP تقليدي، لكن سيتم التحليل بالأنماط النصية."))

        candidates.extend(self._php_regex_candidates(source))
        if not candidates:
            candidates.extend(self._python_regex_fallback(source, ".php"))

        candidates = self._deduplicate_candidates(source, candidates)
        self._assign_occurrences(candidates)
        return candidates, issues

    def _python_from_ast(self, source: str, tree: ast.AST) -> list[ButtonCandidate]:
        candidates: list[ButtonCandidate] = []

        def add_candidate(node: ast.AST, kind: str, insert_mode: str, label: str) -> None:
            if not hasattr(node, "lineno") or not hasattr(node, "end_lineno"):
                return
            start = self._index_from_line_col(source, node.lineno, node.col_offset)
            end = self._index_from_line_col(source, node.end_lineno, node.end_col_offset)
            if start >= end:
                return
            snippet = source[start:end]
            # Keep repeated identical snippets; each occurrence may need a different style.
            line, col = line_col_from_index(source, start)
            candidates.append(
                ButtonCandidate(
                    candidate_id=0,
                    kind=kind,
                    label=label or self._guess_label_from_snippet(snippet),
                    start=start,
                    end=end,
                    line=line,
                    column=col,
                    source_hint=self._source_hint(source, start, end),
                    insert_mode=insert_mode,
                    occurrence=0,
                    existing_style=self._detect_existing_style(snippet),
                )
            )

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = self._call_name(node.func)
                if func_name and func_name.lower().endswith("inlinekeyboardbutton"):
                    label = self._extract_python_call_label(node)
                    add_candidate(node, "python_call", "call", label)
                elif func_name and func_name.lower().endswith("button"):
                    label = self._extract_python_call_label(node)
                    if label:
                        add_candidate(node, "python_call", "call", label)
            elif isinstance(node, ast.Dict):
                if self._looks_like_button_dict(node):
                    label = self._extract_python_dict_label(node)
                    add_candidate(node, "python_dict", "dict", label)

        return candidates

    def _python_regex_fallback(self, source: str, file_type: str) -> list[ButtonCandidate]:
        candidates: list[ButtonCandidate] = []
        patterns = [
            (r"InlineKeyboardButton\s*\(", "python_call", "call"),
            (r"(?<!\w)(?:'text'|\"text\")\s*:\s*['\"]", "python_dict", "dict"),
            (r"(?<!\w)(?:'callback_data'|\"callback_data\")\s*:\s*['\"]", "python_dict", "dict"),
            (r"(?<!\w)(?:array\s*\(|\{|\[)", "generic", "unknown"),
        ]
        for pattern, kind, insert_mode in patterns:
            for match in re.finditer(pattern, source, re.IGNORECASE | re.DOTALL):
                start = self._expand_to_balanced_span(source, match.start(), insert_mode)
                if start is None:
                    continue
                s, e, mode = start
                snippet = source[s:e]
                if len(snippet) < 10:
                    continue
                if "callback_data" not in snippet and "text" not in snippet and "InlineKeyboardButton" not in snippet:
                    continue
                line, col = line_col_from_index(source, s)
                candidates.append(
                    ButtonCandidate(
                        candidate_id=0,
                        kind=kind,
                        label=self._guess_label_from_snippet(snippet),
                        start=s,
                        end=e,
                        line=line,
                        column=col,
                        source_hint=self._source_hint(source, s, e),
                        insert_mode=mode,
                        occurrence=0,
                        existing_style=self._detect_existing_style(snippet),
                    )
                )
        return candidates

    def _php_regex_candidates(self, source: str) -> list[ButtonCandidate]:
        candidates: list[ButtonCandidate] = []
        key_patterns = [
            r"""(?:(?:'text'|"text")\s*=>\s*['"])""",
            r"""(?:(?:'callback_data'|"callback_data")\s*=>\s*['"])""",
            r"""(?:(?:'url'|"url")\s*=>\s*['"])""",
        ]
        for pattern in key_patterns:
            for match in re.finditer(pattern, source, re.IGNORECASE | re.DOTALL):
                span = self._expand_php_array_span(source, match.start())
                if not span:
                    continue
                s, e, mode = span
                snippet = source[s:e]
                if "text" not in snippet and "callback_data" not in snippet and "url" not in snippet:
                    continue
                if len(snippet) < 10:
                    continue
                line, col = line_col_from_index(source, s)
                candidates.append(
                    ButtonCandidate(
                        candidate_id=0,
                        kind="php_array",
                        label=self._guess_label_from_snippet(snippet),
                        start=s,
                        end=e,
                        line=line,
                        column=col,
                        source_hint=self._source_hint(source, s, e),
                        insert_mode=mode,
                        occurrence=0,
                        existing_style=self._detect_existing_style(snippet),
                    )
                )
        return candidates

    def _deduplicate_candidates(self, source: str, candidates: list[ButtonCandidate]) -> list[ButtonCandidate]:
        unique: list[ButtonCandidate] = []
        seen: set[tuple[int, int, str]] = set()
        for cand in sorted(candidates, key=lambda c: (c.start, c.end, c.kind)):
            key = (cand.start, cand.end, cand.kind)
            if key in seen:
                continue
            seen.add(key)
            unique.append(cand)
        for idx, cand in enumerate(unique, 1):
            cand.candidate_id = idx
        return unique

    def _assign_occurrences(self, candidates: list[ButtonCandidate]) -> None:
        groups: dict[str, int] = {}
        for cand in candidates:
            snippet = cand.source_hint.strip()
            groups[snippet] = groups.get(snippet, 0) + 1
            cand.occurrence = groups[snippet]

    def _already_seen(self, snippet: str, candidates: list[ButtonCandidate]) -> bool:
        return any(c.source_hint.strip() == snippet.strip() for c in candidates)

    def _source_hint(self, source: str, start: int, end: int) -> str:
        return make_preview(source, start, end, radius=0)[:260]

    def _guess_label_from_snippet(self, snippet: str) -> str:
        for pattern in (
            r"""(?i)(?:['"]text['"]\s*[:=]\s*['"])(?P<val>[^'"]{1,120})['"]""",
            r"""(?i)(?:['"]text['"]\s*=>\s*['"])(?P<val>[^'"]{1,120})['"]""",
            r"""(?i)InlineKeyboardButton\s*\(\s*['"](?P<val>[^'"]{1,120})['"]""",
            r"""(?i)\btext\s*=\s*['"](?P<val>[^'"]{1,120})['"]""",
        ):
            m = re.search(pattern, snippet, re.DOTALL)
            if m:
                return m.group("val").strip()
        short = re.sub(r"\s+", " ", snippet).strip()
        return short[:80]

    def _extract_python_call_label(self, node: ast.Call) -> str:
        if node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                return first.value
        for kw in node.keywords:
            if kw.arg == "text" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                return kw.value.value
        return ""

    def _extract_python_dict_label(self, node: ast.Dict) -> str:
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and isinstance(key.value, str) and key.value.lower() == "text":
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    return value.value
        return ""

    def _looks_like_button_dict(self, node: ast.Dict) -> bool:
        keys = []
        for key in node.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys.append(key.value.lower())
        has_text = "text" in keys
        has_any = any(k in keys for k in self.BUTTON_KEYS if k != "text")
        return has_text and has_any

    def _call_name(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parts = []
            cur: Any = node
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
            return ".".join(reversed(parts))
        return ""

    def _index_from_line_col(self, source: str, line: int, col: int) -> int:
        if line <= 1:
            return col
        pos = 0
        current_line = 1
        for idx, ch in enumerate(source):
            if current_line == line:
                pos = idx
                break
            if ch == "\n":
                current_line += 1
        else:
            return len(source)
        return pos + col

    def _expand_to_balanced_span(self, source: str, start: int, mode: str) -> Optional[tuple[int, int, str]]:
        opener_map = {
            "call": ("(", ")"),
            "dict": ("{", "}"),
            "unknown": None,
        }
        if mode == "call":
            open_char, close_char = "(", ")"
        elif mode == "dict":
            open_char, close_char = "{", "}"
        else:
            # Generic guess
            if source.find("(", start, start + 80) != -1 and source.find("InlineKeyboardButton", max(0, start - 40), start + 40) != -1:
                open_char, close_char = "(", ")"
                mode = "call"
            elif source.find("{", start, start + 80) != -1:
                open_char, close_char = "{", "}"
                mode = "dict"
            else:
                return None

        open_idx = source.find(open_char, start)
        if open_idx == -1:
            return None
        end_idx = self._find_matching_delimiter(source, open_idx, open_char, close_char)
        if end_idx is None:
            return None
        return start, end_idx + 1, mode

    def _expand_php_array_span(self, source: str, start: int) -> Optional[tuple[int, int, str]]:
        array_idx = source.rfind("array", 0, start + 1)
        bracket_idx = source.rfind("[", 0, start + 1)
        paren_idx = source.rfind("(", 0, start + 1)
        chosen = max(array_idx, bracket_idx, paren_idx)
        if chosen == -1:
            return None

        if array_idx == chosen and source[array_idx:array_idx + 5].lower() == "array":
            open_idx = source.find("(", array_idx, array_idx + 10)
            if open_idx == -1:
                return None
            end_idx = self._find_matching_delimiter(source, open_idx, "(", ")")
            if end_idx is None:
                return None
            return chosen, end_idx + 1, "array"
        if bracket_idx == chosen:
            end_idx = self._find_matching_delimiter(source, bracket_idx, "[", "]")
            if end_idx is None:
                return None
            return chosen, end_idx + 1, "php_array"
        if paren_idx == chosen:
            end_idx = self._find_matching_delimiter(source, paren_idx, "(", ")")
            if end_idx is None:
                return None
            return chosen, end_idx + 1, "array"
        return None

    def _find_matching_delimiter(self, text: str, open_idx: int, open_char: str, close_char: str) -> Optional[int]:
        stack = 0
        in_single = False
        in_double = False
        escaped = False
        in_line_comment = False
        in_block_comment = False

        for idx in range(open_idx, len(text)):
            ch = text[idx]
            nxt = text[idx + 1] if idx + 1 < len(text) else ""

            if in_line_comment:
                if ch == "\n":
                    in_line_comment = False
                continue
            if in_block_comment:
                if ch == "*" and nxt == "/":
                    in_block_comment = False
                continue

            if in_single:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == "'":
                    in_single = False
                continue
            if in_double:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_double = False
                continue

            if ch == "/" and nxt == "/":
                in_line_comment = True
                continue
            if ch == "#" and open_char in ("{", "[", "("):
                in_line_comment = True
                continue
            if ch == "/" and nxt == "*":
                in_block_comment = True
                continue

            if ch == "'":
                in_single = True
                continue
            if ch == '"':
                in_double = True
                continue

            if ch == open_char:
                stack += 1
            elif ch == close_char:
                stack -= 1
                if stack == 0:
                    return idx
        return None

    def _find_style_in_snippet(self, snippet: str, file_type: str, insert_mode: str) -> Optional[tuple[int, int, str]]:
        if insert_mode == "call":
            m = re.search(r"\bstyle\s*=\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", snippet)
            if m:
                return m.start(2), m.end(2), "call"
            return None
        if file_type == ".php" or insert_mode == "php_array":
            m = re.search(r"""(?i)(['"]style['"]\s*=>\s*['"])([^'"]*)(['"])""", snippet)
            if m:
                return m.start(2), m.end(2), "php_array"
            return None
        if insert_mode in {"dict", "unknown"}:
            m = re.search(r"""(?i)(['"]style['"]\s*:\s*['"])([^'"]*)(['"])""", snippet)
            if m:
                return m.start(2), m.end(2), "dict"
            return None
        return None

    def _detect_existing_style(self, snippet: str) -> Optional[str]:
        for pattern in (
            r"""(?i)['"]style['"]\s*[:=]>\s*['"]?([A-Za-z_][A-Za-z0-9_]*)['"]?""",
            r"""(?i)\bstyle\s*=\s*['"]?([A-Za-z_][A-Za-z0-9_]*)['"]?""",
        ):
            m = re.search(pattern, snippet)
            if m:
                return m.group(1)
        return None

    def patch_snippet(self, snippet: str, style: str, file_type: str, insert_mode: str) -> str:
        style = style.strip()
        if insert_mode == "call":
            # Replace existing style if present
            replaced = re.sub(r"\bstyle\s*=\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1", f"style={style!r}", snippet)
            if replaced != snippet:
                return replaced
            idx = self._find_top_level_closer(snippet, "(", ")")
            if idx is None:
                return snippet
            prefix = snippet[:idx].rstrip()
            suffix = snippet[idx:]
            if prefix.endswith("("):
                insertion = f"style={style!r}"
            else:
                insertion = f", style={style!r}"
            return prefix + insertion + suffix

        if file_type == ".php" or insert_mode == "php_array":
            replaced = re.sub(
                r"""(?i)(['"]style['"]\s*=>\s*['"])([^'"]*)(['"])""",
                lambda m: f"{m.group(1)}{escape_for_quote(style, m.group(1)[0])}{m.group(3)}",
                snippet,
            )
            if replaced != snippet:
                return replaced
            closer = self._find_top_level_closer(snippet, "[" if "[" in snippet and snippet.find("[") < snippet.find("(") else "(", "]" if "[" in snippet and snippet.find("[") < snippet.find("(") else ")")
            if closer is None:
                # fallback: insert before last close brace/paren
                closer = max(snippet.rfind("]"), snippet.rfind(")"), snippet.rfind("}"))
                if closer == -1:
                    return snippet
            prefix = snippet[:closer].rstrip()
            suffix = snippet[closer:]
            if prefix.endswith("(") or prefix.endswith("[") or prefix.endswith("{"):
                insertion = f"'style' => {style!r}"
            else:
                insertion = f", 'style' => {style!r}"
            return prefix + insertion + suffix

        # dict/json-like
        replaced = re.sub(
            r"""(?i)(['"]style['"]\s*:\s*['"])([^'"]*)(['"])""",
            lambda m: f"{m.group(1)}{escape_for_quote(style, m.group(1)[0])}{m.group(3)}",
            snippet,
        )
        if replaced != snippet:
            return replaced
        closer = self._find_top_level_closer(snippet, "{", "}")
        if closer is None:
            closer = self._find_top_level_closer(snippet, "[", "]")
        if closer is None:
            return snippet
        prefix = snippet[:closer].rstrip()
        suffix = snippet[closer:]
        if prefix.endswith("{") or prefix.endswith("["):
            insertion = f'"style": "{style}"'
        else:
            insertion = f', "style": "{style}"'
        return prefix + insertion + suffix

    def _find_top_level_closer(self, snippet: str, open_char: str, close_char: str) -> Optional[int]:
        idx = snippet.find(open_char)
        if idx == -1:
            return None
        return self._find_matching_delimiter(snippet, idx, open_char, close_char)

    def apply_selected_styles(self, original_source: str, session: SessionRecord) -> str:
        patches: list[tuple[int, int, str, str]] = []
        candidate_by_id = {c.candidate_id: c for c in session.candidates}
        for cid_str, style in session.selected_styles.items():
            if style not in STYLE_OPTIONS:
                continue
            cand = candidate_by_id.get(int(cid_str))
            if not cand:
                continue
            patches.append((cand.start, cand.end, style, cand.insert_mode))
        patches.sort(key=lambda x: x[0], reverse=True)

        text = original_source
        for start, end, style, insert_mode in patches:
            snippet = text[start:end]
            patched = self.patch_snippet(snippet, style, session.file_type, insert_mode)
            text = text[:start] + patched + text[end:]
        return text

    def validate_final(self, file_type: str, source: str) -> list[Issue]:
        issues: list[Issue] = []
        if file_type == ".py":
            try:
                ast.parse(source)
            except SyntaxError as exc:
                issues.append(Issue("error", f"Python syntax error بعد التعديل: {exc.msg} عند السطر {exc.lineno}."))
        elif file_type == ".php":
            if shutil.which("php"):
                with tempfile.NamedTemporaryFile("w", suffix=".php", delete=False, encoding="utf-8", errors="surrogateescape") as tmp:
                    tmp.write(source)
                    tmp_path = Path(tmp.name)
                try:
                    proc = subprocess.run(
                        ["php", "-l", str(tmp_path)],
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                    if proc.returncode != 0:
                        issues.append(Issue("error", f"PHP lint failed: {proc.stderr.strip() or proc.stdout.strip()}"))
                except Exception as exc:
                    issues.append(Issue("warning", f"تعذر تشغيل php -l: {exc}"))
                finally:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except Exception:
                        pass
            else:
                balances = count_balances(source)
                for sym, diff in balances.items():
                    if diff != 0:
                        issues.append(Issue("warning", f"PHP قد يحتوي عدم توازن في {sym}: {diff}."))
        return issues


analyzer = SourceAnalyzer()

# ---------------------------------------------------------------------------
# Session operations
# ---------------------------------------------------------------------------

def load_session(session_id: int) -> Optional[SessionRecord]:
    return session_store.get_session(session_id)


async def rebuild_and_save(session: SessionRecord) -> tuple[bool, list[Issue], str]:
    lock = get_lock(session.session_id)
    async with lock:
        original = read_text_file(Path(session.backup_path))
        patched = analyzer.apply_selected_styles(original, session)
        final_issues = analyzer.validate_final(session.file_type, patched)
        if any(issue.severity == "error" for issue in final_issues):
            return False, final_issues, patched
        write_text_file(Path(session.original_path), patched)
        return True, final_issues, patched


def current_candidate(session: SessionRecord) -> Optional[ButtonCandidate]:
    if session.current_index < 0 or session.current_index >= len(session.candidates):
        return None
    return session.candidates[session.current_index]


def advance_to_next_pending(session: SessionRecord) -> None:
    while session.current_index < len(session.candidates):
        cand = session.candidates[session.current_index]
        if str(cand.candidate_id) in session.selected_styles:
            session.current_index += 1
            continue
        break
    if session.current_index >= len(session.candidates):
        session.status = SessionStatus.FINISHED.value


async def send_current_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, session: SessionRecord) -> None:
    cand = current_candidate(session)
    if cand is None or session.status == SessionStatus.FINISHED.value:
        await finish_session(update, context, session, completed=True)
        return

    label = cand.label or "(بدون نص واضح)"
    existing = f"\nالقيمة الحالية: {cand.existing_style}" if cand.existing_style else ""
    already = f"\nالاختيار الحالي: {session.selected_styles.get(str(cand.candidate_id), 'غير محدد')}"
    text = (
        f"الجلسة #{session.session_id}\n"
        f"الملف: {session.original_name}\n"
        f"الزر {session.current_index + 1}/{len(session.candidates)}\n"
        f"النص: {label}\n"
        f"السطر: {cand.line} | العمود: {cand.column}\n"
        f"الموضع: {cand.kind}\n"
        f"مقتطف: {cand.source_hint[:180]}{existing}{already}\n\n"
        f"اختر اللون لهذا الزر."
    )

    keyboard = [
        [
            InlineKeyboardButton("success", callback_data=sanitize_callback_payload("S", str(session.session_id), "style", str(cand.candidate_id), "success")),
            InlineKeyboardButton("danger", callback_data=sanitize_callback_payload("S", str(session.session_id), "style", str(cand.candidate_id), "danger")),
            InlineKeyboardButton("primary", callback_data=sanitize_callback_payload("S", str(session.session_id), "style", str(cand.candidate_id), "primary")),
        ],
        [
            InlineKeyboardButton("warning", callback_data=sanitize_callback_payload("S", str(session.session_id), "style", str(cand.candidate_id), "warning")),
            InlineKeyboardButton("secondary", callback_data=sanitize_callback_payload("S", str(session.session_id), "style", str(cand.candidate_id), "secondary")),
        ],
        [
            InlineKeyboardButton("رجوع", callback_data=sanitize_callback_payload("S", str(session.session_id), "back")),
            InlineKeyboardButton("تخطي", callback_data=sanitize_callback_payload("S", str(session.session_id), "skip")),
            InlineKeyboardButton("إعادة اختيار", callback_data=sanitize_callback_payload("S", str(session.session_id), "reselect")),
        ],
        [
            InlineKeyboardButton("معاينة", callback_data=sanitize_callback_payload("S", str(session.session_id), "preview")),
            InlineKeyboardButton("إنهاء", callback_data=sanitize_callback_payload("S", str(session.session_id), "finish")),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup)
    elif update.message:
        await update.message.reply_text(text, reply_markup=reply_markup)


async def finish_session(update: Update, context: ContextTypes.DEFAULT_TYPE, session: SessionRecord, completed: bool = False) -> None:
    session.status = SessionStatus.FINISHED.value if completed else SessionStatus.CANCELLED.value
    session_store.update_session(session)
    summary = build_session_summary(session)
    if completed:
        summary += "\n\nتمت العملية. تم حفظ الملف المعدل في المسار نفسه داخل مساحة العمل."
    else:
        summary += "\n\nتم إيقاف الجلسة."
    if update.callback_query:
        await update.callback_query.edit_message_text(summary)
    elif update.message:
        await update.message.reply_text(summary)


def build_diff(original: str, modified: str, limit: int = 1400) -> str:
    diff = "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            modified.splitlines(keepends=True),
            fromfile="original",
            tofile="modified",
            n=3,
        )
    )
    if len(diff) > limit:
        return diff[:limit] + "\n... (تم اختصار المعاينة)"
    return diff or "لا يوجد اختلاف ظاهر."


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat:
        return
    user_id = update.effective_user.id
    active = session_store.get_active_session_for_user(user_id)
    if active:
        await update.message.reply_text(
            "لديك جلسة نشطة بالفعل. أرسل /resume للمتابعة أو /cancel لإيقافها."
        )
        return

    await update.message.reply_text(
        "أرسل ملفًا بصيغة .py أو .php فقط. سأحلله، أستخرج الأزرار، ثم أجعلك تختار لون كل زر واحدًا تلو الآخر."
    )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    session = session_store.get_active_session_for_user(update.effective_user.id)
    if not session:
        await update.message.reply_text("لا توجد جلسة نشطة للاستكمال.")
        return
    advance_to_next_pending(session)
    session_store.update_session(session)
    await send_current_prompt(update, context, session)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    session = session_store.get_active_session_for_user(update.effective_user.id)
    if not session:
        await update.message.reply_text("لا توجد جلسة نشطة.")
        return
    session.status = SessionStatus.CANCELLED.value
    session_store.update_session(session)
    await update.message.reply_text("تم إيقاف الجلسة الحالية.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user:
        return
    session = session_store.get_active_session_for_user(update.effective_user.id)
    if not session:
        await update.message.reply_text("لا توجد جلسة نشطة.")
        return
    await update.message.reply_text(build_session_summary(session))


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.effective_chat or not update.message or not update.message.document:
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    active = session_store.get_active_session_for_user(user_id)
    if active:
        await update.message.reply_text("لديك جلسة نشطة بالفعل. استخدم /resume أو /cancel أولاً.")
        return

    doc = update.message.document
    file_name = doc.file_name or f"upload_{doc.file_id}"
    ext = normalize_ext(file_name)
    if ext not in ALLOWED_EXTENSIONS and ext == "":
        await update.message.reply_text("الملف غير مدعوم. أرسل فقط ملف .py أو .php.")
        return

    workspace = ensure_workspace(user_id, int(time.time()))
    target_path = workspace / file_name
    backup_path = workspace / f".{file_name}.backup"

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        await tg_file.download_to_drive(custom_path=str(target_path))
    except Exception as exc:
        logger.exception("download error")
        await update.message.reply_text(f"فشل تنزيل الملف من Telegram: {exc}")
        return

    try:
        source = read_text_file(target_path)
    except Exception as exc:
        await update.message.reply_text(f"تعذر قراءة الملف: {exc}")
        return

    detected = sniff_file_type(file_name, source)
    if detected is None:
        await update.message.reply_text("تعذر تحديد النوع المدعوم. أرسل ملف .py أو .php واضح الامتداد أو المحتوى.")
        return

    if detected not in ALLOWED_EXTENSIONS:
        await update.message.reply_text("الملف غير مدعوم.")
        return

    try:
        shutil.copy2(target_path, backup_path)
    except Exception as exc:
        await update.message.reply_text(f"تعذر إنشاء نسخة داخلية احتياطية: {exc}")
        return

    await update.message.reply_text("جارٍ التحليل الذكي للملف واستخراج الأزرار...")

    candidates, issues = analyzer.analyze(detected, source)
    if not candidates:
        await update.message.reply_text(
            "لم أتمكن من استخراج أزرار Inline Keyboard بشكل موثوق من هذا الملف.\n"
            "يمكنك إرسال ملف آخر أو إصلاح البنية ثم إعادة المحاولة.\n\n"
            f"{format_issues(issues)}"
        )
        return

    session = SessionRecord(
        session_id=0,
        user_id=user_id,
        chat_id=chat_id,
        original_name=file_name,
        file_type=detected,
        original_path=str(target_path),
        backup_path=str(backup_path),
        status=SessionStatus.CHOOSING.value,
        current_index=0,
        selected_styles={},
        candidates=candidates,
        issues=issues,
        created_at=now(),
        updated_at=now(),
    )
    session_id = session_store.create_session(session)
    session.session_id = session_id
    session_store.update_session(session)

    # First prompt
    await send_current_prompt(update, context, session)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.callback_query or not update.effective_user:
        return
    query = update.callback_query
    await query.answer()

    parts = parse_callback_payload(query.data or "")
    if len(parts) < 3 or parts[0] != "S":
        await query.answer("بيانات غير صالحة", show_alert=True)
        return

    session_id = int(parts[1])
    action = parts[2]
    session = session_store.get_session(session_id)
    if not session:
        await query.answer("الجلسة غير موجودة أو منتهية.", show_alert=True)
        return
    if session.user_id != update.effective_user.id:
        await query.answer("هذه الجلسة ليست ملكك.", show_alert=True)
        return
    if session.status not in {SessionStatus.CHOOSING.value, SessionStatus.ANALYZING.value}:
        await query.answer("الجلسة غير نشطة.", show_alert=True)
        return

    if action == "style":
        if len(parts) != 5:
            await query.answer("بيانات اختيار اللون غير صالحة.", show_alert=True)
            return
        candidate_id = int(parts[3])
        style = parts[4]
        if style not in STYLE_OPTIONS:
            await query.answer("لون غير مسموح.", show_alert=True)
            return
        candidate = next((c for c in session.candidates if c.candidate_id == candidate_id), None)
        if not candidate:
            await query.answer("الزر غير موجود.", show_alert=True)
            return

        previous_value = session.selected_styles.get(str(candidate_id))
        session.selected_styles[str(candidate_id)] = style
        session.current_index = candidate_id  # next candidate index (1-based id maps to next 0-based index)
        advance_to_next_pending(session)
        session_store.update_session(session)

        ok, final_issues, _patched = await rebuild_and_save(session)
        if final_issues:
            session.issues.extend(final_issues)
            session_store.update_session(session)
        if not ok:
            if previous_value is None:
                session.selected_styles.pop(str(candidate_id), None)
            else:
                session.selected_styles[str(candidate_id)] = previous_value
            session.current_index = candidate_id - 1 if candidate_id > 0 else 0
            session_store.update_session(session)
            await query.edit_message_text(
                "تم اكتشاف خطأ يمنع الحفظ بعد التعديل، وتم التراجع عن الاختيار الأخير.\n\n"
                + format_issues(final_issues)
            )
            return

        await send_current_prompt(update, context, session)
        return

    if action == "back":
        if session.current_index > 0:
            session.current_index -= 1
        session_store.update_session(session)
        await send_current_prompt(update, context, session)
        return

    if action == "skip":
        cand = current_candidate(session)
        if cand:
            # Keep a completion mark that does not affect the final patch.
            session.selected_styles[str(cand.candidate_id)] = cand.existing_style or "__skip__"
        session.current_index += 1
        advance_to_next_pending(session)
        session_store.update_session(session)
        ok, final_issues, _patched = await rebuild_and_save(session)
        if final_issues:
            session.issues.extend(final_issues)
            session_store.update_session(session)
        if session.status == SessionStatus.FINISHED.value:
            await finish_session(update, context, session, completed=True)
            return
        await send_current_prompt(update, context, session)
        return

    if action == "reselect":
        await send_current_prompt(update, context, session)
        return

    if action == "preview":
        original = read_text_file(Path(session.backup_path))
        rebuilt = analyzer.apply_selected_styles(original, session)
        diff = build_diff(original, rebuilt)
        if len(diff) > 3500:
            diff = diff[:3500] + "\n... (تم اختصار المعاينة)"
        await query.message.reply_text(f"المعاينة الحالية:\n\n{diff}")
        return

    if action == "finish":
        ok, final_issues, _patched = await rebuild_and_save(session)
        if final_issues:
            session.issues.extend(final_issues)
            session_store.update_session(session)
        if ok:
            session.status = SessionStatus.FINISHED.value
            session_store.update_session(session)
            await finish_session(update, context, session, completed=True)
        else:
            await query.edit_message_text(
                "تعذر الإنهاء بسبب أخطاء في الملف بعد التعديل.\n\n" + format_issues(final_issues)
            )
        return

    await query.answer("عملية غير معروفة.", show_alert=True)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    text = update.message.text or ""
    if text.startswith("/"):
        return
    await update.message.reply_text("أرسل ملفًا .py أو .php، أو استخدم /resume لاستكمال جلسة نشطة.")


def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Set the BOT_TOKEN environment variable before running.")
    app = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(CallbackQueryHandler(handle_callback, pattern=r"^S\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    return app


def main() -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("Workspace: %s", BASE_DIR)
    app = build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
