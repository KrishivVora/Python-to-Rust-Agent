#!/usr/bin/env python3
"""Self-test for the agent harness. No API key, no network, no cost.

    python selftest.py            # ~2-3 minutes; prints PASS/FAIL per scenario

The real model is replaced by a scripted fake that returns canned tool calls,
so every control path of agent.py runs for real - cargo build/test, the
differential test against the Python `semver` package, stopping rules,
rollback, cost cap, error handling - without spending anything. Everything
happens in a temporary copy of the repo; your working tree is not touched.
"""
from __future__ import annotations
import json, os, pathlib, shutil, subprocess, sys, tempfile, time

HERE = pathlib.Path(__file__).resolve().parent
FAKE_KEY = "sk-ant-selftest-not-a-real-key"

# ---------------------------------------------------------------- fixtures
def _variants(stub: str) -> dict:
    compiles = (stub.replace('todo!("parse")', 'Err(String::from("not implemented"))')
                    .replace('todo!("to_string")', "String::new()")
                    .replace('todo!("compare")', "Ordering::Equal")
                    .replace('todo!("bump_major")', "Version { major: 0, minor: 0, patch: 0, prerelease: None, build: None }")
                    .replace('todo!("bump_minor")', "Version { major: 0, minor: 0, patch: 0, prerelease: None, build: None }")
                    .replace('todo!("bump_patch")', "Version { major: 0, minor: 0, patch: 0, prerelease: None, build: None }"))
    assert 'todo!("' not in compiles, "stub placeholders changed; update selftest fixtures"
    return {"stub": stub, "compiles": compiles,
            "broken": compiles.replace("Ordering::Equal", "Ordering::Equall"),
            "broken2": compiles.replace("String::new()", "String::neww()")}

def _call(name, **args):
    return {"name": name, "arguments": args}

def scripts(v: dict, good: str | None) -> dict:
    """scenario -> (initial lib.rs, extra argv, scripted replies, env tweaks)"""
    big = {"input": 2000, "cache_read": 23000, "output": 10000}      # $0.10+ on Sonnet 5
    s = {
        "translate": (v["stub"], [], [
            {"tool_calls": [_call("note", text="draft"), _call("write_rust", content=v["broken"])]},
            {"tool_calls": [_call("edit_rust", edits=[{"old": "Ordering::Equall", "new": "Ordering::Equal"}]),
                            _call("probe", commands=["parse 1.2.3", "compare 1.0.0-a 1.0.0"])]},
            {"tool_calls": [_call("write_rust", content=good)]} if good else {"tool_calls": []},
        ], {}),
        "rollback": (v["stub"], [], [
            {"tool_calls": [_call("write_rust", content=v["compiles"])]},
            {"tool_calls": [_call("write_rust", content=v["broken"])]},
            {"tool_calls": [_call("write_rust", content=v["broken2"])]},
            {"text": "chatting", "tool_calls": []},
            {"text": "chatting", "tool_calls": []},
        ], {}),
        "auth_error": (good or v["compiles"], [], [TypeError("Could not resolve authentication method")], {}),
        "resume_done": (good or v["compiles"], ["--resume"], [], {}),
        "cost_cap": (v["stub"], ["--max-usd", "0.05"], [
            {"tool_calls": [_call("write_rust", content=v["compiles"])], "usage": big},
        ], {}),
        "repeat": (v["stub"], [], [
            {"tool_calls": [_call("write_rust", content=v["compiles"])]},
        ] + [{"tool_calls": [_call("probe", commands=["parse 1.2.3"])]}] * 3, {}),
        "finish_twice": (v["stub"], [], [
            {"tool_calls": [_call("write_rust", content=v["compiles"])]},
            {"tool_calls": [_call("finish", summary="first")]},
            {"tool_calls": [_call("finish", summary="second")]},
        ], {}),
        "max_tokens": (v["stub"], [], [
            {"stop_reason": "max_tokens", "tool_calls": [_call("write_rust", content=v["compiles"][:200])]},
            {"text": "x", "tool_calls": []}, {"text": "x", "tool_calls": []},
        ], {}),
        "lean": (v["stub"], ["--lean"], [
            {"tool_calls": [_call("read_python", file="version.py", start_line=1, end_line=5),
                            _call("write_rust", content=v["compiles"])]},
            {"tool_calls": [_call("finish", summary="a")]}, {"tool_calls": [_call("finish", summary="b")]},
        ], {}),
        "no_key": (good or v["compiles"], [], [], {"ANTHROPIC_API_KEY": None}),
    }
    return s

