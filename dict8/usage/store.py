"""SQLite store for the usage layer.

Invariant 5 is enforced by the schema rather than by discipline: `messages.message_id` is
the primary key and `turns.prompt_uuid` is the primary key, so a duplicate cannot be
inserted twice by any code path — backfill, tail-reader, or live meter. On this machine
counting occurrences instead of unique ids over-reports by 157.9% (docs/verified-schemas.md §5).

Invariant 6: no USD column exists anywhere in this schema, and none may be added. The unit
is tokens.

`privacy.store_transcripts: features_only`: there is no column for prompt text.

**Turn aggregates are derived, not written.** A turn's tokens, model, wall time and
files-touched are recomputed from `turn_messages ⋈ messages` by `refresh_turn_aggregates()`
after every scan. Writing them at insert time looks simpler and is wrong: the tail-reader
sees a prompt on one tick and the assistant messages answering it on later ticks, so a
write-time sum freezes the turn at whatever had arrived when the prompt was first seen.
Deriving them also makes every scan idempotent — re-running a backfill cannot drift.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from dict8.usage.parser import AssistantMessage, Turn

SCHEMA_VERSION = 5

DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per unique assistant message.id. Invariant 5: the PK is the dedup rule.
CREATE TABLE IF NOT EXISTS messages (
    message_id            TEXT PRIMARY KEY,
    session_id            TEXT NOT NULL,
    project               TEXT NOT NULL,
    cwd                   TEXT,
    model                 TEXT NOT NULL,
    ts                    TEXT NOT NULL,          -- ISO-8601 UTC
    input_tokens          INTEGER NOT NULL,
    output_tokens         INTEGER NOT NULL,
    cache_creation_tokens INTEGER NOT NULL,
    cache_read_tokens     INTEGER NOT NULL,
    is_sidechain          INTEGER NOT NULL DEFAULT 0,
    cc_version            TEXT,
    src_file              TEXT NOT NULL,
    src_line              INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_ts      ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_project ON messages(project);

-- Paths a message touched, for the turn's files-touched feature (§7).
CREATE TABLE IF NOT EXISTS message_files (
    message_id TEXT NOT NULL,
    file_path  TEXT NOT NULL,
    PRIMARY KEY (message_id, file_path)
);

-- One row per human prompt. PK is the prompt's line uuid, which survives a session
-- replay while sessionId does not (docs/verified-schemas.md §6).
-- No prompt-text column: privacy.store_transcripts is features_only.
CREATE TABLE IF NOT EXISTS turns (
    prompt_uuid           TEXT PRIMARY KEY,
    session_id            TEXT NOT NULL,
    project               TEXT NOT NULL,
    cwd                   TEXT,
    ts                    TEXT NOT NULL,
    prompt_chars          INTEGER NOT NULL,       -- summed over every prompt in the turn
    prompt_words          INTEGER NOT NULL,       -- summed over every prompt in the turn
    prompt_count          INTEGER NOT NULL DEFAULT 1,
    task_type             TEXT,                   -- classifier bucket, or null if unclassified
    task_type_confidence  REAL,                   -- classifier's own confidence, 0-1
    task_type_source      TEXT,                   -- classifier.model that produced it
    -- derived by refresh_turn_aggregates(), never written directly:
    files_touched         INTEGER NOT NULL DEFAULT 0,
    model                 TEXT,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    wall_ms               INTEGER,
    message_count         INTEGER NOT NULL DEFAULT 0,
    src_file              TEXT NOT NULL,
    src_line              INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_ts      ON turns(ts);
CREATE INDEX IF NOT EXISTS idx_turns_session ON turns(session_id);

-- Every human prompt that fed a turn. Queued messages mean a turn can have several;
-- turns.prompt_uuid is the first of them (docs/verified-schemas.md §6). src_file/src_line
-- + seq (this prompt's order within the turn) let a classifier re-locate and reassemble
-- the real text on disk WITHOUT it ever being persisted here — see
-- dict8.usage.parser.read_prompt_text. No text column exists on this table, on purpose.
CREATE TABLE IF NOT EXISTS turn_prompts (
    prompt_uuid TEXT PRIMARY KEY,
    turn_uuid   TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    chars       INTEGER NOT NULL,
    words       INTEGER NOT NULL,
    src_file    TEXT NOT NULL,
    src_line    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turn_prompts_turn ON turn_prompts(turn_uuid);

-- "the set of message.ids it covers", per prompts/build.md Phase 1 Step 2.
-- message_id is the PRIMARY KEY, not half of a composite: a message belongs to exactly
-- one turn. A replayed message parsed out of two files would otherwise link to a turn in
-- each and be counted twice in the turn aggregates — invariant 5 one level up from tokens.
-- Measured before the fix: 9 message_ids linked to 2 turns, inflating turn sums to 100.7%
-- of the message total.
CREATE TABLE IF NOT EXISTS turn_messages (
    message_id  TEXT PRIMARY KEY,
    prompt_uuid TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turn_messages_turn ON turn_messages(prompt_uuid);

-- Byte offsets so the tail-reader resumes instead of re-reading whole files.
CREATE TABLE IF NOT EXISTS file_cursors (
    path       TEXT PRIMARY KEY,
    offset     INTEGER NOT NULL,
    size       INTEGER NOT NULL,
    mtime      REAL    NOT NULL,
    updated_at TEXT    NOT NULL
);

-- Manual weekly-quota check-ins (schema v4). Invariant 6: the unit is percent of the
-- weekly quota, never USD, so there is no dollar column here either.
--
-- Two timestamps, because they are two different facts. `ts` is when the reading was
-- TAKEN off Claude Code's /usage; `recorded_at` is when Dict8 wrote it down. They are
-- usually seconds apart and occasionally a day apart, and it is `ts` that decides the
-- age everything downstream is judged on. Collapsing them would let a reading typed in
-- today about yesterday look fresh.
--
-- Append-only, and deliberately not a single-row table: a check-in is an observation,
-- and the calibration this feeds needs the series, not the last value. `source` is
-- carried per row rather than assumed, so a future non-manual source cannot be mistaken
-- for something a human read and typed.
CREATE TABLE IF NOT EXISTS quota_readings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    weekly_pct  REAL NOT NULL,
    ts          TEXT NOT NULL,          -- ISO-8601 UTC, when the reading was taken
    source      TEXT NOT NULL,          -- config quota.source
    recorded_at TEXT NOT NULL           -- ISO-8601 UTC, when this row was written
);
CREATE INDEX IF NOT EXISTS idx_quota_readings_ts ON quota_readings(ts);

-- One row per estimate the UserPromptSubmit hook produced (schema v5), including the
-- ones it REFUSED to produce (`ood = 1`, low/point/high NULL). Phase 6 logs
-- estimate-vs-actual per turn and cannot do that from an estimate that was never written
-- down; a refusal is as much a prediction as a range is, and dropping refusals here would
-- make the recorded estimates look better calibrated than they are.
--
-- No prompt text (privacy.store_transcripts: features_only) and no USD (invariant 6):
-- `prompt_words` is the only thing carried over from what was typed.
--
-- `prompt_id` is the hook payload's own field, OBSERVED equal to `promptId` on the user
-- line of the transcript (docs/verified-schemas.md section 8) — NOT to `turns.prompt_uuid`,
-- which is that line's `uuid`. So it is a join key for Phase 6 via the transcript, not a
-- foreign key into `turns`, and it is deliberately not declared as one.
--
-- `est_group` spells out `group` because GROUP is an SQL keyword: a bare `group` column
-- only works quoted, and the first unquoted query against it is a syntax error at runtime
-- rather than at review time.
CREATE TABLE IF NOT EXISTS hook_estimates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT,                   -- hook payload session_id; NULL if it was absent
    prompt_id    TEXT,                   -- hook payload prompt_id; NULL if it was absent
    ts           TEXT NOT NULL,          -- ISO-8601 UTC, when the hook ran
    prompt_words INTEGER NOT NULL,       -- word count of the submitted prompt
    method       TEXT NOT NULL,          -- estimate.method that produced it
    est_group    TEXT NOT NULL,          -- which population the range came from
    n            INTEGER NOT NULL,       -- turns behind that population
    low          INTEGER,                -- NULL when the estimator refused
    point        INTEGER,
    high         INTEGER,
    ood          INTEGER NOT NULL        -- 1 = out of distribution / refused
);
CREATE INDEX IF NOT EXISTS idx_hook_estimates_ts      ON hook_estimates(ts);
CREATE INDEX IF NOT EXISTS idx_hook_estimates_session ON hook_estimates(session_id);
"""

