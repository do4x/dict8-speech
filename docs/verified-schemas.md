# docs/verified-schemas.md — observed on-disk shapes

Everything here was read off real files on this machine. Nothing is copied from memory or
from the reference implementation without being re-confirmed against disk (CLAUDE.md
invariant 4). A field that is not in this document must not appear in a parser.

**Observed:** 2026-09-15 · `~/.claude/projects/` · 148 `.jsonl` files · 8,545 lines ·
2,866 usage-bearing assistant rows · 1,128 unique `message.id`.

**Claude Code versions present in the corpus** (top-level `version`), measured over the
whole corpus after backfill, not a sample:

| `version` | messages | first seen | last seen |
|---|---|---|---|
| `2.1.270` | 90 | 2026-09-14T21:48 | 2026-09-15T09:24 |
| `2.1.267` | 235 | 2026-09-10T11:04 | 2026-09-14T11:06 |
| `2.1.266` | 350 | 2026-09-09T12:28 | 2026-09-09T23:45 |
| `2.1.261` | 217 | 2026-09-05T09:33 | 2026-09-08T10:58 |
| `2.1.258` | 65 | 2026-09-03T09:47 | 2026-09-03T12:51 |
| `2.1.255` | 6 | 2026-09-03T01:02 | 2026-09-03T01:22 |
| `1.0` | 184 | 2026-09-08T07:29 | 2026-09-09T12:27 |

**Observed writing version: `2.1.270`** — the sessions being appended to right now.

**`claude --version` is not the version writing the transcripts — FINDING.** The CLI on
`PATH` reports **2.1.267**, while the session that produced these very lines writes
**2.1.270**: the editor extension bundles its own copy. Pinning the schema against
`claude --version` would pin it against a client that is not writing the files. Read the
version off the transcript lines, which is what the table above does.

`version: "1.0"` is **not** a Claude Code release. Those lines carry `userType: "unknown"`
and `entrypoint: null`, sit chronologically *between* 2.1.261 and 2.1.266, and appear only
under two projects (`Cleanly`, `uNotch`) — a different client writing into the same tree.
They are otherwise schema-identical: same `message.usage` keys, same `model` strings, real
`sessionId`/`cwd`.

**Do not filter or branch on `version`.** The usage schema is stable across all seven
values, the set grows with every release, and dropping an unrecognised one silently
discards real turns. Record it; never gate on it.

---

## 1. Session log layout

```
~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl
```

`<encoded-cwd>` replaces both `/` and `.` with `-`, so a hidden directory encodes to `--`.
Reference layout for subagents — `<encoded-cwd>/<session-uuid>/subagents/agent-*.jsonl` —
is handled by the reference implementation but **0 such files exist on this machine**. This
Claude Code version marks subagent turns inline with `isSidechain` instead. Observed value
on every assistant line in the sample: `false`. `true` has **not** been observed here, so any
code path keyed on it is unverified and must say so.

Confirms `paths.claude_projects_glob` = `~/.claude/projects/*/*.jsonl`, but the tail-reader
uses `rglob("*.jsonl")` so a future subagent subdirectory is picked up without a config change.

## 2. Top-level line object

`type` values observed, in frequency order:

`assistant` · `user` · `attachment` · `queue-operation` · `last-prompt` · `atis-latch` ·
`bridge-session` · `ai-title` · `system` · `mode` · `custom-title` · `relocated` ·
`file-history-snapshot` · `file-history-delta` · `teleported-from` · `summary`

Only `assistant` and `user` matter to the usage layer. Every other type is ignored, not
errored on — new types appear across Claude Code releases and must never break a parse.

Fields used by Dict8, present on **all** 2,130 assistant lines sampled:

| field | type | use |
|---|---|---|
| `type` | str | line discriminator |
| `timestamp` | str, ISO-8601 with `Z` | day bucketing, wall time |
| `sessionId` | str (uuid) | session grouping |
| `cwd` | str (abs path) | project attribution |
| `uuid` | str | line identity |
| `parentUuid` | str/null | turn threading |
| `isSidechain` | bool | subagent marker (see §1) |
| `version` | str | recorded, never branched on |
| `message` | object | see §3 |

`requestId` is present on 2,129 of 2,130 — **not** guaranteed, do not key on it.

## 3. `message` object (assistant)

Present on all sampled assistant lines: `model`, `id`, `type`, `role`, `content`,
`stop_reason`, `stop_sequence`, `stop_details`, `usage`, `diagnostics`.
`container` and `context_management` appear on ~60% — optional.

- **`message.id`** — the dedup key. Format `msg_*`. Globally unique. See §5.
- **`message.model`** — e.g. `claude-sonnet-5`. Absent → the reference substitutes `"?"`.

## 4. `message.usage` — the token fields

Present on all 2,130 sampled assistant lines:

```
input_tokens                   int   ── counted
output_tokens                  int   ── counted
cache_creation_input_tokens    int   ── counted
cache_read_input_tokens        int   ── counted
service_tier                   str
inference_geo                  str
iterations                     int
speed                          str
cache_creation                 dict  ── DECOMPOSITION, not additional  (§4.1)
output_tokens_details          dict  ── DECOMPOSITION, not additional  (§4.2)
server_tool_use                dict  ── request counts, not tokens     (§4.3)
```

The four counted fields are exactly what the reference reads. Dict8 sums those four and
nothing else.

### 4.1 `cache_creation` is a breakdown — FINDING

```json
{"ephemeral_1h_input_tokens": 15875, "ephemeral_5m_input_tokens": 0}
```

Verified on **2,007 / 2,007** rows carrying the dict:
`sum(cache_creation.values()) == cache_creation_input_tokens`, exact, zero mismatches.