# ------------------------------------------- child: run agent with a fake model
def child(name: str):
    sys.path.insert(0, os.getcwd())
    import agent
    stub = (pathlib.Path("templates") / "lib_stub.rs").read_text(encoding="utf-8")
    good = pathlib.Path(".selftest_good.rs")
    good = good.read_text(encoding="utf-8") if good.exists() else None
    _, argv, replies, _ = scripts(_variants(stub), good)[name]
    replies = list(replies)
    n = {"calls": 0}

    def fake_call_model(messages, tools):
        n["calls"] += 1
        assert messages[0]["role"] == "system" and messages[-1]["role"] == "user"
        if not replies:
            raise RuntimeError("selftest: fake model script exhausted")
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        r = json.loads(json.dumps(r))
        r.setdefault("stop_reason", "tool_use"); r.setdefault("text", None)
        r.setdefault("usage", {"input": 2000, "cache_read": 23000, "output": 1000})
        for i, c in enumerate(r["tool_calls"]):
            c.setdefault("id", f"toolu_{n['calls']}_{i}")
        return r

    agent.call_model = fake_call_model
    sys.argv = ["agent.py"] + argv
    try:
        agent.main()
    finally:
        print(f"SELFTEST_MODEL_CALLS={n['calls']}")

# ----------------------------------------------------------- parent: assertions
def _log_events(work: pathlib.Path) -> list[dict]:
    logs = sorted((work / "logs").glob("run-*.jsonl"))
    return [json.loads(l) for l in logs[-1].read_text(encoding="utf-8").splitlines()] if logs else []

