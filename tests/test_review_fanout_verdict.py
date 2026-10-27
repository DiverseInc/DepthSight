"""Tests for the review_fanout verdict parser.

The rule under test
-------------------
A reviewer counts as having RETURNED only if its output contains an explicit
``VERDICT: YES`` / ``VERDICT: NO`` line. Everything else -- spinner glyphs, an
MCP startup banner, a permission rejection, a request for the question, a
truncated-diff complaint, or plain prose with no verdict -- is NO VERDICT.

The project rule this encodes: a NO VERDICT is NEVER approval.

Runs under pytest, and standalone with no third-party package:

    python tests/test_review_fanout_verdict.py
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import types
from pathlib import Path

TOOL_PATH = Path(__file__).resolve().parent.parent / "tools" / "review_fanout.py"


def _load_tool():
    """Import tools/review_fanout.py by path (tools/ is not a package)."""
    spec = importlib.util.spec_from_file_location(
        "review_fanout_under_test", TOOL_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# Fixtures: the three shapes of NON-answer, plus the shape of an answer.
# --------------------------------------------------------------------------

# Braille spinner glyphs, written as escapes so this file stays ASCII.
SPINNER_ONLY = (
    "\u280b\u2819\u2818\u2803\u2810\u2807\u2800\u2805\u2800\u2815\u2800"
    "\u2803\u2810\u2807\u2800\u2815\u2800\u2803\u2810\u2807\u2800\u2805"
)

# MEASURED 2026-10-26: the exact lines DSH prints while its MCP servers boot.
MCP_BANNER_ONLY = (
    "GitHub MCP Server running on stdio\n"
    "Knowledge Graph MCP Server running on stdio\n"
)

# MEASURED 2026-10-26: an opencode run whose first tool call was blocked.
PERMISSION_REJECTED = (
    "Error: The user rejected permission to use this specific tool call.\n"
)

A_REAL_VERDICT = (
    "The clear path is reachable from the stop handler.\n"
    "VERDICT: NO\n"
)


# --------------------------------------------------------------------------
# The parser
# --------------------------------------------------------------------------


def test_verdict_yes_is_a_verdict():
    fanout = _load_tool()
    verdict, reason = fanout.parse_verdict("VERDICT: YES\n")
    assert verdict == "YES", reason


def test_verdict_no_is_a_verdict():
    fanout = _load_tool()
    verdict, reason = fanout.parse_verdict("VERDICT: NO\n")
    assert verdict == "NO", reason


def test_verdict_with_prose_around_it_is_a_verdict():
    fanout = _load_tool()
    verdict, reason = fanout.parse_verdict(A_REAL_VERDICT)
    assert verdict == "NO", reason


def test_markdown_decorated_verdict_is_a_verdict():
    fanout = _load_tool()
    verdict, _ = fanout.parse_verdict("Some notes.\n\n**VERDICT: YES**\n")
    assert verdict == "YES"


def test_spinner_only_is_not_a_verdict():
    fanout = _load_tool()
    verdict, reason = fanout.parse_verdict(SPINNER_ONLY)
    assert verdict is None, f"a spinner is not a verdict: {reason}"
    assert "NO VERDICT" in reason


def test_mcp_banner_only_is_not_a_verdict():
    fanout = _load_tool()
    verdict, reason = fanout.parse_verdict(MCP_BANNER_ONLY)
    assert verdict is None, f"a startup banner is not a verdict: {reason}"
    assert "NO VERDICT" in reason


def test_permission_rejection_is_not_a_verdict():
    fanout = _load_tool()
    verdict, reason = fanout.parse_verdict(PERMISSION_REJECTED)
    assert verdict is None, f"a blocked tool call is not a verdict: {reason}"
    assert "NO VERDICT" in reason


def test_prose_without_a_verdict_line_is_not_a_verdict():
    fanout = _load_tool()
    verdict, reason = fanout.parse_verdict(
        "I looked at the change and it looks broadly reasonable to me.\n"
    )
    assert verdict is None, reason
    assert "NO VERDICT" in reason


def test_empty_verdict_value_is_not_a_verdict():
    fanout = _load_tool()
    verdict, reason = fanout.parse_verdict("VERDICT:\n")
    assert verdict is None, reason
    assert "NO VERDICT" in reason


def test_asking_for_the_question_is_not_a_verdict():
    fanout = _load_tool()
    verdict, reason = fanout.parse_verdict(
        "What is the question? I do not see one in the request.\n"
    )
    assert verdict is None, reason
    assert "NO VERDICT" in reason


def test_truncated_diff_complaint_is_not_a_verdict():
    fanout = _load_tool()
    verdict, reason = fanout.parse_verdict(
        "DIFF TRUNCATED: tools/prepush_guard.py is not in what you were given.\n"
    )
    assert verdict is None, reason
    assert "NO VERDICT" in reason


def test_echoed_instruction_is_not_a_verdict():
    """A reviewer quoting the format instruction must not fake a verdict."""
    fanout = _load_tool()
    verdict, reason = fanout.parse_verdict(
        "I will answer in the requested format.\n"
        "End your reply with exactly one final line, either 'VERDICT: YES' or "
        "'VERDICT: NO'.\n"
    )
    assert verdict is None, reason
    assert "NO VERDICT" in reason


def test_reviewer_that_quotes_this_tool_is_still_scored_on_its_marker():
    """MEASURED 2026-10-26: opencode, reviewing a diff of this tool, quoted
    this tool's own regexes -- its output contained "truncat...diff" and
    "please provide the question" -- and then correctly ended with
    "VERDICT: NO". The blockers must not silence a real verdict.
    """
    fanout = _load_tool()
    output = (
        "The parser checks `_TRUNCATED` and `_NO_ANSWER`, which match the "
        "patterns \"diff truncated\" and \"please provide the question\".\n"
        "So a reviewer whose output merely discusses the diff can be "
        "mislabelled.\n"
        "VERDICT: NO\n"
    )
    verdict, reason = fanout.parse_verdict(output)
    assert verdict == "NO", reason


def test_the_last_marker_wins():
    """The contract asks the reviewer to END its reply with the verdict."""
    fanout = _load_tool()
    verdict, _ = fanout.parse_verdict(
        "An earlier quoted example:\nVERDICT: YES\n"
        "But the reviewer actually concluded the opposite.\n"
        "VERDICT: NO\n"
    )
    assert verdict == "NO"


def test_a_protocol_truncation_report_is_not_a_verdict():
    """The protocol for a reviewer that cannot see the whole change is to
    reply exactly 'DIFF TRUNCATED'. That is a STANDALONE-LINE check."""
    fanout = _load_tool()
    output = (
        "I could only see the first part of the change.\n"
        "DIFF TRUNCATED\n"
        "VERDICT: NO\n"
    )
    verdict, reason = fanout.parse_verdict(output)
    assert verdict is None, reason
    assert "NO VERDICT" in reason


def test_quoting_this_tool_does_not_downgrade_a_real_verdict():
    """MEASURED 2026-10-26: dsh and opencode, both reviewing a diff of this
    very tool, quoted this tool's own sentence -- "says it could not see the
    whole change" -- while explaining themselves, and a free-text phrase
    match threw away BOTH of their real verdicts. Quoting the tool must not
    silence the tool.
    """
    fanout = _load_tool()
    output = (
        "Note the converse: a marker alone is not sufficient -- the "
        "truncation check downgrades even a real `VERDICT: NO` to NO VERDICT, "
        "so it says it could not see the whole change.\n"
        "truncated              -> verdict=None\n"
        "VERDICT: NO\n"
    )
    verdict, reason = fanout.parse_verdict(output)
    assert verdict == "NO", reason


# --------------------------------------------------------------------------
# The engine runner: the old behaviour counted a blocked run as a "return"
# --------------------------------------------------------------------------


def _run_with_output(fanout, text, name="opencode"):
    """Drive the tool's runner with a faked subprocess, no engine launched."""
    completed = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr=text
    )
    fake_subprocess = types.SimpleNamespace(
        run=lambda *a, **k: completed,
        TimeoutExpired=subprocess.TimeoutExpired,
    )
    fake_shutil = types.SimpleNamespace(which=lambda *a, **k: sys.executable)
    orig_subprocess, orig_shutil = fanout.subprocess, fanout.shutil
    try:
        fanout.subprocess = fake_subprocess
        fanout.shutil = fake_shutil
        return fanout._run(name, ["opencode", "run", "hello"])
    finally:
        fanout.subprocess = orig_subprocess
        fanout.shutil = orig_shutil


