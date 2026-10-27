#!/usr/bin/env python3
"""
One-command adversarial review: fan the current diff out to a panel of
reviewer engines.

Why this is a script and not a habit
------------------------------------
Several separate shell invocations, each with its own invocation quirk, is a
workflow that fails intermittently. On 2026-10-16 two of five reviewers were
run wrong before any of them produced a verdict. Encoding the quirks once means
the fallback paths stop being folklore.

Invocation quirks this encodes (all learned the hard way)
--------------------------------------------------------
* opencode : the message goes BEFORE ``-f``. ``-f`` is an array flag, and the
  reversed order silently treats the prompt as a filename. The file MUST be
  UTF-8 -- PowerShell's ``>`` writes UTF-16 and opencode then reads it as
  binary. It is launched as the standalone ``opencode.exe`` through a
  list-argv subprocess: the ``.cmd`` shim goes through ``cmd /c``, which
  mangles an argument containing newlines.
* hermes   : ``hermes -z``. There is no ``hermes run``. It refuses the system
  temp dir, so the scratch dir is pinned.
* dsh      : ``dsh headless``. The MCP web harness is for interactive browser
  work and is useless for a one-shot review. It is launched as
  ``node <dsh>/lib/bin.js headless <prompt>`` -- the ``dsh.ps1`` shim is run
  with ``powershell -File``, and PowerShell's argument parsing stops at the
  first newline, which would silently drop everything after the first line of
  the diff.
* claude   : ``claude -p``.
* ollama   : the CLI, not the MCP tool. The MCP tool defaults to a model that
  is not pulled on this box and errors; ``qwen2.5-coder:7b`` is present.

The rule that matters more than any of the above
------------------------------------------------
Narrow the question. OpenCode burned 900s on "is this fix safe?" and returned
NO verdict, which is NOT approval. Re-asked as one question with a 20-line
cap, it answered in minutes. Every prompt below is a single question.

The three defects this script was rewritten to fix (all measured 2026-10-26)
----------------------------------------------------------------------------
1. DSH was handed a PATH, not the diff. It cannot open a path named in a
   prompt, so it returned only its MCP startup lines and no verdict -- while
   the fan-out counted it as having returned. The diff is now INLINE.
2. A run that produced NO verdict was counted as a "return". A reviewer now
   counts as having returned ONLY if its output contains an explicit
   ``VERDICT: YES`` / ``VERDICT: NO`` line. Everything else is NO VERDICT.
   This is the project rule: a NO VERDICT is NEVER approval.
3. OpenCode was run with the repository as its working directory. Pointed at
   a 32 MB / 1,337-file tree it read the diff in 700-line chunks and grepped
   the whole repo, blowing past the provider timeout -- six attempts at
   2400s each produced no answer. It is now given an ISOLATED scratch
   directory holding only the diff and curated excerpts, and a 300s cap.

And: never run this while something is mutating the tree. A reviewer will
happily report on a file that only exists inside a mutation harness.

Usage
-----
    python tools/review_fanout.py
    python tools/review_fanout.py --base origin/main --question "is the clear path idempotent?"
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / ".review"

# --------------------------------------------------------------------------
# PROMPT BUDGETS -- how much of the change each reviewer can actually see.
#
# The budget applies to the WHOLE argument the reviewer receives (framing +
# instructions + diff), not to the diff alone, so what the diff gets is what
# is LEFT after the framing. A reviewer that was handed only part of the
# change cannot give a verdict on the change, so the TOOL scores such a run
# NO VERDICT. It must not hope the reviewer notices.
#
# MEASURED 2026-10-27 (measure_hermes_limit2.py): the hermes CLI does NOT
# clip its -z argument. The tail of the prompt -- including the final line --
# was still visible to the model at 32,000 characters: seven consecutive
# probes (6,000 / 12,000 / 20,000 / 28,000 / 31,000 / 32,000) each put a
# token in the TAIL ONLY and asked the model to repeat it, and the model
# repeated it every time. The note this file used to carry -- "the hermes CLI
# truncates a long inline prompt at about 12,000 characters" -- was a
# MISDIAGNOSIS of this tool's OWN inline cap, which cut the diff at 11,500
# characters and then still called it "the complete diff".
#
# The binding limit is the Windows command line: CreateProcess caps
# lpCommandLine at 32,767 characters, and a 40,000-character argument fails
# with FileNotFoundError [WinError 206] -- a diagnostic that reads like a
# missing binary.
HERMES_PROMPT_LIMIT = 30000

# `ollama run` runs a 7B model with a small context. This is a model-context
# guard, not a CLI limit.
OLLAMA_PROMPT_LIMIT = 12000

# DSH's prompt travels on STDIN, so there is no command-line cap. This is a
# model-context guard.
DSH_PROMPT_LIMIT = 80000

# What survives from the 2026-10-19 hermes incident is the RULE, not the
# number: a reviewer that never received the change must never be recorded
# as having reviewed it. The cap now lives in HERMES_PROMPT_LIMIT, the cut
# is MARKED in the prompt, and the tool scores the run NO VERDICT itself.

# MEASURED 2026-10-26: Windows caps a CreateProcess command line at 32,767
# characters, and a 40,000-character argv prompt fails with
# "FileNotFoundError: [WinError 206] The filename or extension is too long" --
# a misleading diagnostic that looks like a missing binary. DSH is therefore
# handed its prompt on STDIN, which has no such limit (MEASURED: `dsh headless`
# with the prompt on stdin answers correctly).

# MEASURED 2026-10-18 on a real diff whose correct answer was already known:
# 7b answered both questions CORRECTLY, and also answered both correctly with
# NO DIFF IN THE PROMPT AT ALL. Its accuracy therefore carries no information --
# it is indistinguishable from a coin flip, and on 2026-10-18 it returned a
# confident "I found no issues" about a diff that disabled a stop-loss.
# qwen2.5-coder:14b was tried as the upgrade: 4x slower (94s vs 23s) and got the
# central question WRONG. There is no trustworthy local reviewer at this VRAM
# budget, so keep the fast one for triage and never treat it as a verdict.
OLLAMA_MODEL = "qwen2.5-coder:7b"

# Reviewers whose output is triage, not a verdict. Printed with that label so a
# confident-but-ungrounded local answer cannot sit in a column that reads like
# an approval from a model that can actually see the diff.
ADVISORY_REVIEWERS = {"ollama"}

DEFAULT_QUESTION = (
    "This diff changes live-money safety code. ONE question, max 20 lines, "
    "no summary: is there any path where trading resumes while a safety stop "
    "is active, or where success is reported but the change did not take "
    "effect? Answer YES/NO then specifics with file:line."
)

HERMES_SCRATCH = "~/hermes/cache/scratch/"

# The verdict contract. A reviewer counts as having RETURNED only if its
# output contains one of these literal lines. Anything else -- spinner
# glyphs, an MCP startup banner, a permission rejection, a question asking
# for the question, a truncated-diff complaint -- is NO VERDICT.
VERDICT_YES = "YES"
VERDICT_NO = "NO"
VERDICT_INSTRUCTION = (
    "End your reply with exactly one final line, either 'VERDICT: YES' or "
    "'VERDICT: NO'."
)

# The protocol goes BEFORE the inline diff, never after it. MEASURED
# 2026-10-26: hermes' CLI truncates its PROMPT at ~11,500 characters, so an
# instruction placed after the diff never reached it -- hermes was told
# nothing, noticed the diff was truncated, and answered anyway. An
# instruction the reviewer cannot see is not an instruction.
TRUNCATION_PROTOCOL = (
    "If the diff below is truncated, or you cannot see all of it, reply "
    "exactly 'DIFF TRUNCATED' and do NOT answer about a different file."
)

# MEASURED 2026-10-26: pointed at the repository, opencode read the diff in
# 700-line chunks and then grepped the whole 32 MB / 1,337-file tree, blowing
# past the 120s provider timeout. Six attempts at a 2400s cap produced no
# answer. Pointed at an ISOLATED scratch directory holding only the diff and
# curated excerpts it answers in 80-137s and returns a well-formed verdict
# every time. 300s is the cap: long enough for the measured 80-137s, short
# enough that a hung run does not eat the whole session.
OPENCODE_TIMEOUT = 300

# Default cap for the other engines.
DEFAULT_TIMEOUT = 900

# Evidence pack limits. A full file is included when it is small enough to
# read; above this size the pack carries the changed regions plus a
# mechanically-generated line index instead.
PACK_MAX_FILE_BYTES = 60 * 1024
PACK_MAX_INDEX_LINES = 3000
PACK_CONTEXT_PAD = 40


def export_diff(base: str | None = None) -> Path:
    """Write the change under review as UTF-8. Never via shell redirection.

    Without `base` this reviews only the uncommitted working tree -- which
    silently reviews NOTHING the moment the work is committed, i.e. exactly
    when a pre-merge review matters most. Pass `--base origin/main` to review
    the committed branch AND any working-tree changes on top of it.
    """
    OUT_DIR.mkdir(exist_ok=True)
    target = OUT_DIR / "review.diff"

    chunks: list[str] = []
    if base:
        committed = subprocess.run(
            ["git", "diff", f"{base}...HEAD"],
            cwd=REPO,
            capture_output=True,
            text=True,
        ).stdout
        if committed:
            chunks.append(f"=== COMMITTED: {base}...HEAD ===\n{committed}")
    uncommitted = subprocess.run(
        ["git", "diff", "HEAD"], cwd=REPO, capture_output=True, text=True
    ).stdout
    if uncommitted:
        chunks.append(f"=== UNCOMMITTED: git diff HEAD ===\n{uncommitted}")
    tracked = "\n".join(chunks)

    # Untracked test files are part of the change and must be reviewed too.
    extra = []
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=REPO,
        capture_output=True,
        text=True,
    ).stdout.split()
    for rel in untracked:
        if not rel.startswith("tests/") and not rel.endswith(".py"):
            continue
        d = subprocess.run(
            ["git", "diff", "--no-index", "--", "/dev/null", rel],
            cwd=REPO,
            capture_output=True,
            text=True,
        ).stdout
        if d:
            extra.append(f"=== NEW FILE: {rel} ===\n{d}")

    payload = tracked + "\n".join(extra)
    target.write_text(payload, encoding="utf-8")  # explicit, no BOM
    return target


def _resolve_argv(cmd: list[str]) -> tuple[list[str], str] | None:
    """Make a bare tool name actually launchable on Windows.

    Windows CreateProcess does NOT resolve `.cmd`/`.bat` from a bare name --
    only `.exe`. So `subprocess.run(["dsh", ...])` raises FileNotFoundError and
    a naive handler reports "not installed / not on PATH" for a tool that is
    present and working. `shutil.which` finds `dsh.CMD` happily; the launch
    is what fails. So: resolve to an absolute path, then dispatch on kind.

      .exe                -> run directly
      .cmd / .bat         -> cmd /c <path>
      .ps1                -> powershell -NoProfile -File <path>

    Returns (argv, why) or None when the tool genuinely is absent.
    """
    exe = cmd[0]
    path = shutil.which(exe)
    if path is None:
        for ext in (".cmd", ".exe", ".ps1", ".bat"):
            path = shutil.which(exe + ext)
            if path:
                break
    if path is None:
        return None

    low = path.lower()
    if low.endswith(".ps1"):
        return (
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                path,
            ]
            + cmd[1:],
            "powershell -File",
        )
    if low.endswith((".cmd", ".bat")):
        return ["cmd", "/c", path] + cmd[1:], "cmd /c"
    return [path] + cmd[1:], "direct"


def resolve_opencode_exe() -> Path | None:
    """Locate the STANDALONE opencode executable.

    The npm shim `opencode.cmd` is launched through `cmd /c`, which mangles an
    argument containing newlines. `opencode.exe` is a standalone binary and is
    launched directly with a list argv.

    Resolution order: explicit override, the known npm global location, then
    whatever `shutil.which` finds (accepting only a real .exe).
    """
    override = os.environ.get("OPENCODE_EXE")
    if override and Path(override).is_file():
        return Path(override)

    appdata = os.environ.get("APPDATA")
    if appdata:
        cand = (
            Path(appdata)
            / "npm"
            / "node_modules"
            / "opencode-ai"
            / "bin"
            / "opencode.exe"
        )
        if cand.is_file():
            return cand

    found = shutil.which("opencode.exe") or shutil.which("opencode")
    if found and found.lower().endswith(".exe") and Path(found).is_file():
        return Path(found)
    return None


def resolve_dsh_entry() -> Path | None:
    """Locate DSH's real entry point (`.../dsh/lib/bin.js`).

    The `dsh.ps1` npm shim is launched with `powershell -File`, and
    PowerShell's argument parsing stops at the first newline -- an inline diff
    would be silently truncated to its first line. Node is launched directly
    with a list argv instead, so newlines inside the prompt survive.
    """
    override = os.environ.get("DSH_ENTRY")
    if override and Path(override).is_file():
        return Path(override)

    appdata = os.environ.get("APPDATA")
    if appdata:
        cand = (
            Path(appdata)
            / "npm"
            / "node_modules"
            / "@deepseek-ai"
            / "dsh"
            / "lib"
            / "bin.js"
        )
        if cand.is_file():
            return cand
    return None


def resolve_node() -> str | None:
    return shutil.which("node.exe") or shutil.which("node")


def _slug(text: str) -> str:
    keep = [c if (c.isalnum() or c in "-_") else "-" for c in text]
    return ("".join(keep).strip("-") or "branch")[:60]


def _branch_name(repo: Path) -> str:
    try:
        name = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except Exception:
        name = ""
    return name or "detached"


# --------------------------------------------------------------------------
# The evidence pack for opencode
# --------------------------------------------------------------------------

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _parse_diff_targets(diff_text: str) -> list[dict]:
    """Pull {path, hunks:[(new_start,new_count)]} out of a unified diff."""
    targets: list[dict] = []
    current: dict | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path == "/dev/null":
                current = None
                continue
            if path.startswith("b/"):
                path = path[2:]
            current = {"path": path, "hunks": []}
            targets.append(current)
            continue
        if line.startswith("--- ") or line.startswith("diff --git"):
            continue
        m = _HUNK_RE.match(line)
        if m and current is not None:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) else 1
            current["hunks"].append((start, count))
    return targets


def _write_utf8(path: Path, text: str) -> None:
    """Write UTF-8 with NO BOM.

    PowerShell's `>` writes UTF-16 and `Out-File -Encoding utf8` adds a BOM;
    opencode's Read then treats the file as binary.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _safe_pack_name(rel: str) -> str:
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "__".join(parts) or "file"


