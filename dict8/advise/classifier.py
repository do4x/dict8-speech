"""Local task-type classifier client — prompts/classify.md, run on-device.

Built local rather than against a cloud API (Denis's call, 2026-09-15): invariant 7 says
audio and transcripts are local-only, and detection can hand this a dictation that was
never meant for Claude Code at all (a Slack message, an email) — a cloud call would upload
that regardless of where it was actually headed. A local model that never leaves the
process avoids that category of leak entirely.

**The output shrank on 2026-09-19 (U4) and it bought most of the budget back.**
`prompts/classify.md` used to ask for `{"bucket":…,"confidence":…,"why":…}`; it now asks
for the bucket alone, because nothing read the other two (see `ClassifyResult`). Those
fields were not free — they are generated one token at a time inside
`classifier.timeout_ms: 800`. Measured on this machine (Apple M5), same model, same eval
set, back to back:

    before  11/12   latency min 605ms  median 636ms  max 713ms   (1 first-call timeout)
    after   11/12   latency min 320ms  median 334ms  max 388ms   (0 restarts)

and on the 40 unclassified turns in a scratch copy of the real store, same two runs:

    before  5 of 40 classified,  max classified prompt 155 words
    after  23 of 40 classified,  max classified prompt 595 words

So the line that used to read "the budget covers roughly the first ~100 words" now reads
**roughly the first ~600**, and the corpus went from 74/109 turns bucketed to 92/109. The
long, detailed prompts — the ones enhancement and the estimate matter most for, and the
ones that got nothing before — are most of what came back.

**One eval case regressed and it is not noise.** `"use opus"` (a voice command that leaked
past the stripper; `classify.evals.yml` marks it "must not classify") returned `unknown`
before and returns `feature-build` after, reproducibly, on two runs. 11/12 still clears the
>=10/12 ship gate, and the earlier 11/12 was a transient first-call timeout rather than a
wrong answer, so this is a real trade: a large latency and coverage win against one
abstention the model no longer makes. Read it as evidence that the `confidence` field was
doing something the prompt's `"unknown"` rule now has to do on its own, not as a wash.

Historical numbers, for comparison: 12/12 on 2026-09-15 at 668-794ms; on 2026-09-18,
69 of 82 turns classified at 598-799ms with every classified turn <=155 words (median 16)
and every omitted one >=93 (median 361, max 4028). The static ~450-token preamble is still
prefilled on every call. `timeout_ms` is still doing real work, which is why `classify()`
enforces it itself rather than trusting the model to stay under it — there is simply more
headroom under it now.

**"68 genuine turns", not the 234 first found.** The first backfill attempt fed this
classifier things like a 28,000-token Skill-loading dump and an IDE selection block as if
they were dictated prompts — `dict8.usage.parser.is_human_prompt()` had a real bug (fixed
2026-09-15, see that module) that let harness-injected content on a user-role line count
as something a person said. Every one of those was slow enough on its own to look like a
fundamentally broken timeout budget; the actual budget is fine once the classifier only
ever sees what someone actually typed or dictated.

**Why a subprocess, not a thread.** The first version of this ran generation in a
single-worker `ThreadPoolExecutor` and gave up waiting after `timeout_ms` without
cancelling the work — Python cannot forcibly cancel a running thread. That is a bug, not
a simplification: once one real call overran the budget, the *next* call queued behind
the still-running first one and also timed out before its own generation ever started,
and every call after that did too. Measured: after the first overrun, 164 of the next 164
calls in a row reported "timeout" — a permanent, silent wedge that looks exactly like a
healthy advisory feature quietly doing nothing forever, which is worse than invariant 2's
"no chip on the overlay" degradation — that degrades once per failure, not once ever.
A subprocess can actually be killed on overrun: the parent enforces the deadline, and a
call that blows it gets its worker terminated and replaced, so the *next* call starts
clean instead of inheriting the backlog. The cost is a reload (~1-2s) on the rare call
that overruns — far cheaper than a wedge that never clears.

**`mlx-lm` is an optional extra, not a base dependency** (2026-09-19, U4). The headless
layer — usage, quota, estimate, the `UserPromptSubmit` hook — never reaches this module,
and making every install of it pull ~290 MB of model runtime was a cost with no user.
Install it with `uv run --extra classify …`. Without it this client does exactly what it
does for every other failure: one log line and `classify()` returns None, the caller shows
no chip, nothing raises (invariant 2). The check is `find_spec` in the PARENT, before a
worker is spawned: discovering a missing import inside a child would cost a process spawn
and surface as the far vaguer "worker failed to load".
"""

from __future__ import annotations

import importlib.util
import json
import logging
import multiprocessing as mp
import queue
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dict8.config import Config, REPO_ROOT

log = logging.getLogger("dict8.advise.classifier")

PROMPT_PATH = REPO_ROOT / "prompts" / "classify.md"

