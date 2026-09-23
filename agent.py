#!/usr/bin/env python3
"""Python -> Rust translation agent.

    ANTHROPIC_API_KEY=... in .env (or the environment) - never hard-coded
    python agent.py                       # translate reference/version.py -> rust/src/lib.rs (from the stub)
    python agent.py --resume              # continue from the current lib.rs instead
    python agent.py --model claude-opus-5-5 --effort xhigh --max-usd 1.0
    python agent.py --lean | --hints      # prompt ablations (source not in prompt / semver hints added)
    python selftest.py                    # offline test of this harness, no key, no cost
    See README.md for the full user guide.

ARCHITECTURE (one paragraph per skeleton TODO)

  1. call_model()    Claude via the Anthropic SDK: streaming, adaptive thinking,
                     prompt caching on the static prefix (tools + system prompt,
                     which holds the whole Python source and its tests).

  2. system_prompt() The Python source IS the specification and goes in verbatim
                     (cached, so ~free after the first call). On top of it: the
                     fixed Rust contract, the grader's rules, general Python->Rust
                     semantic gaps, and how this harness works. Semver-specific
                     answers are NOT baked in by default (--hints turns them on).

  3. build_context() No transcript at all. Every call is a fresh single-turn prompt
                     rebuilt from state that lives on disk / in the harness:
                       WRITE    lib.rs, the best-so-far snapshot, and the agent's own
                                notes are state, not chat history
                       SELECT   only the latest verification report and the last
                                step's tool outputs are shown in full
                       COMPRESS every earlier step is one line (action -> result)
                       ISOLATE  build / test / differential runs happen outside the
                                model; it only sees their compressed verdict
                     The prompt therefore stays roughly constant in size at step 3
                     or step 40, and never replays stale compiler errors.

  4. should_stop()   "Done" is decided by the harness, not the model: build ok,
                     no rule violations, zero unwrap, zero clippy warnings, >= 15
                     passing tests and 100% differential agreement, re-confirmed on
                     3 more fresh seeds.
                     "Stuck" = no best-score improvement for 4 calls, the same
                     action 3 times, or 2 replies in a row with no tool call.
                     Score went DOWN -> one call to recover, then auto-rollback.
                     A finish request with unmet criteria is refused once (and
                     always while the build fails or rules are violated).
                     Errors no retry can fix (bad key, no credits, unknown model)
                     stop at once; a $ cap bounds every run.
                     Whatever happens, the run ends on the best version seen,
                     formatted by rustfmt only if re-verification shows no loss.

  5. tools           write_rust (whole file) and edit_rust (batched, atomic
                     search/replace) for coarse and fine edits; probe (run any
                     command on the Python oracle AND the current Rust build side by
                     side); note (the agent's memory across calls); restore_best;
                     finish; read_python only in --lean mode.
                     cargo build / test / clippy / evaluate are no longer tools: they
                     run automatically after every edit (checks.py), which saves
                     model calls.
"""
from __future__ import annotations
import argparse, hashlib, json, os, pathlib, shutil, subprocess, time, sys

import checks

HERE   = pathlib.Path(__file__).parent
RUST   = HERE / "rust"
LIB    = RUST / "src" / "lib.rs"
PYSRC  = HERE / "reference" / "version.py"
REFDIR = HERE / "reference"
LOGS   = HERE / "logs"
STATE  = HERE / ".agent"
BEST   = STATE / "best_lib.rs"
STUB   = HERE / "templates" / "lib_stub.rs"

def _load_dotenv():
    """KEY=value lines from ./.env (gitignored) - keeps the API key out of code and shell history."""
    p = HERE / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
_load_dotenv()

MODEL  = os.environ.get("AGENT_MODEL", "claude-sonnet-5")   # --model claude-opus-5-5 to escalate
# Set explicitly: Opus 5.5 defaults to "medium". Calls are capped at 40 but tokens are not,
# so spend thinking where it buys correctness. xhigh/max: longer turns, try if time allows.
EFFORT = os.environ.get("AGENT_EFFORT", "high")        # low | medium | high | xhigh | max
USE_FALLBACK = os.environ.get("AGENT_NO_FALLBACK") is None
MAX_USD = float(os.environ.get("AGENT_MAX_USD", "1.50"))   # hard spend cap per run

# $ per million tokens (input, output). Cache writes bill 1.25x input, cache reads 0.1x.
PRICES = {"claude-sonnet-5": (2.0, 10.0), "claude-opus-5-5": (4.0, 20.0),
          "claude-opus-5": (5.0, 25.0), "claude-haiku-4-5": (1.0, 5.0)}

def cost_usd(model: str, usage: dict) -> float:
    pin, pout = PRICES.get(model, (5.0, 25.0))            # unknown model: assume Opus-tier
    return ((usage.get("input") or 0) * pin + (usage.get("cache_write") or 0) * pin * 1.25
            + (usage.get("cache_read") or 0) * pin * 0.1 + (usage.get("output") or 0) * pout) / 1e6

