#!/usr/bin/env python3
"""Deterministic verification for the translation agent. No model calls.

After every change to rust/src/lib.rs the agent's harness calls `verify()`,
which runs, in order:

  1. cargo build   -> compiler diagnostics, de-duplicated and compressed
  2. cargo test    -> pass/fail counts and the failing tests' messages
  3. differential  -> the Rust harness vs the real `semver` package on random
                      non-practice seeds (the agent fixes one pair per run so
                      scores are comparable; "done" is re-confirmed on brand-new
                      seeds, so overfitting to seed 0 buys nothing), plus extra
                      cases that stress prerelease precedence
  4. quality       -> the grader's rule scan, plus FFI / process checks

and folds the result into one scalar `score` and a `done` verdict. The model
never has to spend a call on "please build it": verification is free.

    python checks.py            # verify the current lib.rs, print the report
"""
from __future__ import annotations
import hashlib, json, pathlib, random, re, subprocess, sys

HERE = pathlib.Path(__file__).parent
RUST = HERE / "rust"
LIB  = RUST / "src" / "lib.rs"

import evaluate as ev          # reuse the grader's generator, oracle and harness runner

MIN_TESTS = 15                 # acceptance: at least this many #[test]s, all passing

# ------------------------------------------------------------------ helpers
def _run(cmd, cwd=RUST, timeout=300):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return None, "", f"TIMEOUT after {timeout}s"

def lib_hash() -> str:
    return hashlib.sha1(LIB.read_bytes()).hexdigest()[:12] if LIB.exists() else "missing"

def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + f"\n... [{len(s) - n} more chars clipped]"

# -------------------------------------------------------------------- build
def build() -> dict:
    rc, out, err = _run(["cargo", "build", "--release", "--message-format=json"])
    errors, warnings, seen = [], [], set()
    for line in out.splitlines():
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        if m.get("reason") != "compiler-message":
            continue
        msg = m["message"]
        if msg["level"] not in ("error", "warning") or msg["message"].startswith("aborting due to"):
            continue
        if re.match(r"\d+ (warnings?|errors?) emitted", msg["message"]) or \
           "generated" in msg["message"] and "warning" in msg["message"]:
            continue
        span = next((s for s in msg.get("spans", []) if s.get("is_primary")), None)
        loc = f"{span['file_name']}:{span['line_start']}" if span else "?"
        key = (msg["level"], (msg.get("code") or {}).get("code"), msg["message"], loc)
        if key in seen:
            continue
        seen.add(key)
        item = {"loc": loc, "code": key[1], "msg": msg["message"],
                "rendered": (msg.get("rendered") or msg["message"]).rstrip()}
        (errors if msg["level"] == "error" else warnings).append(item)
    ok = rc == 0
    if not ok and not errors:     # e.g. manifest problems, linker errors, timeouts
        errors.append({"loc": "?", "code": None, "msg": "build failed",
                       "rendered": _clip((err or out).strip(), 2500)})
    return {"ok": ok, "errors": errors, "warnings": warnings}

# ------------------------------------------------------------------- clippy
def clippy() -> dict:
    """Rust's standard linter (default lint set) over lib.rs, tests included.

    Idiomatic Rust is part of the grade, and clippy is the community's definition
    of it. Skipped gracefully when clippy is not installed.
    """
    rc, out, err = _run(["cargo", "clippy", "--release", "--all-targets", "--message-format=json"], timeout=300)
    if rc is None or "no such command" in err or "not installed" in err:
        return {"ran": False, "warnings": []}
    warns, seen = [], set()
    for line in out.splitlines():
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        if m.get("reason") != "compiler-message":
            continue
        msg = m["message"]
        code = (msg.get("code") or {}).get("code") or ""
        span = next((sp for sp in msg.get("spans", []) if sp.get("is_primary")), None)
        if not code.startswith("clippy::") or not span or not span["file_name"].replace("\\", "/").endswith("src/lib.rs"):
            continue
        key = (code, span["line_start"])
        if key in seen:
            continue
        seen.add(key)
        warns.append({"loc": f"src/lib.rs:{span['line_start']}", "code": code, "msg": msg["message"],
                      "rendered": (msg.get("rendered") or msg["message"]).rstrip()})
    return {"ran": True, "warnings": warns}