def test_run_scores_a_permission_rejection_as_no_verdict():
    """RED against the old code: it counted this blocked run as RETURNED."""
    fanout = _load_tool()
    _, _, ok = _run_with_output(fanout, PERMISSION_REJECTED)
    assert ok is False, (
        "a reviewer that was blocked by a permission rejection confirmed "
        "nothing and must not count as returned"
    )


def test_run_scores_a_spinner_only_output_as_no_verdict():
    fanout = _load_tool()
    _, _, ok = _run_with_output(fanout, SPINNER_ONLY)
    assert ok is False, "spinner glyphs are not a verdict"


def test_run_scores_an_mcp_banner_only_output_as_no_verdict():
    fanout = _load_tool()
    _, _, ok = _run_with_output(fanout, MCP_BANNER_ONLY)
    assert ok is False, "a startup banner is not a verdict"


def test_run_accepts_a_real_verdict():
    fanout = _load_tool()
    _, _, ok = _run_with_output(fanout, A_REAL_VERDICT)
    assert ok is True, "a real VERDICT: line must count"


# --------------------------------------------------------------------------
# The summary: every reviewer is reported honestly
# --------------------------------------------------------------------------


def test_summary_names_every_no_verdict_reviewer():
    fanout = _load_tool()
    results = {
        "dsh": ("dsh", "VERDICT: NO\n", True),
        "opencode": ("opencode", "VERDICT: YES\n", True),
        "ollama": ("ollama", "no usable output -- NO VERDICT", False),
    }
    summary = fanout.format_summary(results)
    assert "2/2" in summary, summary
    assert "ollama" in summary, summary
    assert "never approval" in summary, summary


