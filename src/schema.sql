-- forge session store schema.
-- The DB is not just storage, it is a GUARD: illegal rows are refused here,
-- so a misbehaving agent cannot write a state the design forbids. This mirrors
-- the project's own rule (don't trust application discipline; let the DB reject
-- the row).

PRAGMA foreign_keys = ON;

CREATE TABLE session (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    -- A session IS one implementation-spec on an existing project, not the
    -- project itself. It has a lifecycle; the phase is enforced so you cannot
    -- fan out implementers before the graph is validated, cannot freeze
    -- contracts before there is an architecture, etc.
    phase       TEXT NOT NULL DEFAULT 'spec'
                CHECK (phase IN ('spec','architect','tasks','implementing','done')),
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER
);

-- The task FSM. `status` is CHECK-constrained to the legal state set, and a
-- BEFORE UPDATE trigger (below) enforces the legal *transitions* in the DB
-- itself — so even direct SQL can't jump states. forge.py adds the WHO layer
-- (which role may make each move). A task is either an `implement` (goes to
-- merged via review) or a `spike` (goes to resolved: knowledge, no merge).
CREATE TABLE task (
    id           TEXT PRIMARY KEY,
    type         TEXT NOT NULL CHECK (type IN ('implement','spike')),
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','running','submitted','in_review',
                                   'needs_fix','merged','resolved','failed','abandoned')),
    -- two models: the implementer runs on one, the reviewer on another (a
    -- pedantic reviewer often wants a stronger model than the dumb executor).
    -- spikes are not reviewed, so review_model is unused for them.
    impl_model   TEXT,
    review_model TEXT,
    branch       TEXT,               -- git branch, orchestrator-chosen (real, not a hash)
    base_ref     TEXT,               -- what the worktree branched from (HEAD of the
                                     -- integration branch AT START — incremental merge)
    worktree     TEXT,               -- filesystem path of the worktree, under the session dir
    worktree_state TEXT NOT NULL DEFAULT 'none'
                 CHECK (worktree_state IN ('none','live','removed','orphaned')),
    writes_scope TEXT NOT NULL DEFAULT '[]',  -- JSON array: the task's write lane
    spec         TEXT NOT NULL DEFAULT '',    -- what this task must do
    output       TEXT,               -- agent's report / artifact pointer
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER
);

-- State-transition guard, enforced by the DB itself so it holds even against
-- direct SQL (a raw `UPDATE task SET status=...` that would bypass the CLI's
-- role-based FSM). The CLI adds the WHO layer (which role may make a move) on
-- top; this trigger enforces WHICH moves are legal at all. Terminal states
-- (merged/resolved/abandoned) permit no further status change. Keep this list
-- in sync with TRANSITIONS in forge.py.
CREATE TRIGGER task_status_fsm
BEFORE UPDATE OF status ON task
FOR EACH ROW
WHEN OLD.status <> NEW.status AND NOT (
    (OLD.status='pending'   AND NEW.status IN ('running','abandoned')) OR
    (OLD.status='running'   AND NEW.status IN ('submitted','failed','resolved','abandoned')) OR
    (OLD.status='submitted' AND NEW.status IN ('in_review','abandoned')) OR
    (OLD.status='in_review' AND NEW.status IN ('merged','needs_fix','abandoned')) OR
    (OLD.status='needs_fix' AND NEW.status IN ('running','abandoned')) OR
    (OLD.status='failed'    AND NEW.status IN ('needs_fix','abandoned','running'))
)
BEGIN
    SELECT RAISE(ABORT, 'illegal task status transition');
END;

CREATE TABLE task_dep (
    task_id     TEXT NOT NULL REFERENCES task(id),
    depends_on  TEXT NOT NULL REFERENCES task(id),
    PRIMARY KEY (task_id, depends_on),
    CHECK (task_id <> depends_on)     -- no self-dependency
);

-- Frozen contracts. Agents READ these (via `context`); there is no CLI path
-- for an agent role to write here — only the orchestrator's contract-freeze.
CREATE TABLE contract (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    body       TEXT NOT NULL,
    -- who owns the decision this contract encodes. A structural contract frozen
    -- 'mio' is a visible red flag that the agent decided the idea for the user.
    source     TEXT NOT NULL CHECK (source IN ('tuo','mio')),
    frozen_at  INTEGER NOT NULL
);

CREATE TABLE task_contract (
    task_id      TEXT NOT NULL REFERENCES task(id),
    contract_id  TEXT NOT NULL REFERENCES contract(id),
    PRIMARY KEY (task_id, contract_id)
);

-- When an agent finds a frozen contract infeasible it does NOT edit it; it
-- files a challenge that the orchestrator must resolve.
CREATE TABLE contract_challenge (
    id           TEXT PRIMARY KEY,
    contract_id  TEXT NOT NULL REFERENCES contract(id),
    task_id      TEXT REFERENCES task(id),
    reason       TEXT NOT NULL,
    resolved     INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0,1)),
    created_at   INTEGER NOT NULL
);

-- Review verdicts on a task. Review is NOT a task — it is a phase an implement
-- task passes through. One-to-many, ordered by time: a task that goes
-- implement -> review(problems) -> fix -> review(clean) has two rows. The latest
-- decides the merge; all rows together are the convergence trail. Written by a
-- reviewer agent (a different model from the implementer). A review never
-- mutates code or task status by itself; the orchestrator reads the verdict and
-- moves the task to merged or needs_fix.
CREATE TABLE review (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES task(id),
    verdict     TEXT NOT NULL CHECK (verdict IN ('clean','problems','waived')),
    notes       TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL
);

-- Decision log with provenance. `source` is CHECK-locked: a decision with no
-- provenance, or an invented source, cannot enter the table at all.
CREATE TABLE decision (
    id          TEXT PRIMARY KEY,
    text        TEXT NOT NULL,
    source      TEXT NOT NULL CHECK (source IN ('tuo','mio')),
    supersedes  TEXT REFERENCES decision(id),
    superseded  INTEGER NOT NULL DEFAULT 0 CHECK (superseded IN (0,1)),
    created_at  INTEGER NOT NULL
);

CREATE INDEX idx_task_status ON task(status);
CREATE INDEX idx_review_task ON review(task_id);
CREATE INDEX idx_dep_task ON task_dep(task_id);
