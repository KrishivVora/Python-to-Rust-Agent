# Run logs

Every `python agent.py` run writes `run-<timestamp>.jsonl`: one JSON object per event (`start`,
`context`, `model` with the full reply and thinking summary, `tool`, `check` with the full
verification result, `cost`, `polish`, `stop`) and, at the end, the grader's report as
`run-<timestamp>-eval.json`. `console-runN.txt` is what the terminal showed.

All runs used Claude Sonnet 5, effort `high`, starting from the stub. Run numbers match
[`REPORT.md`](../REPORT.md).

| Run | Trajectory | Console | Calls | Cost | Outcome |
|---|---|---|---|---|---|
| 0 | `run-20260923-124229.jsonl` | `console-run0.txt` | 2 (+1 interrupted) | $0.28 | stopped by hand to fix a bug in our checker (an unsatisfiable test case); the log has no `stop` event. Its result later passed every criterion |
| 1 | `run-20260923-135402.jsonl` | `console-run1.txt` | 2 | $0.31 | done: every acceptance criterion held |
| 2 | `run-20260923-163732.jsonl` | `console-run2.txt` | 1 | $0.28 | done after one call. **This is the committed `rust/src/lib.rs`** |
| 3 | `run-20260923-164058.jsonl` | `console-run3.txt` | 2 | $0.30 | done: every acceptance criterion held |
| 4 | `run-20260923-180009.jsonl` | (not captured; run by hand in a terminal) | 2 | $0.27 | done. Run from a fresh `git clone` of this repo with only an API key added, as a new user would |

Costs are estimates from the token counts in each `model` event and list prices.