def _excerpt(lines: list[str], hunks: list[tuple[int, int]], pad: int) -> str:
    """The changed regions of a file, with the REAL line numbers."""
    wanted: set[int] = set()
    for start, count in hunks:
        first = max(1, start - pad)
        last = min(len(lines), start + max(count, 1) - 1 + pad)
        wanted.update(range(first, last + 1))
    if not wanted:
        return ""
    out: list[str] = []
    prev = None
    for n in sorted(wanted):
        if prev is not None and n != prev + 1:
            out.append("        ...")
        out.append(f"{n:>7}: {lines[n - 1]}")
        prev = n
    return "\n".join(out)


def _line_index(lines: list[str], limit: int) -> str:
    """A mechanically-generated line index: number + first 160 chars."""
    out: list[str] = []
    for n, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        if len(out) >= limit:
            out.append(f"        ... [INDEX TRUNCATED at {limit} entries] ...")
            break
        out.append(f"{n:>7}: {line[:160]}")
    return "\n".join(out)


def build_opencode_pack(
    diff: Path, repo: Path, root: Path | None = None
) -> Path:
    """Build an ISOLATED evidence directory for opencode.

    It holds ONLY the diff under review plus curated excerpts of the files
    that diff touches. It is NEVER the repository: pointed at the repo,
    opencode walks the whole tree and times out (measured: six attempts at a
    2400s cap, no answer). The directory is unique per run so nothing is ever
    deleted or overwritten.

    Where it lands is configurable: `root` wins, then the REVIEW_PACK_DIR
    environment variable, then the system temp directory. The parser does
    not depend on any of them.
    """
    diff_text = diff.read_text(encoding="utf-8", errors="replace")
    digest = hashlib.sha256(diff_text.encode("utf-8", "replace")).hexdigest()[:10]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if root is None:
        env_root = os.environ.get("REVIEW_PACK_DIR")
        base = Path(env_root) if env_root else Path(tempfile.gettempdir())
    else:
        base = root
    pack_root = (
        base
        / f"depthsight-review-pack-{_slug(_branch_name(repo))}-{digest}-{stamp}"
    )
    context = pack_root / "context"
    context.mkdir(parents=True, exist_ok=True)

    _write_utf8(pack_root / "review.diff", diff_text)

    notes: list[str] = []
    for target in _parse_diff_targets(diff_text):
        rel = target["path"]
        src = repo / rel
        if not src.is_file():
            notes.append(f"- {rel}: not present in the working tree (new or deleted file)")
            continue
        try:
            raw = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            notes.append(f"- {rel}: could not be read")
            continue
        lines = raw.splitlines()
        name = _safe_pack_name(rel)
        if len(raw) <= PACK_MAX_FILE_BYTES:
            _write_utf8(context / name, raw)
        else:
            excerpt = _excerpt(lines, target["hunks"], PACK_CONTEXT_PAD)
            _write_utf8(
                context / f"{name}.excerpt.txt",
                f"CHANGED REGIONS OF {rel} (line numbers are the real file "
                f"line numbers in the working tree)\n\n{excerpt}\n",
            )
            index = _line_index(lines, PACK_MAX_INDEX_LINES)
            _write_utf8(
                context / f"{name}.index.txt",
                f"LINE INDEX FOR {rel} ({len(lines)} lines; mechanical, "
                f"non-blank lines only)\n\n{index}\n",
            )
        notes.append(f"- {rel}")

    readme = (
        "REVIEW PACK -- isolated evidence for ONE narrow review question.\n\n"
        "This directory contains ONLY:\n"
        "  review.diff                  the complete change under review\n"
        "  context/<file>               full contents of a touched file "
        "(<= 60 KB)\n"
        "  context/<file>.excerpt.txt   the changed regions of a larger file, "
        "with real line numbers\n"
        "  context/<file>.index.txt     a mechanical line index of a larger file\n\n"
        "Files touched by the diff:\n" + ("\n".join(notes) or "(none)") + "\n\n"
        "Answer the question using ONLY the files in this directory. Do NOT try "
        "to read files outside it.\n" + VERDICT_INSTRUCTION + "\n"
    )
    _write_utf8(pack_root / "README.txt", readme)
    return pack_root


