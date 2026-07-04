#!/usr/bin/env python3
"""
forge — session-scoped work orchestration store.

One SQLite DB per session holds the task FSM, frozen contracts, and the
decision log. This CLI is the ONLY sanctioned way to mutate that state:
the orchestrator and the background agents call subcommands, never raw SQL.
Illegal state transitions are rejected here AND by CHECK constraints in the
schema, so a misbehaving agent cannot self-promote a task to `done` or write
outside its lane.

stdlib only. No dependencies, ever.

Layout on disk (session state only; project context is the repo itself):
    <project>/.forge/
        <session-id>/
            session.db        # this file's target
            spikes/

Usage:
    forge.py --forge-dir PATH <command> ...

Run `forge.py --help` or `forge.py <command> --help` for details.
"""

import argparse
import json
import secrets
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# ---- Task FSM ---------------------------------------------------------------
# The single source of truth for legal transitions, keyed by the ROLE of the
# writer. The schema mirrors the terminal states with a CHECK; this table adds
# the who-can-do-what layer that a CHECK alone can't express cleanly.
#
# Roles:
#   orchestrator  the monoagent. Full authority: starts, reviews-verdict-reads,
#                 merges, abandons. It is the Coordinator.
#   implementer   background agent that produces code on the task's branch.
#   reviewer      background agent (DIFFERENT model) that judges the branch and
#                 records a review verdict. Never touches code or status.
#
# A task has ONE of two lifecycles depending on its type:
#   implement:  pending -> running -> submitted -> in_review -> merged
#                              ^                        |
#                              +----- needs_fix <-------+   (verdict: problems)
#   spike:      pending -> running -> resolved
#     A spike explores to answer a question. Its value is knowledge/verdict, not
#     code: no review, no merge, throwaway code. It participates in the graph
#     like any node — it blocks a task iff that task depends_on it.
#
# States:
#   pending    created, deps not all satisfied or not yet started
#   running    an agent is working on it (on its own worktree/branch)
#   submitted  implementer finished producing on the branch; awaiting review
#   in_review  a reviewer has judged it (a `review` row exists); orchestrator acts
#   needs_fix  latest review found problems; task re-runs on the SAME branch
#   merged     (implement) branch integrated into main. Terminal success.
#   resolved   (spike) knowledge delivered. Terminal success.
#   failed     agent gave up / errored
#   abandoned  orchestrator killed it

TRANSITIONS = {
    # role          from          to
    # -- implement branch --
    ("orchestrator", "pending", "running"),
    ("implementer", "running", "submitted"),
    ("implementer", "running", "failed"),
    ("orchestrator", "submitted", "in_review"),  # a review row was recorded
    ("orchestrator", "in_review", "merged"),  # verdict clean + deps merged
    ("orchestrator", "in_review", "needs_fix"),  # verdict problems
    ("orchestrator", "needs_fix", "running"),  # re-run on same branch
    ("orchestrator", "failed", "running"),  # recover after agent fail
    # -- spike branch --
    ("implementer", "running", "resolved"),  # spike delivers knowledge
    # -- recovery / kill (any non-terminal) --
    ("orchestrator", "failed", "needs_fix"),
    ("orchestrator", "failed", "abandoned"),
    ("orchestrator", "running", "abandoned"),
    ("orchestrator", "pending", "abandoned"),
    ("orchestrator", "submitted", "abandoned"),
    ("orchestrator", "in_review", "abandoned"),
    ("orchestrator", "needs_fix", "abandoned"),
}

TERMINAL = {"merged", "resolved", "abandoned"}
SUCCESS_TERMINAL = {"merged", "resolved"}  # a dep is satisfied by either
AGENT_ROLES = {"implementer", "reviewer"}
TASK_TYPES = {"implement", "spike"}

# ---- Session phase machine --------------------------------------------------
# A session is ONE implementation-spec on an existing project. It walks the same
# spec -> architect -> tasks -> implement pipeline the old separate skills did,
# but a single orchestrator drives it and the phase is enforced by the DB rather
# than by a human running the next slash command.
#
#   spec          define the boundary of THIS implementation + hunt collisions
#                 on the joints. Not the project — a bounded change on it.
#   architect     freeze the shared contracts, only as far as they are pinnable
#                 (horizon). Contracts cannot be frozen before this phase.
#   tasks         decompose into the task graph and VALIDATE it (no cycles, deps
#                 complete, writes-scope disjoint among concurrent tasks).
#   implementing  fan out. start/submit/review happen only here.
#   done          closed.
PHASES = ["spec", "architect", "tasks", "implementing", "done"]

PHASE_ORDER = {p: i for i, p in enumerate(PHASES)}

# Which commands are legal in which phases. The gate is mechanical: e.g. you
# cannot task-add before the architecture exists, cannot start an agent before
# the graph is validated (implementing).
PHASE_ALLOWS = {
    "contract-freeze": {"architect", "tasks"},
    # task-add is allowed in implementing too: a review can reveal a
    # decomposition defect (missing/mis-cut task) or a compensation task.
    "task-add": {"tasks", "implementing"},
    "task-add-batch": {"tasks", "implementing"},
    "start": {"implementing"},
    "rerun": {"implementing"},
    "submit": {"implementing"},
    "fail": {"implementing"},
    "review-add": {"implementing"},
    "review-waive": {"implementing"},
    "merge": {"implementing"},
    "abandon": {"tasks", "implementing"},
    "task-edit": {"tasks", "implementing"},
    "contract-challenge": {"tasks", "implementing"},
    "runnable": {"tasks", "implementing"},
    "verify": {"tasks", "implementing"},
    "next": {"tasks", "implementing"},
    # decision-add, context, status, render, phase, validate: any phase
}

MERGE_OK_VERDICTS = {"clean", "waived"}


class ForgeError(Exception):
    """User-facing error. Printed cleanly, exits non-zero."""


# ---- DB plumbing ------------------------------------------------------------


def _db_path(forge_dir: Path, session: str) -> Path:
    return forge_dir / session / "session.db"


def _connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise ForgeError(f"no session db at {path}. Run `init` for this session first.")
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")  # concurrent background agents
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


def _now() -> int:
    return int(time.time())


def _out(obj):
    """Everything an agent needs to read comes back as JSON on stdout."""
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _get_phase(conn) -> str:
    row = conn.execute("SELECT phase FROM session LIMIT 1").fetchone()
    return row["phase"] if row else "spec"


def _require_phase(conn, cmd):
    """Reject a command issued in the wrong session phase."""
    allowed = PHASE_ALLOWS.get(cmd)
    if allowed is None:
        return  # command legal in any phase
    phase = _get_phase(conn)
    if phase not in allowed:
        raise ForgeError(
            f"'{cmd}' is not allowed in phase '{phase}'. "
            f"Legal phases for it: {sorted(allowed)}. "
            f"Advance the session with `phase` first."
        )


def _project_root(forge_dir: Path) -> Path:
    """The git repo root — parent of <project>/.forge/."""
    return forge_dir.parent


def _forge_cli(forge_dir: Path, session: str) -> str:
    py = Path(__file__).resolve()
    fd = forge_dir
    return f'python3 "{py}" --forge-dir "{fd}"'