# --------------------------------------------------------------------- test
def cargo_test() -> dict:
    # Same invocation as the grader; --message-format=short only compacts
    # compiler errors, the libtest output is unchanged.
    rc, out, err = _run(["cargo", "test", "--release", "--message-format=short"], timeout=240)
    if rc is None:
        return {"ran": False, "passed": 0, "failed": 0, "failures": [],
                "compile_errors": ["cargo test TIMED OUT - a test (or the code) loops forever"]}
    results = re.findall(r"test result: \w+\. (\d+) passed; (\d+) failed", out)
    if not results:
        errs = [l.strip() for l in (err + out).splitlines() if ": error" in l or l.startswith("error")]
        return {"ran": False, "passed": 0, "failed": 0, "failures": [],
                "compile_errors": errs[:12] or [_clip((err or out).strip(), 1500)]}
    passed, failed = map(int, results[0])          # the grader reads the first line: lib unit tests
    failures = []
    for m in re.finditer(r"---- (\S+) stdout ----\n(.*?)(?=\n---- |\nfailures:|\Z)", out, re.S):
        failures.append({"test": m.group(1), "output": _clip(m.group(2).strip(), 700)})
    doc_failed = sum(int(f) for _, f in results[1:])
    return {"ran": True, "passed": passed, "failed": failed, "failures": failures[:6],
            "other_failed": doc_failed, "compile_errors": []}

# ------------------------------------------------------------- differential
# Extra cases beyond the grader's generator. Its random compare pairs rarely
# share major.minor.patch, so prerelease precedence is barely exercised by
# chance; SAME_CORE pairs force it. Nothing here is taken from the grading seed.
# Every case must be representable by the frozen API (u64 fields): u64::MAX itself is
# excluded because bump_major of it is 2**64 in Python and unrepresentable in Rust.
EXTRA_VALID = [
    "1.0.0-0", "1.0.0-0.0", "1.0.0-alpha.0", "1.0.0-x.7.z.92", "1.0.0-x-y-z.--",
    "1.0.0-rc.1+build.1", "2.0.0+build.01", "1.2.3----RC-SNAPSHOT.12.9.1--.12+788",
    "1.0.0-alpha-a.b-c-somethinglong+build.1-aef.1-its-okay", "1.2.3-0a", "1.2.3-00a",
    "18446744073709551614.0.0", "1.0.0-99999999999999999999999", "0.0.4", "1.1.2+meta-valid",
]
EXTRA_INVALID = [
    "1.0.0-alpha+beta+gamma", "1.0.0+build..1", "01.1.1", "1.0.0-00", "1.0.0-alpha.01",
    "1.2", "1.2.3.DEV", "1.2-SNAPSHOT", "9.8.7+meta+meta", "1.0.0-", "1.0.0+", "=1.2.3",
    "1.2.3-.", "1.2.3+.", "1.2.3-a.", "1.2.3+a.", "1.2.3-a..b", "00.0.0", "1..0", "..",
    "1.2.3-rc.1+", "1.2.3-$", "1.2.3+!", "1.2.3a", "1.2.3-a b",
]

def _same_core_pairs(rng, n):
    idents = ["alpha", "beta", "rc", "0", "1", "2", "10", "11", "a", "b", "a1", "1a", "-", "--", "x-y"]
    def pre():
        return ".".join(rng.choice(idents) for _ in range(rng.randint(1, 3)))
    out = []
    for _ in range(n):
        core = f"{rng.randint(0, 3)}.{rng.randint(0, 3)}.{rng.randint(0, 3)}"
        a = core + (f"-{pre()}" if rng.random() < 0.85 else "")
        b = core + (f"-{pre()}" if rng.random() < 0.85 else "")
        if rng.random() < 0.3: a += "+" + rng.choice(["b1", "build.5", "0", "zzz"])
        if rng.random() < 0.3: b += "+" + rng.choice(["b2", "build.4", "1", "aaa"])
        out.append((a, b))
    return out