def test_summary_does_not_count_a_triage_only_reviewer_as_a_verdict():
    """MEASURED 2026-10-26: opencode itself flagged that the tally counted
    ollama, which is labelled TRIAGE ONLY, as if it were a verdict."""
    fanout = _load_tool()
    results = {
        "dsh": ("dsh", "VERDICT: NO\n", True),
        "ollama": ("ollama", "VERDICT: NO\n", True),
    }
    summary = fanout.format_summary(results)
    assert "1/1" in summary, summary
    assert "triage" in summary.lower(), summary
    assert "ollama" in summary, summary


def test_summary_reports_the_verdict_values():
    fanout = _load_tool()
    results = {
        "dsh": ("dsh", "VERDICT: NO\n", True),
        "opencode": ("opencode", "VERDICT: YES\n", True),
    }
    summary = fanout.format_summary(results)
    assert "2/2" in summary, summary
    assert "dsh=NO" in summary, summary
    assert "opencode=YES" in summary, summary


# --------------------------------------------------------------------------
# DEFECT 1: DSH gets the diff inline
# --------------------------------------------------------------------------


def test_dsh_prompt_inlines_the_diff():
    fanout = _load_tool()
    prompt = fanout.dsh_prompt("Is the clear path idempotent?", "UNIQUE-DIFF-LINE")
    assert "UNIQUE-DIFF-LINE" in prompt, (
        "DSH cannot open a path named in a prompt; the diff must be inline"
    )


def test_dsh_prompt_asks_for_the_verdict_marker():
    fanout = _load_tool()
    prompt = fanout.dsh_prompt("Is the clear path idempotent?", "x")
    assert "VERDICT: YES" in prompt and "VERDICT: NO" in prompt, prompt


def test_dsh_is_launched_with_the_prompt_on_stdin_not_as_a_path():
    """The diff must travel INSIDE the prompt, and the prompt must fit.

    MEASURED 2026-10-26: a 40,000-character argv prompt fails on Windows
    with "[WinError 206] The filename or extension is too long", so the
    prompt goes on stdin -- which `dsh headless` reads correctly.
    """
    fanout = _load_tool()
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="VERDICT: YES\n", stderr=""
        )

    orig = (
        fanout.subprocess,
        fanout.resolve_dsh_entry,
        fanout.resolve_node,
    )
    try:
        fanout.subprocess = types.SimpleNamespace(
            run=fake_run, TimeoutExpired=subprocess.TimeoutExpired
        )
        fanout.resolve_dsh_entry = lambda: Path("fake-dsh") / "bin.js"
        fanout.resolve_node = lambda: "fake-node"
        name, out, ok = fanout._run_dsh(
            fanout.dsh_prompt("Is the clear path idempotent?", "UNIQUE-DIFF-LINE")
        )
    finally:
        fanout.subprocess, fanout.resolve_dsh_entry, fanout.resolve_node = orig

    assert ok is True, out
    # No prompt in argv: argv carries only the launcher and the subcommand.
    assert captured["argv"] == ["fake-node", str(Path("fake-dsh") / "bin.js"), "headless"]
    # The diff is inside the prompt, on stdin.
    assert "UNIQUE-DIFF-LINE" in captured["kwargs"]["input"]


