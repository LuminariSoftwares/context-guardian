"""No test in this suite hands a subprocess a from-scratch environment.

The four `context-guardian --help` / `--version` checks failed only on
windows-latest, with

    OSError: [WinError 10106] The requested service provider could not be
    loaded or initialized

raised at `import _overlapped` inside asyncio. The cause was a tidy-looking
`env={"PATH": "/usr/bin:/bin", ...}`: no SYSTEMROOT, so CPython cannot
initialise Winsock, and a POSIX PATH on a machine that has no /usr/bin. Two
other tests had the same env and passed by luck, because their children are
stdlib-only and never reach asyncio.

A reviewer cannot see that from a diff -- the broken line reads fine -- and the
suite only tells you on the one platform in the matrix that runs it. So this
reads the test sources instead: every `env=` on a subprocess call goes through
tests/_child_env.py, which starts from os.environ.
"""
import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SPAWNERS = {"run", "Popen", "check_call", "check_output", "call"}


def _spawns(node):
    """Whether this call starts a process: subprocess.run(...), Popen(...)."""
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    return name in SPAWNERS


def _from_scratch_envs(path):
    """(line, source) for every `env=<dict literal>` on a spawning call.

    An AST walk, not a regex: `cg_paths.data_dir(env={})` is a pure function
    taking a mapping to read, and tests/test_data_dir.py calls it that way
    several times. Only a child process cares about SYSTEMROOT.

    A name bound to a dict literal counts too. The line that actually broke
    windows-latest was

        full = {"PATH": "/usr/bin:/bin", ...}
        subprocess.run(..., env=full)

    so a guard that looked only at the call site would have watched the exact
    defect go past. `env=child_env(...)` is a Call and `{k: v for ...}` is a
    DictComp, so neither is caught.
    """
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    literal_dicts = {
        target.id
        for node in ast.walk(tree) if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Dict)
        for target in node.targets if isinstance(target, ast.Name)
    }
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _spawns(node):
            continue
        for kw in node.keywords:
            if kw.arg != "env":
                continue
            value = kw.value
            if isinstance(value, ast.Dict) or (
                    isinstance(value, ast.Name) and value.id in literal_dicts):
                found.append((node.lineno, ast.get_source_segment(text, value) or "{...}"))
    return found


def test_no_test_builds_a_child_environment_from_scratch():
    offenders = {}
    for path in sorted(TESTS.glob("*.py")):
        for lineno, source in _from_scratch_envs(path):
            offenders["%s:%d" % (path.name, lineno)] = source
    assert not offenders, (
        "a subprocess given a dict literal as its environment starts on Linux "
        "and dies on Windows (no SYSTEMROOT -> WinError 10106). Use "
        "_child_env.child_env(**overrides): %s" % offenders)


def test_the_guard_can_tell_the_two_kinds_of_env_apart(tmp_path):
    """Proof the walk above is not just always returning nothing."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import subprocess\n"
        "subprocess.run(['x'], env={'PATH': '/usr/bin'})\n"      # a child: flagged
        "pure_function(env={'HOME': '/home/user'})\n"            # a mapping: ignored
        "subprocess.run(['x'], env=child_env())\n"               # already fixed: ignored
        "full = {'PATH': '/usr/bin'}\n"
        "subprocess.run(['x'], env=full)\n"                      # the real defect: flagged
        "safe = child_env()\n"
        "subprocess.run(['x'], env=safe)\n",                     # already fixed: ignored
        encoding="utf-8")
    found = _from_scratch_envs(sample)
    assert [line for line, _ in found] == [2, 6], found


def test_child_env_keeps_what_windows_needs_and_drops_what_leaks(monkeypatch):
    from _child_env import child_env

    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setenv("GUARDIAN_SPAN_DIR", "/leaked/from/conftest")
    env = child_env(GUARDIAN_NUM_CTX=4096)
    assert env["SYSTEMROOT"] == r"C:\Windows"
    assert env["GUARDIAN_NUM_CTX"] == "4096"
    assert "GUARDIAN_SPAN_DIR" not in env, "conftest's value reached the child"
    assert "PATH" in env


def test_child_env_prepends_to_an_inherited_pythonpath(monkeypatch):
    import os

    from _child_env import child_env

    monkeypatch.setenv("PYTHONPATH", "/already/there")
    assert child_env(pythonpath="/repo")["PYTHONPATH"] == "/repo" + os.pathsep + "/already/there"
    monkeypatch.delenv("PYTHONPATH")
    assert child_env(pythonpath="/repo")["PYTHONPATH"] == "/repo"