def _oracle(cmd: str):
    """What the reference implementation says for one harness command."""
    parts = cmd.split(" ")
    try:
        if parts[0] == "parse":
            v = ev.ref_parse(parts[1])
            return {"ok": False} if v is None else {"ok": True, **v}
        if parts[0] == "compare":
            if ev.ref_parse(parts[1]) is None or ev.ref_parse(parts[2]) is None:
                return {"ok": False}
            return {"ok": True, "cmp": ev.ref_compare(parts[1], parts[2])}
        if parts[0] == "bump":
            if ev.ref_parse(parts[2]) is None:
                return {"ok": False}
            return {"ok": True, "version": ev.ref_bump(parts[1], parts[2])}
        if parts[0] == "format":
            v = ev.ref_parse(parts[1])
            return {"ok": False} if v is None else {"ok": True, "version": str(ev.ref.Version.parse(parts[1]))}
    except Exception as e:                            # oracle itself raised
        return {"ok": False, "error": repr(e)}
    return {"ok": False, "error": "unknown command"}

def _agree(cmd: str, want: dict, got: dict) -> bool:
    kind = cmd.split(" ")[0]
    if not want.get("ok"):
        return not got.get("ok")
    if not got.get("ok"):
        return False
    if kind == "parse":
        return all(got.get(k) == want.get(k) for k in ("major", "minor", "patch", "prerelease", "build"))
    if kind == "compare":
        return got.get("cmp") == want.get("cmp")
    if kind == "bump":
        return got.get("version") == want.get("version")
    if kind == "format":       # grader checks equivalence of the round trip, not exact text
        return ev.ref_parse(got.get("version") or "") == ev.ref_parse(cmd.split(" ")[1])
    return False

def _brief(r: dict) -> str:
    if not r.get("ok"):
        return "ERR" + (f"({r['error']})" if r.get("error") else "")
    if "cmp" in r:     return f"cmp={r['cmp']}"
    if "version" in r: return repr(r["version"])
    return "{" + ", ".join(f"{k}={r.get(k)!r}" for k in ("major", "minor", "patch", "prerelease", "build")) + "}"

def run_commands(cmds: list[str]):
    """Run harness commands through the compiled Rust binary."""
    return ev.run_harness(cmds)

def differential(seeds: list[int], n: int) -> dict:
    fams: dict[str, list[str]] = {"parse valid": [], "parse invalid": [], "compare": [],
                                   "bump": [], "round-trip": []}
    for i, seed in enumerate(seeds):
        valid, invalid, pairs, bumps = ev.build_cases(seed, n)
        rng = random.Random(seed ^ 0x5EED)
        if i == 0:
            valid, invalid = valid + EXTRA_VALID, invalid + EXTRA_INVALID
        pairs = pairs + _same_core_pairs(rng, n)
        fams["parse valid"]   += [f"parse {v}" for v in valid]
        fams["parse invalid"] += [f"parse {v}" for v in invalid if " " not in v]
        fams["compare"]       += [f"compare {a} {b}" for a, b in pairs]
        fams["bump"]          += [f"bump {k} {v}" for k in ("major", "minor", "patch") for v in valid]
        fams["round-trip"]    += [f"format {v}" for v in valid]
    order = [(f, c) for f, cs in fams.items() for c in dict.fromkeys(cs)]   # de-dup, keep order
    got, err = run_commands([c for _, c in order])
    if err:
        return {"ok": False, "error": err, "pct": 0.0, "families": {}, "mismatches": []}
    per, mism = {}, {}
    for (fam, cmd), g in zip(order, got):
        want = _oracle(cmd)
        if fam.startswith("parse"):        # the mutator sometimes yields valid strings
            fam = "parse valid" if want.get("ok") else "parse invalid"
        good = _agree(cmd, want, g)
        ok_n, tot = per.get(fam, (0, 0))
        per[fam] = (ok_n + good, tot + 1)
        if not good:
            mism.setdefault(fam, []).append(f"{cmd}  ->  rust {_brief(g)}   python {_brief(want)}")
    # show a spread across families rather than 12 copies of the same bug
    shown = []
    for k in range(6):
        for fam in fams:
            if k < len(mism.get(fam, [])):
                shown.append(f"[{fam}] {mism[fam][k]}")
    tot_ok = sum(o for o, _ in per.values()); tot = sum(t for _, t in per.values())
    return {"ok": True, "pct": round(100 * tot_ok / max(1, tot), 2), "seeds": seeds,
            "families": {f: {"pass": o, "total": t} for f, (o, t) in per.items()},
            "mismatch_count": tot - tot_ok, "mismatches": shown[:16]}