# --------------------------------------------------------------------------
# DEFECT 3: opencode gets an isolated pack, never the repository
# --------------------------------------------------------------------------


def test_the_panel_is_assembled_without_error():
    """MEASURED 2026-10-26: a NameError inside the hermes prompt made hermes
    fail with "NameError: name 'hermes_truncated' is not defined". Every
    prompt is built when the panel is assembled, so that class of bug
    surfaces here instead of 90 seconds into a subprocess call.
    """
    fanout = _load_tool()
    with tempfile.TemporaryDirectory() as tmp:
        diff = Path(tmp) / "review.diff"
        diff.write_text(
            "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n", encoding="utf-8"
        )
        jobs = fanout.reviewers(diff, "Is this correct?")
        assert set(jobs) == {"ollama", "hermes", "dsh", "opencode"}
        for name, job in jobs.items():
            assert callable(job), name


def test_opencode_pack_is_isolated_from_the_repository():
    fanout = _load_tool()
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        (repo / "pkg").mkdir(parents=True)
        (repo / "pkg" / "mod.py").write_text(
            "x = 1\n" * 5000, encoding="utf-8"
        )
        diff = repo / "review.diff"
        diff.write_text(
            "--- a/pkg/mod.py\n"
            "+++ b/pkg/mod.py\n"
            "@@ -1,3 +1,3 @@\n"
            "-x = 1\n"
            "+x = 2\n",
            encoding="utf-8",
        )
        pack = fanout.build_opencode_pack(diff, repo)
        try:
            assert pack.resolve() != repo.resolve()
            assert repo.resolve() not in pack.resolve().parents
            assert (pack / "review.diff").is_file()
            # UTF-8, no BOM -- opencode's Read treats a BOM'd file as binary.
            raw = (pack / "review.diff").read_bytes()
            assert not raw.startswith(b"\xef\xbb\xbf"), "the pack must be BOM-free"
            # Curated evidence for the touched file is present.
            ctx = pack / "context"
            assert ctx.is_dir()
            assert any(ctx.iterdir()), "the pack must carry evidence"
        finally:
            import shutil

            shutil.rmtree(pack, ignore_errors=True)


def test_opencode_pack_keeps_large_files_as_excerpts_and_index():
    fanout = _load_tool()
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        repo.mkdir(parents=True)
        big = repo / "big.py"
        big.write_text("".join(f"line {i}\n" for i in range(20000)), encoding="utf-8")
        diff = repo / "review.diff"
        diff.write_text(
            "--- a/big.py\n+++ b/big.py\n@@ -1,2 +1,2 @@\n-old\n+new\n",
            encoding="utf-8",
        )
        pack = fanout.build_opencode_pack(diff, repo)
        try:
            ctx = pack / "context"
            names = {p.name for p in ctx.iterdir()}
            assert any(n.endswith(".excerpt.txt") for n in names), names
            assert any(n.endswith(".index.txt") for n in names), names
            # The whole 20k-line file must NOT be copied verbatim.
            assert not any(p.stat().st_size > 5_000_000 for p in ctx.iterdir())
        finally:
            import shutil

            shutil.rmtree(pack, ignore_errors=True)


# --------------------------------------------------------------------------
# Standalone runner (no pytest required)
# --------------------------------------------------------------------------

_ALL_TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main() -> int:
    failures = []
    for fn in _ALL_TESTS:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as exc:
            failures.append((fn.__name__, str(exc)))
            print(f"FAIL {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures.append((fn.__name__, f"{type(exc).__name__}: {exc}"))
            print(f"ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(_ALL_TESTS) - len(failures)}/{len(_ALL_TESTS)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
