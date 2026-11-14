# tests/test_deployed_commit_public_ref.py
"""The deploy stamp must name the PUBLIC repository's MAIN branch.

WHAT THIS FILE PROVES
---------------------
`ops/scripts/record_deployed_commit.sh` writes `DEPLOYED_COMMIT=<sha>` into
`.env`, and that value is what the source offer publishes as "the source of
the running build":
  https://github.com/DiverseInc/DepthSight/archive/<commit>.tar.gz
and `frontend/source.html` says the running service is deployed from the
`main` branch of THAT repository.

So the stamp is only true when all of the following hold, and the script
refuses (exit 1, nothing written) when any of them does not:

  (a) the commit is in the history of `refs/remotes/origin/main` -- that one
      ref -- and not merely contained by some other remote-tracking branch
      of some other remote;
  (b) `origin` IS the public repository the offer names, in any of the url
      forms a real checkout uses (https, scp-style ssh, ssh://), with or
      without the trailing `.git`;
  (c) the LIVE remote agrees: `git ls-remote origin main` has to answer, it
      has to have a `main` branch, and the commit has to be an ancestor of
      it. A remote that cannot be reached is a REFUSAL, never an assumption;
  (d) the refusals that already existed still happen, exactly as they did:
      a dirty checkout, a missing `.env` (which is NOT created);
  (e) the success path still writes `DEPLOYED_COMMIT=<full sha>`, idempotently.

HOW IT IS PROVED
----------------
By running the REAL script, as bytes, under a real bash, in a throwaway git
repository whose `origin` is a REAL bare repository. No network is touched:
the fixture DECLARES the public url for `origin` and adds an `insteadOf`
rewrite that maps that url onto the local bare repository, so the script's
own check runs against the real thing while the transport stays local.

EVERY assertion below is BEHAVIOURAL: the exit code, the text of the
refusal on stderr, and the bytes left in `.env`. Nothing here greps the
script's source -- a source-string check cannot tell whether the script
REFUSED, and refusing is the whole point.

The commit under test is really pushed to the bare repository, because the
script refuses to stamp a commit that is not published: a repository with no
remote would be refused for the wrong reason and every "it stamps
correctly" test would be testing the refusal instead of the stamp.
"""

import base64
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "ops" / "scripts" / "record_deployed_commit.sh"

# The ONE repository the AGPL-3.0 section 13 offer names, in the url forms a
# real checkout uses. All six of these have to be accepted; see the tests.
PUBLIC_HTTPS = "https://github.com/DiverseInc/DepthSight.git"
PUBLIC_HTTPS_NO_GIT = "https://github.com/DiverseInc/DepthSight"
PUBLIC_SCP = "git@github.com:DiverseInc/DepthSight.git"
PUBLIC_SCP_NO_GIT = "git@github.com:DiverseInc/DepthSight"
PUBLIC_SSH_URL = "ssh://git@github.com/DiverseInc/DepthSight.git"
PUBLIC_SSH_URL_NO_GIT = "ssh://git@github.com/DiverseInc/DepthSight"

# The sentence every refusal of this script ends with. A refusal that does
# not say that it changed nothing is not an actionable refusal.
NOTHING_CHANGED = "NOTHING HAS BEEN CHANGED OR DELETED BY THIS SCRIPT."


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _bash() -> str:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available on this machine")
    return bash


def _script_bytes() -> bytes:
    assert SCRIPT.is_file(), f"the deploy stamp script is missing: {SCRIPT}"
    return SCRIPT.read_bytes()