def _git_run(repo: Path, *args, timeout=30):
    """Run git in repo; return (ok, stdout, stderr). Never raises."""
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.returncode == 0, (r.stdout or "").strip(), (r.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, "", str(e)


def _git_is_repo(repo: Path) -> bool:
    ok, _, _ = _git_run(repo, "rev-parse", "--git-dir")
    return ok


def _git_rev(repo: Path, ref: str):
    ok, out, _ = _git_run(repo, "rev-parse", ref)
    return out if ok else None


def _git_task_verify(repo: Path, task: sqlite3.Row) -> dict:
    """Preflight checks for a task's worktree/branch vs recorded base_ref."""
    wt = task["worktree"]
    branch = task["branch"]
    base = task["base_ref"]
    problems = []
    warnings = []
    result = {
        "git_repo": _git_is_repo(repo),
        "worktree": wt,
        "branch": branch,
        "base_ref": base,
        "ready_to_submit": False,
        "ready_to_merge_record": False,
        "problems": problems,
        "warnings": warnings,
    }
    if not result["git_repo"]:
        problems.append("project root is not a git repository")
        return result
    if not wt:
        problems.append("no worktree registered — run `start` first")
        return result
    wt_path = Path(wt)
    if not wt_path.is_dir():
        problems.append(f"worktree path missing: {wt}")
        return result
    if not _git_is_repo(wt_path):
        problems.append(f"worktree path is not a git checkout: {wt}")
        return result

    head = _git_rev(wt_path, "HEAD")
    result["head_sha"] = head
    if base:
        base_sha = _git_rev(repo, base)
        result["base_sha"] = base_sha
        if not base_sha:
            problems.append(f"base_ref '{base}' does not resolve in the repo")
        elif head and head == base_sha:
            warnings.append(
                "HEAD equals base_ref — no commits yet on this branch (ok if just started)"
            )
        elif head and base_sha:
            ok_anc, _, _ = _git_run(repo, "merge-base", "--is-ancestor", base_sha, head)
            if not ok_anc:
                problems.append(
                    f"worktree HEAD ({head[:8]}) is not descended from base_ref "
                    f"{base} ({base_sha[:8]}) — wrong checkout or stale worktree"
                )
            ok_behind, out, _ = _git_run(
                repo, "rev-list", "--count", f"{head}..{base_sha}"
            )
            if ok_behind and out and int(out) > 0:
                problems.append(
                    f"worktree is {out} commit(s) BEHIND {base} — rebase or recreate "
                    f"worktree from current integration HEAD before continuing"
                )
            ok_ahead, out, _ = _git_run(repo, "rev-list", "--count", f"{base_sha}..{head}")
            if ok_ahead:
                result["commits_ahead_of_base"] = int(out or 0)

    if branch:
        ok_br, cur, _ = _git_run(wt_path, "rev-parse", "--abbrev-ref", "HEAD")
        if ok_br:
            result["checked_out_branch"] = cur
            if cur != branch:
                problems.append(
                    f"worktree on branch '{cur}', expected '{branch}'"
                )

    ok_dirty, out, _ = _git_run(wt_path, "status", "--porcelain")
    if ok_dirty:
        dirty = bool(out)
        result["dirty"] = dirty
        if dirty:
            problems.append(
                "uncommitted changes in worktree — commit (or stash) before submit"
            )

    ok_log, out, _ = _git_run(wt_path, "log", "--oneline", "-1")
    if ok_log:
        result["latest_commit"] = out

    result["ready_to_submit"] = (
        task["status"] == "running"
        and not problems
        and result.get("commits_ahead_of_base", 0) > 0
    )
    return result


def _git_branch_merged_into(repo: Path, base_ref: str, branch: str) -> bool:
    """True if branch tip is reachable from base_ref (already integrated)."""
    if not base_ref or not branch:
        return False
    base_sha = _git_rev(repo, base_ref)
    branch_sha = _git_rev(repo, branch)
    if not base_sha or not branch_sha:
        return False
    ok, _, _ = _git_run(repo, "merge-base", "--is-ancestor", branch_sha, base_sha)
    return ok


def _latest_review(conn, task_id: str):
    return conn.execute(
        "SELECT verdict, notes FROM review WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
        (task_id,),
    ).fetchone()


def _task_next_actions(conn, forge_dir: Path, session: str, task: sqlite3.Row) -> dict:
    """Operational next steps for one task — the antidote to FSM guesswork."""
    status = task["status"]
    ttype = task["type"]
    tid = task["id"]
    cli = _forge_cli(forge_dir, session)
    never = [
        "NEVER `git merge` into the integration branch before `review-add` is clean/waived "
        "and you are ready to record — but ALWAYS run git merge BEFORE `merge` records state",
        "NEVER `review-add` unless status is `submitted` (after fix: `rerun` → work → `submit` first)",
        "NEVER self-review as orchestrator — spawn reviewer or `review-waive` with explicit reason",
        "NEVER skip `submit` after implementer finishes (including fix rounds)",
    ]
    steps = []
    notes = []
    repo = _project_root(forge_dir)

    if status == "pending":
        ok = ",".join(f"'{s}'" for s in sorted(SUCCESS_TERMINAL))
        bad = conn.execute(
            f"SELECT d.depends_on FROM task_dep d JOIN task dt ON dt.id=d.depends_on "
            f"WHERE d.task_id=? AND dt.status NOT IN ({ok})",
            (tid,),
        ).fetchall()
        if bad:
            notes.append(
                f"blocked by unmet deps: {[b['depends_on'] for b in bad]}"
            )
        else:
            steps.append(
                {
                    "order": 1,
                    "who": "orchestrator",
                    "cmd": f'{cli} start --session {session} --task {tid} '
                    f'--branch <branch> --base <integration-head-NOW>',
                    "why": "register branch/base/worktree, then run the printed git worktree add",
                }
            )
            steps.append(
                {
                    "order": 2,
                    "who": "orchestrator",
                    "cmd": f'{cli} verify --session {session} --task {tid}',
                    "why": "preflight after worktree exists — HEAD must descend from base",
                }
            )
            steps.append(
                {
                    "order": 3,
                    "who": "orchestrator",
                    "cmd": f'{cli} context --session {session} --task {tid} --role implementer',
                    "why": "bootstrap payload; spawn implementer in that worktree only",
                }
            )

    elif status == "running":
        steps.append(
            {
                "order": 1,
                "who": "orchestrator",
                "cmd": f'{cli} verify --session {session} --task {tid}',
                "why": "confirm worktree HEAD, branch, commits, no dirty tree",
            }
        )
        if ttype == "implement":
            steps.append(
                {
                    "order": 2,
                    "who": "implementer",
                    "cmd": "commit all work on the task branch in the worktree",
                    "why": "orchestrator must not merge uncommitted renames",
                }
            )
            steps.append(
                {
                    "order": 3,
                    "who": "implementer",
                    "cmd": f'{cli} submit --session {session} --task {tid} --output "..."',
                    "why": "running → submitted; mandatory before any review",
                }
            )
        else:
            steps.append(
                {
                    "order": 2,
                    "who": "implementer",
                    "cmd": f'{cli} resolve --session {session} --task {tid} --output "finding"',
                    "why": "spike terminates with knowledge, not a merge",
                }
            )

    elif status == "submitted" and ttype == "implement":
        steps.append(
            {
                "order": 1,
                "who": "orchestrator",
                "cmd": f'{cli} verify --session {session} --task {tid}',
                "why": "confirm commits exist before paying for review",
            }
        )
        steps.append(
            {
                "order": 2,
                "who": "orchestrator",
                "cmd": f"spawn reviewer on {task['review_model'] or '<review-model>'}",
                "why": "reviewer must be a different agent/model from implementer",
            }
        )
        steps.append(
            {
                "order": 3,
                "who": "reviewer",
                "cmd": f'{cli} context --session {session} --task {tid} --role reviewer',
                "why": "diff hint + prior reviews",
            }
        )
        steps.append(
            {
                "order": 4,
                "who": "reviewer",
                "cmd": f'{cli} review-add --session {session} --task {tid} '
                f'--verdict clean|problems --notes "..."',
                "why": "submitted → in_review",
            }
        )
        notes.append(
            "orchestrator self-review is forbidden; for trivial work use "
            f"`review-waive` (audited) instead of skipping review"
        )

    elif status == "in_review" and ttype == "implement":
        last = _latest_review(conn, tid)
        verdict = last["verdict"] if last else None
        if verdict in MERGE_OK_VERDICTS:
            merged_git = _git_branch_merged_into(
                repo, task["base_ref"], task["branch"]
            )
            if merged_git:
                notes.append(
                    "git shows branch already integrated into base — only `merge` record remains"
                )
                steps.append(
                    {
                        "order": 1,
                        "who": "orchestrator",
                        "cmd": f'{cli} merge --session {session} --task {tid}',
                        "why": "record merged in forge (git integration already done)",
                    }
                )
            else:
                steps.append(
                    {
                        "order": 1,
                        "who": "orchestrator",
                        "cmd": f"git checkout {task['base_ref']} && "
                        f"git merge --no-ff {task['branch']}",
                        "why": "integrate branch into base FIRST",
                    }
                )
                steps.append(
                    {
                        "order": 2,
                        "who": "orchestrator",
                        "cmd": f'{cli} merge --session {session} --task {tid}',
                        "why": "record merged only AFTER git merge succeeds",
                    }
                )
        elif verdict == "problems":
            steps.append(
                {
                    "order": 1,
                    "who": "orchestrator",
                    "cmd": f'{cli} needs-fix --session {session} --task {tid}',
                    "why": "in_review → needs_fix (if not already routed)",
                }
            )
            steps.append(
                {
                    "order": 2,
                    "who": "orchestrator",
                    "cmd": f'{cli} rerun --session {session} --task {tid}',
                    "why": "needs_fix → running + respawn checklist",
                }
            )
        else:
            notes.append(f"unexpected review verdict: {verdict}")

    elif status == "needs_fix":
        steps.append(
            {
                "order": 1,
                "who": "orchestrator",
                "cmd": f'{cli} rerun --session {session} --task {tid}',
                "why": "needs_fix → running on SAME branch/worktree",
            }
        )
        steps.append(
            {
                "order": 2,
                "who": "implementer",
                "cmd": "apply fixes, commit on task branch",
                "why": "same worktree — do not recreate unless verify fails",
            }
        )
        steps.append(
            {
                "order": 3,
                "who": "implementer",
                "cmd": f'{cli} submit --session {session} --task {tid} --output "..."',
                "why": "mandatory — review-add will refuse otherwise",
            }
        )
        steps.append(
            {
                "order": 4,
                "who": "reviewer",
                "cmd": f'{cli} review-add --session {session} --task {tid} --verdict clean|problems',
                "why": "new review round after fix",
            }
        )

    elif status == "failed":
        steps.append(
            {
                "order": 1,
                "who": "orchestrator",
                "cmd": f'{cli} rerun --session {session} --task {tid} OR abandon',
                "why": "recover or kill",
            }
        )

    elif status in TERMINAL:
        notes.append(f"terminal state ({status}) — no further actions")

    return {
        "task": tid,
        "type": ttype,
        "status": status,
        "branch": task["branch"],
        "base_ref": task["base_ref"],
        "never": never,
        "steps": steps,
        "notes": notes,
    }


# ---- commands ---------------------------------------------------------------


def cmd_list(args, forge_dir: Path):
    """List every session under the forge dir with its phase and task counts, so
    the orchestrator (and the human) can see what's live and what to resume,
    instead of guessing from `ls`."""
    if not forge_dir.exists():
        _out([])
        return
    sessions = []
    for sdir in sorted(forge_dir.iterdir()):
        db = sdir / "session.db"
        if not db.is_file():
            continue
        try:
            conn = sqlite3.connect(str(db))
            conn.row_factory = sqlite3.Row
            s = conn.execute(
                "SELECT id, title, phase, created_at FROM session LIMIT 1"
            ).fetchone()
            counts = {}
            for r in conn.execute(
                "SELECT status, COUNT(*) c FROM task GROUP BY status"
            ):
                counts[r["status"]] = r["c"]
            conn.close()
            if s:
                sessions.append(
                    {
                        "id": s["id"],
                        "title": s["title"],
                        "phase": s["phase"],
                        "created_at": s["created_at"],
                        "tasks": counts,
                    }
                )
        except sqlite3.Error:
            sessions.append({"id": sdir.name, "error": "unreadable session.db"})
    _out(sessions)


def _slugify(text: str) -> str:
    """A short, filesystem-safe slug from a title. Not unique on its own — the
    generated id prefix guarantees uniqueness; this is only for readability."""
    out = []
    for ch in (text or "").lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in " -_/":
            out.append("-")
    slug = "".join(out)
    while "--" in slug:
        slug = slug.replace("--", "-")
    slug = slug.strip("-")
    return slug[:40] or "session"


def cmd_init(args, forge_dir: Path):
    # The id is ALWAYS generated — never taken from the caller — so the agent
    # cannot reuse a title as an id and cause collisions. The folder name is
    # `<id>-<title-slug>`: the id guarantees uniqueness, the slug gives you
    # readability in `ls`. This combined name IS the session key used everywhere.
    title = args.title or "session"
    slug = _slugify(title)
    for _ in range(100):
        session = f"{_new_id('s')}-{slug}"
        if not (forge_dir / session).exists():
            break
    else:
        raise ForgeError("could not generate a unique session id; too many sessions?")
    sdir = forge_dir / session
    sdir.mkdir(parents=True, exist_ok=False)
    (sdir / "spikes").mkdir(exist_ok=True)
    # worktrees for this session's tasks live physically here; keep their files
    # out of the main working tree's status.
    (sdir / ".gitignore").write_text("worktrees/\n")
    path = _db_path(forge_dir, session)
    conn = sqlite3.connect(str(path))
    conn.executescript(SCHEMA_PATH.read_text())
    conn.execute(
        "INSERT INTO session(id, title, created_at) VALUES(?,?,?)",
        (session, title, _now()),
    )
    conn.commit()
    conn.close()
    _out({"session": session, "db": str(path), "title": args.title or session})


def cmd_task_add_batch(args, forge_dir: Path):
    """Create many tasks in ONE transaction: all succeed or none do. This is the
    honest way to decompose in bulk — no half-written graph if one task is
    malformed, and no reason to script sequential task-add calls (which leave
    partial state on failure). Reads a JSON array from --file or stdin, each item:
      {"spec","type"?,"impl_model"?,"review_model"?,"writes_scope"?,
       "depends_on"?,"contract"?,"ref"?}
    `ref` is a local alias so items can depend on earlier items in the same batch
    (depends_on may list refs or existing task ids). The graph is still validated
    separately; this only guarantees atomic creation."""
    raw = Path(args.file).read_text() if args.file else sys.stdin.read()
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ForgeError(f"batch input is not valid JSON: {e}")
    if not isinstance(items, list) or not items:
        raise ForgeError("batch must be a non-empty JSON array of task objects")
    conn = _connect(_db_path(forge_dir, args.session))
    ref_to_id = {}
    created = []
    try:
        with conn:  # single transaction: any exception rolls back ALL inserts
            for i, it in enumerate(items):
                ttype = it.get("type", "implement")
                if ttype not in TASK_TYPES:
                    raise ForgeError(
                        f"item {i}: type must be one of {sorted(TASK_TYPES)}"
                    )
                tid = _new_id("t")
                scope = json.dumps(it.get("writes_scope") or [])
                conn.execute(
                    "INSERT INTO task(id, type, status, impl_model, review_model, "
                    "writes_scope, spec, created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        tid,
                        ttype,
                        "pending",
                        it.get("impl_model"),
                        it.get("review_model"),
                        scope,
                        it.get("spec", ""),
                        _now(),
                    ),
                )
                if it.get("ref"):
                    ref_to_id[it["ref"]] = tid
                created.append({"ref": it.get("ref"), "task": tid, "type": ttype})
            # second pass: wire deps and contracts (refs now all resolvable)
            for it, rec in zip(items, created):
                tid = rec["task"]
                for d in it.get("depends_on") or []:
                    dep = ref_to_id.get(d, d)  # a ref or a real id
                    if not conn.execute(
                        "SELECT 1 FROM task WHERE id=?", (dep,)
                    ).fetchone():
                        raise ForgeError(
                            f"task {tid}: depends_on '{d}' resolves to unknown task"
                        )
                    conn.execute(
                        "INSERT INTO task_dep(task_id, depends_on) VALUES(?,?)",
                        (tid, dep),
                    )
                for c in it.get("contract") or []:
                    if not conn.execute(
                        "SELECT 1 FROM contract WHERE id=?", (c,)
                    ).fetchone():
                        raise ForgeError(f"task {tid}: unknown contract {c}")
                    conn.execute(
                        "INSERT INTO task_contract(task_id, contract_id) VALUES(?,?)",
                        (tid, c),
                    )
    except ForgeError:
        raise  # transaction already rolled back by the `with conn` context
    _out(
        {
            "created": created,
            "count": len(created),
            "note": "atomic: all created together. Now `validate` the graph.",
        }
    )