TOKEN_COLUMNS = ("input_tokens", "output_tokens", "cache_creation_tokens", "cache_read_tokens")

# Recompute every derived turn column from the deduped message rows. Correlated subqueries
# rather than a join+group-by so a turn with no messages yet keeps sane zeros.
REFRESH_SQL = """
UPDATE turns SET
    message_count = (
        SELECT COUNT(*) FROM turn_messages tm JOIN messages m USING(message_id)
        WHERE tm.prompt_uuid = turns.prompt_uuid),
    input_tokens = (
        SELECT COALESCE(SUM(m.input_tokens),0) FROM turn_messages tm JOIN messages m USING(message_id)
        WHERE tm.prompt_uuid = turns.prompt_uuid),
    output_tokens = (
        SELECT COALESCE(SUM(m.output_tokens),0) FROM turn_messages tm JOIN messages m USING(message_id)
        WHERE tm.prompt_uuid = turns.prompt_uuid),
    cache_creation_tokens = (
        SELECT COALESCE(SUM(m.cache_creation_tokens),0) FROM turn_messages tm JOIN messages m USING(message_id)
        WHERE tm.prompt_uuid = turns.prompt_uuid),
    cache_read_tokens = (
        SELECT COALESCE(SUM(m.cache_read_tokens),0) FROM turn_messages tm JOIN messages m USING(message_id)
        WHERE tm.prompt_uuid = turns.prompt_uuid),
    files_touched = (
        SELECT COUNT(DISTINCT mf.file_path) FROM turn_messages tm
        JOIN message_files mf USING(message_id)
        WHERE tm.prompt_uuid = turns.prompt_uuid),
    model = (
        SELECT m.model FROM turn_messages tm JOIN messages m USING(message_id)
        WHERE tm.prompt_uuid = turns.prompt_uuid
        ORDER BY m.ts DESC, m.message_id DESC LIMIT 1),
    wall_ms = (
        SELECT CAST(ROUND((julianday(MAX(m.ts)) - julianday(turns.ts)) * 86400000.0) AS INTEGER)
        FROM turn_messages tm JOIN messages m USING(message_id)
        WHERE tm.prompt_uuid = turns.prompt_uuid)
"""