def _drive(
    setup: str = "",
    origin_url: str | None = PUBLIC_HTTPS,
    publish: str = "published",
) -> dict:
    """Run the REAL script in a throwaway git repository and report what happened.

    The script is fed to bash over STDIN as BYTES: as a command-line
    argument the Windows->WSL launcher re-quotes it and the quoting does not
    survive, and in text mode Windows rewrites every "\\n" to "\\r\\n" on the
    way into the pipe, so bash then sees a carriage return on every line.

    Everything the script touches lives in a temp dir INSIDE the shell, so
    nothing depends on how the host translates paths. The script runs in a
    SUBSHELL: it calls `exit 1` when it refuses, and that must be the
    script's exit code, not the shell's.

    `origin_url` is the url DECLARED for origin. It is also the url the
    `insteadOf` rewrite maps onto the local bare repository, so the transport
    stays local and the script's own check runs against the declared url.

    `publish` says how the commit under test relates to the remote:
      * "published"         -- HEAD is pushed to the remote's main branch;
      * "not-on-main"       -- HEAD is pushed to the remote's `feature`
                               branch only, so it IS published but NOT on
                               main;
      * "other-remote"      -- HEAD is published on a SECOND remote's main
                               branch only, so the commit is contained by a
                               remote-tracking branch of ANOTHER remote;
      * "stale-local-cache" -- the remote's main already contains HEAD, but
                               the LOCAL copy of origin/main has not been
                               refreshed since. HEAD is on the remote's
                               `feature` branch as well, so the old check
                               (`git branch -r --contains HEAD`) is
                               satisfied and the refusal is really about
                               `main`;
      * "stale-remote"      -- the local copy of origin/main contains HEAD,
                               but the remote's main has been rewound and no
                               longer contains it;
      * "unreachable"       -- the remote repository is gone, so it cannot be
                               asked anything;
      * "no-main-on-remote" -- the remote answers, but has no `main` branch;
      * "unpushed"          -- HEAD was never pushed at all.
    """
    body = (
        "set -u\n"
        'WORK=$(mktemp -d) || exit 1\n'
        'ORIGIN_BASE=$(mktemp -d) || exit 1\n'
        "trap 'rm -rf \"$WORK\" \"$ORIGIN_BASE\"' EXIT\n"
        'cd "$WORK" || exit 1\n'
        "git init -q . >/dev/null 2>&1\n"
        "git config user.email t@example.com\n"
        "git config user.name t\n"
        "printf 'print(1)\\n' > app.py\n"
        "printf 'services: {}\\n' > docker-compose.yml\n"
        "printf '.env\\n' > .gitignore\n"
        "git add -A >/dev/null 2>&1\n"
        "git commit -qm first >/dev/null 2>&1\n"
        'git init -q --bare "$ORIGIN_BASE/origin.git" >/dev/null 2>&1\n'
        "FIRST=$(git rev-parse HEAD)\n"
    )
    if origin_url is not None:
        body += (
            f'git remote add origin "{origin_url}"\n'
            'git config "url.$ORIGIN_BASE/origin.git.insteadOf" '
            f'"{origin_url}"\n'
        )
    if origin_url is not None:
        if publish == "published":
            body += (
                "git push -q origin HEAD:refs/heads/main >/dev/null 2>&1\n"
                "git fetch -q origin >/dev/null 2>&1\n"
            )
        elif publish == "not-on-main":
            body += (
                "git push -q origin HEAD:refs/heads/main >/dev/null 2>&1\n"
                "git fetch -q origin >/dev/null 2>&1\n"
                "printf 'print(2)\\n' > app.py\n"
                "git commit -qam second >/dev/null 2>&1\n"
                "git push -q origin HEAD:refs/heads/feature >/dev/null 2>&1\n"
                "git fetch -q origin >/dev/null 2>&1\n"
            )
        elif publish == "other-remote":
            body += (
                "git push -q origin HEAD:refs/heads/main >/dev/null 2>&1\n"
                "git fetch -q origin >/dev/null 2>&1\n"
                'git init -q --bare "$ORIGIN_BASE/other.git" >/dev/null 2>&1\n'
                'git remote add other "$ORIGIN_BASE/other.git"\n'
                "git push -q other HEAD:refs/heads/main >/dev/null 2>&1\n"
                "git fetch -q other >/dev/null 2>&1\n"
                "printf 'print(2)\\n' > app.py\n"
                "git commit -qam second >/dev/null 2>&1\n"
                "git push -q other HEAD:refs/heads/main >/dev/null 2>&1\n"
                "git fetch -q other >/dev/null 2>&1\n"
            )
        elif publish == "stale-local-cache":
            # The remote's main ALREADY contains the commit, but the LOCAL
            # copy of origin/main has not been refreshed since.
            #
            # `git push` updates the remote-tracking ref opportunistically,
            # so the local copy is put back where it was by hand. The commit
            # is also on the remote's `feature` branch, so the OLD check
            # (`git branch -r --contains HEAD`) is satisfied and the refusal
            # really is about `main` and not about "never pushed".
            body += (
                "git push -q origin HEAD:refs/heads/main >/dev/null 2>&1\n"
                "git fetch -q origin >/dev/null 2>&1\n"
                "printf 'print(2)\\n' > app.py\n"
                "git commit -qam second >/dev/null 2>&1\n"
                "git push -q origin HEAD:refs/heads/feature >/dev/null 2>&1\n"
                "git push -q origin HEAD:refs/heads/main >/dev/null 2>&1\n"
                'git update-ref refs/remotes/origin/main "$FIRST"\n'
            )
        elif publish == "stale-remote":
            body += (
                "git push -q origin HEAD:refs/heads/main >/dev/null 2>&1\n"
                "git fetch -q origin >/dev/null 2>&1\n"
                "printf 'print(2)\\n' > app.py\n"
                "git commit -qam second >/dev/null 2>&1\n"
                "git push -q origin HEAD:refs/heads/main >/dev/null 2>&1\n"
                "git fetch -q origin >/dev/null 2>&1\n"
                'git --git-dir="$ORIGIN_BASE/origin.git" update-ref '
                'refs/heads/main "$FIRST"\n'
            )
        elif publish == "unreachable":
            body += (
                "git push -q origin HEAD:refs/heads/main >/dev/null 2>&1\n"
                "git fetch -q origin >/dev/null 2>&1\n"
                'rm -rf "$ORIGIN_BASE/origin.git"\n'
            )
        elif publish == "no-main-on-remote":
            body += (
                "git push -q origin HEAD:refs/heads/main >/dev/null 2>&1\n"
                "git fetch -q origin >/dev/null 2>&1\n"
                'git --git-dir="$ORIGIN_BASE/origin.git" update-ref -d '
                "refs/heads/main\n"
            )
        elif publish == "unpushed":
            body += (
                "git push -q origin HEAD:refs/heads/main >/dev/null 2>&1\n"
                "git fetch -q origin >/dev/null 2>&1\n"
                "printf 'print(2)\\n' > app.py\n"
                "git commit -qam second >/dev/null 2>&1\n"
            )
        else:
            raise ValueError(f"unknown publish mode: {publish}")

    body += setup
    body += "SHA=$(git rev-parse HEAD)\n"
    body += "(\n" + _script_bytes().decode("utf-8") + "\n)\n"
    body += (
        "rc=$?\n"
        "printf 'RC=%s\\n' \"$rc\"\n"
        "if [ -f \"$WORK/.env\" ]; then\n"
        "  printf 'ENV=%s\\n' \"$(base64 \"$WORK/.env\" | tr -d '\\n')\"\n"
        "else\n"
        "  printf 'ENV=MISSING\\n'\n"
        "fi\n"
        "printf 'SHA=%s\\n' \"$SHA\"\n"
        "printf 'FIRST=%s\\n' \"$FIRST\"\n"
        "printf 'LOCAL_MAIN=%s\\n' "
        "\"$(git rev-parse refs/remotes/origin/main 2>/dev/null || true)\"\n"
        "printf 'REMOTE_MAIN=%s\\n' \"$(git ls-remote origin main 2>/dev/null "
        "| awk '$2 == \"refs/heads/main\" { print $1 }')\"\n"
    )

    proc = subprocess.run(
        [_bash(), "-s"],
        input=body.encode("utf-8"),
        capture_output=True,
        timeout=300,
    )
    out = proc.stdout.decode("utf-8", errors="replace")
    err = proc.stderr.decode("utf-8", errors="replace")
    fields = {}
    for line in out.splitlines():
        key, _sep, value = line.partition("=")
        if key in ("RC", "ENV", "SHA", "FIRST", "LOCAL_MAIN", "REMOTE_MAIN"):
            fields[key] = value
    env_b64 = fields.get("ENV", "MISSING")
    return {
        "rc": int(fields.get("RC", "255")),
        "stdout": out,
        "stderr": err,
        "env": b"" if env_b64 == "MISSING" else base64.b64decode(env_b64),
        "env_exists": env_b64 != "MISSING",
        "sha": fields.get("SHA", ""),
        "first": fields.get("FIRST", ""),
        "local_main": fields.get("LOCAL_MAIN", ""),
        "remote_main": fields.get("REMOTE_MAIN", ""),
    }