PATIENCE      = 4      # model calls without a new best score before we call it stuck
MAX_REPEATS   = 3      # identical action this many times -> stuck
MAX_IDLE      = 2      # consecutive replies with no tool call -> stop
MAX_API_FAILS = 3      # consecutive API failures -> stop

for _s in (sys.stdout, sys.stderr):                     # Windows consoles default to cp1252
    try:
        _s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    except AttributeError:
        pass

# ============================================================== TODO 1
_client = None

def request_kwargs(messages: list[dict], tools: list[dict]) -> dict:
    """The exact request body (shared by call_model and the preflight check)."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    convo  = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]
    api_tools = [{"name": t["name"], "description": t["description"],
                  "input_schema": t["parameters"], "eager_input_streaming": True} for t in tools]
    kwargs = dict(
        model=MODEL,
        max_tokens=64000,                 # a whole lib.rs plus tests fits comfortably
        # One breakpoint on the system prompt caches tools + system (render order),
        # i.e. everything except the per-step user message.
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        tools=api_tools,
        messages=convo,
        thinking={"type": "adaptive", "display": "summarized"},   # summary is logged, never fed back
        output_config={"effort": EFFORT},
    )
    if USE_FALLBACK:                      # re-run on another model if a safety classifier declines
        kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    return kwargs

def client():
    global _client
    import anthropic
    if _client is None:
        _client = anthropic.Anthropic(max_retries=4, timeout=900.0)   # key from the environment
    return _client

def call_model(messages: list[dict], tools: list[dict]) -> dict:
    """Send `messages` + `tools` to Claude; return
        {"text": str | None, "tool_calls": [{"id", "name", "arguments"}], ...extras}
    """
    with client().beta.messages.stream(**request_kwargs(messages, tools)) as stream:
        msg = stream.get_final_message()

    text, calls, thoughts = [], [], []
    for b in msg.content:
        if b.type == "text":
            text.append(b.text)
        elif b.type == "tool_use":
            calls.append({"id": b.id, "name": b.name,
                          "arguments": b.input if isinstance(b.input, dict) else {}})
        elif b.type == "thinking" and b.thinking:
            thoughts.append(b.thinking)
    u = msg.usage
    return {
        "text": "\n".join(text).strip() or None,
        "tool_calls": calls,
        "stop_reason": msg.stop_reason,
        "thinking": "\n".join(thoughts) or None,
        "model": msg.model,
        "usage": {"input": u.input_tokens, "output": u.output_tokens,
                  "cache_read": getattr(u, "cache_read_input_tokens", None),
                  "cache_write": getattr(u, "cache_creation_input_tokens", None)},
    }

# ============================================================== TODO 2
CONTRACT = """\
```rust
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Version {
    pub major: u64,
    pub minor: u64,
    pub patch: u64,
    pub prerelease: Option<String>,   // text after '-', separator stripped; None when absent
    pub build: Option<String>,        // text after '+', separator stripped; None when absent
}
pub fn parse(s: &str) -> Result<Version, String>
pub fn to_string(v: &Version) -> String
pub fn compare(a: &Version, b: &Version) -> std::cmp::Ordering
pub fn bump_major(v: &Version) -> Version
pub fn bump_minor(v: &Version) -> Version
pub fn bump_patch(v: &Version) -> Version
```
`rust/src/main.rs` (fixed, you cannot see or change it) reads commands like
`parse 1.2.3-rc.1`, `compare A B`, `bump patch A`, `format A` and calls exactly
these functions: `format` is `to_string(&parse(A)?)`, `bump` prints
`to_string(&bump_x(&parse(A)?))`, `compare` maps Ordering to -1/0/1. The struct's
field names and types and these six signatures are frozen. You may add private
helpers, extra `impl`s (e.g. `fmt::Display`, `FromStr`) and tests."""

RULES = """\
- NO `unsafe` - the grader counts the bare word `unsafe` anywhere outside `//` comments,
  including string literals, so do not write that word at all.
- std only: no dependencies, no FFI, no `std::process`, never call back into Python.
- NO `todo!`, `unimplemented!`, `panic!` anywhere - tests included. Avoid `unreachable!`
  and `.expect()` in library code too: every input must yield Ok/Err, never a panic
  (watch slicing/indexing bounds and integer overflow).
- NO `.unwrap()` anywhere - tests included (the grader counts them). In tests, compare
  whole values: `assert_eq!(parse("1.2.3"), Ok(Version { .. }))`, or make the test
  `fn t() -> Result<(), String>` and use `?`.
- Keep `.clone()` / `.to_owned()` rare. The struct owns its Strings; functions borrow
  (`&Version`, `&str`) and build new values. A bump constructs a fresh `Version`, it
  does not clone-and-mutate. Owning a `String` is idiomatic; cloning a borrow to escape
  the borrow checker is not.
