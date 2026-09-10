"""Test framework detection, machine-readable invocation and result parsing.

Every runner produces the same thing: a dict {test_id: "passed"|"failed"|"skipped"} plus the
exit code. The builder diffs these maps between "base + test patch" and "base + full patch"
to derive FAIL_TO_PASS and PASS_TO_PASS, exactly like SWE-bench.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

PASSED, FAILED, SKIPPED = "passed", "failed", "skipped"

TEST_PATH_PATTERNS = [
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)__tests__/"),
    re.compile(r"(^|/)spec/"),
    re.compile(r"(^|/)test_[^/]*\.py$"),
    re.compile(r"_test\.py$"),
    re.compile(r"(^|/)conftest\.py$"),
    re.compile(r"\.(test|spec)\.[cm]?[jt]sx?$"),
    re.compile(r"_test\.go$"),
    re.compile(r"_spec\.rb$"),
    re.compile(r"(^|/)src/test/"),
    re.compile(r"(^|/)testing/"),
    re.compile(r"Tests?\.(java|kt|cs|swift)$"),
    re.compile(r"(^|/)tests\.rs$"),
    re.compile(r"_test\.rs$"),
]

# Commands typed by engineers (captured from Bash tool calls) that indicate "I ran tests".
TEST_CMD_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("pytest", re.compile(r"(^|\s|/)(pytest|py\.test)(\s|$)|python3?\s+-m\s+pytest|uv\s+run\s+(-m\s+)?pytest|poetry\s+run\s+pytest|pipenv\s+run\s+pytest|tox\b")),
    ("unittest", re.compile(r"python3?\s+-m\s+unittest")),
    ("jest", re.compile(r"(^|\s)(npx\s+|pnpm\s+(exec\s+)?|yarn\s+|bunx?\s+)?jest(\s|$)")),
    ("vitest", re.compile(r"(^|\s)(npx\s+|pnpm\s+(exec\s+)?|yarn\s+|bunx?\s+)?vitest(\s|$)")),
    ("npm-test", re.compile(r"(^|\s)(npm|pnpm|yarn|bun)\s+(run\s+)?test(\s|$|:)")),
    ("go", re.compile(r"(^|\s)go\s+test(\s|$)")),
    ("cargo", re.compile(r"(^|\s)cargo\s+(nextest\s+run|test)(\s|$)")),
    ("rspec", re.compile(r"(^|\s)(bundle\s+exec\s+)?rspec(\s|$)")),
    ("make-test", re.compile(r"(^|\s)make\s+(test|check)(\s|$)")),
    ("gradle", re.compile(r"(^|\s)(\./)?gradlew?\s+test(\s|$)")),
    ("maven", re.compile(r"(^|\s)mvn\s+.*\btest\b")),
    ("dotnet", re.compile(r"(^|\s)dotnet\s+test(\s|$)")),
    ("mix", re.compile(r"(^|\s)mix\s+test(\s|$)")),
    ("phpunit", re.compile(r"phpunit(\s|$)")),
]


def is_test_path(path: str) -> bool:
    return any(p.search(path) for p in TEST_PATH_PATTERNS)


def classify_test_command(cmd: str) -> Optional[str]:
    """Return the runner family for a shell command if it looks like a test invocation."""
    cmd = cmd.strip()
    if not cmd:
        return None
    for name, pat in TEST_CMD_PATTERNS:
        if pat.search(cmd):
            return name
    return None


def detect_language(files: Sequence[str], repo: Optional[Path] = None) -> str:
    ext_counts: Dict[str, int] = {}
    for f in files:
        ext = Path(f).suffix.lower()
        if ext:
            ext_counts[ext] = ext_counts.get(ext, 0) + 1
    mapping = {
        ".py": "python", ".ts": "typescript", ".tsx": "typescript", ".js": "javascript", ".jsx": "javascript",
        ".mjs": "javascript", ".cjs": "javascript", ".go": "go", ".rs": "rust", ".rb": "ruby", ".java": "java",
        ".kt": "kotlin", ".cs": "csharp", ".php": "php", ".ex": "elixir", ".exs": "elixir", ".swift": "swift",
        ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp",
    }
    best = None
    for ext, n in sorted(ext_counts.items(), key=lambda kv: -kv[1]):
        if ext in mapping:
            best = mapping[ext]
            break
    if best:
        return best
    if repo:
        if (repo / "pyproject.toml").exists() or (repo / "setup.py").exists():
            return "python"
        if (repo / "package.json").exists():
            return "javascript"
        if (repo / "go.mod").exists():
            return "go"
        if (repo / "Cargo.toml").exists():
            return "rust"
        if (repo / "Gemfile").exists():
            return "ruby"
    return "unknown"


def detect_runner(repo: Path, language: str, captured_cmds: Sequence[str] = ()) -> Optional[str]:
    """Pick a runner family: from what the engineer actually ran first, then from project files."""
    families = [classify_test_command(c) for c in captured_cmds]
    families = [f for f in families if f]
    if families:
        # Most frequent wins; ties broken by most recent.
        counts: Dict[str, int] = {}
        for f in families:
            counts[f] = counts.get(f, 0) + 1
        best = max(counts.items(), key=lambda kv: (kv[1], families[::-1].index(kv[0]) * -1))[0]
        return _normalize_family(best, repo)
    # Project-file heuristics.
    pkg = repo / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
        test_script = (data.get("scripts") or {}).get("test", "")
        if "vitest" in deps or "vitest" in test_script:
            return "vitest"
        if "jest" in deps or "jest" in test_script:
            return "jest"
        if test_script:
            return "npm-test"
    if (repo / "go.mod").exists():
        return "go"
    if (repo / "Cargo.toml").exists():
        return "cargo"
    if (repo / "Gemfile").exists() and ((repo / "spec").exists() or (repo / ".rspec").exists()):
        return "rspec"
    if language == "python" or (repo / "pytest.ini").exists() or (repo / "pyproject.toml").exists() \
            or (repo / "setup.py").exists() or (repo / "tests").exists():
        return "pytest"
    if (repo / "Makefile").exists():
        try:
            if re.search(r"^test:", (repo / "Makefile").read_text(encoding="utf-8", errors="replace"), re.M):
                return "make-test"
        except Exception:
            pass
    return None


def _normalize_family(family: str, repo: Path) -> str:
    if family == "npm-test":
        # "npm test" usually wraps jest or vitest; prefer the structured runner when visible.
        pkg = repo / "package.json"
        if pkg.exists():
            try:
                data = json.loads(pkg.read_text(encoding="utf-8"))
                script = (data.get("scripts") or {}).get("test", "")
                if "vitest" in script:
                    return "vitest"
                if "jest" in script:
                    return "jest"
            except Exception:
                pass
    if family == "unittest":
        return "pytest"  # pytest runs unittest suites and gives us junit output
    return family


@dataclass
class RunResult:
    runner: str
    command: List[str]
    returncode: int
    results: Dict[str, str] = field(default_factory=dict)  # test_id -> status
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration: float = 0.0

    @property
    def structured(self) -> bool:
        return bool(self.results)

    def summary(self) -> Dict[str, int]:
        out = {PASSED: 0, FAILED: 0, SKIPPED: 0}
        for v in self.results.values():
            out[v] = out.get(v, 0) + 1
        return out


def _python_for(repo: Path) -> str:
    for cand in (".venv/bin/python", "venv/bin/python", ".venv/Scripts/python.exe"):
        p = repo / cand
        if p.exists():
            return str(p)
    return sys.executable or "python3"


def _js_runner(repo: Path, tool: str) -> List[str]:
    local = repo / "node_modules" / ".bin" / tool
    if local.exists():
        return [str(local)]
    return ["npx", "--no-install", tool]


def build_command(runner: str, repo: Path, test_files: Sequence[str], report: Path) -> Tuple[List[str], dict]:
    """Return (argv, extra_env) for a machine-readable test run restricted to `test_files`."""
    files = [f for f in test_files]
    env: dict = {}
    if runner == "pytest":
        py = _python_for(repo)
        # Prepend the checkout to PYTHONPATH so an editable install of the *original* repo (via a
        # symlinked .venv) does not shadow the code under test.
        pp = [str(repo)]
        if (repo / "src").is_dir():
            pp.append(str(repo / "src"))
        if os.environ.get("PYTHONPATH"):
            pp.append(os.environ["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(pp)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        argv = [py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "junit_family=xunit1",
                f"--junitxml={report}", "--continue-on-collection-errors"]
        argv += [f for f in files if f.endswith(".py") and "conftest" not in f] if files else []
        return argv, env
    if runner in ("jest", "vitest"):
        env["CI"] = "1"
        if runner == "jest":
            argv = _js_runner(repo, "jest") + ["--json", f"--outputFile={report}", "--ci", "--silent"]
        else:
            argv = _js_runner(repo, "vitest") + ["run", "--reporter=json", f"--outputFile={report}"]
        argv += list(files)
        return argv, env
    if runner == "npm-test":
        return ["npm", "test", "--silent"], {"CI": "1"}
    if runner == "go":
        pkgs = sorted({"./" + str(Path(f).parent) if str(Path(f).parent) != "." else "." for f in files}) or ["./..."]
        return ["go", "test", "-json", "-count=1", *pkgs], {}
    if runner == "cargo":
        return ["cargo", "test", "--no-fail-fast", "--", "--test-threads=1"], {"CARGO_TERM_COLOR": "never"}
    if runner == "rspec":
        argv = ["bundle", "exec", "rspec"] if (repo / "Gemfile").exists() else ["rspec"]
        argv += ["--format", "json", "--out", str(report)]
        argv += list(files)
        return argv, {}
    if runner == "make-test":
        return ["make", "test"], {}
    if runner == "gradle":
        return ["./gradlew" if (repo / "gradlew").exists() else "gradle", "test", "--console=plain"], {}
    if runner == "maven":
        return ["mvn", "-q", "-B", "test"], {}
    if runner == "dotnet":
        return ["dotnet", "test"], {}
    if runner == "mix":
        return ["mix", "test"], {}
    if runner == "phpunit":
        return [str(repo / "vendor/bin/phpunit") if (repo / "vendor/bin/phpunit").exists() else "phpunit",
                "--log-junit", str(report)], {}
    raise ValueError(f"unknown runner {runner}")


def run_tests(runner: str, repo: Path, test_files: Sequence[str] = (), timeout: float = 900,
              setup_cmd: Optional[str] = None, extra_env: Optional[dict] = None) -> RunResult:
    import time
    report_dir = Path(tempfile.mkdtemp(prefix="repogym-report-"))
    report = report_dir / "report.json"
    if runner in ("pytest", "phpunit"):
        report = report_dir / "report.xml"
    argv, env = build_command(runner, repo, test_files, report)
    full_env = dict(os.environ)
    full_env.update(env)
    if extra_env:
        full_env.update(extra_env)
    full_env.setdefault("REPOGYM_TEST_RUN", "1")
    start = time.time()
    if setup_cmd:
        try:
            subprocess.run(setup_cmd, shell=True, cwd=str(repo), env=full_env, capture_output=True, text=True,
                           timeout=timeout)
        except subprocess.TimeoutExpired:
            return RunResult(runner, argv, 124, {}, "", "setup_cmd timed out", True, time.time() - start)
    timed_out = False
    try:
        proc = subprocess.run(argv, cwd=str(repo), env=full_env, capture_output=True, text=True, errors="replace",
                              timeout=timeout, stdin=subprocess.DEVNULL)
        rc, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        timed_out = True
        rc = 124
        out = (e.stdout or b"").decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = (e.stderr or b"").decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
    except FileNotFoundError as e:
        rc, out, err = 127, "", f"runner not found: {e}"
    results = parse_results(runner, repo, report, out, err)
    res = RunResult(runner, argv, rc, results, out[-20000:], err[-20000:], timed_out, time.time() - start)
    try:
        import shutil
        shutil.rmtree(report_dir, ignore_errors=True)
    except Exception:
        pass
    return res


# ---- parsers -----------------------------------------------------------------------------

def parse_results(runner: str, repo: Path, report: Path, stdout: str, stderr: str) -> Dict[str, str]:
    try:
        if runner == "pytest":
            return _parse_junit(report, style="pytest")
        if runner == "phpunit":
            return _parse_junit(report, style="generic")
        if runner in ("jest", "vitest"):
            return _parse_jest_json(report, repo)
        if runner == "go":
            return _parse_go_json(stdout)
        if runner == "cargo":
            return _parse_cargo_text(stdout + "\n" + stderr)
        if runner == "rspec":
            return _parse_rspec_json(report)
    except Exception:
        return {}
    return {}


def _parse_junit(report: Path, style: str) -> Dict[str, str]:
    if not report.exists():
        return {}
    tree = ET.parse(str(report))
    root = tree.getroot()
    results: Dict[str, str] = {}
    for tc in root.iter("testcase"):
        name = tc.get("name") or ""
        classname = tc.get("classname") or ""
        file_attr = tc.get("file")
        if style == "pytest":
            tid = _pytest_nodeid(classname, name, file_attr)
        else:
            tid = f"{classname}::{name}" if classname else name
        status = PASSED
        for child in tc:
            if child.tag in ("failure", "error"):
                status = FAILED
                break
            if child.tag == "skipped":
                status = SKIPPED
        # Same id can appear twice (e.g. setup error + test); failure wins.
        if results.get(tid) == FAILED:
            continue
        results[tid] = status
    return results


def _pytest_nodeid(classname: str, name: str, file_attr: Optional[str]) -> str:
    """Rebuild a pytest node id (path::Class::test) from junit attributes."""
    if file_attr:
        path = file_attr.replace(os.sep, "/")
        module = path[:-3].replace("/", ".") if path.endswith(".py") else path.replace("/", ".")
        rest = classname
        if rest == module:
            rest = ""
        elif rest.startswith(module + "."):
            rest = rest[len(module) + 1:]
        else:
            # classname might already be "pkg.mod.Class"; keep any trailing class parts that
            # are not part of the module path.
            mod_parts = module.split(".")
            cls_parts = classname.split(".")
            i = 0
            while i < min(len(mod_parts), len(cls_parts)) and mod_parts[i] == cls_parts[i]:
                i += 1
            rest = ".".join(cls_parts[i:]) if i == len(mod_parts) else ""
        parts = [path] + ([p for p in rest.split(".") if p] if rest else []) + [name]
        return "::".join(parts)
    # No file attribute: approximate path from dotted classname.
    parts = classname.split(".") if classname else []
    return "::".join(parts + [name])


def _parse_jest_json(report: Path, repo: Path) -> Dict[str, str]:
    if not report.exists():
        return {}
    data = json.loads(report.read_text(encoding="utf-8"))
    results: Dict[str, str] = {}
    for suite in data.get("testResults", []):
        fname = suite.get("name") or suite.get("testFilePath") or ""
        try:
            rel = os.path.relpath(fname, str(repo)) if fname else ""
        except ValueError:
            rel = fname
        rel = rel.replace(os.sep, "/")
        asserts = suite.get("assertionResults", [])
        if not asserts and suite.get("status") == "failed":
            results[f"{rel}::<suite>"] = FAILED
        for a in asserts:
            full = a.get("fullName") or " ".join(a.get("ancestorTitles", []) + [a.get("title", "")])
            status = a.get("status")
            results[f"{rel}::{full}"] = PASSED if status == "passed" else SKIPPED if status in (
                "pending", "skipped", "todo", "disabled") else FAILED
    return results


def _parse_go_json(stdout: str) -> Dict[str, str]:
    results: Dict[str, str] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        action = ev.get("Action")
        test = ev.get("Test")
        pkg = ev.get("Package", "")
        if action not in ("pass", "fail", "skip"):
            continue
        tid = f"{pkg}/{test}" if test else f"{pkg}/<package>"
        if test is None and action != "fail":
            continue  # only record package-level failures (build errors)
        results[tid] = PASSED if action == "pass" else SKIPPED if action == "skip" else FAILED
    return results


_CARGO_RE = re.compile(r"^test (\S+) \.\.\. (ok|FAILED|ignored|failed)", re.M)


def _parse_cargo_text(text: str) -> Dict[str, str]:
    results: Dict[str, str] = {}
    for m in _CARGO_RE.finditer(text):
        name, status = m.group(1), m.group(2)
        results[name] = PASSED if status == "ok" else SKIPPED if status == "ignored" else FAILED
    return results


def _parse_rspec_json(report: Path) -> Dict[str, str]:
    if not report.exists():
        return {}
    data = json.loads(report.read_text(encoding="utf-8"))
    results: Dict[str, str] = {}
    for ex in data.get("examples", []):
        tid = f"{ex.get('file_path', '').lstrip('./')}::{ex.get('full_description', ex.get('id'))}"
        st = ex.get("status")
        results[tid] = PASSED if st == "passed" else SKIPPED if st == "pending" else FAILED
    return results


def split_test_command_targets(cmd: str) -> List[str]:
    """Extract path-like arguments from a captured test command (e.g. `pytest tests/test_x.py -k foo`)."""
    try:
        parts = shlex.split(cmd)
    except ValueError:
        return []
    out = []
    for p in parts:
        if p.startswith("-"):
            continue
        if "/" in p or p.endswith((".py", ".ts", ".js", ".tsx", ".jsx", ".go", ".rb", ".rs")):
            out.append(p.split("::")[0])
    return out