ENV_LINE = "printf 'POSTGRES_PASSWORD=secret\\n' > .env\n"


# --------------------------------------------------------------------------
# (b) `origin` has to BE the public repository the offer names
# --------------------------------------------------------------------------
class TestTheOriginRemoteMustBeThePublicRepository:
    @pytest.mark.parametrize(
        "url",
        [
            PUBLIC_HTTPS,
            PUBLIC_HTTPS_NO_GIT,
            PUBLIC_SCP,
            PUBLIC_SCP_NO_GIT,
            PUBLIC_SSH_URL,
            PUBLIC_SSH_URL_NO_GIT,
        ],
    )
    def test_every_accepted_form_of_the_public_url_is_accepted(self, url):
        """https and ssh, with and without the trailing `.git`, are all the
        same repository and all have to be accepted."""
        result = _drive(ENV_LINE, origin_url=url)
        assert result["rc"] == 0, (url, result["stderr"])
        assert f"DEPLOYED_COMMIT={result['sha']}\n".encode() in result["env"]

    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/DiverseInc/DepthSight-fork.git",
            "https://github.com/DiverseInc/Other.git",
            "https://github.com/SomeoneElse/DepthSight.git",
            "https://gitlab.com/DiverseInc/DepthSight.git",
            "git@github.com:SomeoneElse/DepthSight.git",
            "git@github.com:DiverseInc/DepthSight-fork.git",
            "ssh://git@github.com/SomeoneElse/DepthSight.git",
            "/srv/local/mirror.git",
            "file:///srv/local/mirror.git",
        ],
    )
    def test_any_other_url_is_refused(self, url):
        """A private fork, another project, another host: the archive url the
        offer publishes for the stamp would be a 404."""
        result = _drive(ENV_LINE, origin_url=url)
        assert result["rc"] == 1, (url, result["stdout"], result["stderr"])
        assert "not the public repository" in result["stderr"], (
            f"the refusal must say the origin is not the public repository; "
            f"it said: {result['stderr']!r}"
        )
        assert NOTHING_CHANGED in result["stderr"]

    def test_the_refusal_names_the_offending_url(self):
        """A refusal the operator cannot act on is not actionable."""
        result = _drive(
            ENV_LINE, origin_url="https://github.com/DiverseInc/DepthSight-fork.git"
        )
        assert result["rc"] == 1
        assert "DepthSight-fork" in result["stderr"], (
            f"the refusal must show which url it refused: {result['stderr']!r}"
        )

    def test_the_refusal_of_another_url_writes_nothing(self):
        result = _drive(
            ENV_LINE, origin_url="https://github.com/DiverseInc/DepthSight-fork.git"
        )
        assert result["rc"] == 1
        assert result["env"] == b"POSTGRES_PASSWORD=secret\n", (
            "the refusal rewrote the environment file"
        )
        assert b"DEPLOYED_COMMIT=" not in result["env"]

    def test_a_checkout_with_no_origin_remote_at_all_is_refused(self):
        """There is no repository to check the commit against, so nothing may
        be promised about it."""
        result = _drive(ENV_LINE, origin_url=None)
        assert result["rc"] == 1, (result["stdout"], result["stderr"])
        assert "no 'origin' remote" in result["stderr"], (
            f"the refusal must say there is no origin remote; it said: "
            f"{result['stderr']!r}"
        )
        assert NOTHING_CHANGED in result["stderr"]

    def test_the_refusal_of_a_checkout_with_no_origin_writes_nothing(self):
        result = _drive(ENV_LINE, origin_url=None)
        assert result["rc"] == 1
        assert result["env"] == b"POSTGRES_PASSWORD=secret\n"
        assert b"DEPLOYED_COMMIT=" not in result["env"]