def cmd_task_add(args, forge_dir: Path):
    """Orchestrator creates a task. writes_scope + deps + models declared up front.
    implement tasks go to merged (via review); spike tasks go to resolved."""
    conn = _connect(_db_path(forge_dir, args.session))
    if args.type not in TASK_TYPES:
        raise ForgeError(f"type must be one of {sorted(TASK_TYPES)}")
    tid = _new_id("t")
    scope = json.dumps(args.writes_scope or [])
    deps = args.depends_on or []
    contracts = args.contract or []
    with conn:
        for d in deps:
            if not conn.execute("SELECT 1 FROM task WHERE id=?", (d,)).fetchone():
                raise ForgeError(f"depends_on references unknown task {d}")
        for c in contracts:
            if not conn.execute("SELECT 1 FROM contract WHERE id=?", (c,)).fetchone():
                raise ForgeError(f"contract references unknown contract {c}")
        conn.execute(
            "INSERT INTO task(id, type, status, impl_model, review_model, "
            "writes_scope, spec, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                tid,
                args.type,
                "pending",
                args.impl_model,
                args.review_model,
                scope,
                args.spec or "",
                _now(),
            ),
        )
        for d in deps:
            conn.execute(
                "INSERT INTO task_dep(task_id, depends_on) VALUES(?,?)", (tid, d)
            )
        for c in contracts:
            conn.execute(
                "INSERT INTO task_contract(task_id, contract_id) VALUES(?,?)", (tid, c)
            )
    _out({"task": tid, "type": args.type, "status": "pending"})