- No ``` code fences in doc comments: rustdoc would run them as doc-tests."""

GAPS = """\
Python -> Rust semantic gaps that silently change behaviour. Check each one against the source:
- Python `int` is unbounded; Rust integers are not. Where the Rust type is fixed (u64 fields)
  reject what does not fit rather than wrapping. Where Python only COMPARES numbers that may be
  arbitrarily long, compare them without converting (e.g. digit strings with no leading zeros:
  shorter is smaller, equal length compares lexically).
- Regexes: mirror the exact grammar, anchors (`\\Z` is end-of-string, no trailing newline),
  flags (`re.ASCII` makes `\\d` mean [0-9]) and alternation order. `fullmatch` vs `match` matter.
  Hand-write the grammar as a small validator; there is no regex crate. Beware separator
  characters that may legally appear again inside a later component.
- `str.isdigit()`, truthiness (`if s:` is False for "" and None), `zip` stopping at the shorter
  input, lexicographic tuple comparison, `(a > b) - (a < b)` is `a.cmp(&b)`.
- Every `raise ValueError` path becomes an `Err(String)`; every path that returns must return
  exactly what Python returns - read the code, do not assume the "standard" behaviour.
- Optional native accelerators (e.g. a `try: import <native backend>` block) are not part of
  the spec: translate the pure-Python path."""

SEMVER_HINTS = """\
Semver pitfalls first drafts get wrong (verified against the oracle):
- build metadata is ignored entirely for precedence; a version WITH a prerelease is lower than
  the same version without one.
- prerelease identifiers compare left to right; numeric ones numerically and always lower than
  alphanumeric ones; alphanumerics compare in ASCII order; if all shared identifiers are equal,
  the one with more identifiers is greater.
- numeric prerelease identifiers may not have leading zeros ("01"); build identifiers may.
- bump_major/minor/patch drop prerelease and build (read bump_patch - do not guess)."""

HOW = """\
How this harness works - it is NOT a chat:
- Every reply you send costs 1 model call out of a small budget. Tool calls inside a reply are
  free, so put EVERYTHING you want to do into one reply: several edits in one `edit_rust` call,
  or a complete file via `write_rust`. Probes run after your edits in the same reply.
- You have no memory between calls. Each call you get a fresh snapshot: the current lib.rs, an
  automatic verification report, a one-line-per-step log, the full output of last step's tools,
  and the notes you wrote. In EVERY reply, call `note` with a short status: what you changed,
  what you believe is still wrong, what you will do next. It is shown back to you verbatim.
- After any change to lib.rs the harness automatically runs cargo build, cargo test, a
  differential test against the real Python `semver` package on FRESH random inputs every time
  (so fix root causes; never special-case an input), and a quality scan. Never ask for these.
- The harness keeps a copy of the best-scoring version. If a change makes the score worse you
  get one call to fix it, then it is rolled back automatically.
- The run ends by itself when every acceptance criterion holds. Call `finish` only if you are
  convinced you cannot improve further.

Acceptance criteria (all verified by the harness):
1. builds; 2. no rule violations and zero `.unwrap()`; 3. 100% agreement with the Python
oracle on parse (accept AND reject), compare, bump_* and round-trip; 4. `cargo test` passes
with >= 15 `#[test]` functions ported from reference/test_*.py; 5. zero `cargo clippy` warnings
(default lints, tests included). Port only what the Rust API
can express (parse, to_string, compare, bump_major/minor/patch, round-trips, invalid inputs,
the semver.org precedence chain); skip Python-only features (dict/tuple/bytes inputs,
TypeError, optional_minor_and_patch, bump_prerelease, bump_build, match, replace, ...).

Strategy:
- First reply: study the Python carefully, then `write_rust` the COMPLETE lib.rs -
  implementation and the ported tests - in one go. Get it right the first time; you have
  the whole source below, so you do not need to explore.