# --------------------------------------------------------------------------
# (a) the commit has to be on `main` -- not on some other branch or remote
# --------------------------------------------------------------------------
class TestTheCommitHasToBeOnTheMainBranch:
    def test_a_commit_on_the_public_main_branch_is_stamped(self):
        result = _drive(ENV_LINE)
        assert result["rc"] == 0, result["stderr"]
        assert result["remote_main"] == result["sha"], (
            "the fixture did not publish HEAD on main, so this test would not "
            "be testing what it claims to test"
        )
        assert f"DEPLOYED_COMMIT={result['sha']}\n".encode() in result["env"]

    def test_a_commit_published_on_another_branch_is_refused(self):
        """The old check accepted ANY remote-tracking branch that contained
        the commit. `origin/feature` is such a branch, and the offer says the
        running service comes from `main`."""
        result = _drive(ENV_LINE, publish="not-on-main")
        assert result["rc"] == 1, (result["stdout"], result["stderr"])
        assert "not on the main branch" in result["stderr"], (
            f"the refusal must say the commit is not on main; it said: "
            f"{result['stderr']!r}"
        )
        assert NOTHING_CHANGED in result["stderr"]

    def test_a_commit_published_only_on_another_remote_is_refused(self):
        """A commit contained by `other/main` is published too -- but the
        offer publishes an archive url on the PUBLIC repository, so a commit
        that only exists on a second remote is not on the public main."""
        result = _drive(ENV_LINE, publish="other-remote")
        assert result["rc"] == 1, (result["stdout"], result["stderr"])
        assert "not on the main branch" in result["stderr"], (
            f"the refusal must say the commit is not on the public main; it "
            f"said: {result['stderr']!r}"
        )
        assert NOTHING_CHANGED in result["stderr"]

    def test_the_refusal_of_a_commit_off_main_writes_nothing(self):
        result = _drive(ENV_LINE, publish="not-on-main")
        assert result["rc"] == 1
        assert result["env"] == b"POSTGRES_PASSWORD=secret\n"
        assert b"DEPLOYED_COMMIT=" not in result["env"]

    def test_a_commit_the_local_cache_has_not_caught_up_with_is_refused(self):
        """The LIVE remote's main already contains the commit, but the local
        copy of `origin/main` has not been refreshed since.

        The local ref is the one the requirement names, so the script refuses
        until it is brought up to date -- and it says how.
        """
        result = _drive(ENV_LINE, publish="stale-local-cache")
        assert result["remote_main"] == result["sha"], (
            "the fixture did not publish HEAD on the remote's main"
        )
        assert result["local_main"] != result["sha"], (
            "the fixture did not leave the local copy of origin/main behind"
        )
        assert result["rc"] == 1, (result["stdout"], result["stderr"])
        assert "not on the main branch" in result["stderr"], (
            f"the refusal must say the commit is not in the local origin/main; "
            f"it said: {result['stderr']!r}"
        )
        assert "git fetch origin main" in result["stderr"], (
            f"the refusal must say how to refresh the local copy: "
            f"{result['stderr']!r}"
        )
        assert result["env"] == b"POSTGRES_PASSWORD=secret\n"
        assert b"DEPLOYED_COMMIT=" not in result["env"]

    def test_a_commit_that_was_never_pushed_is_refused(self):
        result = _drive(ENV_LINE, publish="unpushed")
        assert result["rc"] == 1, (result["stdout"], result["stderr"])
        assert "not published" in result["stderr"].lower(), (
            f"the refusal must say the commit is not published; it said: "
            f"{result['stderr']!r}"
        )
        assert result["env"] == b"POSTGRES_PASSWORD=secret\n"
        assert b"DEPLOYED_COMMIT=" not in result["env"]