# The backend module name behind `classifier.backend: mlx-lm`. Not a threshold and not a
# model name — an import path, the same kind of fixed artifact name as `hooks.LOG_FILENAME`.
BACKEND_MODULE = "mlx_lm"

MISSING_BACKEND_HINT = (
    f"{BACKEND_MODULE} is not importable — it ships as the optional `classify` extra. "
    f"Re-run with `uv run --extra classify …` (or `uv sync --extra classify`)."
)


class BackendUnavailable(RuntimeError):
    """`classifier.backend` is not installed in this environment.

    Its own exception type so a caller can tell "you did not install the extra" (fixable
    in one command) from "the model would not load" (a real fault) without matching on a
    message string.
    """


def backend_available() -> bool:
    """Is `classifier.backend`'s module importable here? Checked without importing it:
    `mlx_lm` pulls in mlx and transformers and costs ~1 s, which is most of the hook's
    entire budget and is wasted on every process that was never going to classify."""
    return importlib.util.find_spec(BACKEND_MODULE) is not None

# macOS default is already "spawn", but pin it explicitly: "fork" after mlx/Metal has
# touched GPU state in the parent is a known way to get a hung or corrupted child.
_CTX = mp.get_context("spawn")


@dataclass(frozen=True, slots=True)
class ClassifyResult:
    """The bucket and how long it took. Nothing else.

    `confidence` and `why` were dropped on 2026-09-19 (U4). Nothing consumed either one —
    `prompts/build.md` Phase 5 says the classifier "returns a bucket only", the bucket to
    model mapping is a deterministic lookup in `config.models`, and `task_type_confidence`
    was written to the store and never read by anything. They were not free: every token
    of `"confidence":0.94,"why":"single mechanical rename"` is generated serially inside
    `classifier.timeout_ms: 800`, against a measured margin of 0-170 ms. Measured effect,
    same machine, same eval set — see the module docstring's numbers and HANDOFF section 5.

    `turns.task_type_confidence` is deliberately KEPT as a column and written NULL: dropping
    a column means rewriting the table, and a null reads honestly as "this classifier did
    not report one" where a 0.0 would read as "it was certain it had no idea".
    """

    bucket: str
    latency_ms: float


def load_preamble(path: Path = PROMPT_PATH) -> str:
    """Everything in classify.md up to the prefill instruction — role, buckets, rules,
    examples. The final line ("Prefill the assistant turn with...") is an instruction to
    the caller, not prompt content, so it is not sent to the model.
    """
    text = path.read_text(encoding="utf-8")
    marker = "Prefill the assistant turn"
    if marker not in text:
        raise ValueError(f"{path}: expected marker {marker!r} not found — prompt file changed shape")
    return text.split(marker)[0].strip()


def _extract_json_object(text: str) -> Optional[dict]:
    """First balanced {...} in `text`. A small model sometimes rambles past the object;
    only the object matters."""
    depth = 0
    end = None
    for i, ch in enumerate(text):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        return None
    try:
        parsed = json.loads(text[:end])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _worker_main(model_id: str, preamble: str, max_output_tokens: int,
                 req_q, res_q, ready_q) -> None:
    """Runs in the child process. Loads the model once, then serves requests until the
    parent kills it. A request is `(call_id, transcript)`; a response is
    `(call_id, raw_text_or_None, error_or_None)`.

    `max_output_tokens` is passed in rather than read from config here: this process was
    started with "spawn", so it re-imports the module but inherits nothing, and a second
    config read in the child could disagree with the parent's if config.yml changed
    between the two.
    """
    try:
        from mlx_lm import generate, load

        model, tokenizer = load(model_id)
    except Exception as exc:  # model load failed — tell the parent, don't hang forever
        ready_q.put(("error", repr(exc)))
        return
    ready_q.put(("ready", None))

    while True:
        call_id, transcript = req_q.get()  # blocks; parent sends a poison pill to stop
        if call_id is None:
            return
        try:
            msgs = [
                {"role": "system", "content": preamble},
                {"role": "user", "content": f'Input: "{transcript}"'},
            ]
            prompt = (
                tokenizer.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
                + '{"bucket":'
            )
            out = generate(model, tokenizer, prompt=prompt, max_tokens=max_output_tokens)
            res_q.put((call_id, '{"bucket":' + out, None))
        except Exception as exc:
            res_q.put((call_id, None, repr(exc)))


