#!/usr/bin/env python3
"""Evaluate a Rust translation of reference/version.py.

    python evaluate.py                  # practice seed, human-readable report
    python evaluate.py --seed 9999      # the grading seed is NOT 0
    python evaluate.py --json out.json  # machine-readable, for aggregation

Five things are measured, and only the first is a gate:

  1. does it build
  2. cargo test           — the tests your agent wrote
  3. differential testing  — your Rust vs the real `semver` package
  4. round-trip            — parse(to_string(v)) == v
  5. quality               — unsafe / clone / todo! / deps

A high differential score with a bad quality score is not a good result.
Read the README section "Why correctness is not the whole score".
"""
from __future__ import annotations
import argparse, json, random, re, string, subprocess, sys, pathlib

HERE = pathlib.Path(__file__).parent
RUST = HERE / "rust"
LIB  = RUST / "src" / "lib.rs"
BIN  = RUST / "target" / "release" / ("harness.exe" if sys.platform == "win32" else "harness")

try:
    import semver as ref
except ImportError:
    sys.exit("Missing reference oracle. Run:  pip install -r requirements.txt")

# ----------------------------------------------------------------- case gen
IDENT = string.ascii_lowercase + string.digits + "-"

def rand_ident(rng):
    kind = rng.random()
    if kind < 0.35:                                  # numeric identifier
        return str(rng.randint(0, 250))
    return "".join(rng.choice(IDENT) for _ in range(rng.randint(1, 6))).strip("-") or "x"

def rand_version(rng):
    v = f"{rng.randint(0,30)}.{rng.randint(0,30)}.{rng.randint(0,30)}"
    if rng.random() < 0.55:
        v += "-" + ".".join(rand_ident(rng) for _ in range(rng.randint(1, 3)))
    if rng.random() < 0.30:
        v += "+" + ".".join(rand_ident(rng) for _ in range(rng.randint(1, 2)))
    return v

# The precedence chain from semver.org section 11. If a translation gets
# this wrong it has misunderstood prerelease ordering, which is the single
# most common failure.
SPEC_CHAIN = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta",
              "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0"]

CURATED_VALID = SPEC_CHAIN + [
    "0.0.0", "1.2.3", "10.20.30", "1.0.0+build", "1.0.0-a+b",
    "1.0.0-0A.is.legal", "1.0.0-alpha0.valid", "1.0.0--hyphen",
    "99999999999999999999.0.0" if False else "4294967295.0.0",
    "1.0.0+21AF26D3--117B344092BD",
]

# NOTE: no whitespace cases here. The harness protocol is space-delimited,
# so a version containing a space cannot reach the Rust side intact. Testing
# it would penalise students for something they cannot control.
CURATED_INVALID = [
    "1", "1.0", "1.0.0-", "1.0.0+", "01.0.0", "1.01.0", "1.0.01",
    "-1.0.0", "1.0.0-alpha..1", "1.0.0-01", "1.0.0.", ".1.0.0",
    "v1.0.0", "1.0.0-alpha_beta", "a.b.c", "1.2.3.4", "1.2.3-",
    "1.2.3-+b", "+1.0.0", "1.-1.0", "1.0.0-alpha@1",
]