def main():
    t0 = time.time()
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="py2rust-selftest-"))
    work = tmp / "repo"
    try:
        files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=HERE,
                               capture_output=True, text=True).stdout.split()
    except OSError:
        files = []
    if not files:                                       # not a git checkout: copy what matters
        files = [str(p.relative_to(HERE)) for p in HERE.rglob("*") if p.is_file()
                 and not any(x in p.parts for x in ("target", ".agent", "logs", ".git", "__pycache__"))
                 and p.name != ".env"]
    for f in files:
        if f.startswith("logs/") or f == ".env":
            continue
        dst = work / f
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(HERE / f, dst)
    shutil.copy2(__file__, work / "selftest.py")

    sys.path.insert(0, str(work))
    stub = (work / "templates" / "lib_stub.rs").read_text(encoding="utf-8")
    v = _variants(stub)
    lib = work / "rust" / "src" / "lib.rs"

    def verify_in_work():
        r = subprocess.run([sys.executable, "checks.py"], cwd=work, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        return r.returncode == 0, r.stdout

    # Is the repo's own translation a passing one? Then it doubles as the "good" answer.
    good = lib.read_text(encoding="utf-8")
    ok, _ = verify_in_work()
    if ok:
        (work / ".selftest_good.rs").write_text(good, encoding="utf-8")
    else:
        good = None
        print("note: rust/src/lib.rs does not pass every criterion, so the 'done' paths are skipped\n")

    results = []

    def run(name, check):
        init, _, _, env_tweaks = scripts(v, good)[name]
        lib.write_text(init, encoding="utf-8")
        shutil.rmtree(work / ".agent", ignore_errors=True)
        shutil.rmtree(work / "logs", ignore_errors=True)
        env = dict(os.environ, ANTHROPIC_API_KEY=FAKE_KEY, PYTHONIOENCODING="utf-8")
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        for k, val in env_tweaks.items():
            if val is None: env.pop(k, None)
            else: env[k] = val
        cmd = ([sys.executable, "agent.py"] if name == "no_key"
               else [sys.executable, "-c", f"import selftest; selftest.child({name!r})"])
        t = time.time()
        p = subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=900)
        out = p.stdout + p.stderr
        try:
            problems = check(out, p.returncode, _log_events(work), lib.read_text(encoding="utf-8"), init)
        except Exception as e:                                         # a check itself crashed
            problems = [f"check raised {type(e).__name__}: {e}"]
        results.append((name, not problems, problems, time.time() - t, out))
        mark = "PASS" if not problems else "FAIL"
        print(f"  {mark}  {name:<13} {time.time() - t:5.1f}s" + ("" if not problems else "  <- " + "; ".join(problems)))

    def need(cond, msg, probs):
        if not cond: probs.append(msg)

    def calls(out):
        return int(out.rsplit("SELFTEST_MODEL_CALLS=", 1)[1].split()[0]) if "SELFTEST_MODEL_CALLS=" in out else -1

    def c_translate(out, rc, ev, final, init):
        pr = []
        if good:
            need("[stop] done: every acceptance criterion holds" in out, "did not stop as done", pr)
            need(calls(out) == 3, f"expected 3 model calls, got {calls(out)}", pr)
            need(verify_in_work()[0], "final lib.rs does not pass", pr)
        need(any(e["event"] == "check" and "build FAIL" in e.get("result", "") for e in ev), "broken draft not caught", pr)
        need(any(e["event"] == "tool" and e.get("name") == "probe" and "python:" in e.get("output", "") for e in ev),
             "probe output missing", pr)
        need(any(e["event"] == "polish" for e in ev), "rustfmt polish step missing", pr)
        return pr

    def c_rollback(out, rc, ev, final, init):
        pr = []
        need(any(e["event"] == "rollback" for e in ev), "no rollback event", pr)
        need(final == v["compiles"] or "rustfmt applied" in out, "did not end on the best version", pr)
        need("replies without a tool call" in out, "idle stop not triggered", pr)
        return pr

    def c_auth(out, rc, ev, final, init):
        pr = []
        need("cannot continue: the API key was rejected" in out, "wrong stop reason", pr)
        need(final == init, "lib.rs not restored after a run with no successful call", pr)
        need("final score" not in out, "pointless final report printed", pr)
        return pr

    def c_resume(out, rc, ev, final, init):
        pr = []
        if good:
            need("already meets every acceptance criterion" in out, "did not exit early", pr)
            need(calls(out) == 0, f"made {calls(out)} model calls", pr)
        return pr

    def c_cost(out, rc, ev, final, init):
        return [] if "cost cap reached" in out and calls(out) == 1 else [f"cost cap not enforced (calls={calls(out)})"]

    def c_repeat(out, rc, ev, final, init):
        return [] if "same action was issued" in out else ["repeat detector did not fire"]

    def c_finish(out, rc, ev, final, init):
        fr = [e for e in ev if e["event"] == "finish_request"]
        pr = []
        need(len(fr) == 2 and not fr[0]["accepted"] and fr[1]["accepted"], "finish refuse-then-accept wrong", pr)
        need("accepted on 2nd request" in out, "stop reason missing", pr)
        return pr

    def c_maxtok(out, rc, ev, final, init):
        pr = []
        need(not any(e["event"] == "tool" and e.get("name") == "write_rust" for e in ev), "truncated write was applied", pr)
        return pr

    def c_lean(out, rc, ev, final, init):
        ok_ = any(e["event"] == "tool" and e.get("name") == "read_python"
                  and "reference/version.py lines 1-5" in e.get("output", "") for e in ev)
        return [] if ok_ else ["read_python tool did not work in --lean mode"]

    def c_nokey(out, rc, ev, final, init):
        pr = []
        need(rc == 1 and "No Anthropic API credentials" in out, "no-key path wrong", pr)
        need(final == init, "lib.rs modified without a key", pr)
        return pr

    print(f"selftest in {work}\n")
    for name, chk in [("no_key", c_nokey), ("auth_error", c_auth), ("resume_done", c_resume),
                      ("translate", c_translate), ("rollback", c_rollback), ("cost_cap", c_cost),
                      ("repeat", c_repeat), ("finish_twice", c_finish), ("max_tokens", c_maxtok),
                      ("lean", c_lean)]:
        run(name, chk)

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} scenarios passed in {time.time() - t0:.0f}s")
    for name, _, probs, _, out in failed:
        print(f"\n--- {name} output (tail) ---\n" + "\n".join(out.splitlines()[-25:]))
    if not failed:
        shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