class Classifier:
    """One resident worker process, model held warm across calls (mirrors
    `stt.keep_resident`) — a fresh model load per call would blow the latency budget
    on its own. Only ever one call in flight; a caller that needs concurrency should
    run multiple `Classifier` instances, not share one.
    """

    def __init__(self, cfg: Config, *, prompt_path: Path = PROMPT_PATH) -> None:
        self.model_id = str(cfg.require("classifier.model"))
        self.timeout_s = int(cfg.require("classifier.timeout_ms")) / 1000.0
        self.on_error = str(cfg.get("classifier.on_error", "omit"))
        self.valid_buckets = set(cfg.require("classifier.buckets")) | {"unknown"}
        # Every budget below comes from config.yml (invariant 3); see the `classifier:`
        # block there for where each number came from.
        self.max_output_tokens = int(cfg.require("classifier.max_output_tokens"))
        self.load_timeout_s = float(cfg.require("classifier.load_timeout_ms")) / 1000.0
        self.join_timeout_s = float(cfg.require("classifier.worker_join_timeout_ms")) / 1000.0
        self.preamble = load_preamble(prompt_path)
        self.restarts = 0
        self._proc: Optional[mp.process.BaseProcess] = None
        self._req_q = None
        self._res_q = None

    def _spawn(self) -> None:
        if not backend_available():
            # Before the spawn, not after: see the module docstring.
            raise BackendUnavailable(MISSING_BACKEND_HINT)
        req_q: mp.Queue = _CTX.Queue()
        res_q: mp.Queue = _CTX.Queue()
        ready_q: mp.Queue = _CTX.Queue()
        proc = _CTX.Process(
            target=_worker_main,
            args=(self.model_id, self.preamble, self.max_output_tokens,
                  req_q, res_q, ready_q),
            daemon=True,
        )
        proc.start()
        try:
            status, detail = ready_q.get(timeout=self.load_timeout_s)
        except queue.Empty:
            proc.kill()
            raise RuntimeError(
                f"classifier worker did not become ready within "
                f"classifier.load_timeout_ms={self.load_timeout_s * 1000:g}"
            )
        if status != "ready":
            proc.kill()
            raise RuntimeError(f"classifier worker failed to load {self.model_id}: {detail}")
        self._proc, self._req_q, self._res_q = proc, req_q, res_q

    def _kill(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        self._proc.join(timeout=self.join_timeout_s)
        if self._proc.is_alive():
            self._proc.kill()
            self._proc.join(timeout=self.join_timeout_s)
        self._proc = self._req_q = self._res_q = None

    def warm(self) -> None:
        """Load the model now rather than on the first classify() call."""
        if self._proc is None or not self._proc.is_alive():
            self._spawn()

    def classify(self, transcript: str) -> Optional[ClassifyResult]:
        """Bucket `transcript`, or None on any failure — timeout, bad JSON, invalid
        bucket, worker crash. Per `classifier.on_error: omit`: the caller shows no chip,
        never blocks the send. Every failure is logged with why, per invariant 8. A
        timeout kills and replaces the worker so the *next* call starts clean — see the
        module docstring for why that matters.
        """
        if not transcript or not transcript.strip():
            return None

        try:
            self.warm()
        except BackendUnavailable as exc:
            # One line, no traceback. An uninstalled optional extra is a configuration
            # fact, not a fault, and invariant 2 says the layer degrades to no chip.
            log.warning("classifier unavailable — omitting: %s", exc)
            return None
        except Exception as exc:
            log.warning("classifier failed to start: %r — omitting", exc)
            return None

        call_id = uuid.uuid4().hex
        self._req_q.put((call_id, transcript))
        t0 = time.monotonic()
        try:
            got_id, raw, err = self._res_q.get(timeout=self.timeout_s)
        except queue.Empty:
            elapsed = time.monotonic() - t0
            log.warning("classifier timeout after %.0fms (budget %.0fms) — killing and "
                        "replacing the worker, omitting this turn",
                        elapsed * 1000, self.timeout_s * 1000)
            self._kill()
            self.restarts += 1
            return None
        elapsed = time.monotonic() - t0

        if got_id != call_id:
            # Stale response from a call this Classifier gave up on earlier. Shouldn't
            # happen (a timeout kills the worker that owed it), but never attribute one
            # call's answer to another.
            log.warning("classifier response id mismatch — discarding and omitting")
            return None
        if err is not None:
            log.warning("classifier worker error: %s — omitting", err)
            return None

        parsed = _extract_json_object(raw or "")
        if parsed is None:
            # The MODEL's output, truncated — never the transcript. The only thing worth
            # seeing when a bucket fails to parse is what the model actually emitted.
            log.warning("classifier returned unparseable output — omitting: %r", (raw or "")[:200])
            return None

        bucket = parsed.get("bucket")
        if bucket not in self.valid_buckets:
            log.warning("classifier named an out-of-config bucket %r — omitting", bucket)
            return None

        # Any other key the model volunteers is ignored rather than errored on: the prompt
        # asks for `{"bucket": ...}` alone, and a small model occasionally adds a field
        # back from its own priors. Reading only what is consumed is what keeps that a
        # non-event.
        return ClassifyResult(bucket=bucket, latency_ms=elapsed * 1000)

    def close(self) -> None:
        if self._proc is not None and self._proc.is_alive() and self._req_q is not None:
            try:
                self._req_q.put((None, None))
            except Exception:
                pass
        self._kill()

    def __enter__(self) -> "Classifier":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
