"""Local task-type classifier client — prompts/classify.md, run on-device.

Built local rather than against a cloud API (Denis's call, 2026-09-15): invariant 7 says
audio and transcripts are local-only, and detection can hand this a dictation that was
never meant for Claude Code at all (a Slack message, an email) — a cloud call would upload
that regardless of where it was actually headed. A local model that never leaves the
process avoids that category of leak entirely.

Measured on this machine (Apple M5) against `prompts/classify.evals.yml`, 2026-09-15:
`mlx-community/Qwen2.5-3B-Instruct-4bit` scores 12/12 (gate is >=10/12), warm latency
668-794ms on that eval set's 5-15 word inputs, against `classifier.timeout_ms: 800` — real
margin but thin (as little as 6ms on the worst case). Real historical turns run wider:
backfilling the 68 genuine turns in this corpus, 55 classified within budget at
599-792ms and 13 legitimately exceeded it — turns over roughly 500 words need more than
800ms to prefill, which is expected, not a bug. `timeout_ms` is doing real work here, not
a formality, which is the whole reason `classify()` enforces it itself rather than
trusting the model to stay under it.

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
"""

from __future__ import annotations

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

# macOS default is already "spawn", but pin it explicitly: "fork" after mlx/Metal has
# touched GPU state in the parent is a known way to get a hung or corrupted child.
_CTX = mp.get_context("spawn")


@dataclass(frozen=True, slots=True)
class ClassifyResult:
    bucket: str
    confidence: float
    why: str
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


def _worker_main(model_id: str, preamble: str, req_q, res_q, ready_q) -> None:
    """Runs in the child process. Loads the model once, then serves requests until the
    parent kills it. A request is `(call_id, transcript)`; a response is
    `(call_id, raw_text_or_None, error_or_None)`.
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
            out = generate(model, tokenizer, prompt=prompt, max_tokens=80)
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
        self.preamble = load_preamble(prompt_path)
        self.restarts = 0
        self._proc: Optional[mp.process.BaseProcess] = None
        self._req_q = None
        self._res_q = None

    def _spawn(self, load_timeout_s: float = 30.0) -> None:
        req_q: mp.Queue = _CTX.Queue()
        res_q: mp.Queue = _CTX.Queue()
        ready_q: mp.Queue = _CTX.Queue()
        proc = _CTX.Process(
            target=_worker_main,
            args=(self.model_id, self.preamble, req_q, res_q, ready_q),
            daemon=True,
        )
        proc.start()
        try:
            status, detail = ready_q.get(timeout=load_timeout_s)
        except queue.Empty:
            proc.kill()
            raise RuntimeError(f"classifier worker did not become ready within {load_timeout_s}s")
        if status != "ready":
            proc.kill()
            raise RuntimeError(f"classifier worker failed to load {self.model_id}: {detail}")
        self._proc, self._req_q, self._res_q = proc, req_q, res_q

    def _kill(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        self._proc.join(timeout=1.0)
        if self._proc.is_alive():
            self._proc.kill()
            self._proc.join(timeout=1.0)
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
            log.warning("classifier returned unparseable output — omitting: %r", (raw or "")[:200])
            return None

        bucket = parsed.get("bucket")
        if bucket not in self.valid_buckets:
            log.warning("classifier named an out-of-config bucket %r — omitting", bucket)
            return None

        try:
            confidence = float(parsed.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        why = str(parsed.get("why", ""))[:200]

        return ClassifyResult(bucket=bucket, confidence=confidence, why=why,
                               latency_ms=elapsed * 1000)

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
