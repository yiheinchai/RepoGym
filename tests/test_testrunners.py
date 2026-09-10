from pathlib import Path

from repogym.testrunners import (_pytest_nodeid, classify_test_command, detect_runner, is_test_path,
                                 split_test_command_targets)


def test_is_test_path():
    assert is_test_path("tests/test_x.py")
    assert is_test_path("src/foo/__tests__/bar.test.ts")
    assert is_test_path("pkg/handler_test.go")
    assert is_test_path("spec/models/user_spec.rb")
    assert not is_test_path("src/app.py")
    assert not is_test_path("testimonials/page.tsx")


def test_classify_commands():
    assert classify_test_command("python -m pytest tests/test_x.py -q") == "pytest"
    assert classify_test_command("uv run pytest") == "pytest"
    assert classify_test_command("npx jest src/") == "jest"
    assert classify_test_command("npm test") == "npm-test"
    assert classify_test_command("go test ./...") == "go"
    assert classify_test_command("cargo test") == "cargo"
    assert classify_test_command("ls -la") is None
    assert classify_test_command("git commit -m 'test'") is None


def test_detect_runner_prefers_captured_commands(pyrepo):
    assert detect_runner(pyrepo, "python", ["pytest tests", "pytest tests", "npm test"]) == "pytest"
    assert detect_runner(pyrepo, "python", []) == "pytest"


def test_pytest_nodeid_reconstruction():
    assert _pytest_nodeid("tests.test_core", "test_add", "tests/test_core.py") == "tests/test_core.py::test_add"
    assert _pytest_nodeid("tests.test_core.TestX", "test_y", "tests/test_core.py") == "tests/test_core.py::TestX::test_y"
    assert _pytest_nodeid("tests.test_core", "test_p[1-2]", "tests/test_core.py") == "tests/test_core.py::test_p[1-2]"


def test_split_targets():
    assert split_test_command_targets("pytest tests/test_x.py::test_a -k foo") == ["tests/test_x.py"]
    assert split_test_command_targets("npm test") == []