# A message belongs to one turn; if it is already claimed, the earlier turn keeps it.
LINK_SQL = """
INSERT INTO turn_messages(message_id, prompt_uuid) VALUES (?,?)
ON CONFLICT(message_id) DO UPDATE SET prompt_uuid = excluded.prompt_uuid
WHERE (SELECT ts FROM turns WHERE prompt_uuid = excluded.prompt_uuid)
    < (SELECT ts FROM turns WHERE prompt_uuid = turn_messages.prompt_uuid)
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        # WAL so the tail-reader can write while a reader (CLI, hook) queries.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        # Read before the DDL runs: afterwards every database looks like the current
        # schema, so this is the only moment an upgrade can be observed rather than claimed.
        self.schema_version_before = self._stored_schema_version()
        self.conn.executescript(DDL)
        self._migrate()
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def _stored_schema_version(self) -> int | None:
        """What the file on disk says its schema version is, or None for a fresh database."""
        try:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        except sqlite3.OperationalError:
            return None                      # no meta table yet: this file is brand new
        return int(row["value"]) if row else None

    def _migrate(self) -> None:
        """Additive column migrations. DDL above is CREATE-IF-NOT-EXISTS, which never
        adds a column to a table that already exists from an older schema version — this
        covers that case for anyone who ran the Phase 1 schema (v2) before v3 added the
        classifier columns.

        v3 -> v4 adds `quota_readings`, and v4 -> v5 adds `hook_estimates`. Both are whole
        new tables rather than new columns, so they need no ALTER: `CREATE TABLE IF NOT
        EXISTS` in the DDL above has already run and an existing older database gains the
        empty table on open with every row it holds untouched. Nothing is dropped, renamed
        or backfilled — opening an older database is still non-destructive, and
        `schema_version_before` records what it was so the upgrade can be shown.
        """
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(turns)")}
        if "task_type_confidence" not in cols:
            self.conn.execute("ALTER TABLE turns ADD COLUMN task_type_confidence REAL")
        if "task_type_source" not in cols:
            self.conn.execute("ALTER TABLE turns ADD COLUMN task_type_source TEXT")
        pcols = {r[1] for r in self.conn.execute("PRAGMA table_info(turn_prompts)")}
        if "seq" not in pcols:
            self.conn.execute("ALTER TABLE turn_prompts ADD COLUMN seq INTEGER NOT NULL DEFAULT 0")
        if "src_file" not in pcols:
            self.conn.execute("ALTER TABLE turn_prompts ADD COLUMN src_file TEXT NOT NULL DEFAULT ''")
        if "src_line" not in pcols:
            self.conn.execute("ALTER TABLE turn_prompts ADD COLUMN src_line INTEGER NOT NULL DEFAULT 0")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ---- writes -----------------------------------------------------------------

    def upsert_messages(self, messages: Iterable[AssistantMessage]) -> int:
        """Insert messages, keeping the deterministic winner among duplicates.

        The row-value comparison implements `dedup_order` — earliest timestamp, then source
        file, then line. Without it the surviving copy depends on filesystem iteration order
        and a re-run can reattribute a cross-project duplicate to a different project (§5).
        An identical re-read of the same line is allowed through so a re-scan is idempotent.
        """
        messages = list(messages)
        if not messages:
            return 0
        rows = [
            (
                m.message_id, m.session_id, m.project, m.cwd, m.model, m.ts.isoformat(),
                m.input_tokens, m.output_tokens, m.cache_creation_tokens, m.cache_read_tokens,
                int(m.is_sidechain), m.cc_version, m.src_file, m.src_line,
            )
            for m in messages
        ]
        file_rows = [(m.message_id, fp) for m in messages for fp in m.file_paths]
        with self.tx() as conn:
            before = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            conn.executemany(
                """
                INSERT INTO messages (
                    message_id, session_id, project, cwd, model, ts,
                    input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens,
                    is_sidechain, cc_version, src_file, src_line
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(message_id) DO UPDATE SET
                    session_id=excluded.session_id, project=excluded.project, cwd=excluded.cwd,
                    model=excluded.model, ts=excluded.ts,
                    input_tokens=excluded.input_tokens, output_tokens=excluded.output_tokens,
                    cache_creation_tokens=excluded.cache_creation_tokens,
                    cache_read_tokens=excluded.cache_read_tokens,
                    is_sidechain=excluded.is_sidechain, cc_version=excluded.cc_version,
                    src_file=excluded.src_file, src_line=excluded.src_line
                WHERE (excluded.ts, excluded.src_file, excluded.src_line)
                    <= (messages.ts, messages.src_file, messages.src_line)
                """,
                rows,
            )
            if file_rows:
                conn.executemany(
                    "INSERT OR IGNORE INTO message_files(message_id, file_path) VALUES (?,?)",
                    file_rows,
                )
            after = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        return after - before

    def upsert_turns(self, turns: Iterable[Turn]) -> int:
        """Insert turn identity rows and their message links.

        Only prompt-derived columns are written here. Everything else is filled by
        `refresh_turn_aggregates()` — see the module docstring for why.
        """
        turns = list(turns)
        if not turns:
            return 0
        turn_rows = [
            (
                t.prompt.uuid, t.prompt.session_id, t.prompt.project, t.prompt.cwd,
                t.prompt.ts.isoformat(), t.chars, t.words, len(t.prompts),
                t.prompt.src_file, t.prompt.src_line,
            )
            for t in turns
        ]
        prompt_rows = [
            (p.uuid, t.prompt.uuid, seq, p.chars, p.words, p.src_file, p.src_line)
            for t in turns for seq, p in enumerate(t.prompts)
        ]
        link_rows = [(mid, t.prompt.uuid) for t in turns for mid in t.message_ids]
        with self.tx() as conn:
            before = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
            conn.executemany(
                """
                INSERT INTO turns (
                    prompt_uuid, session_id, project, cwd, ts,
                    prompt_chars, prompt_words, prompt_count, src_file, src_line
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(prompt_uuid) DO UPDATE SET
                    session_id=excluded.session_id, project=excluded.project, cwd=excluded.cwd,
                    ts=excluded.ts, prompt_chars=excluded.prompt_chars,
                    prompt_words=excluded.prompt_words, prompt_count=excluded.prompt_count,
                    src_file=excluded.src_file, src_line=excluded.src_line
                WHERE (excluded.ts, excluded.src_file, excluded.src_line)
                    <= (turns.ts, turns.src_file, turns.src_line)
                """,
                turn_rows,
            )
            if prompt_rows:
                conn.executemany(
                    "INSERT INTO turn_prompts(prompt_uuid, turn_uuid, seq, chars, words, "
                    "src_file, src_line) VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT(prompt_uuid) DO UPDATE SET "
                    "turn_uuid=excluded.turn_uuid, seq=excluded.seq, chars=excluded.chars, "
                    "words=excluded.words, src_file=excluded.src_file, src_line=excluded.src_line",
                    prompt_rows,
                )
            if link_rows:
                conn.executemany(LINK_SQL, link_rows)
            after = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
        return after - before

    def link_messages_to_open_turns(self, messages: Iterable[AssistantMessage]) -> int:
        """Attach assistant messages to the turn already open in their session.

        The tail-reader reads only new bytes, so a tick often yields assistant messages
        whose prompt was parsed on an earlier tick and is no longer in the batch. Without
        this they would count toward totals but belong to no turn, and the estimator would
        train on turns that look like they cost nothing.
        """
        linked = 0
        with self.tx() as conn:
            for m in messages:
                row = conn.execute(
                    "SELECT prompt_uuid FROM turns WHERE session_id = ? AND ts <= ? "
                    "ORDER BY ts DESC, src_line DESC LIMIT 1",
                    (m.session_id, m.ts.isoformat()),
                ).fetchone()
                if row is None:
                    continue
                cur = conn.execute(LINK_SQL, (m.message_id, row["prompt_uuid"]))
                linked += cur.rowcount
        return linked

    def set_task_type(self, prompt_uuid: str, bucket: str, confidence: float, source: str) -> None:
        with self.tx() as conn:
            conn.execute(
                "UPDATE turns SET task_type=?, task_type_confidence=?, task_type_source=? "
                "WHERE prompt_uuid=?",
                (bucket, confidence, source, prompt_uuid),
            )

    def unclassified_turns(self):
        """(turn row, [ordered (src_file, src_line) for each of its prompts]) for every
        turn with no task_type yet, oldest first. Callers re-read the prompt text
        transiently from disk (dict8.usage.parser.read_prompt_text) — it is never
        persisted here.
        """
        turns = self.conn.execute(
            "SELECT prompt_uuid, session_id, ts, prompt_count, message_count "
            "FROM turns WHERE task_type IS NULL ORDER BY ts"
        ).fetchall()
        out = []
        for t in turns:
            locs = self.conn.execute(
                "SELECT src_file, src_line FROM turn_prompts "
                "WHERE turn_uuid = ? ORDER BY seq", (t["prompt_uuid"],)
            ).fetchall()
            out.append((t, [(r["src_file"], r["src_line"]) for r in locs]))
        return out

    def refresh_turn_aggregates(self) -> None:
        with self.tx() as conn:
            conn.execute(REFRESH_SQL)

    # ---- cursors ----------------------------------------------------------------

    def get_cursor(self, path: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT offset, size, mtime FROM file_cursors WHERE path = ?", (path,)
        ).fetchone()

    def set_cursor(self, path: str, offset: int, size: int, mtime: float) -> None:
        with self.tx() as conn:
            conn.execute(
                "INSERT INTO file_cursors(path, offset, size, mtime, updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
                "offset=excluded.offset, size=excluded.size, mtime=excluded.mtime, "
                "updated_at=excluded.updated_at",
                (path, offset, size, mtime, _now()),
            )

    def clear_cursors(self) -> None:
        with self.tx() as conn:
            conn.execute("DELETE FROM file_cursors")

    # ---- quota check-ins --------------------------------------------------------

    def add_quota_reading(self, weekly_pct: float, ts: str, source: str,
                          recorded_at: str | None = None) -> int:
        """Append one reading. Returns its rowid.

        Validation lives in `dict8.usage.quota`, not here, so that every caller gets the
        same bounds and the same message — but the column types still refuse anything
        that is not a number.
        """
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO quota_readings(weekly_pct, ts, source, recorded_at) "
                "VALUES (?,?,?,?)",
                (float(weekly_pct), ts, source, recorded_at or _now()),
            )
        return int(cur.lastrowid)

    def latest_quota_reading(self) -> sqlite3.Row | None:
        """The most recent reading by the time it was TAKEN, not by the time it was
        written: a check-in typed in late is still a reading about the moment it names.
        Ties break on id so a re-run is deterministic."""
        return self.conn.execute(
            "SELECT id, weekly_pct, ts, source, recorded_at FROM quota_readings "
            "ORDER BY ts DESC, id DESC LIMIT 1"
        ).fetchone()

    def recent_quota_readings(self, limit: int) -> list[sqlite3.Row]:
        """The `limit` most recent readings, newest first, ordered the same way
        `latest_quota_reading()` orders them so the first row of this list and that row
        are always the same reading.

        A burn rate needs two points; `latest_quota_reading()` returns one. Taking the
        second by any other ordering (recorded_at, id) would silently pair readings that
        are not adjacent in the series the age is measured on.
        """
        return list(self.conn.execute(
            "SELECT id, weekly_pct, ts, source, recorded_at FROM quota_readings "
            "ORDER BY ts DESC, id DESC LIMIT ?", (int(limit),)))

    # ---- hook estimates ---------------------------------------------------------

    def add_hook_estimate(self, *, session_id: str | None, prompt_id: str | None, ts: str,
                          prompt_words: int, method: str, est_group: str, n: int,
                          low: int | None, point: int | None, high: int | None,
                          ood: bool) -> int:
        """Append one estimate produced at UserPromptSubmit. Returns its rowid.

        Keyword-only: the ten columns are mostly integers, and a positional call that
        transposes `low` and `high` would store a backwards band that nothing downstream
        could detect.
        """
        with self.tx() as conn:
            cur = conn.execute(
                "INSERT INTO hook_estimates(session_id, prompt_id, ts, prompt_words, "
                "method, est_group, n, low, point, high, ood) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (session_id, prompt_id, ts, int(prompt_words), method, est_group, int(n),
                 low, point, high, int(bool(ood))),
            )
        return int(cur.lastrowid)

    # ---- reads ------------------------------------------------------------------

    def totals(self, since: str | None = None, until: str | None = None) -> sqlite3.Row:
        where, params = [], []
        if since:
            where.append("ts >= ?")
            params.append(since)
        if until:
            where.append("ts <= ?")
            params.append(until)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return self.conn.execute(
            f"""SELECT COUNT(*) AS messages,
                       COALESCE(SUM(input_tokens),0)          AS input_tokens,
                       COALESCE(SUM(output_tokens),0)         AS output_tokens,
                       COALESCE(SUM(cache_creation_tokens),0) AS cache_creation_tokens,
                       COALESCE(SUM(cache_read_tokens),0)     AS cache_read_tokens
                FROM messages {clause}""",
            params,
        ).fetchone()

    def by_day(self, tz) -> list[dict]:
        """Per-day totals in the given `tzinfo`.

        Bucketing happens in Python, not SQL: SQLite has no IANA tz database, and
        `usage_oracle.tz_env` exists precisely because getting the day boundary wrong
        misbuckets everything. Takes a tzinfo rather than a name so `reconcile` can pass
        the same fixed offset it hands the oracle.
        """
        buckets: dict[str, dict] = {}
        for row in self.conn.execute(
            "SELECT ts, input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens "
            "FROM messages"
        ):
            day = datetime.fromisoformat(row["ts"]).astimezone(tz).date().isoformat()
            b = buckets.setdefault(day, {"messages": 0, **dict.fromkeys(TOKEN_COLUMNS, 0)})
            b["messages"] += 1
            for col in TOKEN_COLUMNS:
                b[col] += row[col]
        return [{"day": d, **v} for d, v in sorted(buckets.items())]

    def count(self, table: str) -> int:
        if table not in {"messages", "turns", "turn_messages", "turn_prompts",
                         "message_files", "file_cursors", "quota_readings",
                         "hook_estimates"}:
            raise ValueError(f"unknown table {table!r}")
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