# --------------------------------------------------------------------------
# The verdict parser -- the rule that a NO VERDICT is never approval
# --------------------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07")

# The protocol for a reviewer that cannot see the whole change is to reply
# exactly 'DIFF TRUNCATED'. That is checked as a STANDALONE LINE, and it is
# the ONLY truncation check that can override a marker.
#
# A free-text phrase match cannot tell a self-report from a QUOTE, and
# MEASURED 2026-10-26 proved it: dsh and opencode, both reviewing a diff of
# this very tool, quoted this tool's own sentence -- "says it could not see
# the whole change" -- while explaining themselves, and the phrase match
# threw away BOTH of their real verdicts. Quoting the tool must not silence
# the tool.
#
# The protection against a partial review is therefore structural, not
# lexical: the protocol goes FIRST in every prompt (so an engine whose prompt
# is truncated still receives it), and a reviewer that follows it emits
# 'DIFF TRUNCATED' and no marker.
_PROTOCOL_TRUNCATED = re.compile(
    r"^[ \t>*#`\-]*DIFF TRUNCATED\b.*$", re.MULTILINE
)

# A reviewer that asks for the question instead of answering it has confirmed
# NOTHING. On 2026-10-19 two of five reviewers (dsh, opencode) replied "What is
# the question?" and the >=5-words test scored both as RETURNED -- the same
# false green this tool exists to prevent, reproduced inside the tool. A
# non-answer must be scored as NO VERDICT even though it is full of words.
_NO_ANSWER = re.compile(
    r"what is the question"
    r"|what would you like me to answer"
    r"|no question was included"
    r"|did ?n'?t (?:actually )?(?:ask|include|provide) a question"
    r"|you (?:did ?n'?t|have not|haven'?t) (?:ask|include|provide|state) a question"
    r"|please (?:provide|clarify|re-?state|specify) the question"
    r"|could you (?:please )?(?:clarify|re-?state|provide|specify)"
    r"|i (?:don'?t|do not) see a question"
    r"|i'?m not sure what you(?:'re| are) asking"
    r"|(?:please )?(?:re-)?state (?:your |the )?question",
    re.IGNORECASE,
)