def build_cases(seed, n_random):
    rng = random.Random(seed)
    valid   = CURATED_VALID + [rand_version(rng) for _ in range(n_random)]
    invalid = list(CURATED_INVALID)
    for _ in range(n_random // 2):                   # mutate valid -> likely invalid
        s = rand_version(rng)
        i = rng.randrange(len(s))
        invalid.append(s[:i] + rng.choice("..++__@@!") + s[i:])   # no spaces
    pairs = [(SPEC_CHAIN[i], SPEC_CHAIN[j])
             for i in range(len(SPEC_CHAIN)) for j in range(len(SPEC_CHAIN))]
    pool = CURATED_VALID + [rand_version(rng) for _ in range(n_random)]
    pairs += [(rng.choice(pool), rng.choice(pool)) for _ in range(n_random * 2)]
    bumps = [(k, v) for v in valid for k in ("major", "minor", "patch")]
    return valid, invalid, pairs, bumps

# ------------------------------------------------------------------ oracle
def ref_parse(s):
    try:
        v = ref.Version.parse(s)
    except (ValueError, TypeError):
        return None
    return dict(major=v.major, minor=v.minor, patch=v.patch,
                prerelease=v.prerelease, build=v.build)

def ref_compare(a, b):
    return ref.Version.parse(a).compare(ref.Version.parse(b))

def ref_bump(kind, s):
    return str(getattr(ref.Version.parse(s), f"bump_{kind}")())

# ------------------------------------------------------------- run harness
def run_harness(commands, timeout=120):
    if not BIN.exists():
        return None, f"binary not found at {BIN}"
    try:
        p = subprocess.run([str(BIN)], input="\n".join(commands) + "\n",
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"harness exceeded {timeout}s (infinite loop?)"
    lines = [l for l in p.stdout.splitlines() if l.strip()]
    if len(lines) != len(commands):
        return None, (f"harness returned {len(lines)} lines for "
                      f"{len(commands)} commands (crashed part-way?)")
    out = []
    for l in lines:
        try:
            out.append(json.loads(l))
        except json.JSONDecodeError:
            out.append({"ok": False, "error": "unparseable harness output"})
    return out, None

# -------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=0,
                    help="0 is the practice seed; grading uses a different one")
    ap.add_argument("--n", type=int, default=300, help="random cases per family")
    ap.add_argument("--json", help="also write a machine-readable report here")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    R = {"seed": a.seed}
    say = (lambda *x: None) if a.quiet else print

    say("\n" + "=" * 68)
    say(f"  py2rust evaluation   seed={a.seed}   n={a.n}")
    say("=" * 68)

    # 1 -------------------------------------------------------------- build
    b = subprocess.run(["cargo", "build", "--release"], cwd=RUST,
                       capture_output=True, text=True)
    R["build"] = b.returncode == 0
    say(f"\n[1] cargo build            {'PASS' if R['build'] else 'FAIL'}")
    if not R["build"]:
        say("\n" + "\n".join(b.stderr.splitlines()[-25:]))
        say("\nNothing else can run until it builds.")
        if a.json: pathlib.Path(a.json).write_text(json.dumps(R, indent=2))
        return 1

    # 2 --------------------------------------------------------- cargo test
    t = subprocess.run(["cargo", "test", "--release"], cwd=RUST,
                       capture_output=True, text=True)
    m = re.search(r"(\d+) passed; (\d+) failed", t.stdout)
    R["cargo_test"] = {"passed": int(m.group(1)), "failed": int(m.group(2))} if m else None
    if R["cargo_test"]:
        say(f"[2] cargo test             {R['cargo_test']['passed']} passed, "
            f"{R['cargo_test']['failed']} failed")
    else:
        say("[2] cargo test             could not parse result")

    # 3 ------------------------------------------------- differential tests
    valid, invalid, pairs, bumps = build_cases(a.seed, a.n)
    cmds  = [f"parse {v}" for v in valid]
    cmds += [f"parse {v}" for v in invalid]
    cmds += [f"compare {x} {y}" for x, y in pairs]
    cmds += [f"bump {k} {v}" for k, v in bumps]
    cmds += [f"format {v}" for v in valid]

    got, err = run_harness(cmds)
    if err:
        say(f"\n[3] differential           ABORTED: {err}")
        R["error"] = err
        if a.json: pathlib.Path(a.json).write_text(json.dumps(R, indent=2))
        return 1

    i = 0
    fam = {}
    fails = []

    def score(name, n, check):
        nonlocal i
        ok = 0
        for k in range(n):
            g = got[i + k]
            try:
                good, detail = check(k, g)
            except Exception as e:
                good, detail = False, f"checker error: {e}"
            if good: ok += 1
            elif len(fails) < 12: fails.append(f"{name}: {detail}")
        fam[name] = (ok, n)
        i += n

    def chk_valid(k, g):
        s = valid[k]; want = ref_parse(s)
        if not g.get("ok"): return False, f"parse({s!r}) rejected; expected {want}"
        mine = {f: g.get(f) for f in ("major","minor","patch","prerelease","build")}
        return (mine == want), f"parse({s!r}) -> {mine}, expected {want}"

    def chk_invalid(k, g):
        s = invalid[k]
        if ref_parse(s) is not None: return True, ""      # oracle accepts; skip
        return (not g.get("ok")), f"parse({s!r}) accepted, but it is invalid"

    def chk_cmp(k, g):
        x, y = pairs[k]; want = ref_compare(x, y)
        if not g.get("ok"): return False, f"compare({x!r},{y!r}) errored"
        return (g.get("cmp") == want), f"compare({x!r},{y!r}) -> {g.get('cmp')}, expected {want}"

    def chk_bump(k, g):
        kind, s = bumps[k]; want = ref_bump(kind, s)
        if not g.get("ok"): return False, f"bump {kind} {s!r} errored"
        return (g.get("version") == want), f"bump {kind} {s!r} -> {g.get('version')!r}, expected {want!r}"

    def chk_round(k, g):
        s = valid[k]
        if not g.get("ok"): return False, f"format({s!r}) errored"
        out = g.get("version")
        return (ref_parse(out) == ref_parse(s)), f"format({s!r}) -> {out!r}, not equivalent"

    score("parse valid",   len(valid),   chk_valid)
    score("parse invalid", len(invalid), chk_invalid)
    score("compare",       len(pairs),   chk_cmp)
    score("bump",          len(bumps),   chk_bump)
    score("round-trip",    len(valid),   chk_round)

    say("\n[3] differential testing")
    tot_ok = tot_n = 0
    for name, (ok, n) in fam.items():
        tot_ok += ok; tot_n += n
        pct = 100 * ok / n if n else 0
        bar = "#" * int(pct / 4)
        say(f"      {name:<15} {ok:>5}/{n:<5} {pct:5.1f}%  {bar}")
    overall = 100 * tot_ok / tot_n if tot_n else 0
    say(f"      {'OVERALL':<15} {tot_ok:>5}/{tot_n:<5} {overall:5.1f}%")
    R["differential"] = {k: {"pass": v[0], "total": v[1]} for k, v in fam.items()}
    R["differential_pct"] = round(overall, 2)

    # the spec chain, called out separately — it is the diagnostic one
    chain_ok = all(got[len(valid) + len(invalid) + x * len(SPEC_CHAIN) + y].get("cmp")
                   == ref_compare(SPEC_CHAIN[x], SPEC_CHAIN[y])
                   for x in range(len(SPEC_CHAIN)) for y in range(len(SPEC_CHAIN)))
    R["spec_precedence_chain"] = chain_ok
    say(f"      semver.org precedence chain: {'PASS' if chain_ok else 'FAIL'}")

    # "parse invalid" is trivially 100% for anything that rejects everything,
    # so it only means something when "parse valid" is also healthy.
    pv = fam["parse valid"][0] / max(1, fam["parse valid"][1])
    pi = fam["parse invalid"][0] / max(1, fam["parse invalid"][1])
    R["degenerate_rejector"] = bool(pv < 0.5 and pi > 0.9)
    if R["degenerate_rejector"]:
        say("      NOTE: 'parse invalid' is near-perfect only because almost")
        say("            nothing parses at all. It is not evidence of anything.")

    # 4 ------------------------------------------------------------ quality
    src = LIB.read_text(encoding="utf-8") if LIB.exists() else ""
    nodoc = re.sub(r"//.*", "", src)
    q = {
        "unsafe_blocks":   len(re.findall(r"\bunsafe\b", nodoc)),
        "clone_calls":     len(re.findall(r"\.clone\(\)", nodoc)),
        "to_owned_calls":  len(re.findall(r"\.to_owned\(\)", nodoc)),
        "todo_macros":     len(re.findall(r"\b(todo!|unimplemented!)", nodoc)),
        "panic_macros":    len(re.findall(r"\bpanic!", nodoc)),
        "unwrap_calls":    len(re.findall(r"\.unwrap\(\)", nodoc)),
        "lines":           len([l for l in src.splitlines() if l.strip()]),
    }
    deps = re.search(r"\[dependencies\]\s*(.*?)(\n\[|\Z)",
                     (RUST / "Cargo.toml").read_text(), re.S)
    q["extra_dependencies"] = len([l for l in (deps.group(1).splitlines() if deps else [])
                                   if l.strip() and not l.strip().startswith("#")])
    R["quality"] = q

    say("\n[4] quality")
    limits = {"unsafe_blocks": 0, "todo_macros": 0, "extra_dependencies": 0,
              "panic_macros": 0}
    for k, v in q.items():
        flag = ""
        if k in limits and v > limits[k]:  flag = "   <-- VIOLATION"
        elif k == "clone_calls" and v > 20: flag = "   <-- suspiciously high"
        elif k == "unwrap_calls" and v > 10: flag = "   <-- suspiciously high"
        say(f"      {k:<20} {v:>5}{flag}")

    violations = [k for k, lim in limits.items() if q[k] > lim]
    R["violations"] = violations

    # 5 ------------------------------------------------------------ verdict
    say("\n" + "-" * 68)
    if violations:
        say(f"  correctness {overall:.1f}%  --  BUT RULE VIOLATIONS: {', '.join(violations)}")
        say("  A translation that passes tests by breaking the rules has not")
        say("  done the task. See README, 'Why correctness is not the whole score'.")
    else:
        say(f"  correctness {overall:.1f}%   no rule violations")
    say("-" * 68 + "\n")

    if fails:
        say("First failures:")
        for f in fails: say(f"  - {f}")
        say("")

    if a.json:
        pathlib.Path(a.json).write_text(json.dumps(R, indent=2))
        say(f"wrote {a.json}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
