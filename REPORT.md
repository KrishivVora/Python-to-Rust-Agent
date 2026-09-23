# Report: Python → Rust translation agent

Model: Claude Sonnet 5 (`claude-sonnet-5`, effort `high`) · 5 runs from the stub (the last one by
hand, from a fresh clone of this repo) · total API spend ≈ $1.45

## 1. Final score, and where it lost points

The committed `rust/src/lib.rs` (run 2, [`logs/run-20260923-163732.jsonl`](logs/run-20260923-163732.jsonl)):
builds; `cargo test` 25/25; `evaluate.py` practice seed **100.0 %** (2425/2425) with the semver.org
precedence chain passing; 0 `unsafe`, `panic!`, `todo!`, `.unwrap()`, `.clone()`; 0 clippy warnings.

| Run | Calls | Cost | First draft (after call 1) | What the harness made it fix |
|---|---|---|---|---|
| 0 | 2 (+1 killed) | $0.28 | diff 99.96 %, 22/22 tests, **`panic!` in a test** | the `panic!` (rule violation) |
| 1 | 2 | $0.31 | diff 100 %, **20/21 tests** | a wrong test expectation |
| 2 | 1 | $0.28 | everything passing | nothing: done after one call |
| 3 | 2 | $0.30 | diff 100 %, **2 clippy warnings** | `map_or(true, …)` → `is_none_or(…)` |
| 4 | 2 | $0.27 | diff 100 %, 26/26 tests, **3 clippy warnings** | the 3 lints (clean clone, run by hand with only an API key) |

5/5 runs compiled and reached every acceptance criterion, each stopping on its own as "done"
except run 0, which we interrupted. All five final versions score 100 % on seed 0; runs 0–3 were
also checked on 20 random seeds each. The only points lost anywhere are on other seeds (99.8–99.9 %), and
they are the grader's, not ours: its generator sometimes labels invalid versions such as
`0.24.21-n.05.rf` "valid", so the check fails whatever the implementation does. For the committed
translation over 50 seeds (121 250 cases), that is 0.12 % of cases, with zero genuine mismatches. Accepting those inputs would win a few
round-trip points back and break the spec; the agent does not do it.

## 2. How much came from the agent, and how much from the scaffold?

**About 65 % agent, 35 % scaffold.** Every line of Rust is the model's, and its first draft was
99.96–100 % correct in all five runs: the translation skill itself is the model's. But only one of
five raw first drafts was submittable. The others had a rule violation that voids the result, a
failing test, or lint warnings. The scaffold turned 1/5 into 5/5 and stopped each run at 1–2 calls.
It did this through automatic build/test/diff/clippy verification after every edit, precise
failure reports, the rule "if Python agrees 100 % and a test fails, the test is wrong", and a
harness-verified definition of done. The prompt is also scaffold: it holds the whole Python source
as the spec and the grader's rules. We did not have budget to run the `--lean`/`--hints` ablations,
so its share is an estimate rather than a measurement. Context management (TODO 3) contributed
little *here* because no run went past two calls; it is exercised by `selftest.py` instead.

## 3. What did the agent do that we did not intend?

- **Wrote `panic!` inside a test** although the prompt says "no `panic!` anywhere - tests
  included". Rules get followed where the model thinks the grader looks.
- **Let a prior from a different library leak into a test.** Its `bump_major` correctly followed the
  Python source (`1.0.0-rc.1` → `2.0.0`), but its test expected `1.0.0`, which is how npm's semver
  behaves. The implementation came from the spec and the test from memory.
- **Found a bug in our scaffold before we did.** Our extra test cases asked for
  `bump_major("18446744073709551615.0.0")`, which is 2⁶⁴ in Python and unrepresentable in the fixed
  `u64` field. The agent's reasoning called it "an unavoidable artifact of using fixed-width
  integers" and declined to chase it. It was right: our acceptance criterion was unsatisfiable,
  and would have burned calls until the stuck detector fired. We removed that case.

## 4. Challenges and how we overcame them

- **Broken skeleton on Windows.** `evaluate.py` looked for `harness` without `.exe`, so the
  differential tests silently aborted, and it read `lib.rs` as cp1252. The given loop also had an
  inverted `if tool` check that crashed every tool call. We fixed all three first.
- **API constraints shaped the context design.** Newer Claude models reject edited histories that
  replay thinking blocks, so trimming a transcript was not safe. Each call is instead a fresh
  prompt rebuilt from state on disk: the current file, the latest verification, a one-line step
  log, and the agent's notes. It stays about 6–24k characters per step (plus a cached 23k-token
  prefix) at any step count.
- **Noisy scores.** Fresh random seeds on every check made no-op edits look like regressions.
  Scoring now uses fixed per-run seeds, and "done" is re-confirmed on three brand-new seeds.
- **A $5 budget.** We used Sonnet 5 with the static prefix cached, a free token count plus a
  sub-cent preflight call before the first real run, a hard $ cap per run, and a halt if any run
  cost more than expected. Development and all five runs cost about $1.45.
- **Trust.** `selftest.py` replays scripted model replies through the real harness, covering
  rollback, stopping rules, the cost cap and error paths, so anyone can check the machinery with no
  API key.