# A reviewer that says it never received the change has confirmed nothing,
# however confidently it words the rest. On 2026-10-19 hermes was silently
# truncated at ~12,000 chars, the file it was asked about started at byte
# 13,679, and it answered NO about the wrong code. A diff-related truncation
# complaint is therefore a NO VERDICT, not a disagreement.
_TRUNCATED = re.compile(
    r"diff truncated"
    r"|truncat(?:ed|ion)[^.!?]{0,40}diff"
    r"|diff[^.!?]{0,40}truncat(?:ed|ion)"
    r"|is not in what you were given"
    r"|not present in the diff"
    r"|does not appear in the diff"
    r"|diff you (?:were|are) given",
    re.IGNORECASE,
)

# MEASURED 2026-10-26: an opencode run that was blocked by a permission
# prompt produced a think block and a tool call ending in "Error: The user
# rejected permission to use this specific tool call." -- and the fan-out
# scored it as a RETURNED reviewer, because the output had plenty of words.
# A reviewer that never got to answer confirms nothing.
_PERMISSION_DENIED = re.compile(
    r"user rejected permission"
    r"|rejected permission to use"
    r"|permission to use this specific tool call"
    r"|permission (?:was )?denied"
    r"|tool call (?:was )?(?:rejected|denied|blocked)"
    r"|requires (?:user )?approval",
    re.IGNORECASE,
)

# A permission BLOCK, as opposed to a permission MENTION.
#
# The runtime reports a rejected tool call as its own error line:
#     Error: The user rejected permission to use this specific tool call.
# A reviewer that is EXPLAINING this tool quotes that text inside a
# sentence, not as a line that starts with "Error:". The distinction matters
# because a block must beat the marker (a blocked reviewer confirmed
# nothing) while a mention must not (quoting the tool must not silence the
# tool).
_PERMISSION_ERROR_LINE = re.compile(
    r"^[ \t>]*Error:[^\n]*\b(?:permission|denied|rejected|blocked)\b[^\n]*$",
    re.IGNORECASE | re.MULTILINE,
)

# A reviewer that merely MENTIONED a permission problem while explaining is
# caught by _PERMISSION_DENIED above; it is NOT a block. The block is the
# runtime error line.

# MEASURED 2026-10-26: DSH's MCP startup lines look like this. They are the
# tool talking about itself, not a reviewer answering the question.
_MCP_BANNER = re.compile(
    r"MCP\s+Server\s+running\s+on\s+\S+", re.IGNORECASE
)

# A verdict line must START the line (after optional markdown decoration).
# That anchoring is deliberate: a reviewer that echoes the instruction
# "the form 'VERDICT: YES' or 'VERDICT: NO'" does not start its line with
# VERDICT, so the echo cannot be mistaken for a verdict.
_VERDICT_LINE = re.compile(
    r"^[ \t>*#`\-]*(?P<marker>VERDICT)[ \t]*:(?P<rest>.*)$",
    re.IGNORECASE | re.MULTILINE,
)

# An AMBIGUOUS value is not a decision. "VERDICT: YES or NO" and "VERDICT:
# YES maybe" both start with a decision token and then say something that
# cancels it. The tail is checked only at the START of what follows the
# token, so an explanation ("VERDICT: NO - because ...") is unaffected.
_AMBIGUOUS_TAIL = re.compile(
    r"^[\s\-—:;,/|.]*"
    r"(?:or|and|but|maybe|perhaps|possibly|either|yes|no|y|n|verdict)\b",
    re.IGNORECASE,
)


def _blocked_by_permission(cleaned: str) -> bool:
    """Did this reviewer get BLOCKED, rather than merely mentioning a block?

    Three signals, in order of strength:

    1. the runtime's own error line ("Error: ... permission ...") -- that is
       a tool call the reviewer was not allowed to make;
    2. no verdict marker at all -- a blocked run never concluded;
    3. the permission complaint is the last thing it said -- the run ended
       on the complaint.

    A reviewer that is EXPLAINING this tool quotes the complaint inside a
    sentence and then concludes. That is not a block, and it must not be
    silenced.
    """
    complaint = _PERMISSION_DENIED.search(cleaned)
    if not complaint:
        return False
    if _PERMISSION_ERROR_LINE.search(cleaned):
        return True
    markers = list(_VERDICT_LINE.finditer(cleaned))
    if not markers:
        return True
    tail = cleaned[complaint.end():]
    tail = re.sub(r"^[ \t]*(?:MCP\s+Server\s+running\s+on\s+\S+[ \t]*\n?)*", "", tail)
    if not re.search(r"[A-Za-z]{2,}", tail):
        # The complaint is the last thing it said.
        return True
    return False


def _meaningful(text: str) -> str:
    """Strip terminal control codes and judge whether there is a real answer.

    `ollama run` paints a braille spinner to the same stream it writes the
    answer to. Non-empty stdout therefore proves NOTHING -- the first working
    run scored 5/5 with three of them returning pure spinner glyphs. Requiring
    actual words is the whole point: a reviewer that produced no prose has not
    approved anything.
    """
    cleaned = _ANSI.sub("", text or "")
    # Drop the spinner glyphs themselves (U+2800 braille block).
    cleaned = "".join(ch for ch in cleaned if not (0x2800 <= ord(ch) <= 0x28FF))
    return cleaned.strip()


def _extract_verdict(text: str) -> tuple[str | None, str | None]:
    """Find an explicit verdict line.

    Returns (verdict, bad_value). `verdict` is "YES"/"NO" when a marker line
    carries one of those values. `bad_value` is the text of a marker line
    whose value was not a decision, so the caller can say so precisely.

    Two rules, both measured:

    * the LAST marker decides -- the contract asks the reviewer to END its
      reply with the verdict, so a marker quoted earlier in the analysis does
      not override the one the reviewer actually concluded with;
    * markers with NOTHING between them are a MENU, not a choice, so they
      are ambiguous rather than a decision.
    """
    markers = list(_VERDICT_LINE.finditer(text))
    if not markers:
        return None, None

    # A MENU: two markers with no prose between them.
    for first, second in zip(markers, markers[1:]):
        between = text[first.end():second.start()].strip()
        between = between.strip("*`'\"").strip()
        if not between:
            return None, (
                "a LIST of options: two verdict markers with nothing between "
                "them, which is a menu rather than a decision"
            )

    bad: str | None = None
    for m in reversed(markers):
        rest = (m.group("rest") or "").strip().strip("*`'\" \t")
        word = re.match(r"[A-Za-z]+", rest)
        if not word:
            bad = f"not a decision ({rest or '(empty)'!r})"
            continue
        token = word.group(0).upper()
        if token not in (VERDICT_YES, VERDICT_NO):
            bad = f"not a decision ({rest!r})"
            continue
        tail = rest[word.end():]
        if _AMBIGUOUS_TAIL.match(tail):
            # "VERDICT: YES or NO", "VERDICT: YES maybe" -- a token that
            # cancels itself is not a decision.
            return None, (
                f"AMBIGUOUS ({rest!r}): it names a decision and then takes it "
                f"back"
            )
        return token, None
    return None, bad


