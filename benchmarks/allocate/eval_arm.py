"""Grade one arm of the allocate benchmark.

usage: python eval_arm.py <label> <dir containing the arm's allocate.py and test_allocate.py>

Reports: the arm's own tests on its own code, the hidden suite on its code (by category), the arm's own tests
run against the reference (portability) and against 15 seeded-bug mutants of the reference (test strength).
"""
import json, pathlib, re, shutil, subprocess, sys, tempfile

HERE = pathlib.Path(__file__).resolve().parent
REF = HERE / "reference" / "allocate.py"
HIDDEN = HERE / "hidden" / "test_hidden_allocate.py"

MUTATIONS = [
    ("M01 tie-break reversed (higher index first)", "key=lambda i: (-remainders[i], i)", "key=lambda i: (-remainders[i], -i)"),
    ("M02 negative totals use floor semantics", "    n = abs(total)\n", "    n = total\n",
     ("    if total < 0:\n        shares = [-s for s in shares]\n", "")),
    ("M03 float arithmetic", "shares = [n * w // W for w in weights]", "shares = [int(n * w / W) for w in weights]",
     ("remainders = [n * w % W for w in weights]", "remainders = [(n * w / W) % 1 for w in weights]")),
    ("M04 leftover to largest weight, not largest remainder", "key=lambda i: (-remainders[i], i)", "key=lambda i: (-weights[i], i)"),
    ("M05 bool accepted as int", "if isinstance(value, bool) or not isinstance(value, int):", "if not isinstance(value, int):"),
    ("M06 all-zero weights not rejected", '    if W == 0:\n        raise ValueError("at least one weight must be positive")\n', ""),
    ("M07 split_evenly(n<1) returns []", '    if n < 1:\n        raise ValueError("n must be at least 1")\n', "    if n < 1:\n        return []\n"),
    ("M08 tuple weights accepted", '    if not isinstance(weights, list):\n        raise TypeError("weights must be a list")\n', ""),
    ("M09 negative weight not rejected", '    if any(w < 0 for w in weights):\n        raise ValueError("weights must be non-negative")\n', ""),
    ("M10 zero total returns []", "    n = abs(total)\n", "    if total == 0:\n        return []\n    n = abs(total)\n"),
    ("M11 leftover off by one", "order[:leftover]", "order[:leftover + 1]"),
    ("M12 ties broken by larger weight before index", "key=lambda i: (-remainders[i], i)", "key=lambda i: (-remainders[i], -weights[i], i)"),
    ("M13 empty weights returns []", '    if not weights:\n        raise ValueError("weights must not be empty")\n', "    if not weights:\n        return []\n"),
    ("M14 negative tie-break mirrored", "key=lambda i: (-remainders[i], i)", "key=lambda i: (-remainders[i], i if total >= 0 else -i)"),
    ("M15 leftover handed out in index order, ignoring remainders", "order = sorted(range(len(weights)), key=lambda i: (-remainders[i], i))", "order = list(range(len(weights)))"),
]


def make_mutant(spec):
    label, old, new = spec[0], spec[1], spec[2]
    src = REF.read_text(encoding="utf-8")
    assert src.count(old) == 1, (label, src.count(old))
    src = src.replace(old, new)
    if len(spec) > 3:
        old2, new2 = spec[3]
        assert src.count(old2) == 1, (label, "second", src.count(old2))
        src = src.replace(old2, new2)
    return src


def run_pytest(workdir, target, timeout=90):
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=no", "-rf", target],
                           cwd=str(workdir), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"returncode": None, "passed": 0, "failed": 0, "error": "timeout", "failures": []}
    out = r.stdout
    passed = sum(int(x) for x in re.findall(r"(\d+) passed", out))
    failed = sum(int(x) for x in re.findall(r"(\d+) failed", out))
    errors = sum(int(x) for x in re.findall(r"(\d+) error", out))
    failures = re.findall(r"^FAILED \S+::(\S+)", out, re.M)
    return {"returncode": r.returncode, "passed": passed, "failed": failed, "errors": errors, "failures": failures,
            "tail": out.strip().splitlines()[-1] if out.strip() else ""}


def main():
    label, arm = sys.argv[1], pathlib.Path(sys.argv[2])
    result = {"label": label}
    arm_code, arm_tests = arm / "allocate.py", arm / "test_allocate.py"
    result["files_present"] = {"allocate.py": arm_code.exists(), "test_allocate.py": arm_tests.exists()}
    if not arm_code.exists():
        print(json.dumps(result, indent=1)); return
    with tempfile.TemporaryDirectory() as tmp:
        tmp = pathlib.Path(tmp)
        # 1. own tests on own code
        d = tmp / "own"; d.mkdir()
        shutil.copy(arm_code, d / "allocate.py")
        if arm_tests.exists():
            shutil.copy(arm_tests, d / "test_allocate.py")
            result["own_tests_on_own_code"] = run_pytest(d, "test_allocate.py")
        # 2. hidden suite on own code
        d = tmp / "hidden"; d.mkdir()
        shutil.copy(arm_code, d / "allocate.py")
        shutil.copy(HIDDEN, d / "test_hidden_allocate.py")
        h = run_pytest(d, "test_hidden_allocate.py")
        cats = {}
        for name in h["failures"]:
            cats.setdefault(name.split("_")[1] if name.startswith("test_") else "other", []).append(name)
        h["failures_by_category"] = {k: len(v) for k, v in sorted(cats.items())}
        result["hidden_suite"] = h
        if not arm_tests.exists():
            print(json.dumps(result, indent=1)); return
        # 3. own tests on the reference
        d = tmp / "ref"; d.mkdir()
        shutil.copy(REF, d / "allocate.py"); shutil.copy(arm_tests, d / "test_allocate.py")
        result["own_tests_on_reference"] = run_pytest(d, "test_allocate.py")
        # 4. own tests against mutants
        killed, survived = [], []
        for spec in MUTATIONS:
            d = tmp / spec[0][:3]; d.mkdir()
            (d / "allocate.py").write_text(make_mutant(spec), encoding="utf-8")
            shutil.copy(arm_tests, d / "test_allocate.py")
            r = run_pytest(d, "test_allocate.py")
            (killed if (r["returncode"] != 0) else survived).append(spec[0])
        result["mutants_killed"] = f"{len(killed)}/{len(MUTATIONS)}"
        result["mutants_survived"] = survived
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