# --------------------------------------------------------------------------
# (c) the LIVE remote has to confirm it
# --------------------------------------------------------------------------
class TestTheLiveRemoteHasToConfirmIt:
    def test_a_remote_that_no_longer_has_the_commit_is_refused(self):
        """The local cache still says the commit is on origin/main, but the
        LIVE remote's main was rewound and no longer contains it. This is
        the stale-cache case the live check exists for."""
        result = _drive(ENV_LINE, publish="stale-remote")
        assert result["local_main"] == result["sha"], (
            "the fixture did not leave the local copy of origin/main ahead of "
            "the remote"
        )
        assert result["remote_main"] != result["sha"], (
            "the fixture did not rewind the remote's main"
        )
        assert result["rc"] == 1, (result["stdout"], result["stderr"])
        assert "not published" in result["stderr"].lower(), (
            f"the refusal must say the live main does not contain the commit; "
            f"it said: {result['stderr']!r}"
        )
        assert NOTHING_CHANGED in result["stderr"]

    def test_the_refusal_of_a_stale_remote_writes_nothing(self):
        result = _drive(ENV_LINE, publish="stale-remote")
        assert result["rc"] == 1
        assert result["env"] == b"POSTGRES_PASSWORD=secret\n"
        assert b"DEPLOYED_COMMIT=" not in result["env"]

    def test_an_unreachable_remote_is_refused_loudly(self):
        """A remote that cannot be asked is a REFUSAL, not an assumption."""
        result = _drive(ENV_LINE, publish="unreachable")
        assert result["rc"] == 1, (result["stdout"], result["stderr"])
        assert "could not be asked" in result["stderr"], (
            f"the refusal must say the live remote could not be asked; it "
            f"said: {result['stderr']!r}"
        )
        assert NOTHING_CHANGED in result["stderr"]

    def test_the_refusal_of_an_unreachable_remote_writes_nothing(self):
        result = _drive(ENV_LINE, publish="unreachable")
        assert result["rc"] == 1
        assert result["env"] == b"POSTGRES_PASSWORD=secret\n"
        assert b"DEPLOYED_COMMIT=" not in result["env"]

    def test_a_remote_without_a_main_branch_is_refused(self):
        """The remote answers, but it has no `main`. There is no branch to
        say the commit is published on."""
        result = _drive(ENV_LINE, publish="no-main-on-remote")
        assert result["rc"] == 1, (result["stdout"], result["stderr"])
        assert "no 'main' branch" in result["stderr"], (
            f"the refusal must say the remote has no main branch; it said: "
            f"{result['stderr']!r}"
        )
        assert NOTHING_CHANGED in result["stderr"]

    def test_the_refusal_of_a_remote_without_main_writes_nothing(self):
        result = _drive(ENV_LINE, publish="no-main-on-remote")
        assert result["rc"] == 1
        assert result["env"] == b"POSTGRES_PASSWORD=secret\n"
        assert b"DEPLOYED_COMMIT=" not in result["env"]


