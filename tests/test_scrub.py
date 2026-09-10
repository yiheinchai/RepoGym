from repogym.scrub import REDACTED, path_excluded, scrub_text


def test_scrubs_common_tokens():
    s = "key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789 and ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef0123 and AKIAIOSFODNN7EXAMPLE"
    out = scrub_text(s)
    assert "sk-ant" not in out and "ghp_" not in out and "AKIA" not in out
    assert out.count(REDACTED) == 3


def test_scrubs_assignments_and_urls():
    out = scrub_text('DATABASE_URL=postgres://admin:hunter2secret@db.internal/app\npassword = "correct-horse-battery"')
    assert "hunter2secret" not in out
    assert "correct-horse-battery" not in out
    assert "postgres://" in out


def test_leaves_normal_code_alone():
    code = "def add(a, b):\n    return a + b\n"
    assert scrub_text(code) == code


def test_path_exclusions():
    globs = [".env", ".env.*", "*.pem", "*secret*"]
    assert path_excluded(".env", globs)
    assert path_excluded("config/.env.production", globs)
    assert path_excluded("certs/server.pem", globs)
    assert path_excluded("app/secrets.yaml", globs)
    assert not path_excluded("src/main.py", globs)
