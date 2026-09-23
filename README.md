# Python → Rust translation agent

**CS 6158 — Software Engineering in the Era of ML/AI.** An LLM agent that translates
[`reference/version.py`](reference/version.py) (python-semver, 831 lines) into a std-only Rust
library, [`rust/src/lib.rs`](rust/src/lib.rs), and verifies itself against the real Python package
while it works. The committed `lib.rs` is the agent's own output.

<!-- RESULTS:START -->
| | Committed translation (run 2) | Across all 4 runs |
|---|---|---|
| Compiles | yes | 4 / 4 |
| `cargo test` | 25 / 25 passing | 21–25 tests, all passing, in every run |
| Differential vs Python `semver` (`evaluate.py`, practice seed 0) | **100.0 %** (2425 / 2425) | 100.0 % in every run |
| Random seeds | 0 genuine mismatches over 50 seeds, 121 250 cases ([grader quirk](#known-grader-quirk)) | 0 genuine mismatches over 20 seeds, every run |
| `unsafe` / `panic!` / `todo!` / `.unwrap()` / `.clone()` | 0 / 0 / 0 / 0 / 0 | 0 in every run |
| `cargo clippy` warnings | 0 | 0 at the end of every run |
| Model calls / cost | 1 call, $0.28 | 1–2 calls, $0.28–0.31 per run (Claude Sonnet 5) |
<!-- RESULTS:END -->

The original assignment brief is kept verbatim at the [end of this file](#appendix-the-original-assignment-brief).

---

## Quick start

### Prerequisites

- **Python 3.10+** (the Anthropic SDK requires it). On macOS/Linux you may need `python3` / `pip3`.
- **Rust** with `cargo`: <https://rustup.rs>. Recommended components:
  `rustup component add clippy rustfmt` (the agent uses them for quality checks; it still runs without them).
- **An Anthropic API key** with a few dollars of credit, from <https://console.anthropic.com>
  (API Keys; add credit under Billing). A Claude.ai subscription does *not* include API access.
- Internet access (for the model API only; all checks run locally).

### 1. Install

```sh
git clone https://github.com/KrishivVora/Python-to-Rust-Agent.git
cd Python-to-Rust-Agent
pip install -r requirements.txt          # semver (the reference oracle) + anthropic (the SDK)
```

### 2. Provide your API key

Either put it in a `.env` file next to `agent.py` (git ignores `.env`):

```sh
cp .env.example .env                      # Windows: copy .env.example .env
# edit .env so it reads:  ANTHROPIC_API_KEY=sk-ant-...
```

or export it in your shell:

```sh
export ANTHROPIC_API_KEY=sk-ant-...       # macOS / Linux
$env:ANTHROPIC_API_KEY = "sk-ant-..."     # Windows PowerShell
```

### 3. (Optional, free) check the harness before spending anything

```sh
python selftest.py        # ~1-2 min, no API key or network needed; expect "10/10 scenarios passed"
python evaluate.py        # the grader's own report on the committed translation, no key needed
```

### 4. Run the agent

```sh
python agent.py
```

This starts from the unimplemented stub (the current `lib.rs` is first saved to
`.agent/lib_before_run.rs`) and translates from scratch. In our runs this took
about 3–4 minutes, 1–2 model calls and $0.28–0.31 on the default model. It can never exceed 40
model calls or $1.50 (see [Options](#options)).

### What a run looks like

<!-- SAMPLE:START -->
Verbatim from run 3 ([`logs/console-run3.txt`](logs/console-run3.txt)), which needed one fix:

```text
previous rust/src/lib.rs saved to .agent/lib_before_run.rs; starting from the stub
model=claude-sonnet-5 effort=high cost cap=$1.00
[0] baseline: build ok, diff 6.99%, tests 1/1, violations 1, score -157.06
[1] calling claude-sonnet-5 ... (first call writes the whole file: expect a few minutes)
[1] $0.27  tool_use  in=3157 cached=23180 out=26129
      -> write_rust(565 lines)  =>  build ok, diff 100.0%, tests 23/23, violations 0, score 811.0
[2] calling claude-sonnet-5 ...
[2] $0.30  tool_use  in=11192 cached=23180 out=259  Fixing the two clippy warnings by replacing `map_or(true, ...)` with `is_none_or(...)`.
      -> edit_rust(1 edits)  =>  build ok, diff 100.0%, tests 23/23, violations 0, score 815.0
[end] rustfmt applied (re-verified: no regression)
[stop] done: every acceptance criterion holds, re-confirmed on fresh seeds   (model calls used: 2, est. cost $0.30)
... full verification report, then the grader's evaluate.py report ...
```

(The first draft was correct but had two `clippy::unnecessary_map_or` warnings; the second
call fixed them. Run 2, the committed one, was done after its first call.)
<!-- SAMPLE:END -->

Each `[n]` line is one model call with the running cost (`in` = uncached input tokens,
`cached` = tokens served from the prompt cache, `out` = output incl. thinking). Each `->` line
is what the model did and the result of the automatic verification that followed: build,
`cargo test`, differential agreement with Python (`diff`), rule violations, and a composite
score used to detect progress and regressions.

The run **stops by itself** when every acceptance criterion holds (re-confirmed on three fresh
random seeds), or when it is stuck (no improvement in 4 calls, the same action three times,
two replies without acting), or at the call/cost cap. Whatever the reason, it ends on the best
version it produced, never a worse later one.

### What you get

| Where | What |
|---|---|
| `rust/src/lib.rs` | the translation (best version of the run, rustfmt-formatted) |
| `logs/run-<timestamp>.jsonl` | full trajectory: every prompt size, model reply (incl. thinking summary), tool call, verification result, cost, and the stop reason |
| `logs/run-<timestamp>-eval.json` | the grader's (`evaluate.py`) machine-readable report on the final result |
| `.agent/` | scratch state: best-version snapshot, the pre-run `lib.rs` backup (git-ignored) |

To get the committed translation back after a run: `git checkout rust/src/lib.rs`
(or copy `.agent/lib_before_run.rs` over it).

---

## Options

| Flag | Env var (or `.env` line) | Default | Meaning |
|---|---|---|---|
| `--model ID` | `AGENT_MODEL` | `claude-sonnet-5` | any Claude model, e.g. `claude-opus-5-5` |
| `--effort LEVEL` | `AGENT_EFFORT` | `high` | `low` / `medium` / `high` / `xhigh` / `max` thinking effort |
| `--max-usd N` | `AGENT_MAX_USD` | `1.50` | stop before spending more than this (checked before each call, so it can overshoot by one call) |
| `--budget N` | | `40` | max model calls (the assignment caps this at 40) |
| `--resume` | | off | continue from the current `lib.rs` instead of the stub; exits immediately, with no model calls, if it already passes |
| `--hints` | | off | ablation: add semver-specific pitfalls to the prompt |
| `--lean` | | off | ablation: leave the Python source out of the prompt; the agent must read it with a tool |
| | `AGENT_NO_FALLBACK=1` | fallback on | disable server-side refusal fallback to another model |

Costs are estimated from the token counts the API returns and the list prices in
`agent.py` (`PRICES`); your console's billing page is authoritative.

---

## Checking results without an API key

| Command | What it does |
|---|---|
| `python evaluate.py` | the course grader: build, `cargo test`, ~2 400 differential cases on the practice seed, quality scan. `--seed N` for other seeds |
| `python checks.py` | the agent's own verification: grader cases on random non-practice seeds plus extra edge cases, clippy, and the acceptance verdict |
| `python selftest.py` | replays scripted model replies through the real harness (no key, no network): 10 scenarios covering translation, rollback, stopping rules, cost cap, and error handling |
| `cd rust && cargo test --release` | just the ported unit tests |

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `No Anthropic API credentials...` | no key found: create `.env` (step 2) or export `ANTHROPIC_API_KEY`. Nothing was changed. |
| `cannot continue: the API key was rejected` | the key is wrong or revoked; `lib.rs` was restored to what it was |
| `cannot continue: out of API credits` | add credit at console.anthropic.com → Billing |
| `model '...' not found` | typo in `--model` / `AGENT_MODEL`, or your key cannot use that model |
| `cost cap reached` | raise it with `--max-usd`; the best version so far is kept |
| `pip` fails installing `anthropic` | Python older than 3.10; upgrade Python |
| `cargo: command not found` | install Rust from https://rustup.rs and open a new terminal |
| `[clippy] not installed - skipped` | optional: `rustup component add clippy` |
| `evaluate.py` says `binary not found` on Windows | fixed in this repo (`harness.exe`); pull the latest version |
| the first `[1] calling ...` line sits for minutes | normal: the first call writes the whole ~500-line file with tests |

---

## How the agent works

One model call per step; everything else is deterministic Python. Full design notes are in the
docstring at the top of [`agent.py`](agent.py) and in [`REPORT.md`](REPORT.md).

- **Model** (`call_model`): Claude via the Anthropic SDK with streaming, adaptive thinking and
  prompt caching of the static prefix (tools + system prompt, ≈23k tokens, cached after the first call).
- **Prompt** (`system_prompt`): the whole Python source and its tests verbatim (the spec), the
  frozen Rust signatures, the grader's rules, general Python→Rust semantic gaps (unbounded ints,
  regex flags, truthiness…) and how the harness works. No semver answers are baked in by default.
- **Context** (`build_context`): no chat transcript. Every call is a fresh prompt rebuilt from
  state: the current `lib.rs`, the latest verification report, a one-line log of earlier steps,
  last step's tool outputs, and the agent's own notes. The prompt stays the same size at step 2
  or step 40.
- **Tools**: `write_rust` (whole file), `edit_rust` (batched exact replacements, all-or-nothing),
  `probe` (run any input through Python and the current Rust build side by side), `note`
  (memory for the next call), `restore_best`, `finish`; `read_python` in `--lean` mode.
- **Verification** ([`checks.py`](checks.py)), automatic after every edit and free: `cargo build`,
  `cargo test`, differential testing against the Python `semver` package, `cargo clippy`, and a
  rule scan (`unsafe`, `panic!`, `todo!`, `.unwrap()`, FFI/process calls, dependencies).
- **Stopping** (`should_stop`): done only when the harness verifies it. Stuck detection,
  regression rollback, refusal of premature `finish`, call and cost caps. Always ends on the
  best version, then formats it with `rustfmt` (kept only if re-verification shows no regression).

### Acceptance criteria (what "done" means)

1. `cargo build --release` succeeds.
2. No rule violations (`unsafe`, `panic!`, `todo!`/`unimplemented!`, dependencies, FFI/process calls) and zero `.unwrap()`.
3. 100 % agreement with the Python `semver` package on parse (accept and reject), compare, bump and round-trip.
4. `cargo test` passes with at least 15 tests ported from `reference/test_*.py`.
5. Zero `cargo clippy` warnings (default lints).

---

## Known grader quirk

`evaluate.py`'s random generator occasionally produces a "valid" version that semver itself
rejects, e.g. `0.24.21-n.05.rf` (a numeric identifier with a leading zero). The grader then
expects it to parse, or crashes comparing it, so no correct implementation can pass those cases:
on some seeds the maximum achievable score is 99.8–99.9 %. Across 50 seeds this affected about
0.1–0.15 % of cases (it depends on which seeds are drawn), and there were **zero** mismatches
on any other case. Accepting such inputs to win
those points back would violate semver; the agent does not do that.

---

## Repository layout

| Path | |
|---|---|
| `agent.py` | the agent: model call, prompt, context, stopping, tools, main loop |
| `checks.py` | deterministic verification used after every edit |
| `selftest.py` | offline self-test of the harness with a scripted fake model |
| `evaluate.py` | the course grader (two Windows fixes: `harness.exe`, UTF-8 read) |
| `reference/` | the Python source and its tests (python-semver, BSD-3-Clause) |
| `rust/src/lib.rs` | the translation (written by the agent) |
| `rust/src/main.rs` | the grader's harness protocol (given; unmodified) |
| `templates/lib_stub.rs` | the original stub each run starts from |
| `logs/` | run trajectories |
| `REPORT.md` | write-up: results, what came from the agent vs the scaffold, surprises |

---

## Appendix: the original assignment brief

> Verbatim from the course repository; only the heading levels were changed.

### Translating Python to Rust with an agent

**CS 6158 — Software Engineering in the Era of ML/AI**

You will build an agent that translates a real Python module into Rust, and
you will be graded by differential testing against the original.

The module is `version.py` from
[python-semver](https://github.com/python-semver/python-semver) (BSD-3-Clause)
— 831 lines, no dependencies, pure functions. The semantics look simple and
are not: version precedence has enough edge cases that a first-pass
translation reliably gets several of them wrong.

---

#### Setup

```sh
pip install -r requirements.txt
python fetch_source.py          # downloads the Python source into reference/
cd rust && cargo build --release && cd ..
python evaluate.py              # should report ~8% — the stub is unimplemented
```

If `cargo` is missing: https://rustup.rs

---

#### What you are building

`rust/src/lib.rs` must expose exactly these, and your agent must write them:

```rust
pub struct Version { major, minor, patch, prerelease, build }

pub fn parse(s: &str)      -> Result<Version, String>
pub fn to_string(v: &Version) -> String
pub fn compare(a: &Version, b: &Version) -> std::cmp::Ordering
pub fn bump_major(v: &Version) -> Version
pub fn bump_minor(v: &Version) -> Version
pub fn bump_patch(v: &Version) -> Version
```

`rust/src/main.rs` is **given and must not be edited** — it is the protocol
`evaluate.py` speaks. You may add anything you like to `lib.rs` beyond the
signatures above.

##### Rules

| Rule | Why |
|---|---|
| no `unsafe` | you migrate to Rust *for* memory safety |
| no extra dependencies — std only | otherwise you are grading a crate someone else wrote |
| no calling back into Python | yes, someone tries this every year |
| no `todo!()` / `unimplemented!()` / `panic!` | these compile; they are not translations |
| **max 40 model calls** | the point is a good loop, not a big budget |

Violations are reported by `evaluate.py` and are not negotiable after the fact.

---

#### How you are graded

`evaluate.py` measures five things:

1. **Does it build.** A gate — nothing else runs if it fails.
2. **`cargo test`** — the tests your agent wrote. Port cases from
   `reference/test_*.py`.
3. **Differential testing** — your Rust against the real `semver` package, on
   several thousand generated cases: valid parses, invalid rejections,
   comparisons, bumps, and round-trips.
4. **The semver.org precedence chain**, reported separately because it is the
   single best diagnostic:
   `1.0.0-alpha < 1.0.0-alpha.1 < 1.0.0-alpha.beta < 1.0.0-beta < 1.0.0-beta.2
   < 1.0.0-beta.11 < 1.0.0-rc.1 < 1.0.0`
5. **Quality** — `unsafe`, `.clone()`, `.unwrap()`, `todo!`, dependencies.

`python evaluate.py` runs the **practice** seed (0). Grading uses a different
seed you do not have. Tuning to seed 0 will not help you.

#### Submission

Submit your compile and test pass rates to this excel sheet [leaderboard](https://docs.google.com/spreadsheets/d/1yZACTe5F9g39eSasnhkhc8vqFa-3t_42gcSU2hMJD7k/edit?usp=sharing).

##### Why correctness is not the whole score

Your agent can reach a high differential score and still have failed the task:

- `.clone()` on everything to escape lifetimes — compiles, passes, and throws
  away the performance you migrated for
- `unsafe` to silence the borrow checker — compiles, passes, and throws away
  the *memory safety* you migrated for
- `todo!()` on the hard function — compiles, and the harness will report the
  panic as a failed case

This is the whole lesson. **Your reward signal is a test suite, and a test
suite is an incomplete specification of what you actually wanted.** An
optimiser pointed at an incomplete specification will find the gap. You are
building the optimiser, so you will watch it happen.

A submission at 85% with clean Rust scores above one at 95% with twelve
`unsafe` blocks.

---

#### What to fill in

`agent.py` runs as given and accomplishes nothing. Five `TODO`s:

| TODO | What | Note |
|---|---|---|
| 1 | `call_model()` | any provider; keep the return shape |
| 2 | `system_prompt()` | how much spec do you bake in vs. make it read the source? |
| 3 | `build_context()` | **the hard one** |
| 4 | `should_stop()` | **the one everyone forgets** |
| 5 | the tool set | coarser/finer actions change results more than you expect |

Feel free to make any other changes you think are necessary to the code.

##### On TODO 3

The naive version — send the whole history — fills the window around step 15
once compiler errors accumulate, and the agent starts repeating work. You may
**not** solve this by raising the context limit. Use write / select /
compress / isolate.

##### On TODO 4

An agent that runs until its budget runs out has not terminated, it has been
stopped. Decide what "done" means, what "stuck" looks like, and what happens
when the score goes *down*.

---

#### What to hand in

```
rust/src/lib.rs        your translation
agent.py               your completed agent
logs/run-*.jsonl       at least one full trajectory
REPORT.md              one page, see below
```

**REPORT.md** — one page, answering:

1. Your final score, and where it lost points.
2. **How much came from the agent, and how much from the scaffold you wrote
   around it?** Guess a split and justify it.
3. What did your agent do that you did not intend?
4. What are challenges you faced and how did you overcome them?



---

#### Hints, in the order you will need them

- Read `reference/version.py` before writing any prompt. You cannot specify a
  translation you do not understand.
- Do **not** guess semantics — `bump_patch` on a prerelease, leading zeros in
  build vs. prerelease metadata. Check the oracle: `python -c "import semver; ..."`.
- Build metadata is **ignored entirely** for precedence. Almost every first
  draft gets this wrong.
- Numeric prerelease identifiers rank **below** non-numeric ones. So does a
  version with a prerelease rank below the same version without one.
- When the borrow checker fights you, the idiomatic fix is usually to own the
  string (`String`) rather than to clone a borrow. Both compile. Only one is
  what a Rust programmer would write.