- Later replies: act on the verification report. Build errors first. Then differential
  mismatches: each shows the input, what Rust returned and what Python returned - find the
  line of Python that explains it. If a cargo test fails while the differential is 100%, the
  test's expectation is wrong: fix the test, not the code. Use `probe` only when a mismatch
  is genuinely ambiguous; it costs a call to see its output."""

def _ref_files(lean: bool) -> str:
    if lean:
        return ("The Python source is NOT included here. Use `read_python` to read "
                "reference/version.py (831 lines) and reference/test_*.py - read in large "
                "ranges, each read costs a call.")
    out = []
    for name in ("version.py", "_types.py", "test_parsing.py", "test_compare.py", "test_bump.py"):
        p = REFDIR / name
        if p.exists():
            out.append(f"### reference/{name}\n```python\n{p.read_text(encoding='utf-8').rstrip()}\n```")
    return "\n\n".join(out)

def system_prompt(lean: bool = False, hints: bool = False) -> str:
    """Stable across the whole run (it is the cached prefix): nothing volatile goes here."""
    parts = [
        "You are a senior Rust engineer translating a Python module into a std-only Rust "
        "library, working autonomously inside an automated harness. The Python source is the "
        "specification: match its observable behaviour exactly, and write the Rust a careful "
        "Rust programmer would write - safe, panic-free, allocation-conscious, idiomatic.",
        "## Task\nTranslate `reference/version.py` (python-semver) into `rust/src/lib.rs`.",
        "## The fixed Rust contract\n" + CONTRACT,
        "## Hard rules (a violation voids the result, whatever the test scores)\n" + RULES,
        "## Translating faithfully\n" + GAPS,
    ]
    if hints:
        parts.append("## Domain notes\n" + SEMVER_HINTS)
    parts += ["## " + HOW, "## Reference source (the specification)\n" + _ref_files(lean)]
    return "\n\n".join(parts)

# ============================================================== TODO 3
class State:
    """Everything the agent 'remembers' lives here or on disk - not in a transcript."""
    def __init__(self):
        self.log: list[dict] = []           # one compact record per model call
        self.last_outputs: list[tuple] = [] # (tool name, full output) from the previous step
        self.notes: list[tuple] = []        # (step, text the model wrote)
        self.harness_msgs: list[str] = []   # warnings for the next context only
        self.check: dict | None = None      # latest verification of lib.rs
        self.best_score = float("-inf")
        self.best_step = 0
        self.best_line = ""
        self.no_improve = 0
        self.regressed = False
        self.done = False
        self.finish_refusals = 0
        self.finish_accepted = False
        self.finish_summary = ""
        self.idle = 0
        self.api_fails = 0
        self.spent = 0.0
        self.replies = 0          # model calls that actually returned
        self.fatal = ""           # an error no retry can fix (bad key, bad request, no credits)
        self.actions: dict[str, int] = {}
        self.repeat_hit = False

def _numbered(text: str) -> str:
    return "\n".join(f"{i:4d} | {l}" for i, l in enumerate(text.splitlines(), 1))

def build_context(state: State, step: int, budget: int, sys_prompt: str) -> list[dict]:
    """The messages actually sent: [system (static, cached), user (fresh snapshot)]."""
    lib = LIB.read_text(encoding="utf-8") if LIB.exists() else "(missing)"
    U = [f"# Step {step} of {budget} (model calls left after this one: {budget - step})"]

    U.append("## Current rust/src/lib.rs\n```rust\n" + _numbered(lib) + "\n```")

    if state.log:
        lines = []
        for r in state.log[:-1][-25:]:                              # COMPRESS: one line per step
            lines.append(f"- step {r['step']}: {r['actions']} -> {r['result']}")
        r = state.log[-1]
        lines.append(f"- step {r['step']} (last): {r['actions']} -> {r['result']}")
        U.append("## Step log (oldest first)\n" + "\n".join(lines))
    if state.best_step:
        U.append(f"Best version so far: step {state.best_step}: {state.best_line} "
                 "(kept as a snapshot; `restore_best` brings it back).")

    if state.notes:                                                  # WRITE: the agent's own memory
        U.append("## Your notes from earlier calls (most recent last)\n" + "\n\n".join(
            f"[step {s}] {checks._clip(t, 1500)}" for s, t in state.notes[-3:]))

    if state.last_outputs:                                           # SELECT: last step in full
        U.append("## Output of the tools you called last step\n" + "\n\n".join(
            f"### {name}\n{checks._clip(out, 6000)}" for name, out in state.last_outputs))

    if state.check:
        U.append("## Verification of the current lib.rs (automatic)\n" + checks.render(state.check))

    if state.harness_msgs:
        U.append("## Harness messages\n" + "\n".join(f"- {m}" for m in state.harness_msgs))

    U.append("Act now: make every change you intend in this one reply, and call `note`.")
    return [{"role": "system", "content": sys_prompt},
            {"role": "user", "content": "\n\n".join(U)}]

# ============================================================== TODO 4
def should_stop(state: State, step: int, budget: int) -> tuple[bool, str]:
    if state.done:
        return True, "done: every acceptance criterion holds, re-confirmed on fresh seeds"
    if state.finish_accepted:
        return True, f"agent finished: {state.finish_summary[:200]}"
    if state.fatal:
        return True, f"cannot continue: {state.fatal}"
    if state.spent >= MAX_USD:
        return True, f"cost cap reached (${state.spent:.2f} >= ${MAX_USD:.2f}; raise with --max-usd)"
    if step >= budget:
        return True, f"budget exhausted ({budget} model calls)"
    if state.api_fails >= MAX_API_FAILS:
        return True, f"stuck: {state.api_fails} consecutive model API failures"
    if state.idle >= MAX_IDLE:
        return True, f"stuck: {state.idle} consecutive replies without a tool call"
    if state.repeat_hit:
        return True, f"stuck: the same action was issued {MAX_REPEATS} times"
    if state.no_improve >= PATIENCE:
        return True, f"stuck: no improvement on the best score in {PATIENCE} model calls"
    return False, ""

RUN_SEEDS: list[int] = []

def verify() -> dict:
    return checks.verify(seeds=RUN_SEEDS, n=150)

def confirm_done(state: State) -> bool:
    """A single passing check can be luck with the random seeds; re-verify wider."""
    if state.check is None or state.check["unmet"]:
        return False
    wide = checks.verify(n=400, seeds=[int.from_bytes(os.urandom(4), "big") % 2**31 for _ in range(3)])
    state.check = wide
    if wide["unmet"]:
        state.harness_msgs.append("Looked done, but a wider re-check on 3 new seeds found problems (see report).")
        return False
    return True

# ================================================================== tools
def _read_lib() -> str:
    return LIB.read_text(encoding="utf-8")

def t_write_rust(args, state):
    content = args.get("content")
    if not isinstance(content, str) or not content.strip():
        return "ERROR: `content` must be the complete, non-empty file."
    LIB.write_text(content, encoding="utf-8")
    return f"wrote {len(content.splitlines())} lines to rust/src/lib.rs"

def t_edit_rust(args, state):
    edits = args.get("edits")
    if not isinstance(edits, list) or not edits:
        return "ERROR: `edits` must be a non-empty list of {old, new}."
    src = _read_lib()
    for i, e in enumerate(edits):                      # atomic: all apply or none do
        old, new = (e or {}).get("old"), (e or {}).get("new")
        if not isinstance(old, str) or not isinstance(new, str) or not old:
            return f"ERROR in edit {i}: needs string `old` (non-empty) and `new`. Nothing applied."
        n = src.count(old)
        if n != 1:
            return (f"ERROR in edit {i}: `old` matched {n} times (must be exactly 1; copy it from the "
                    f"current file without line-number prefixes, add context to disambiguate). Nothing applied.")
        src = src.replace(old, new, 1)
    LIB.write_text(src, encoding="utf-8")
    return f"applied {len(edits)} edit(s)"

def t_restore_best(args, state):
    if not BEST.exists():
        return "no best snapshot yet"
    shutil.copyfile(BEST, LIB)
    return f"restored the step-{state.best_step} version"

def t_probe(args, state):
    cmds = args.get("commands")
    if not isinstance(cmds, list) or not cmds:
        return "ERROR: `commands` must be a non-empty list of harness commands."
    cmds = [str(c).strip() for c in cmds[:40] if str(c).strip()]
    built = bool(state.check and state.check["build"]["ok"])
    got, err = checks.run_commands(cmds) if built else (None, "current lib.rs does not build")
    rows = []
    for k, c in enumerate(cmds):
        want = checks._oracle(c)
        mine = got[k] if got else None
        verdict = "" if mine is None else ("  MATCH" if checks._agree(c, want, mine) else "  DIFF")
        rows.append(f"{c}\n    python: {checks._brief(want)}"
                    + ("" if mine is None else f"\n    rust:   {checks._brief(mine)}{verdict}"))
    return ("" if built else f"(rust side unavailable: {err})\n") + "\n".join(rows)

def t_read_python(args, state):
    name = args.get("file") or "version.py"
    p = REFDIR / pathlib.Path(str(name)).name
    if not p.exists() or p.suffix != ".py":
        return f"no such file: reference/{name}"
    lines = p.read_text(encoding="utf-8").splitlines()
    a = max(1, int(args.get("start_line") or 1))
    b = min(len(lines), int(args.get("end_line") or len(lines)))
    return f"reference/{p.name} lines {a}-{b} of {len(lines)}\n" + \
        "\n".join(f"{i:4d} | {lines[i - 1]}" for i in range(a, b + 1))

def t_note(args, state):
    return "noted"

def t_finish(args, state):
    return "(handled by the harness after verification)"

TOOLS = [
    dict(name="write_rust", fn=t_write_rust, kind="edit",
         description="Replace rust/src/lib.rs with `content`, which must be the COMPLETE file "
                     "(implementation + tests). Use for the first draft or large rewrites.",
         parameters={"type": "object", "required": ["content"],
                     "properties": {"content": {"type": "string"}}}),
    dict(name="edit_rust", fn=t_edit_rust, kind="edit",
         description="Apply several exact search/replace edits to rust/src/lib.rs atomically (all or "
                     "none). Each `old` must occur exactly once in the current file; copy it verbatim "
                     "WITHOUT the line-number prefixes shown to you. Prefer this for targeted fixes.",
         parameters={"type": "object", "required": ["edits"], "properties": {
             "edits": {"type": "array", "items": {"type": "object", "required": ["old", "new"],
                       "properties": {"old": {"type": "string"}, "new": {"type": "string"}}}}}}),
    dict(name="restore_best", fn=t_restore_best, kind="edit",
         description="Revert rust/src/lib.rs to the best-scoring version seen so far.",
         parameters={"type": "object", "properties": {}}),
    dict(name="probe", fn=t_probe, kind="probe",
         description="Run harness commands on BOTH the Python oracle and the current Rust build "
                     "(rebuilt after this reply's edits) and show them side by side. Commands: "
                     "'parse V', 'compare A B', 'bump major|minor|patch V', 'format V'. Max 40. "
                     "You see the output next call.",
         parameters={"type": "object", "required": ["commands"],
                     "properties": {"commands": {"type": "array", "items": {"type": "string"}}}}),
    dict(name="note", fn=t_note, kind="note",
         description="Record a short status note for your next call (you have no other memory): "
                     "what you changed, what is still wrong, what you will do next. Call it every reply.",
         parameters={"type": "object", "required": ["text"],
                     "properties": {"text": {"type": "string"}}}),
    dict(name="finish", fn=t_finish, kind="finish",
         description="Declare the translation complete. Refused if acceptance criteria are unmet, "
                     "unless you insist a second time (never accepted while the build fails or "
                     "rules are violated).",
         parameters={"type": "object", "required": ["summary"],
                     "properties": {"summary": {"type": "string"}}}),
]
READ_PYTHON = dict(name="read_python", fn=t_read_python, kind="read",
                   description="Read reference/<file> (version.py, _types.py, test_parsing.py, "
                               "test_compare.py, test_bump.py) with line numbers, optionally a range.",
                   parameters={"type": "object", "properties": {
                       "file": {"type": "string"}, "start_line": {"type": "integer"},
                       "end_line": {"type": "integer"}}})

def rustfmt_polish() -> str:
    """Mechanical, model-free polish: rustfmt lib.rs (never main.rs, which is frozen) and keep
    the result only if verification is at least as good as before."""
    before = LIB.read_bytes()
    pre = verify()
    try:
        p = subprocess.run(["rustfmt", "--edition", "2021", str(LIB)],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return "rustfmt not available - skipped"
    if p.returncode != 0 or LIB.read_bytes() == before:
        LIB.write_bytes(before)
        return "rustfmt: already formatted" if p.returncode == 0 else "rustfmt failed - left unformatted"
    post = verify()
    if post["score"] < pre["score"] or len(post["unmet"]) > len(pre["unmet"]):
        LIB.write_bytes(before)
        return "rustfmt output verified worse - reverted"
    return "rustfmt applied (re-verified: no regression)"

def schemas(tools):
    return [{k: t[k] for k in ("name", "description", "parameters")} for t in tools]

def _summarize_call(c) -> str:
    a = c.get("arguments") or {}
    if c["name"] == "write_rust":
        return f"write_rust({len(str(a.get('content', '')).splitlines())} lines)"
    if c["name"] == "edit_rust":
        return f"edit_rust({len(a.get('edits') or [])} edits)"
    if c["name"] == "probe":
        return f"probe({len(a.get('commands') or [])} cmds)"
    if c["name"] == "note":
        return "note"
    if c["name"] == "read_python":
        return f"read_python({a.get('file', 'version.py')} {a.get('start_line', '')}-{a.get('end_line', '')})"
    return c["name"]

# =================================================================== loop
def main():
    global USE_FALLBACK, RUN_SEEDS, MODEL, EFFORT, MAX_USD
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=40, help="max model calls (graded cap: 40)")
    ap.add_argument("--resume", action="store_true",
                    help="continue from the current lib.rs instead of translating from the stub")
    ap.add_argument("--fresh", action="store_true", help=argparse.SUPPRESS)   # old flag; now the default
    ap.add_argument("--lean", action="store_true", help="do not put the Python source in the prompt")
    ap.add_argument("--hints", action="store_true", help="add semver-specific pitfalls to the prompt")
    ap.add_argument("--model", help=f"model id (default {MODEL})")
    ap.add_argument("--effort", help=f"low|medium|high|xhigh|max (default {EFFORT})")
    ap.add_argument("--max-usd", type=float, help=f"stop once this run has spent this much (default {MAX_USD})")
    ap.add_argument("--task", default="Translate reference/version.py into rust/src/lib.rs.")
    a = ap.parse_args()
    MODEL, EFFORT = a.model or MODEL, a.effort or EFFORT
    MAX_USD = a.max_usd if a.max_usd is not None else MAX_USD
    if a.budget > 40:
        sys.exit("--budget above 40 is not allowed by the assignment")
    if not PYSRC.exists():
        sys.exit("reference/version.py missing - run fetch_source.py")
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or (pathlib.Path.home() / ".config" / "anthropic").exists()):
        sys.exit("No Anthropic API credentials. Put ANTHROPIC_API_KEY=sk-ant-... in a .env file next to "
                 "agent.py (or export it). lib.rs was not touched. To score the committed lib.rs "
                 "without a key, run: python evaluate.py")

    LOGS.mkdir(exist_ok=True); STATE.mkdir(exist_ok=True)
    backup = STATE / "lib_before_run.rs"
    backed_up = False
    if not a.resume:
        # Default: translate from scratch. Keep whatever was in lib.rs so nothing is lost.
        if LIB.exists() and LIB.read_bytes() != STUB.read_bytes():
            shutil.copyfile(LIB, backup)
            backed_up = True
            print("previous rust/src/lib.rs saved to .agent/lib_before_run.rs; starting from the stub")
        shutil.copyfile(STUB, LIB)
    if BEST.exists():
        BEST.unlink()
    log = LOGS / f"run-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    def rec(**kw):
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), **kw}) + "\n")

    tools = TOOLS + ([READ_PYTHON] if a.lean else [])
    by_name = {t["name"]: t for t in tools}
    sys_prompt = system_prompt(lean=a.lean, hints=a.hints)
    print(f"model={MODEL} effort={EFFORT} cost cap=${MAX_USD:.2f}")
    rec(event="start", budget=a.budget, task=a.task, model=MODEL, effort=EFFORT, max_usd=MAX_USD,
        lean=a.lean, hints=a.hints, system_prompt_chars=len(sys_prompt),
        system_prompt_sha=hashlib.sha1(sys_prompt.encode()).hexdigest()[:12])

    # Scoring uses the SAME two random seeds for the whole run so scores are comparable
    # step to step (fresh seeds each time made no-op edits look like regressions).
    # They are never the practice seed, and "done" is re-confirmed on brand-new seeds.
    RUN_SEEDS = [int.from_bytes(os.urandom(4), "big") % 2**31 or 1 for _ in range(2)]
    state = State()
    state.check = verify()
    rec(event="check", step=0, result=checks.one_line(state.check), check=state.check)
    print(f"[0] baseline: {checks.one_line(state.check)}")
    if a.resume and not state.check["unmet"]:
        print("lib.rs already meets every acceptance criterion - nothing to do (no model calls made).")
        rec(event="stop", reason="already done at start", steps=0)
        return

    def update_best(step):
        s = state.check["score"]
        if s > state.best_score:
            shutil.copyfile(LIB, BEST)
            state.best_score, state.best_step = s, step
            state.best_line = checks.one_line(state.check)
            state.regressed = False
            return True
        if s < state.best_score:
            if not state.regressed:
                state.regressed = True
                state.harness_msgs.append(
                    f"REGRESSION: score {s} is below the best {state.best_score} (step {state.best_step}). "
                    "Fix it this call or call restore_best; otherwise it is rolled back automatically.")
            else:
                shutil.copyfile(BEST, LIB)
                state.check = verify()
                state.regressed = False
                state.harness_msgs.append(
                    f"Still below the best after a second try: rolled back to the step-{state.best_step} "
                    "version. Try a different fix.")
                rec(event="rollback", step=step, to_step=state.best_step)
        return False

    update_best(0)
    step = 0
    try:
        while True:
            stop, why = should_stop(state, step, a.budget)
            if stop:
                break
            step += 1
            msgs = build_context(state, step, a.budget, sys_prompt)
            state.harness_msgs = []
            rec(event="context", step=step, user_chars=len(msgs[-1]["content"]))
            print(f"[{step}] calling {MODEL} ..." + (" (first call writes the whole file: expect a few minutes)"
                                                     if step == 1 else ""), flush=True)
            try:
                reply = call_model(msgs, schemas(tools))
                state.api_fails = 0
                state.replies += 1
            except Exception as e:                       # SDK already retried transient errors
                import anthropic
                if isinstance(e, anthropic.BadRequestError) and USE_FALLBACK and "fallback" in str(e).lower():
                    USE_FALLBACK = False                 # beta not offered for this model: drop it
                    print(f"[{step}] fallbacks rejected by the API; retrying without them")
                    rec(event="api_error", step=step, error=str(e), action="disable fallbacks, not counted")
                    step -= 1                            # rejected before any model ran
                    continue
                if isinstance(e, (anthropic.AuthenticationError, TypeError)):
                    state.fatal = "the API key was rejected - check ANTHROPIC_API_KEY in .env"
                elif isinstance(e, anthropic.PermissionDeniedError):
                    state.fatal = f"this key may not use {MODEL} (permission denied)"
                elif isinstance(e, anthropic.NotFoundError):
                    state.fatal = f"model {MODEL!r} not found - check --model / AGENT_MODEL"
                elif isinstance(e, anthropic.BadRequestError):
                    msg = getattr(e, "message", str(e))
                    state.fatal = ("out of API credits - top up at console.anthropic.com"
                                   if "credit" in msg.lower() else f"request rejected: {msg[:200]}")
                else:
                    state.api_fails += 1
                print(f"[{step}] model call failed: {type(e).__name__}: {e}")
                rec(event="api_error", step=step, error=f"{type(e).__name__}: {e}")
                state.log.append({"step": step, "actions": "model call failed", "result": "no change"})
                continue
            rec(event="model", step=step, reply=reply)
            u = reply.get("usage") or {}
            state.spent += cost_usd(MODEL, u)
            rec(event="cost", step=step, usd=round(state.spent, 4))
            print(f"[{step}] ${state.spent:.2f}  {reply.get('stop_reason')}  in={u.get('input')} cached={u.get('cache_read')} "
                  f"out={u.get('output')}  {(reply.get('text') or '').splitlines()[0][:150] if reply.get('text') else ''}")
            noted = [str((c.get("arguments") or {}).get("text", "")) for c in reply.get("tool_calls") or []
                     if c["name"] == "note"]
            if noted or reply.get("text"):
                state.notes.append((step, "\n".join(noted) or reply["text"]))

            calls = reply.get("tool_calls") or []
            if reply.get("stop_reason") == "max_tokens" and calls:
                state.harness_msgs.append("Your last reply hit max_tokens and its tool calls were discarded "
                                          "(inputs may be truncated). Use smaller edit_rust edits.")
                calls = []
            if not calls:
                # No tool call: either chatting, claiming to be done, or refused. It changes
                # nothing on disk, so it is at best a wasted call - nudge, then stop.
                state.idle += 1
                state.no_improve += 1
                state.harness_msgs.append("Your last reply contained no tool call and changed nothing. "
                                          "Every reply must act through tools (or call finish).")
                state.log.append({"step": step, "actions": "no tool call", "result": "no change"})
                state.last_outputs = []
                continue
            state.idle = 0

            sig = hashlib.sha1(json.dumps([[c["name"], c.get("arguments")] for c in calls],
                                          sort_keys=True, default=str).encode()).hexdigest()
            state.actions[sig] = state.actions.get(sig, 0) + 1
            if state.actions[sig] == MAX_REPEATS - 1:
                state.harness_msgs.append("You just repeated an earlier action exactly. It will not give a "
                                          "different result; change approach.")
            state.repeat_hit = state.actions[sig] >= MAX_REPEATS

            # edits first (in order), then verify once, then probes/reads, then finish
            before, outputs = checks.lib_hash(), []
            order = sorted(calls, key=lambda c: {"edit": 0, "read": 1, "probe": 2, "note": 2, "finish": 3}
                           .get(by_name.get(c["name"], {}).get("kind"), 1))
            for c in order:
                if by_name.get(c["name"], {}).get("kind") != "edit":
                    continue
                out = by_name[c["name"]]["fn"](c.get("arguments") or {}, state)
                outputs.append((c["name"], out))
                rec(event="tool", step=step, name=c["name"], output=out)

            changed = checks.lib_hash() != before
            if changed:
                state.check = verify()
                rec(event="check", step=step, result=checks.one_line(state.check), check=state.check)
                improved = update_best(step)
                if not state.check["unmet"] and confirm_done(state):
                    # a confirmed version is final: pin it as the snapshot we end on
                    state.done = True
                    shutil.copyfile(LIB, BEST)
                    state.best_score, state.best_step = state.check["score"], step
                    state.best_line = checks.one_line(state.check)
                    rec(event="check", step=step, result="confirm: " + checks.one_line(state.check),
                        check=state.check)
            else:
                improved = False
            state.no_improve = 0 if improved else state.no_improve + 1

            finish = None
            for c in order:
                t = by_name.get(c["name"])
                if t is None:
                    outputs.append((c["name"], f"unknown tool {c['name']!r}"))
                elif t["kind"] in ("probe", "read", "note"):
                    out = t["fn"](c.get("arguments") or {}, state)
                    outputs.append((c["name"], out))
                    rec(event="tool", step=step, name=c["name"], output=out[:8000])
                elif t["kind"] == "finish":
                    finish = c
            if finish is not None and not state.done:
                summary = str((finish.get("arguments") or {}).get("summary", ""))
                if state.check and not state.check["unmet"]:
                    state.finish_accepted, state.finish_summary = True, summary
                elif state.finish_refusals >= 1 and state.check and state.check["build"]["ok"]                         and not state.check["quality"]["violations"]:
                    state.finish_accepted = True
                    state.finish_summary = f"(accepted on 2nd request, criteria unmet) {summary}"
                else:
                    state.finish_refusals += 1
                    outputs.append(("finish", "REFUSED - acceptance criteria not met:\n- " +
                                    "\n- ".join(state.check["unmet"] if state.check else ["no check"])))
                rec(event="finish_request", step=step, accepted=state.finish_accepted, summary=summary)

            state.last_outputs = [(n, o) for n, o in outputs]
            result = checks.one_line(state.check) if changed else "no change to lib.rs"
            state.log.append({"step": step, "actions": ", ".join(_summarize_call(c) for c in calls),
                              "result": result})
            print(f"      -> {state.log[-1]['actions']}  =>  {result}")
    except KeyboardInterrupt:
        why = "interrupted by user"
    else:
        stop, why = should_stop(state, step, a.budget)

    if state.replies == 0:
        # Nothing was ever generated: put back what was there and skip the (pointless) report.
        if backed_up:
            shutil.copyfile(backup, LIB)
        print(f"\n[stop] {why}\nNo model call succeeded, so nothing was translated; "
              f"rust/src/lib.rs is {'restored to what it was' if backed_up else 'unchanged'}.")
        rec(event="stop", reason=why, steps=step, spent=state.spent)
        return

    # Always end on the best version seen, never on the last one.
    if BEST.exists() and not state.done and (state.check is None or state.check["score"] < state.best_score):
        shutil.copyfile(BEST, LIB)
        print(f"[end] restored best version from step {state.best_step}")
    polish = rustfmt_polish()
    print(f"[end] {polish}")
    rec(event="polish", result=polish)
    final = checks.verify(n=300)
    print(f"\n[stop] {why}   (model calls used: {step}, est. cost ${state.spent:.2f})")
    rec(event="stop", reason=why, steps=step, final=checks.one_line(final), check=final)
    print(checks.render(final))

    print(f"\ntrajectory: {log}")
    print("final score (practice seed, grader's own script):")
    subprocess.run([sys.executable, str(HERE / "evaluate.py"), "--json",
                    str(LOGS / (log.stem + "-eval.json"))], cwd=HERE)

if __name__ == "__main__":
    main()