# ------------------------------------------------------------------ quality
def quality() -> dict:
    src = LIB.read_text(encoding="utf-8") if LIB.exists() else ""
    nodoc = re.sub(r"//.*", "", src)                   # identical to the grader's scan
    body = nodoc.split("#[cfg(test)]")[0]              # non-test code only
    q = {
        "unsafe_blocks":  len(re.findall(r"\bunsafe\b", nodoc)),
        "clone_calls":    len(re.findall(r"\.clone\(\)", nodoc)),
        "to_owned_calls": len(re.findall(r"\.to_owned\(\)", nodoc)),
        "todo_macros":    len(re.findall(r"\b(todo!|unimplemented!)", nodoc)),
        "panic_macros":   len(re.findall(r"\bpanic!", nodoc)),
        "unwrap_calls":   len(re.findall(r"\.unwrap\(\)", nodoc)),
        "expect_in_lib":  len(re.findall(r"\.expect\(", body)),
        "unreachable":    len(re.findall(r"\bunreachable!", body)),
        "ffi_or_process": len(re.findall(r"std::process|Command::new|extern\s+\"|#\[link|std::ffi|libloading|pyo3", body)),
        "tests":          len(re.findall(r"#\[test\]", src)),
        "lines":          len([l for l in src.splitlines() if l.strip()]),
    }
    deps = re.search(r"\[dependencies\]\s*(.*?)(\n\[|\Z)", (RUST / "Cargo.toml").read_text(), re.S)
    q["extra_dependencies"] = len([l for l in (deps.group(1).splitlines() if deps else [])
                                   if l.strip() and not l.strip().startswith("#")])
    hard = {"unsafe_blocks", "todo_macros", "panic_macros", "extra_dependencies", "ffi_or_process"}
    q["violations"] = sorted(k for k in hard if q[k] > 0)
    return q

# -------------------------------------------------------------- aggregation
def score_of(r: dict) -> float:
    """One number to track progress and detect regressions.

    build is a gate; then differential correctness dominates, then tests,
    then quality. Rule violations cost more than any correctness gain can
    buy back - that ordering is the whole point of the assignment.
    """
    if not r["build"]["ok"]:
        return 0.0
    q, t = r["quality"], r["test"]
    s = 100.0
    s += 6.0 * r["diff"].get("pct", 0.0)                                   # up to 600
    total = t["passed"] + t["failed"]
    s += 100.0 * (t["passed"] / total if total else 0.0)                  # up to 100
    s += min(t["passed"], MIN_TESTS)                                       # up to 15
    s -= 400.0 * len(q["violations"])
    s -= 3.0 * q["unwrap_calls"] + 2.0 * q["expect_in_lib"] + 2.0 * q["unreachable"]
    s -= 1.0 * max(0, q["clone_calls"] + q["to_owned_calls"] - 3)
    s -= 0.5 * min(20, len(r["build"]["warnings"]))
    s -= 2.0 * len(r.get("clippy", {}).get("warnings", []))
    return round(s, 2)

def unmet(r: dict) -> list[str]:
    """Acceptance criteria that do not hold yet. Empty list == done."""
    out = []
    if not r["build"]["ok"]:
        return ["crate does not build"]
    q, t, d = r["quality"], r["test"], r["diff"]
    if q["violations"]:
        out.append(f"rule violations: {', '.join(q['violations'])}")
    if q["unwrap_calls"]:
        out.append(f"{q['unwrap_calls']} .unwrap() call(s) (tests included) - use ?, match, or assert_eq! on the Result")
    if q["expect_in_lib"] or q["unreachable"]:
        out.append("expect()/unreachable! in library code can panic - return Err or restructure")
    if not d.get("ok"):
        out.append(f"differential harness failed: {d.get('error')}")
    elif d["pct"] < 100.0:
        out.append(f"differential agreement {d['pct']}% (< 100%): {d['mismatch_count']} mismatching cases")
    cw = r.get("clippy", {}).get("warnings", [])
    if cw:
        out.append(f"{len(cw)} clippy warning(s) - idiomatic Rust is part of the grade; fix each one")
    if not t["ran"]:
        out.append("cargo test does not compile/run")
    else:
        if t["failed"] or t.get("other_failed"):
            out.append(f"{t['failed'] + t.get('other_failed', 0)} failing test(s)")
        if t["passed"] < MIN_TESTS:
            out.append(f"only {t['passed']} passing tests (need >= {MIN_TESTS}, ported from reference/test_*.py)")
    return out