def parse_verdict(text: str) -> tuple[str | None, str]:
    """Score a reviewer's output. Returns (verdict, reason).

    `verdict` is "YES", "NO" or None. It is not None ONLY when the output
    contains an explicit `VERDICT: YES` / `VERDICT: NO` line. Everything else
    -- spinner glyphs, an MCP startup banner, a permission rejection, a
    request for the question, a truncated-diff complaint, or plain prose with
    no verdict -- is None, and the reason says why.

    The rule this encodes: a NO VERDICT is never approval.

    THE MARKER IS AUTHORITATIVE. The blocker phrases (permission rejection,
    truncation complaint, request for the question) are only consulted when
    there is NO marker. MEASURED 2026-10-26: opencode, reviewing a diff of
    this very tool, explained itself by quoting this tool's own regexes --
    its output contained the literal text "truncat...diff" and "please
    provide the question" -- and then correctly ended with "VERDICT: NO".
    With the blockers checked first, that real verdict was thrown away and
    the reviewer was reported as NO VERDICT. A reviewer that is EXPLAINING
    this tool must not be silenced by this tool's own vocabulary.
    """
    cleaned = _meaningful(text)

    verdict, bad = _extract_verdict(cleaned)
    if verdict is not None:
        # A reviewer that was BLOCKED never reached a conclusion. The block
        # beats the marker -- otherwise a run that ended in "Error: The user
        # rejected permission to use this specific tool call." would be
        # recorded as an approval. A reviewer that merely QUOTED that text
        # while explaining the tool is not blocked, and keeps its verdict.
        if _blocked_by_permission(cleaned):
            return None, (
                "the reviewer was BLOCKED by a permission rejection and never "
                "answered -- NO VERDICT"
            )
        # The marker is authoritative -- UNLESS the reviewer used the
        # protocol to report that it could not see the whole change. A
        # verdict about a partial view is not a verdict on the change.
        if _PROTOCOL_TRUNCATED.search(cleaned):
            return None, (
                "the reviewer reported DIFF TRUNCATED, so it could not see "
                "the whole change -- NO VERDICT"
            )
        return verdict, f"VERDICT: {verdict}"
    if bad is not None:
        return None, f"the verdict marker is {bad} -- NO VERDICT"

    # No marker: the reviewer never answered in the required form.
    # A reviewer that was BLOCKED never answered, whatever else it printed.
    if _PERMISSION_DENIED.search(cleaned):
        return None, (
            "the reviewer was BLOCKED by a permission rejection and never "
            "answered -- NO VERDICT"
        )
    # It never saw all of the diff, so whatever it concluded is about a
    # partial view. That is not a verdict on the change.
    if _TRUNCATED.search(cleaned):
        return None, (
            "the reviewer reported the diff was TRUNCATED for it -- it "
            "reviewed only part of the change, so this is NO VERDICT"
        )
    # Full of words, but it asked for the question instead of answering it.
    if _NO_ANSWER.search(cleaned):
        return None, (
            "the reviewer ASKED FOR THE QUESTION instead of answering it "
            "-- NO VERDICT"
        )

    words = len(re.findall(r"[A-Za-z0-9]{2,}", cleaned))
    if words < 5:
        return None, (
            f"no usable output ({words} words after stripping control codes "
            f"and spinner glyphs) -- NO VERDICT"
        )
    if _MCP_BANNER.search(cleaned):
        return None, (
            "the output is a tool startup banner (MCP server announcement), "
            "not a review -- NO VERDICT"
        )
    return None, "no VERDICT: YES/NO line in the output -- NO VERDICT"


# A reviewer output can carry credentials. Never print them.
_SECRET_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_\-]{16,}"), r"\1-<redacted>"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"), "<redacted-gh-token>"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), "<redacted-slack-token>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<redacted-aws-key>"),
    (re.compile(r"\b(bearer)\s+[A-Za-z0-9._\-]{16,}", re.IGNORECASE),
     r"\1 <redacted>"),
    (
        re.compile(
            r"([\"']?(?:api[_-]?key|apikey|secret|token|password|passwd|pwd)"
            r"[\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9_\-\.]{12,})",
            re.IGNORECASE,
        ),
        r"\1<redacted>",
    ),
]


def redact(text: str) -> str:
    """Mask anything that looks like a credential before printing it."""
    out = text or ""
    for pattern, repl in _SECRET_PATTERNS:
        out = pattern.sub(repl, out)
    return out


# --------------------------------------------------------------------------
# Engine runners
# --------------------------------------------------------------------------

# (name, output, has_verdict)
ReviewOutcome = tuple[str, str, bool]