def cmd_task_runnable(args, forge_dir: Path):
    """Tasks the orchestrator may legally start now: pending + every dep in a
    success-terminal state (merged for implement deps, resolved for spike deps)."""
    conn = _connect(_db_path(forge_dir, args.session))
    ok = ",".join(f"'{s}'" for s in sorted(SUCCESS_TERMINAL))
    rows = conn.execute(
        f"""
        SELECT t.id, t.type, t.impl_model, t.review_model, t.writes_scope, t.spec
        FROM task t
        WHERE t.status = 'pending'
          AND NOT EXISTS (
            SELECT 1 FROM task_dep d
            JOIN task dt ON dt.id = d.depends_on
            WHERE d.task_id = t.id AND dt.status NOT IN ({ok})
          )
        ORDER BY t.created_at
        """
    ).fetchall()
    _out([dict(r) for r in rows])


def cmd_task_start(args, forge_dir: Path):
    """Orchestrator moves a task pending->running and registers the git worktree.
    The orchestrator (NOT a worktree-isolated subagent) owns the git plumbing:
    it chooses a real, readable branch name, branches from the CURRENT head of
    the integration branch (so incrementally-merged work is included), and places
    the worktree physically under the session dir. This command RECORDS that and
    tells you the exact git command to run — forge does not run git itself."""
    if not args.branch:
        raise ForgeError(
            "--branch is required (a real, readable branch name you choose)"
        )
    if not args.base:
        raise ForgeError(
            "--base is required (HEAD of the integration branch NOW, "
            "e.g. the current feature branch — so incremental merges are included)"
        )
    conn = _connect(_db_path(forge_dir, args.session))
    # worktree lives under the session's own folder: isolation on two levels
    wt = str(forge_dir / args.session / "worktrees" / args.branch.replace("/", "+"))
    with conn:
        conn.execute(
            "UPDATE task SET branch=?, base_ref=?, worktree=?, worktree_state='live' WHERE id=?",
            (args.branch, args.base, wt, args.task),
        )
    _transition(
        forge_dir, args.session, args.task, "running", "orchestrator", quiet=True
    )
    _out(
        {
            "task": args.task,
            "status": "running",
            "branch": args.branch,
            "base": args.base,
            "worktree": wt,
            "run_this": f"git worktree add {wt} -b {args.branch} {args.base}",
            "then": f'{_forge_cli(forge_dir, args.session)} verify --session {args.session} --task {args.task}',
            "note": "spawn the implementer as a normal subagent told to work in that worktree "
            "path and commit there. Not isolation:worktree — you own the branch name.",
        }
    )


def cmd_next(args, forge_dir: Path):
    """Print the exact legal next actions for a task (or all non-terminal tasks)."""
    conn = _connect(_db_path(forge_dir, args.session))
    if args.task:
        row = conn.execute("SELECT * FROM task WHERE id=?", (args.task,)).fetchone()
        if not row:
            raise ForgeError(f"unknown task {args.task}")
        _out(_task_next_actions(conn, forge_dir, args.session, row))
        return
    tasks = conn.execute(
        "SELECT * FROM task WHERE status NOT IN ('merged','resolved','abandoned') "
        "ORDER BY created_at"
    ).fetchall()
    _out(
        {
            "session": args.session,
            "tasks": [
                _task_next_actions(conn, forge_dir, args.session, t) for t in tasks
            ],
        }
    )


def cmd_verify(args, forge_dir: Path):
    """Preflight git/worktree checks for a task before submit, review, or respawn."""
    conn = _connect(_db_path(forge_dir, args.session))
    row = conn.execute("SELECT * FROM task WHERE id=?", (args.task,)).fetchone()
    if not row:
        raise ForgeError(f"unknown task {args.task}")
    repo = _project_root(forge_dir)
    report = _git_task_verify(repo, row)
    report["task"] = args.task
    report["status"] = row["status"]
    report["ok"] = not report["problems"]
    if row["status"] == "in_review":
        last = _latest_review(conn, args.task)
        if last and last["verdict"] in MERGE_OK_VERDICTS:
            report["git_merged_into_base"] = _git_branch_merged_into(
                repo, row["base_ref"], row["branch"]
            )
            report["ready_to_merge_record"] = (
                report["ok"] and report.get("git_merged_into_base", False)
            )
    _out(report)


def cmd_rerun(args, forge_dir: Path):
    """needs_fix|failed → running on the SAME branch/worktree. Emits respawn checklist."""
    conn = _connect(_db_path(forge_dir, args.session))
    row = conn.execute("SELECT * FROM task WHERE id=?", (args.task,)).fetchone()
    if not row:
        raise ForgeError(f"unknown task {args.task}")
    if row["status"] not in ("needs_fix", "failed"):
        raise ForgeError(
            f"rerun is for needs_fix|failed; {args.task} is '{row['status']}'. "
            f"Run `next --task {args.task}` for the correct sequence."
        )
    if not row["branch"]:
        raise ForgeError(
            f"task {args.task} has no branch — cannot rerun; use `start` from pending"
        )
    cli = _forge_cli(forge_dir, args.session)
    refresh_base = args.base
    with conn:
        if refresh_base:
            conn.execute(
                "UPDATE task SET base_ref=?, updated_at=? WHERE id=?",
                (refresh_base, _now(), args.task),
            )
    base = refresh_base or row["base_ref"]
    _transition(
        forge_dir, args.session, args.task, "running", "orchestrator", quiet=True
    )
    conn2 = _connect(_db_path(forge_dir, args.session))
    fresh = conn2.execute("SELECT * FROM task WHERE id=?", (args.task,)).fetchone()
    conn2.close()
    repo = _project_root(forge_dir)
    verify = _git_task_verify(repo, fresh)
    recreate = None
    if verify["problems"] and row["worktree"] and base:
        wt = row["worktree"]
        recreate = (
            f"git worktree remove --force {wt} 2>/dev/null; "
            f"git worktree add {wt} -b {row['branch']} {base}"
        )
    _out(
        {
            "task": args.task,
            "status": "running",
            "branch": row["branch"],
            "base_ref": base,
            "worktree": row["worktree"],
            "verify": verify,
            "recreate_worktree_if_needed": recreate,
            "spawn": {
                "work_in": row["worktree"],
                "branch": row["branch"],
                "model": row["impl_model"],
            },
            "exit_protocol": [
                "commit all changes on the task branch",
                f'{cli} verify --session {args.session} --task {args.task}',
                f'{cli} submit --session {args.session} --task {args.task} --output "..."',
            ],
            "then": [
                f"spawn reviewer ({row['review_model'] or 'review-model'})",
                f'{cli} review-add --session {args.session} --task {args.task} --verdict clean|problems',
            ],
            "note": "do NOT review-add until submit succeeds; do NOT git-merge integration branch until review is clean/waived",
        }
    )


def cmd_review_waive(args, forge_dir: Path):
    """Orchestrator-only audited skip of reviewer spawn. submitted → in_review with verdict waived."""
    if not args.reason or len(args.reason.strip()) < 10:
        raise ForgeError(
            "--reason is required (min 10 chars) — explain why reviewer spawn is skipped"
        )
    conn = _connect(_db_path(forge_dir, args.session))
    t = conn.execute(
        "SELECT type, status FROM task WHERE id=?", (args.task,)
    ).fetchone()
    if not t:
        raise ForgeError(f"unknown task {args.task}")
    if t["type"] != "implement":
        raise ForgeError(f"only implement tasks are reviewed; {args.task} is '{t['type']}'")
    if t["status"] != "submitted":
        raise ForgeError(
            f"review-waive requires status 'submitted'; got '{t['status']}'. "
            f"Run `next --task {args.task}`."
        )
    rid = _new_id("r")
    notes = f"WAIVED by orchestrator: {args.reason.strip()}"
    with conn:
        conn.execute(
            "INSERT INTO review(id, task_id, verdict, notes, created_at) VALUES(?,?,?,?,?)",
            (rid, args.task, "waived", notes, _now()),
        )
    _transition(
        forge_dir, args.session, args.task, "in_review", "orchestrator", quiet=True
    )
    _out(
        {
            "review": rid,
            "task": args.task,
            "verdict": "waived",
            "status": "in_review",
            "warning": "audited waiver — not a substitute for review on risky/domain tasks",
            "then": f'{_forge_cli(forge_dir, args.session)} next --session {args.session} --task {args.task}',
        }
    )