# --------------------------------------------------------------------------
# (d) the refusals that already existed still happen, and the success path
#     still works
# --------------------------------------------------------------------------
class TestTheExistingRefusalsStillHold:
    def test_a_dirty_checkout_is_still_refused(self):
        result = _drive(
            ENV_LINE + "printf 'SECRET = 1\\n' > sneaky.py\n"
        )
        assert result["rc"] == 1, (result["stdout"], result["stderr"])
        assert (
            "REFUSING to stamp a checkout that is not the committed tree"
            in result["stderr"]
        ), f"the original dirty refusal changed: {result['stderr']!r}"
        assert NOTHING_CHANGED in result["stderr"]
        assert result["env"] == b"POSTGRES_PASSWORD=secret\n"

    def test_a_missing_environment_file_is_still_refused(self):
        result = _drive("")
        assert result["rc"] == 1, (result["stdout"], result["stderr"])
        assert ".env" in result["stderr"]

    def test_a_missing_environment_file_is_still_not_created(self):
        result = _drive("")
        assert result["rc"] == 1
        assert not result["env_exists"], (
            "the script created a stub .env; that would start the app with no "
            "configuration"
        )

    def test_the_stamp_is_the_full_sha_and_is_written_once(self):
        result = _drive(ENV_LINE)
        assert result["rc"] == 0, result["stderr"]
        assert len(result["sha"]) == 40
        assert result["env"].count(b"DEPLOYED_COMMIT=") == 1
        assert f"DEPLOYED_COMMIT={result['sha']}\n".encode() in result["env"]

    def test_the_other_lines_of_the_environment_file_are_untouched(self):
        result = _drive(
            "printf 'POSTGRES_PASSWORD=secret\\nREDIS_API_PASSWORD=other\\n' > .env\n"
        )
        assert result["rc"] == 0, result["stderr"]
        assert b"POSTGRES_PASSWORD=secret\n" in result["env"]
        assert b"REDIS_API_PASSWORD=other\n" in result["env"]