def _run(
    name: str,
    cmd: list[str],
    timeout: int = DEFAULT_TIMEOUT,
    diff_complete: bool = True,
) -> ReviewOutcome:
    """Run an engine that takes an argv list.

    `diff_complete` says whether this reviewer was handed the WHOLE change.
    When it was not, the run is scored NO VERDICT by the TOOL, whatever the
    reviewer said: a reviewer that saw part of the change cannot give a
    verdict on the change. The engine still runs, so the evidence is saved
    and a human can judge it -- but it is never counted.
    """
    resolved = _resolve_argv(cmd)
    if resolved is None:
        return name, (
            f"ENGINE FAILURE: {name} is genuinely not installed / not on "
            f"PATH -- NO VERDICT"
        ), False
    argv, how = resolved
    env = dict(os.environ)
    if "OLLAMA_HOST" in env or name == "ollama":
        # This box exports OLLAMA_HOST=0.0.0.0:11434. Ollama answers on
        # 127.0.0.1; the wildcard address makes the CLI drop connections under
        # parallel load and every ollama review came back `EOF`. Pin it per
        # subprocess rather than trusting the ambient environment.
        env["OLLAMA_HOST"] = "http://127.0.0.1:11434"
    if name == "hermes":
        # `hermes -z` is the only working form; there is no `hermes run`. It
        # refuses to read the system temp dir and wants a scratch dir under the
        # user's own tree, so give it one that exists rather than letting it
        # fail and be scored as "no verdict".
        home = Path(env.get("USERPROFILE") or env.get("HOME") or Path.home())
        scratch = home / "hermes" / "cache" / "scratch"
        try:
            scratch.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
    try:
        p = subprocess.run(
            argv,
            cwd=str(REPO),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
        raw = (p.stdout or "") + (p.stderr or "")
        out = _meaningful(raw)
        verdict, _reason = parse_verdict(out)
        # The FULL output is returned, never a truncated slice: the saved
        # file is the evidence, and a truncation that hides the tail of a
        # review is how a blocked run gets mistaken for an answer.
        if not diff_complete:
            return name, (
                f"PARTIAL DIFF: {name} was handed only part of the change, so "
                f"its answer is about a partial view -- NO VERDICT\n\n{out}"
            ), False
        return name, out, verdict is not None
    except FileNotFoundError:
        return name, (
            f"ENGINE FAILURE: launch failed (FileNotFoundError) via {how}; the "
            f"binary exists at {argv[0] if argv else '?'} but could not be "
            f"started -- NO VERDICT"
        ), False
    except subprocess.TimeoutExpired:
        # A reviewer that timed out has NOT approved anything.
        return name, f"ENGINE FAILURE: TIMED OUT after {timeout}s -- NO VERDICT", False
    except Exception as exc:
        return name, (
            f"ENGINE FAILURE: {name} runner crashed: {type(exc).__name__}: "
            f"{exc} -- NO VERDICT"
        ), False


def _run_dsh(
    prompt: str, timeout: int = DEFAULT_TIMEOUT, diff_complete: bool = True
) -> ReviewOutcome:
    """Run DSH with the diff INLINE.

    DEFECT 1 (measured 2026-10-26): DSH was handed `Diff at <path>`. It cannot
    open a path named in a prompt, so it returned only its MCP startup lines
    and no verdict -- while the fan-out counted it as having returned. The
    diff now travels INSIDE the prompt.

    Two launch details, both measured:

    * it is launched as `node <dsh>/lib/bin.js headless <prompt>` rather than
      through the `dsh.ps1` npm shim: the shim runs under `powershell -File`,
      whose argument parsing stops at the first newline and would silently
      truncate the inline diff to its first line;
    * the prompt travels on STDIN, not in argv. Windows caps a command line at
      32,767 characters and a 40,000-character argv prompt fails with
      "[WinError 206] The filename or extension is too long". `dsh headless`
      reads its prompt from stdin correctly, so stdin is how a diff of any
      size reaches it.
    """
    entry = resolve_dsh_entry()
    node = resolve_node()
    if entry is None:
        return "dsh", (
            "ENGINE FAILURE: dsh entry point not found (expected "
            ".../dsh/lib/bin.js) -- NO VERDICT"
        ), False
    if node is None:
        return "dsh", "ENGINE FAILURE: node is not on PATH -- NO VERDICT", False

    argv = [node, str(entry), "headless"]
    try:
        p = subprocess.run(
            argv,
            input=prompt,
            cwd=str(REPO),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        raw = (p.stdout or "") + (p.stderr or "")
        out = _meaningful(raw)
        verdict, _reason = parse_verdict(out)
        if not diff_complete:
            return "dsh", (
                "PARTIAL DIFF: dsh was handed only part of the change, so its "
                "answer is about a partial view -- NO VERDICT\n\n" + out
            ), False
        return "dsh", out, verdict is not None
    except subprocess.TimeoutExpired:
        return "dsh", f"ENGINE FAILURE: TIMED OUT after {timeout}s -- NO VERDICT", False
    except Exception as exc:
        return "dsh", (
            f"ENGINE FAILURE: dsh runner crashed: {type(exc).__name__}: {exc} "
            f"-- NO VERDICT"
        ), False


def _run_opencode(
    message: str,
    pack: Path,
    attach: Path,
    timeout: int = OPENCODE_TIMEOUT,
    diff_complete: bool = True,
) -> ReviewOutcome:
    """Run opencode against an ISOLATED evidence pack.

    DEFECT 3 (measured 2026-10-26): opencode was launched with the repository
    as its working directory. It read the diff in 700-line chunks, then grepped
    the whole 32 MB / 1,337-file tree and blew past the provider timeout --
    six attempts at a 2400s cap produced no answer. Pointed at a scratch
    directory holding only the diff plus curated excerpts, it answers in
    80-137s and returns a well-formed verdict every time.

    The recipe that works, encoded here:
      * the standalone `opencode.exe`, launched with a list argv (the `.cmd`
        shim goes through `cmd /c` and mangles arguments containing newlines);
      * the MESSAGE BEFORE `-f` (`-f` is an array flag);
      * `--auto`, or its first tool call is auto-rejected;
      * `--dir <pack>`, never the repository;
      * a 300s cap, not 2400s;
      * UTF-8, no BOM, on every file handed to it.
    """
    exe = resolve_opencode_exe()
    if exe is None:
        return "opencode", (
            "ENGINE FAILURE: the standalone opencode.exe was not found (set "
            "OPENCODE_EXE) -- NO VERDICT"
        ), False

    argv = [
        str(exe),
        "run",
        "--auto",
        "--dir",
        str(pack),
        message,
        "-f",
        str(attach),
    ]
    try:
        p = subprocess.run(
            argv,
            cwd=str(pack),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        raw = (p.stdout or "") + (p.stderr or "")
        out = _meaningful(raw)
        verdict, _reason = parse_verdict(out)
        if not diff_complete:
            return "opencode", (
                "PARTIAL DIFF: opencode was handed only part of the change, so "
                "its answer is about a partial view -- NO VERDICT\n\n" + out
            ), False
        return "opencode", out, verdict is not None
    except subprocess.TimeoutExpired:
        return "opencode", (
            f"ENGINE FAILURE: TIMED OUT after {timeout}s -- NO VERDICT"
        ), False
    except Exception as exc:
        return "opencode", (
            f"ENGINE FAILURE: opencode runner crashed: {type(exc).__name__}: "
            f"{exc} -- NO VERDICT"
        ), False


# --------------------------------------------------------------------------
# Building the panel
# --------------------------------------------------------------------------


def dsh_prompt_parts(question: str) -> tuple[str, str]:
    """The framing that goes AROUND the inline diff for DSH.

    The protocol comes FIRST, so it is still there if the engine truncates
    the prompt.
    """
    prefix = (
        f"{question}\n\n"
        f"{VERDICT_INSTRUCTION}\n\n"
        f"{TRUNCATION_PROTOCOL}\n\n"
        f"Review THIS diff, pasted inline. If it carries a TRUNCATED marker, "
        f"it is incomplete -- reply exactly 'DIFF TRUNCATED' and do not "
        f"answer.\n\n"
        f"```diff\n"
    )
    suffix = (
        "\n```\n\n"
        f"Answer only from the diff above. If it does not contain enough "
        f"to answer, say exactly that."
    )
    return prefix, suffix


def dsh_prompt(question: str, diff_text: str) -> str:
    """The prompt handed to DSH. The diff travels INSIDE it.

    DEFECT 1 (measured 2026-10-26): DSH was handed `Diff at <path>`. It
    cannot open a path named in a prompt, so it returned only its MCP startup
    lines and no verdict -- while the fan-out counted it as having returned.
    """
    prefix, suffix = dsh_prompt_parts(question)
    return prefix + diff_text + suffix


def opencode_message(question: str) -> str:
    """The message handed to opencode.

    One line: the diff is attached as a file and lives in the isolated pack,
    so the message does not need newlines -- and a single-line argument cannot
    be mangled by anything that shells out.
    """
    return " ".join(
        [
            question,
            VERDICT_INSTRUCTION,
            "The change under review is attached as review.diff and is also "
            "in this directory; read review.diff FIRST.",
            "Supporting excerpts of the touched files are under context/.",
            "Answer in at most 20 lines, using only files in this directory.",
        ]
    )


def reviewers(
    diff: Path, question: str, include_paid: bool = False
) -> dict[str, Callable[[], ReviewOutcome]]:
    """Build the reviewer set.

    Cost matters here. The DEFAULT set is the free/local tier only:

      ollama  - local model, zero tokens, zero subscription
      hermes  - local, zero tokens, zero subscription
      dsh     - no cloud key configured on this box
      opencode- resolves without a cloud key here

    Copilot is the one paid engine on this box, and it stays opt-in behind
    --with-paid so that a routine safety review never quietly bills anything.
    """
    diff_s = str(diff)

    # A MULTI-LINE question is truncated at its first newline for the
    # reviewers that are PowerShell wrappers: they are launched as
    # `powershell -NoProfile -File <script.ps1> <question>`, and PowerShell's
    # -File argument parsing stops the argument at the newline. Collapse the
    # question onto ONE line so every reviewer receives all of it.
    question = " ".join(question.split())

    full_text = diff.read_text(encoding="utf-8", errors="replace")

    # Every prompt is assembled HERE, not inside the lambda, and every
    # prompt is assembled against a BUDGET: the reviewer receives the whole
    # argument -- framing, instructions and diff -- so the diff gets what is
    # left after the framing. A prompt that would exceed the budget is cut,
    # the cut is MARKED, and the run is scored NO VERDICT by the TOOL: a
    # reviewer that saw part of the change cannot give a verdict on the
    # change.
    #
    # A NameError in a prompt must also surface here, not 90 seconds later
    # inside a subprocess call: MEASURED 2026-10-26, a typo in the hermes
    # prompt made hermes fail with "NameError: name 'hermes_truncated' is
    # not defined", which the panel honestly reported as NO VERDICT but
    # which cost a whole reviewer slot.
    ollama_prefix = (
        f"{question}\n\n"
        f"{VERDICT_INSTRUCTION}\n\n"
        f"{TRUNCATION_PROTOCOL}\n\n"
        f"Review THIS diff, pasted inline. If it carries a TRUNCATED marker, "
        f"it is incomplete -- reply exactly 'DIFF TRUNCATED' and do not "
        f"answer.\n\n"
        f"```diff\n"
    )
    ollama_suffix = (
        "\n```\n\n"
        f"Answer only from the diff above. If it does not contain enough "
        f"to answer, say exactly that."
    )
    ollama_prompt, ollama_complete = assemble_prompt(
        ollama_prefix, ollama_suffix, full_text, OLLAMA_PROMPT_LIMIT
    )

    hermes_prefix = (
        f"{question}\n\n"
        f"{VERDICT_INSTRUCTION}\n\n"
        f"{TRUNCATION_PROTOCOL}\n\n"
        f"The COMPLETE diff is on disk at {diff_s}. If the inline copy below "
        f"carries a TRUNCATED marker, read that file instead of relying on "
        f"the copy.\n\n"
        f"Review THIS diff, pasted inline:\n\n"
        f"```diff\n"
    )
    hermes_suffix = (
        "\n```\n\n"
        f"Answer only from the diff above. If the inline copy is truncated, "
        f"say exactly 'DIFF TRUNCATED' and do not answer about a different "
        f"file."
    )
    hermes_prompt, hermes_complete = assemble_prompt(
        hermes_prefix, hermes_suffix, full_text, HERMES_PROMPT_LIMIT
    )

    dsh_prefix, dsh_suffix = dsh_prompt_parts(question)
    dsh_prompt_text, dsh_complete = assemble_prompt(
        dsh_prefix, dsh_suffix, full_text, DSH_PROMPT_LIMIT
    )

    jobs: dict[str, Callable[[], ReviewOutcome]] = {}

    ollama_argv = ["ollama", "run", OLLAMA_MODEL, ollama_prompt]
    jobs["ollama"] = lambda: _run(
        "ollama", ollama_argv, diff_complete=ollama_complete
    )

    hermes_argv = ["hermes", "-z", hermes_prompt]
    jobs["hermes"] = lambda: _run(
        "hermes", hermes_argv, diff_complete=hermes_complete
    )

    # DEFECT 1 FIX: the diff travels inside the prompt.
    jobs["dsh"] = lambda: _run_dsh(dsh_prompt_text, diff_complete=dsh_complete)

    # DEFECT 3 FIX: an ISOLATED evidence pack, never the repository. The
    # diff reaches opencode as a FILE, so it is never truncated by a prompt
    # budget -- this engine always sees the whole change.
    pack = build_opencode_pack(diff, REPO)
    attach = pack / "review.diff"
    oc_message = opencode_message(question)
    jobs["opencode"] = lambda: _run_opencode(
        oc_message, pack, attach, OPENCODE_TIMEOUT
    )

    if include_paid:
        copilot_argv = [
            "copilot",
            "-p",
            f"{question} {VERDICT_INSTRUCTION} The diff under review is at "
            f"{diff_s}; read that file. Repo root: {REPO}.",
        ]
        jobs["copilot"] = lambda: _run("copilot", copilot_argv)
    return jobs


def run_all(jobs: dict[str, Callable[[], ReviewOutcome]]) -> dict[str, ReviewOutcome]:
    results: dict[str, ReviewOutcome] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as pool:
        futures = {name: pool.submit(job) for name, job in jobs.items()}
        for name, fut in futures.items():
            try:
                results[name] = fut.result()
            except Exception as exc:
                results[name] = (
                    name,
                    f"ENGINE FAILURE: {name} runner crashed: "
                    f"{type(exc).__name__}: {exc} -- NO VERDICT",
                    False,
                )
    return results


def assemble_prompt(
    prefix: str, suffix: str, diff_text: str, limit: int
) -> tuple[str, bool]:
    """Assemble prefix + diff + suffix so the WHOLE prompt fits `limit`.

    Returns (prompt, diff_complete).

    The budget belongs to the WHOLE argument the reviewer receives -- framing,
    instructions and diff together -- not to the diff alone. A reviewer's
    CLI sees the whole thing, so the diff gets what is LEFT after the
    framing.

    When the diff does not fit, it is cut and the cut is MARKED in the
    prompt, and `diff_complete` is False. The caller must then score the
    run NO VERDICT: a reviewer that saw part of the change cannot give a
    verdict on the change, and the tool must not leave that to the
    reviewer's diligence.
    """
    reserve = 220  # room for the truncation marker itself
    budget = limit - len(prefix) - len(suffix) - reserve
    if budget < 0:
        budget = 0
    if len(diff_text) <= budget:
        return prefix + diff_text + suffix, True
    marker = (
        f"\n\n... [TRUNCATED: only the first {budget} of {len(diff_text)} "
        f"characters of this diff are shown. Do NOT answer about a partial "
        f"view.] ...\n"
    )
    prompt = prefix + diff_text[:budget] + marker + suffix
    if len(prompt) > limit:
        over = len(prompt) - limit
        prompt = prefix + diff_text[: max(0, budget - over)] + marker + suffix
    return prompt, False


def _no_verdict_reason(out: str) -> str:
    """Why this reviewer has no verdict.

    An engine-level failure diagnostic already explains itself; anything else
    is scored by the parser.
    """
    if out.startswith("ENGINE FAILURE:") or out.startswith("PARTIAL DIFF:"):
        return out.splitlines()[0]
    _, reason = parse_verdict(out)
    return reason


def format_summary(results: dict[str, ReviewOutcome]) -> str:
    """The honest tally. Every reviewer appears, and NO VERDICT is named.

    A triage-only reviewer (see ADVISORY_REVIEWERS) is NEVER counted as a
    verdict: it is listed separately so the headline number cannot read like
    an approval from a model that cannot see the diff.
    """
    total = len(results)
    # Every advisory reviewer is excluded from BOTH the numerator and the
    # denominator: it is triage, and a tally that counted it would read like
    # an approval from a model that cannot see the diff.
    advisory = sorted(n for n in results if n in ADVISORY_REVIEWERS)
    counted = sorted(
        n for n, (_e, _o, ok) in results.items()
        if ok and n not in ADVISORY_REVIEWERS
    )
    triage = sorted(
        n for n, (_e, _o, ok) in results.items()
        if ok and n in ADVISORY_REVIEWERS
    )
    missing = sorted(n for n, (_e, _o, ok) in results.items() if not ok)
    non_triage_total = total - len(advisory)
    verdicts: list[str] = []
    for name, (_engine, out, ok) in results.items():
        if not ok or name in ADVISORY_REVIEWERS:
            continue
        verdict, _ = parse_verdict(out)
        if verdict:
            verdicts.append(f"{name}={verdict}")
    lines = [
        f"reviewers that returned a VERDICT: {len(counted)}/{non_triage_total}  "
        f"{counted}",
    ]
    if triage:
        lines.append(
            "triage only, NOT counted as a verdict: " + ", ".join(triage)
        )
    if verdicts:
        lines.append("verdicts: " + ", ".join(sorted(verdicts)))
    if missing:
        lines.append(
            "NO VERDICT (never approval): " + ", ".join(missing)
        )
    else:
        lines.append("NO VERDICT (never approval): none")
    lines.append("A NO VERDICT is never approval.")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--question", default=DEFAULT_QUESTION)
    ap.add_argument(
        "--base",
        default=None,
        help="git ref to review against, e.g. origin/main. Reviews the committed "
        "branch plus working-tree changes. Without it, only uncommitted work is "
        "reviewed -- which is empty right after a commit.",
    )
    ap.add_argument(
        "--with-paid",
        action="store_true",
        help="also run the paid engine (Copilot). Off by default so a routine "
        "review never spends another plan's budget.",
    )
    args = ap.parse_args()

    # Reviewers emit UTF-8 (Ollama alone produces braille spinner glyphs).
    # The Windows console defaults to cp1252 and raises UnicodeEncodeError on
    # print -- which killed the fan-out AFTER the reviews had run. Reconfigure
    # stdout/stderr so a successful review cannot crash the reporter.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    os.environ["PATH"] = os.environ.get("PATH", "")
    # hermes refuses the system temp dir.
    os.environ.setdefault("HOME", os.path.expanduser("~"))

    diff = export_diff(args.base)
    size = diff.stat().st_size
    print(f"[review] diff exported: {diff} ({size} bytes, UTF-8)")
    if size == 0:
        print("[review] nothing to review -- working tree is clean.")
        return 0

    started = time.time()
    jobs = reviewers(diff, args.question, include_paid=args.with_paid)
    tier = (
        "FREE (local / no cloud key)"
        if not args.with_paid
        else "INCLUDING PAID SUBSCRIPTION REVIEWERS"
    )
    print(f"[review] reviewer tier: {tier}")
    print(f"[review] fanning out to {len(jobs)}: {', '.join(jobs)}\n")

    results = run_all(jobs)

    print("=" * 70)
    for name, (_engine, out, ok) in results.items():
        verdict, _ = parse_verdict(out)
        if ok:
            flag = f"VERDICT: {verdict}"
            reason = ""
        else:
            flag = "NO VERDICT"
            reason = _no_verdict_reason(out)
        if name in ADVISORY_REVIEWERS:
            flag += " / TRIAGE ONLY - NOT A VERDICT"
        print(f"\n--- {name}  [{flag}] ---")
        if reason:
            print(f"[{name}] {reason}")
        body = out.strip()
        print(redact(body[-1800:]) if body else "(empty response)")
        OUT_DIR.mkdir(exist_ok=True)
        # The saved file is the EVIDENCE: the full output, plus the reason
        # when there is no verdict. Never a truncated slice.
        saved = out if ok else f"{reason}\n\n{out}"
        (OUT_DIR / f"{name}.txt").write_text(redact(saved), encoding="utf-8")

    print("\n" + "=" * 70)
    print(format_summary(results))
    print(f"elapsed: {time.time() - started:.1f}s")
    print(f"full outputs: {OUT_DIR}")
    print(
        "\nReminder: check each verdict against the code before acting on it. "
        "A reviewer handed the answer in prose confirms your arithmetic, not "
        "reality."
    )
    print(
        "\nTRIAGE ONLY (see ADVISORY_REVIEWERS): the local ollama model was "
        "measured scoring CORRECTLY on a diff and CORRECTLY with no diff at "
        "all. Its accuracy is indistinguishable from a coin flip, so it is "
        "reported but never counted as approval. A reviewer that agrees with "
        "you about a diff it cannot see is worse than no reviewer."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