def _transition(forge_dir, session, task_id, to_state, role, output=None, quiet=False):
    conn = _connect(_db_path(forge_dir, session))
    with conn:
        row = conn.execute("SELECT status FROM task WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise ForgeError(f"unknown task {task_id}")
        frm = row["status"]
        if frm in TERMINAL:
            raise ForgeError(f"task {task_id} is terminal ({frm}); refusing")
        if (role, frm, to_state) not in TRANSITIONS:
            legal = sorted({t for (r, f, t) in TRANSITIONS if r == role and f == frm})
            raise ForgeError(
                f"role '{role}' cannot move task from '{frm}' to '{to_state}'. "
                f"Legal from '{frm}' for this role: {legal or 'none'}"
            )
        # deps gate is enforced again here, defense in depth
        if to_state == "running":
            ok = ",".join(f"'{s}'" for s in sorted(SUCCESS_TERMINAL))
            bad = conn.execute(
                f"SELECT d.depends_on FROM task_dep d JOIN task dt ON dt.id=d.depends_on "
                f"WHERE d.task_id=? AND dt.status NOT IN ({ok})",
                (task_id,),
            ).fetchall()
            if bad:
                raise ForgeError(
                    f"cannot run {task_id}: unmet deps {[b['depends_on'] for b in bad]}"
                )
        fields = "status=?, updated_at=?"
        params = [to_state, _now()]
        if output is not None:
            fields += ", output=?"
            params.append(output)
        params.append(task_id)
        conn.execute(f"UPDATE task SET {fields} WHERE id=?", params)
    if not quiet:
        _out({"task": task_id, "from": frm, "to": to_state, "by": role})


def cmd_submit(args, forge_dir: Path):
    """Implementer reports it finished producing on the branch. Reaches only
    'submitted' — never merged. The orchestrator reviews and merges."""
    _transition(
        forge_dir,
        args.session,
        args.task,
        "submitted",
        "implementer",
        output=args.output,
    )


def cmd_fail(args, forge_dir: Path):
    _transition(
        forge_dir, args.session, args.task, "failed", "implementer", output=args.output
    )


def cmd_resolve(args, forge_dir: Path):
    """Spike delivers its knowledge/verdict and terminates as 'resolved'. No
    review, no merge — the code was throwaway; the value is the finding, which
    typically feeds a decision-add or a contract-freeze."""
    conn = _connect(_db_path(forge_dir, args.session))
    row = conn.execute("SELECT type FROM task WHERE id=?", (args.task,)).fetchone()
    if not row:
        raise ForgeError(f"unknown task {args.task}")
    if row["type"] != "spike":
        raise ForgeError(f"only spike tasks resolve; {args.task} is '{row['type']}'")
    _transition(
        forge_dir,
        args.session,
        args.task,
        "resolved",
        "implementer",
        output=args.output,
    )


def cmd_review_add(args, forge_dir: Path):
    """Reviewer records a verdict on a task (a DIFFERENT model from the
    implementer). Writes a review row; moves the task to in_review. Never merges,
    never touches code. The orchestrator then reads the verdict to merge or fix."""
    conn = _connect(_db_path(forge_dir, args.session))
    if args.verdict not in ("clean", "problems"):
        raise ForgeError("verdict must be 'clean' or 'problems' (use review-waive to skip reviewer)")
    t = conn.execute(
        "SELECT type, status FROM task WHERE id=?", (args.task,)
    ).fetchone()
    if not t:
        raise ForgeError(f"unknown task {args.task}")
    if t["type"] != "implement":
        raise ForgeError(
            f"only implement tasks are reviewed; {args.task} is '{t['type']}'"
        )
    if t["status"] != "submitted":
        raise ForgeError(
            f"task {args.task} is '{t['status']}', not 'submitted'; nothing to review"
        )
    rid = _new_id("r")
    with conn:
        conn.execute(
            "INSERT INTO review(id, task_id, verdict, notes, created_at) VALUES(?,?,?,?,?)",
            (rid, args.task, args.verdict, args.notes or "", _now()),
        )
    # advance to in_review silently (no separate _out), then emit one result
    _transition(
        forge_dir, args.session, args.task, "in_review", "orchestrator", quiet=True
    )
    _out(
        {
            "review": rid,
            "task": args.task,
            "verdict": args.verdict,
            "status": "in_review",
        }
    )


def cmd_needs_fix(args, forge_dir: Path):
    """Orchestrator routes an in_review task back to work after a 'problems'
    verdict. Re-runs on the SAME branch."""
    _transition(forge_dir, args.session, args.task, "needs_fix", "orchestrator")


def cmd_merge(args, forge_dir: Path):
    """Coordinator marks an implement task as merged. This RECORDS state; it does
    NOT run git — the orchestrator performs the git merge itself, then calls this
    to record success. forge is a state guard, not a git wrapper.

    Refuses unless: task is in_review, latest review is clean, and every dep is
    already in a success-terminal state (topological merge order). Merge is
    incremental and post-review: a task integrates as soon as it's clean and its
    deps are merged. --force overrides the clean-review check only, deliberately."""
    conn = _connect(_db_path(forge_dir, args.session))
    t = conn.execute(
        "SELECT type, status FROM task WHERE id=?", (args.task,)
    ).fetchone()
    if not t:
        raise ForgeError(f"unknown task {args.task}")
    if t["type"] != "implement":
        raise ForgeError(f"only implement tasks merge; {args.task} is '{t['type']}'")
    if t["status"] != "in_review":
        raise ForgeError(
            f"task {args.task} is '{t['status']}', not 'in_review'; "
            f"run `next --task {args.task}`"
        )
    # deps must already be merged/resolved (topological order)
    ok = ",".join(f"'{s}'" for s in sorted(SUCCESS_TERMINAL))
    bad = conn.execute(
        f"SELECT d.depends_on FROM task_dep d JOIN task dt ON dt.id=d.depends_on "
        f"WHERE d.task_id=? AND dt.status NOT IN ({ok})",
        (args.task,),
    ).fetchall()
    if bad:
        raise ForgeError(
            f"cannot merge {args.task} before its deps: {[b['depends_on'] for b in bad]} "
            f"not yet merged/resolved. Merge in topological order."
        )
    if not args.force:
        last = _latest_review(conn, args.task)
        if not last:
            raise ForgeError(
                f"no review for {args.task}; review it before merging (or --force)"
            )
        if last["verdict"] not in MERGE_OK_VERDICTS:
            raise ForgeError(
                f"latest review is '{last['verdict']}', not clean/waived; route to needs-fix "
                f"(or --force to override)"
            )
    row = conn.execute(
        "SELECT branch, base_ref, worktree, worktree_state FROM task WHERE id=?",
        (args.task,),
    ).fetchone()
    repo = _project_root(forge_dir)
    git_merged = _git_branch_merged_into(repo, row["base_ref"], row["branch"])
    if not args.force and _git_is_repo(repo) and row["branch"] and row["base_ref"]:
        if not git_merged:
            raise ForgeError(
                f"git merge not done yet: branch '{row['branch']}' is not integrated into "
                f"'{row['base_ref']}'. Run:\n"
                f"  git checkout {row['base_ref']} && git merge --no-ff {row['branch']}\n"
                f"then call `merge` again. Run `next --task {args.task}` for the full sequence."
            )
    _transition(
        forge_dir, args.session, args.task, "merged", "orchestrator", quiet=True
    )

    # Clean up the worktree. Maintenance of forge's own folder — not project integration.
    wt = row["worktree"]
    cleanup = {"attempted": False}
    repo = _project_root(forge_dir)
    if wt and row["worktree_state"] == "live":
        cleanup["attempted"] = True
        try:
            r = subprocess.run(
                ["git", "-C", str(repo), "worktree", "remove", "--force", wt],
                capture_output=True,
                text=True,
                timeout=30,
            )
            state = "removed" if r.returncode == 0 else "orphaned"
            cleanup["result"] = state
            if r.returncode != 0:
                cleanup["error"] = r.stderr.strip()[:300]
        except Exception as e:
            state = "orphaned"
            cleanup["result"] = "orphaned"
            cleanup["error"] = str(e)[:300]
        with conn:
            conn.execute(
                "UPDATE task SET worktree_state=? WHERE id=?", (state, args.task)
            )

    _out(
        {
            "task": args.task,
            "status": "merged",
            "branch": row["branch"],
            "worktree": wt,
            "worktree_cleanup": cleanup,
            "git_integrated": git_merged,
            "note": "forge state recorded after git integration. "
            "If worktree_cleanup shows 'orphaned', run: "
            f"git worktree remove --force {wt}",
        }
    )


def cmd_abandon(args, forge_dir: Path):
    """Orchestrator kills a task (wrong, superseded, no longer needed). Legal from
    pending/running/failed. A done task is not abandoned — it's consolidated."""
    _transition(
        forge_dir,
        args.session,
        args.task,
        "abandoned",
        "orchestrator",
        output=args.reason,
    )


def cmd_task_edit(args, forge_dir: Path):
    """Orchestrator edits a task in place instead of abandon+recreate (which would
    orphan its deps/contracts). Only the orchestrator, and only fields that don't
    rewrite history: spec, writes_scope, model, and add/remove deps & contracts.
    Editing scope re-opens collision risk, so re-run `validate` after."""
    conn = _connect(_db_path(forge_dir, args.session))
    t = conn.execute("SELECT status FROM task WHERE id=?", (args.task,)).fetchone()
    if not t:
        raise ForgeError(f"unknown task {args.task}")
    if t["status"] in TERMINAL:
        raise ForgeError(f"task {args.task} is terminal ({t['status']}); cannot edit")
    sets, params = [], []
    if args.spec is not None:
        sets.append("spec=?")
        params.append(args.spec)
    if args.impl_model is not None:
        sets.append("impl_model=?")
        params.append(args.impl_model)
    if args.review_model is not None:
        sets.append("review_model=?")
        params.append(args.review_model)
    if args.writes_scope is not None:
        sets.append("writes_scope=?")
        params.append(json.dumps(args.writes_scope))
    with conn:
        if sets:
            params.append(args.task)
            conn.execute(
                f"UPDATE task SET {', '.join(sets)}, updated_at={_now()} WHERE id=?",
                params,
            )
        for d in args.add_dep or []:
            if not conn.execute("SELECT 1 FROM task WHERE id=?", (d,)).fetchone():
                raise ForgeError(f"add-dep references unknown task {d}")
            conn.execute(
                "INSERT OR IGNORE INTO task_dep(task_id, depends_on) VALUES(?,?)",
                (args.task, d),
            )
        for d in args.rm_dep or []:
            conn.execute(
                "DELETE FROM task_dep WHERE task_id=? AND depends_on=?", (args.task, d)
            )
    _out(
        {
            "task": args.task,
            "edited": True,
            "note": "scope/deps changed — re-run `validate` before advancing",
        }
    )


def cmd_contract_freeze(args, forge_dir: Path):
    """Orchestrator freezes a contract. Agents read these; they cannot write them.
    --source records who owns the decision the contract encodes: 'tuo' (the user
    owns it) or 'mio' (mechanical, user can override). A structural contract
    frozen 'mio' is a red flag that the idea was decided for the user."""
    conn = _connect(_db_path(forge_dir, args.session))
    if args.source not in ("tuo", "mio"):
        raise ForgeError(
            "--source is required: 'tuo' (user owns) or 'mio' (mechanical)"
        )
    cid = _new_id("c")
    body = args.body
    if args.body_file:
        body = Path(args.body_file).read_text()
    if not body:
        raise ForgeError("provide --body or --body-file")
    with conn:
        conn.execute(
            "INSERT INTO contract(id, name, body, source, frozen_at) VALUES(?,?,?,?,?)",
            (cid, args.name, body, args.source, _now()),
        )
    _out({"contract": cid, "name": args.name, "source": args.source})


def cmd_contract_challenge(args, forge_dir: Path):
    """An agent found a frozen contract infeasible. It does NOT edit it — it flags."""
    conn = _connect(_db_path(forge_dir, args.session))
    if not conn.execute(
        "SELECT 1 FROM contract WHERE id=?", (args.contract,)
    ).fetchone():
        raise ForgeError(f"unknown contract {args.contract}")
    chid = _new_id("ch")
    with conn:
        conn.execute(
            "INSERT INTO contract_challenge(id, contract_id, task_id, reason, "
            "resolved, created_at) VALUES(?,?,?,?,0,?)",
            (chid, args.contract, args.task, args.reason, _now()),
        )
    _out(
        {
            "challenge": chid,
            "contract": args.contract,
            "note": "orchestrator must resolve before dependent work proceeds",
        }
    )


def cmd_decision_add(args, forge_dir: Path):
    """Log a decision with provenance. source is CHECK-constrained to tuo|mio."""
    conn = _connect(_db_path(forge_dir, args.session))
    if args.source not in ("tuo", "mio"):
        raise ForgeError("source must be 'tuo' (human) or 'mio' (agent)")
    did = _new_id("d")
    with conn:
        conn.execute(
            "INSERT INTO decision(id, text, source, supersedes, created_at) "
            "VALUES(?,?,?,?,?)",
            (did, args.text, args.source, args.supersedes, _now()),
        )
        if args.supersedes:
            conn.execute(
                "UPDATE decision SET superseded=1 WHERE id=?", (args.supersedes,)
            )
    _out({"decision": did, "source": args.source})


def cmd_context(args, forge_dir: Path):
    """
    The bootstrap payload for a background agent given only a task_id.
    Returns: the task spec, writes_scope, its branch, the models, and the frozen
    contracts it may read. Nothing else from the session. Project context is not
    here — the agent reads the repo (code, docs, rules) directly.

    For a reviewer (--role reviewer), also returns how to see the task's diff:
    `git diff <base>...<branch>` isolates exactly this task's changes against the
    base it branched from, so the reviewer never deduces attribution from a shared
    tree. Includes prior reviews so the reviewer sees the convergence history.
    """
    conn = _connect(_db_path(forge_dir, args.session))
    t = conn.execute("SELECT * FROM task WHERE id=?", (args.task,)).fetchone()
    if not t:
        raise ForgeError(f"unknown task {args.task}")
    contracts = conn.execute(
        "SELECT c.id, c.name, c.body FROM contract c "
        "JOIN task_contract tc ON tc.contract_id=c.id WHERE tc.task_id=?",
        (args.task,),
    ).fetchall()
    payload = {
        "task": t["id"],
        "type": t["type"],
        "spec": t["spec"],
        "writes_scope": json.loads(t["writes_scope"] or "[]"),
        "branch": t["branch"],
        "base_ref": t["base_ref"],
        "worktree": t["worktree"],
        "impl_model": t["impl_model"],
        "review_model": t["review_model"],
        "contracts": [dict(c) for c in contracts],
        "forge_cli": _forge_cli(forge_dir, args.session),
        "session": args.session,
    }
    cli = payload["forge_cli"]
    s = args.session
    tid = t["id"]
    if args.role == "implementer":
        payload["preflight"] = [
            f"work ONLY in worktree: {t['worktree']}",
            f"branch: {t['branch']} (must match checked-out branch)",
            f"base_ref: {t['base_ref']} — if HEAD is behind base, call fail and stop",
        ]
        payload["exit_protocol"] = {
            "mandatory": True,
            "steps": [
                "commit all changes on the task branch",
                f'{cli} verify --session {s} --task {tid}',
                (
                    f'{cli} submit --session {s} --task {tid} --output "..."'
                    if t["type"] == "implement"
                    else f'{cli} resolve --session {s} --task {tid} --output "finding"'
                ),
            ],
            "on_block": f'{cli} fail --session {s} --task {tid} --output "blocked because ..."',
            "on_contract": f'{cli} contract-challenge --session {s} --contract <id> --task {tid} --reason "..."',
            "never": [
                "do not merge into integration branch",
                "do not review your own work",
                "do not skip submit/resolve",
            ],
        }
    if args.role == "reviewer":
        payload["diff_hint"] = (
            f"git -C {t['worktree']} diff {t['base_ref']}...{t['branch']}"
            if t["branch"] and t["base_ref"]
            else None
        )
        payload["output"] = t["output"]
        payload["prior_reviews"] = [
            dict(r)
            for r in conn.execute(
                "SELECT verdict, notes, created_at FROM review WHERE task_id=? "
                "ORDER BY created_at",
                (args.task,),
            )
        ]
        payload["exit_protocol"] = {
            "mandatory": True,
            "steps": [
                f"inspect diff: {payload.get('diff_hint')}",
                f'{cli} review-add --session {s} --task {tid} --verdict clean|problems --notes "..."',
            ],
            "never": [
                "do not fix code yourself",
                "do not merge",
                "do not call review-waive (orchestrator only)",
            ],
        }
    _out(payload)


def _validate_graph(conn):
    """
    Structural validation of the task graph, run at the tasks->implementing
    transition. Returns a list of problems (empty = valid). This is the
    self-validation the old `tasks` skill did, made mechanical:
      1. every task's declared contracts are all frozen
      2. no dependency cycles
      3. writes_scope disjoint among CONCURRENT tasks (tasks with no dependency
         path ordering them). Sequential tasks may share scope; they never run
         together.
    """
    problems = []
    tasks = conn.execute("SELECT id, writes_scope FROM task").fetchall()
    tids = [t["id"] for t in tasks]
    scope = {t["id"]: set(json.loads(t["writes_scope"] or "[]")) for t in tasks}

    # adjacency: task -> its dependencies
    deps = {t: set() for t in tids}
    for r in conn.execute("SELECT task_id, depends_on FROM task_dep"):
        if r["task_id"] in deps:
            deps[r["task_id"]].add(r["depends_on"])

    # 1. contracts frozen
    for r in conn.execute(
        "SELECT tc.task_id, tc.contract_id FROM task_contract tc "
        "LEFT JOIN contract c ON c.id = tc.contract_id WHERE c.id IS NULL"
    ):
        problems.append(
            f"task {r['task_id']} references unfrozen contract {r['contract_id']}"
        )

    # 2. cycle detection (DFS over deps)
    WHITE, GREY, BLACK = 0, 1, 2
    color = {t: WHITE for t in tids}

    def dfs(u, stack):
        color[u] = GREY
        for v in deps.get(u, ()):
            if v not in color:
                continue
            if color[v] == GREY:
                cyc = stack[stack.index(v) :] + [v]
                problems.append("dependency cycle: " + " -> ".join(cyc))
                return True
            if color[v] == WHITE and dfs(v, stack + [v]):
                return True
        color[u] = BLACK
        return False

    for t in tids:
        if color[t] == WHITE:
            if dfs(t, [t]):
                break

    # 3. transitive closure of "ordered by dependency" -> concurrency check
    # u is ordered wrt v if u reaches v or v reaches u through deps.
    def reaches(a, b, seen=None):
        seen = seen or set()
        for v in deps.get(a, ()):
            if v == b:
                return True
            if v not in seen:
                seen.add(v)
                if reaches(v, b, seen):
                    return True
        return False

    # 3. writes_scope disjoint among CONCURRENT tasks.
    # Only tasks that still have writes ahead of them can collide. A `done` task
    # is consolidated code in the repo; a `submitted` task has written and may yet
    # rewrite on needs_fix; pending/needs_fix/running will write. `done`/`abandoned`
    # are out — a new task touching their scope BUILDS on them (incremental
    # modification), it does not collide. Cycle detection above stays over ALL
    # tasks; a cycle is a structural defect regardless of state.
    ACTIVE = {"pending", "needs_fix", "running", "submitted", "in_review"}
    state = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM task")}
    for i in range(len(tids)):
        for j in range(i + 1, len(tids)):
            a, b = tids[i], tids[j]
            if state.get(a) not in ACTIVE or state.get(b) not in ACTIVE:
                continue
            if not scope[a] or not scope[b]:
                continue
            overlap = scope[a] & scope[b]
            if not overlap:
                continue
            ordered = reaches(a, b) or reaches(b, a)
            if not ordered:
                problems.append(
                    f"concurrent tasks {a} and {b} both write {sorted(overlap)} "
                    f"(no dependency orders them) -> they would collide"
                )
    return problems


def cmd_validate(args, forge_dir: Path):
    conn = _connect(_db_path(forge_dir, args.session))
    problems = _validate_graph(conn)
    _out({"valid": not problems, "problems": problems})


def cmd_phase(args, forge_dir: Path):
    """Advance (or report) the session phase. tasks->implementing is gated by
    graph validation; you cannot fan out over an invalid graph.
    Going backward (--back) is a real move for re-architecting/re-speccing after
    a review, but it is a human decision: it requires --confirm, meaning you have
    put the choice to the user first."""
    conn = _connect(_db_path(forge_dir, args.session))
    cur = _get_phase(conn)
    if not args.to:
        _out({"phase": cur})
        return
    if args.to not in PHASE_ORDER:
        raise ForgeError(f"phase must be one of {PHASES}")

    if args.back:
        # backward move: re-spec / re-architect after a review found a deeper defect
        if PHASE_ORDER[args.to] >= PHASE_ORDER[cur]:
            raise ForgeError(
                f"--back must target an earlier phase than '{cur}'; got '{args.to}'"
            )
        if not args.confirm:
            raise ForgeError(
                f"going back '{cur}' -> '{args.to}' is a human decision. Put the "
                f"choice to the user, then re-run with --confirm. This is expected "
                f"after a review reveals a contract or idea defect — not a failure."
            )
        with conn:
            conn.execute(
                "UPDATE session SET phase=?, updated_at=? WHERE id=?",
                (args.to, _now(), args.session),
            )
        _out(
            {
                "phase_from": cur,
                "phase_to": args.to,
                "direction": "back",
                "note": "revisit contracts/tasks that depended on the changed decision",
            }
        )
        return

    # forward: only one step at a time
    if PHASE_ORDER[args.to] != PHASE_ORDER[cur] + 1:
        raise ForgeError(
            f"cannot move phase '{cur}' -> '{args.to}'. "
            f"Phases advance one step forward: {PHASES} (use --back to go back)"
        )
    # gate: entering implementing requires a valid graph
    if args.to == "implementing":
        problems = _validate_graph(conn)
        if problems and not args.force:
            raise ForgeError(
                "task graph is invalid; refusing to enter 'implementing'. "
                "Fix these (or --force):\n  - " + "\n  - ".join(problems)
            )
    with conn:
        conn.execute(
            "UPDATE session SET phase=?, updated_at=? WHERE id=?",
            (args.to, _now(), args.session),
        )
    _out({"phase_from": cur, "phase_to": args.to})


def cmd_status(args, forge_dir: Path):
    """Full session snapshot for the orchestrator (or the human)."""
    conn = _connect(_db_path(forge_dir, args.session))
    phase = _get_phase(conn)
    tasks = conn.execute(
        "SELECT id, type, status, impl_model, review_model, branch FROM task ORDER BY created_at"
    ).fetchall()
    open_ch = conn.execute(
        "SELECT id, contract_id, task_id, reason FROM contract_challenge WHERE resolved=0"
    ).fetchall()
    _out(
        {
            "phase": phase,
            "tasks": [dict(r) for r in tasks],
            "open_challenges": [dict(r) for r in open_ch],
            "hint": f"run `next --session {args.session}` for legal next actions per task",
            "counts": {
                s: conn.execute(
                    "SELECT COUNT(*) c FROM task WHERE status=?", (s,)
                ).fetchone()["c"]
                for s in [
                    "pending",
                    "running",
                    "submitted",
                    "in_review",
                    "needs_fix",
                    "merged",
                    "resolved",
                    "failed",
                    "abandoned",
                ]
            },
        }
    )


def cmd_render(args, forge_dir: Path):
    """Human-readable markdown view generated FROM the db. The db stays the truth."""
    conn = _connect(_db_path(forge_dir, args.session))
    s = conn.execute("SELECT * FROM session LIMIT 1").fetchone()
    lines = [f"# Session {s['id']} — {s['title']}", "", f"**Phase:** {s['phase']}", ""]
    lines.append("## Tasks")
    for t in conn.execute("SELECT * FROM task ORDER BY created_at"):
        line = f"- `{t['id']}` [{t['type']}] **{t['status']}**"
        if t["branch"]:
            line += f" _({t['branch']})_"
        lines.append(line)
        if t["spec"]:
            lines.append(f"    - {t['spec'].splitlines()[0][:120]}")
        # show latest review verdict inline
        lastr = conn.execute(
            "SELECT verdict FROM review WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
            (t["id"],),
        ).fetchone()
        if lastr:
            lines.append(f"    - review: {lastr['verdict']}")
    lines.append("\n## Decisions")
    for d in conn.execute(
        "SELECT * FROM decision WHERE superseded=0 ORDER BY created_at"
    ):
        tag = "TUO" if d["source"] == "tuo" else "MIO"
        lines.append(f"- [{tag}] {d['text']}")
    lines.append("\n## Contracts (frozen)")
    for c in conn.execute("SELECT * FROM contract ORDER BY frozen_at"):
        tag = "TUO" if c["source"] == "tuo" else "MIO ⚠"
        lines.append(f"- [{tag}] **{c['name']}** (`{c['id']}`)")
    ch = conn.execute("SELECT * FROM contract_challenge WHERE resolved=0").fetchall()
    if ch:
        lines.append("\n## ⚠ Open contract challenges")
        for c in ch:
            lines.append(f"- `{c['contract_id']}`: {c['reason']}")
    print("\n".join(lines))


# ---- arg parsing ------------------------------------------------------------


def build_parser():
    p = argparse.ArgumentParser(
        prog="forge.py", description="Session work store (SQLite)."
    )
    p.add_argument(
        "--forge-dir", required=True, help="path to the project's .forge dir"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_session(sp):
        sp.add_argument("--session", required=True)

    sp = sub.add_parser("init", help="create a new session (id is auto-generated)")
    sp.add_argument(
        "--title",
        required=True,
        help="human-readable title; folder becomes <id>-<title-slug>",
    )
    sp.set_defaults(fn=cmd_init)

    sp = sub.add_parser("list", help="list all sessions with id, title, phase")
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser(
        "task-add", help="orchestrator: create a task (implement|spike)"
    )
    add_session(sp)
    sp.add_argument("--type", required=True, help="implement|spike")
    sp.add_argument("--spec", help="what this task must do")
    sp.add_argument(
        "--impl-model", dest="impl_model", help="model the implementer runs on"
    )
    sp.add_argument(
        "--review-model", dest="review_model", help="model the reviewer runs on"
    )
    sp.add_argument(
        "--writes-scope",
        nargs="*",
        dest="writes_scope",
        help="modules/paths this task may write",
    )
    sp.add_argument("--depends-on", nargs="*", dest="depends_on", help="task ids")
    sp.add_argument("--contract", nargs="*", help="contract ids this task may read")
    sp.set_defaults(fn=cmd_task_add)

    sp = sub.add_parser(
        "task-add-batch", help="orchestrator: create many tasks atomically (JSON array)"
    )
    add_session(sp)
    sp.add_argument("--file", help="JSON file; if omitted, reads stdin")
    sp.set_defaults(fn=cmd_task_add_batch)

    sp = sub.add_parser(
        "runnable", help="list tasks whose deps are all merged/resolved"
    )
    add_session(sp)
    sp.set_defaults(fn=cmd_task_runnable)

    sp = sub.add_parser(
        "start", help="orchestrator: pending->running (registers worktree)"
    )
    add_session(sp)
    sp.add_argument("--task", required=True)
    sp.add_argument(
        "--branch", required=True, help="a real, readable branch name you choose"
    )
    sp.add_argument(
        "--base",
        required=True,
        help="HEAD of the integration branch NOW (includes incremental merges)",
    )
    sp.set_defaults(fn=cmd_task_start)

    sp = sub.add_parser(
        "next", help="exact next actions for a task (or all active tasks)"
    )
    add_session(sp)
    sp.add_argument("--task", help="one task; omit for all non-terminal tasks")
    sp.set_defaults(fn=cmd_next)

    sp = sub.add_parser(
        "verify", help="preflight git/worktree checks for a task"
    )
    add_session(sp)
    sp.add_argument("--task", required=True)
    sp.set_defaults(fn=cmd_verify)

    sp = sub.add_parser(
        "rerun",
        help="needs_fix|failed -> running on same branch (respawn checklist)",
    )
    add_session(sp)
    sp.add_argument("--task", required=True)
    sp.add_argument(
        "--base",
        help="refresh base_ref to current integration HEAD (after dep merge)",
    )
    sp.set_defaults(fn=cmd_rerun)

    sp = sub.add_parser("submit", help="implementer: running->submitted")
    add_session(sp)
    sp.add_argument("--task", required=True)
    sp.add_argument("--output", help="what the agent produced / where")
    sp.set_defaults(fn=cmd_submit)

    sp = sub.add_parser("fail", help="implementer: running->failed")
    add_session(sp)
    sp.add_argument("--task", required=True)
    sp.add_argument("--output")
    sp.set_defaults(fn=cmd_fail)

    sp = sub.add_parser(
        "resolve", help="spike: running->resolved (knowledge delivered)"
    )
    add_session(sp)
    sp.add_argument("--task", required=True)
    sp.add_argument("--output", help="the finding / verdict")
    sp.set_defaults(fn=cmd_resolve)

    sp = sub.add_parser(
        "review-add", help="reviewer: record a verdict; task->in_review"
    )
    add_session(sp)
    sp.add_argument("--task", required=True)
    sp.add_argument("--verdict", required=True, help="clean|problems")
    sp.add_argument("--notes")
    sp.set_defaults(fn=cmd_review_add)

    sp = sub.add_parser(
        "review-waive",
        help="orchestrator: skip reviewer spawn (audited waiver, submitted only)",
    )
    add_session(sp)
    sp.add_argument("--task", required=True)
    sp.add_argument("--reason", required=True, help="why review is skipped (min 10 chars)")
    sp.set_defaults(fn=cmd_review_waive)

    sp = sub.add_parser(
        "needs-fix", help="orchestrator: in_review->needs_fix (problems)"
    )
    add_session(sp)
    sp.add_argument("--task", required=True)
    sp.set_defaults(fn=cmd_needs_fix)

    sp = sub.add_parser(
        "merge", help="coordinator: record a task merged (git done by you)"
    )
    add_session(sp)
    sp.add_argument("--task", required=True)
    sp.add_argument("--force", action="store_true", help="merge without a clean review")
    sp.set_defaults(fn=cmd_merge)

    sp = sub.add_parser("abandon", help="orchestrator: kill a task (wrong/superseded)")
    add_session(sp)
    sp.add_argument("--task", required=True)
    sp.add_argument("--reason", help="why it was abandoned")
    sp.set_defaults(fn=cmd_abandon)

    sp = sub.add_parser(
        "task-edit", help="orchestrator: edit a task in place (no orphans)"
    )
    add_session(sp)
    sp.add_argument("--task", required=True)
    sp.add_argument("--spec")
    sp.add_argument("--impl-model", dest="impl_model")
    sp.add_argument("--review-model", dest="review_model")
    sp.add_argument("--writes-scope", nargs="*", dest="writes_scope")
    sp.add_argument(
        "--add-dep", nargs="*", dest="add_dep", help="task ids to add as deps"
    )
    sp.add_argument(
        "--rm-dep", nargs="*", dest="rm_dep", help="task ids to remove as deps"
    )
    sp.set_defaults(fn=cmd_task_edit)

    sp = sub.add_parser("contract-freeze", help="orchestrator: freeze a contract")
    add_session(sp)
    sp.add_argument("--name", required=True)
    sp.add_argument(
        "--source", required=True, help="tuo (user owns) | mio (mechanical)"
    )
    sp.add_argument("--body")
    sp.add_argument("--body-file")
    sp.set_defaults(fn=cmd_contract_freeze)

    sp = sub.add_parser(
        "contract-challenge", help="agent: flag a contract as infeasible"
    )
    add_session(sp)
    sp.add_argument("--contract", required=True)
    sp.add_argument("--task", required=True)
    sp.add_argument("--reason", required=True)
    sp.set_defaults(fn=cmd_contract_challenge)

    sp = sub.add_parser(
        "decision-add", help="log a decision with [tuo]/[mio] provenance"
    )
    add_session(sp)
    sp.add_argument("--text", required=True)
    sp.add_argument("--source", required=True, help="tuo|mio")
    sp.add_argument("--supersedes", help="decision id this replaces")
    sp.set_defaults(fn=cmd_decision_add)

    sp = sub.add_parser("context", help="agent bootstrap payload for a task_id")
    add_session(sp)
    sp.add_argument("--task", required=True)
    sp.add_argument("--role", default="implementer", help="implementer|reviewer")
    sp.set_defaults(fn=cmd_context)

    sp = sub.add_parser("status", help="session snapshot (json)")
    add_session(sp)
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("render", help="markdown view generated from the db")
    add_session(sp)
    sp.set_defaults(fn=cmd_render)

    sp = sub.add_parser("phase", help="report or advance the session phase")
    add_session(sp)
    sp.add_argument("--to", help="target phase: architect|tasks|implementing|done")
    sp.add_argument(
        "--force",
        action="store_true",
        help="enter implementing despite an invalid graph",
    )
    sp.add_argument(
        "--back",
        action="store_true",
        help="go back a phase (re-spec/re-architect); needs --confirm",
    )
    sp.add_argument(
        "--confirm",
        action="store_true",
        help="confirm a --back move (a human decision)",
    )
    sp.set_defaults(fn=cmd_phase)

    sp = sub.add_parser("validate", help="structural check of the task graph")
    add_session(sp)
    sp.set_defaults(fn=cmd_validate)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    forge_dir = Path(args.forge_dir).resolve()
    try:
        # phase gate: reject commands issued in the wrong session phase.
        # init has no session yet; skip. Commands operating on a session go
        # through the gate before their handler runs.
        if args.cmd not in ("init",) and getattr(args, "session", None):
            db = _db_path(forge_dir, args.session)
            if db.exists():
                gate_conn = _connect(db)
                try:
                    _require_phase(gate_conn, args.cmd)
                finally:
                    gate_conn.close()
        args.fn(args, forge_dir)
    except ForgeError as e:
        print(f"forge: {e}", file=sys.stderr)
        sys.exit(2)
    except sqlite3.IntegrityError as e:
        # a CHECK / FK fired — the db refused an illegal row
        print(f"forge: rejected by db integrity constraint: {e}", file=sys.stderr)
        sys.exit(3)
    except FileExistsError:
        # two inits raced on the same id; the folder-create lost
        print("forge: session id collided (concurrent init?); retry", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