It is a split of the flat field by TTL, **not an addition to it**. Adding both double-counts
every cache write. The reference ignores the dict; that is correct and Dict8 does the same.

### 4.2 `output_tokens_details.thinking_tokens` is inside `output_tokens` — FINDING

Verified on **1,703 / 1,703** rows carrying a non-zero value: `thinking_tokens <= output_tokens`,
zero exceptions. Samples `(thinking, output)`: `(69, 190)`, `(847, 1100)`.

Thinking tokens are a component of `output_tokens`, not a sibling of it. Adding them
double-counts reasoning output. Not present in the reference's field set at all.

### 4.3 `server_tool_use` is not tokens

Keys: `web_search_requests`, `web_fetch_requests`. Request **counts**. Never summed into a
token total.

## 5. Dedup by `message.id` — measured

CLAUDE.md invariant 5 says naive counting "over-reports substantially". Measured on the full
corpus:

| | tokens |
|---|---|
| counting every occurrence | **680,747,884** |
| counting each `message.id` once | **263,973,953** |
| **naive over-report** | **+157.9%** (2.58×) |

- 895 of 1,128 unique ids occur more than once.
- 35 ids occur in **more than one file**.

**Cross-project duplicates exist.** Example — one id in two different project directories:

```
msg_011CeueXb1dkh7kRR1KQAbU9
  -Users-doax-Projects-AutoScroll-debug/674766b4-….jsonl
  -Users-doax-Projects-uNotch/1d3cc0cd-….jsonl
```

Consistent with the `relocated` / `teleported-from` line types in §2.

**Consequence the reference does not handle.** `claude-tokens` dedups on first-seen while
iterating `rglob` in arbitrary filesystem order, so which *project* a cross-file duplicate is
attributed to is nondeterministic between runs. Totals are unaffected; per-project grouping is.
Dict8 resolves it deterministically instead: **earliest `timestamp` wins, ties broken by
`(path, line number)`**, so a backfill is reproducible.

## 6. `message` object (user) — turn assembly

`message.content` is **either a string or a list** (observed 133 str / 1,048 list). Block
types inside the list: `tool_result` (1,006), `text` (54), `image` (2), `document` (1).

A user line is a **human prompt** iff its content is a `str`, or a list containing at least one
`text` block and no `tool_result` block. A `tool_result`-bearing line is the harness returning
output, not a person typing — counting those as prompts inflates the turn count several-fold.

**CORRECTION (2026-09-15), found while building the Phase 5 classifier.** The rule above
is necessary but not sufficient — verified wrong against this corpus's own `turns` table,
which came back with entries of 14,000+ `prompt_words`. The text behind them was
harness-injected content sitting on a user-role line with a `str` or bare-`text` content
shape identical to a real prompt: `<ide_selection>...</ide_selection>`,
`<local-command-caveat>...</local-command-caveat>` (the wrapper on every slash command —
143 occurrences in this corpus), `<command-name>`/`<command-message>`/`<command-args>`,
`<task-notification>`, `<system-reminder>`, `[Image: ...]` attachment captions, and a
Skill's full loaded instructions opening with the literal string `"Base directory for
this skill:"` (one instance alone was 108,386 characters — bigger than this machine's
entire real prompt history combined).

`isMeta: True` looked like the obvious flag for "not really the user" and measured
**wrong**: it also appears on a genuine, deliberately composed 15,681-word human prompt in
this corpus, and is absent on some of the synthetic lines above (older Claude Code
versions predate the field entirely — present on only 176 of 1,910 user lines checked).
Blanket-filtering on it would silently have dropped a real prompt from the data while
missing several synthetic ones. Content-based stripping is what's verified to work:
regex-remove any well-formed `<tag>...</tag>` block (generic over the tag name — every
one observed here is well-formed, and a tag Claude Code adds in a future version is still
caught) and any `[Image: ...]` placeholder, then treat the *residual* text as the prompt.
A line starting with the Skill-preamble prefix has zero residual in 100% of instances
checked, so it's dropped outright rather than parsed for a trailing few words that were
never observed to exist. Implemented in `dict8.usage.parser._strip_synthetic()`; the
result changed the corpus from 234 "turns" to 68 genuine ones. Effect on this document's
other findings: **none** — dedup, token totals, and reconciliation (§4, §5) are computed
from `assistant` lines and untouched by anything on a `user` line.

Known remaining gap, accepted rather than chased further: a small number of other
system-authored lines (a hook-activation notice, a scheduled goal check-in) carry none of
the markers above and still pass through as "human". Single-digit occurrences in this
corpus — worth revisiting if it shows up materially in the classifier's bucket
distribution, not worth an open-ended pattern blocklist today.

A **turn** = one human prompt plus every assistant line that follows it, within the same
`sessionId`, until the next human prompt. A run of human prompts with no assistant reply
between them (queued dictation, or one prompt interrupting another) folds into **one**
turn — Claude answers them together, so crediting the tokens to only the last prompt
attributes a merged turn's full cost to whichever line happened to close it.

## 7. Files touched

`tool_use` blocks inside assistant `message.content`. Observed tools carrying a path:

| tool | input keys |
|---|---|
| `Read` | `file_path`, `limit`, `offset` |
| `Edit` | `file_path`, `new_string`, `old_string`, `replace_all` |
| `Write` | `file_path`, `content` |

Most-used tool overall is `Bash` (738), which carries **no** `file_path`. Dict8 counts
**distinct `input.file_path` values across the turn**, read generically from any `tool_use`
block that has that key — so a tool added in a later Claude Code release is picked up without
a code change, per invariant 3.