def verify(seeds: list[int] | None = None, n: int = 150) -> dict:
    seeds = seeds or [random.randrange(1, 2**31) for _ in range(2)]
    r = {"hash": lib_hash(), "build": build()}
    if r["build"]["ok"]:
        r["test"] = cargo_test()
        r["diff"] = differential(seeds, n)
        r["clippy"] = clippy()
    else:
        r["test"] = {"ran": False, "passed": 0, "failed": 0, "failures": [], "compile_errors": []}
        r["diff"] = {"ok": False, "pct": 0.0, "error": "not built", "families": {}, "mismatches": []}
        r["clippy"] = {"ran": False, "warnings": []}
    r["quality"] = quality()
    r["score"] = score_of(r)
    r["unmet"] = unmet(r)
    return r

# ---------------------------------------------------------------- rendering
def render(r: dict, max_errors: int = 4) -> str:
    """Compress a verification result into what the model needs to act on."""
    L = []
    b = r["build"]
    if not b["ok"]:
        L.append(f"[build] FAIL - {len(b['errors'])} distinct error(s). Fix these first; nothing else ran.")
        for e in b["errors"][:max_errors]:
            L.append(_clip(e["rendered"], 1800))
        for e in b["errors"][max_errors:max_errors + 12]:
            L.append(f"  also: {e['loc']} {e['code'] or ''} {e['msg']}")
    else:
        L.append(f"[build] ok, {len(b['warnings'])} warning(s)" +
                 "".join(f"\n  warning: {w['loc']} {w['msg']}" for w in b["warnings"][:6]))
        t = r["test"]
        if not t["ran"]:
            L.append("[cargo test] DID NOT RUN:\n  " + "\n  ".join(t["compile_errors"][:12]))
        else:
            L.append(f"[cargo test] {t['passed']} passed, {t['failed']} failed"
                     + (f" (+{t['other_failed']} failing doc-tests)" if t.get("other_failed") else ""))
            for f in t["failures"]:
                L.append(f"  FAILED {f['test']}:\n    " + f["output"].replace("\n", "\n    "))
        d = r["diff"]
        if not d.get("ok"):
            L.append(f"[differential] ABORTED: {d.get('error')}")
        else:
            fam = "  ".join(f"{k} {v['pass']}/{v['total']}" for k, v in d["families"].items())
            L.append(f"[differential vs Python semver, random non-practice seeds] {d['pct']}%   {fam}")
            if d["mismatches"]:
                L.append("  mismatches (sample, spread across families):")
                L += [f"    {m}" for m in d["mismatches"]]
        c = r.get("clippy", {})
        if c.get("ran"):
            L.append(f"[clippy] {len(c['warnings'])} warning(s)")
            for w in c["warnings"][:4]:
                L.append("  " + _clip(w["rendered"], 900).replace("\n", "\n  "))
            for w in c["warnings"][4:12]:
                L.append(f"  also: {w['loc']} {w['code']} {w['msg']}")
        else:
            L.append("[clippy] not installed - skipped")
    q = r["quality"]
    L.append("[quality] " + ", ".join(f"{k}={q[k]}" for k in
             ("unsafe_blocks", "panic_macros", "todo_macros", "unwrap_calls", "expect_in_lib",
              "unreachable", "clone_calls", "to_owned_calls", "ffi_or_process", "extra_dependencies", "tests", "lines")))
    if q["violations"]:
        L.append(f"  RULE VIOLATIONS: {', '.join(q['violations'])}  <- these void the result")
    L.append(f"[score] {r['score']}")
    L.append("[acceptance] " + ("ALL CRITERIA MET" if not r["unmet"] else
                               "not met:\n  - " + "\n  - ".join(r["unmet"])))
    return "\n".join(L)

def one_line(r: dict) -> str:
    if not r["build"]["ok"]:
        return f"build FAIL ({len(r['build']['errors'])} errors) score 0"
    t, d = r["test"], r["diff"]
    return (f"build ok, diff {d.get('pct', 0)}%, tests {t['passed']}/{t['passed'] + t['failed']}"
            f"{'' if t['ran'] else ' (not run)'}, violations {len(r['quality']['violations'])}, score {r['score']}")

if __name__ == "__main__":
    res = verify()
    print(render(res))
    sys.exit(0 if not res["unmet"] else 1)
