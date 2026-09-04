# Capturing Answers in OpenSpec

How the answers to [`Architecture-Open-Questions.md`](./Architecture-Open-Questions.md) get recorded.

Short version: **the answers don't go in `docs/` — they go into a *change* under
`openspec/changes/`, mostly in `design.md` (decisions + rationale) and the delta `spec.md` files
(the resulting requirements).** There are currently zero changes, so step one is creating one.

---

## Where each thread's answer lands

```
openspec/changes/<change-name>/          (schema order: proposal -> specs -> design -> tasks)
|-- proposal.md   <- what / why / scope   (Thread E may split into its own change)
|-- specs/
|   |-- aggregation/spec.md        <- A (intrabar cadence), B (time-driven close),
|   |                                 D (recovery semantics) as REQUIREMENTS
|   |-- streaming-gateway/spec.md  <- A (conflation), slow-consumer policy
|   |-- ...                        <- C, F as resiliency requirements
|-- design.md     <- the DECISION + rationale for A, B, C, D, F
|                    "we chose X over Y because Z"
|-- tasks.md      <- the implementation checklist that falls out
```

Rule of thumb:
- **`design.md`** = the reasoning and the trade-off you picked.
- **`spec.md`** = the observable behavior that must hold
  (e.g. "a 1m bar MUST close within 100ms of its boundary regardless of tick arrival").

---

## Two layers of commands

There are two distinct things called "commands" here - keep them separate:

| Layer | Looks like | Who runs it | What it is |
|---|---|---|---|
| **Claude skill** | `/opsx:propose`, `/opsx:apply`, ... (typed at the Claude Code prompt) | **You type it** | A packaged workflow. Claude follows it, and internally runs the `openspec` CLI for you. |
| **`openspec` CLI** | `openspec new change ...`, `openspec status ...` | Claude runs it (inside a skill). You only run it by hand for read-only checks. | The underlying tool that scaffolds dirs, tracks status, validates. |

**You almost never type `openspec ...` yourself.** You type an `/opsx:*` skill; the skill drives the CLI.

### The `/opsx:*` skills

| Skill | You type | What it does | Artifacts it touches |
|---|---|---|---|
| `/opsx:explore` | `/opsx:explore [topic]` | Thinking-partner mode (this session). No code. *Can* capture change artifacts when you explicitly confirm scope. | any change artifact, on request |
| `/opsx:propose` | `/opsx:propose <change-name>` | One shot: scaffolds the change **and** generates `proposal` + `specs` + `design` + `tasks`. | all four, created |
| `/opsx:update` | `/opsx:update <change-name>` | Revise the artifacts of an existing change and keep them mutually coherent. Never writes code. | any subset, revised |
| `/opsx:apply` | `/opsx:apply <change-name>` | Implement the change - works through `tasks.md`, **writes code**, checks off tasks. | `tasks.md` (checkboxes) + source code |
| `/opsx:sync` | `/opsx:sync <change-name>` | Merge the change's delta `specs/*/spec.md` into the project's **main** specs (`openspec/specs/`). Change stays active. | `openspec/specs/**`, updated |
| `/opsx:archive` | `/opsx:archive <change-name>` | After implementation: sync specs **and** move the change to `openspec/changes/archive/`. | main specs + archive move |

---

## Two ways to do it

### Path 1 - one-shot generate, then refine (fastest)

You type:

```
/opsx:propose add-price-pipeline
```

...then in the same message paste (or point at) the architecture doc + your thread answers.
The `/opsx:propose` skill scaffolds the change and generates proposal + specs + design + tasks in
one pass. You review/edit the files, then `/opsx:apply add-price-pipeline`.

Best when your answers to threads A-F are already settled.

### Path 2 - stay in explore, capture incrementally

You are already in `/opsx:explore` (this session).

1. Answer threads A-F - in chat, or by editing `Architecture-Open-Questions.md`.
2. When a batch settles, tell Claude "capture A/B/D now". Claude - still inside `/opsx:explore`,
   with your explicit go-ahead - runs `openspec new change` and writes the relevant artifacts.
3. As later threads land, you type `/opsx:update add-price-pipeline` so Claude folds the new
   decisions in and re-checks that proposal/specs/design/tasks still agree with each other.
4. When the change is complete, leave explore and type `/opsx:apply add-price-pipeline`.

Best when threads still have open sub-choices to talk through (B, C, D do).

---

## Suggested sequence (Path 2, step by step)

Three actors:

- **You** - make the design decisions, type the `/opsx:*` skills, review and edit prose, approve scope.
- **`/opsx:*` skill** (Claude) - runs the `openspec` CLI, drafts artifact content from your answers, writes the artifact files.
- **`openspec` CLI** - scaffolds the change, tracks artifact status, validates. Invoked by the skill, not by you.

---

### Step 1 - Decide the answers

| | |
|---|---|
| **Skill in use** | `/opsx:explore` (already active) |
| **You** | Answer threads A-F. Write them into `Architecture-Open-Questions.md`, or paste them here thread by thread. Each answer says *what* you chose and *why* (the "why" becomes `design.md`, the "what" becomes `spec.md`). |
| **Claude** | May ask follow-ups where a thread has unresolved sub-choices. Writes nothing to OpenSpec yet. |
| **CLI run** | none |
| **Artifacts** | `docs/Architecture-Open-Questions.md` only - not an OpenSpec artifact yet |

Gate: don't move on until at least the A / B / D cluster (`aggregation-svc` behavior) is settled.

---

### Step 2 - Scaffold the change

| | |
|---|---|
| **Skill in use** | `/opsx:explore` |
| **You** | Confirm the change name (proposed: `add-price-pipeline`) and say "go ahead and scaffold it". |
| **Claude** | Runs the CLI, then reports status. |
| **CLI run** | `openspec new change "add-price-pipeline"` then `openspec status --change "add-price-pipeline" --json` |
| **Artifacts created** | `openspec/changes/add-price-pipeline/` + `.openspec.yaml` (metadata - never hand-edited). Status shows `proposal` = `ready`, the rest `blocked`. |

---

### Step 3 - Write `proposal.md`

| | |
|---|---|
| **Skill in use** | `/opsx:explore` ("capture the proposal now") - or start fresh with `/opsx:propose add-price-pipeline` which does Steps 2-6 in one go |
| **You** | Give the go-ahead. Afterward, review and edit the scope - confirm whether Thread E is *in* this change or split out. |
| **Claude** | Fetches instructions, drafts the file from the architecture doc: Phase 1 scope, what/why, non-goals (Dukascopy, cross-provider view, auth). |
| **CLI run** | `openspec instructions "proposal" --change "add-price-pipeline" --json` |
| **Artifact** | `openspec/changes/add-price-pipeline/proposal.md`. Status then shows `specs` = `ready`. |

This schema's artifact order is **proposal -> specs -> design -> tasks** - requirements before rationale.

---

### Step 4 - Write the delta specs (the requirements)

| | |
|---|---|
| **Skill in use** | `/opsx:explore` ("capture the specs now") |
| **You** | Go-ahead. Then review each requirement - observable? testable? threshold right (e.g. "close within 100ms of boundary")? |
| **Claude** | Drafts one spec per capability: `specs/aggregation/spec.md` (A, B, D as MUST/SHOULD), `specs/streaming-gateway/spec.md` (A conflation, slow-consumer policy), resiliency requirements for C / F. |
| **CLI run** | `openspec instructions "specs" --change "add-price-pipeline" --json` |
| **Artifacts** | `openspec/changes/add-price-pipeline/specs/*/spec.md`. Status then shows `design` = `ready`. |

---

### Step 5 - Write `design.md` (the rationale)

| | |
|---|---|
| **Skill in use** | `/opsx:explore` ("capture the design now") |
| **You** | Go-ahead. Then review the reasoning; correct anything Claude inferred wrong from your Step 1 answers. |
| **Claude** | Drafts one section per decided thread - A, B, C, D, F - each as "decision + rationale + alternatives rejected", tied back to the Step 4 requirements. Reads `proposal.md` + `specs/*/spec.md` first. |
| **CLI run** | `openspec instructions "design" --change "add-price-pipeline" --json` |
| **Artifact** | `openspec/changes/add-price-pipeline/design.md`. Status then shows `tasks` = `ready`. |

---

### Step 6 - Write `tasks.md`

| | |
|---|---|
| **Skill in use** | `/opsx:explore` ("capture the tasks now") |
| **You** | Go-ahead. Then review ordering and granularity. |
| **Claude** | Drafts the implementation checklist that falls out of the specs. Reads all prior artifacts first. |
| **CLI run** | `openspec instructions "tasks" --change "add-price-pipeline" --json` |
| **Artifact** | `openspec/changes/add-price-pipeline/tasks.md` |

---

### Step 7 - Validate

| | |
|---|---|
| **Skill in use** | `/opsx:explore` |
| **You** | Ask Claude to validate. Final read-through of the whole change. |
| **Claude** | Runs the check, reports problems if any. |
| **CLI run** | `openspec validate --change "add-price-pipeline"` |

---

### Step 8 - Revise as later threads land (optional, repeatable)

| | |
|---|---|
| **Skill in use** | `/opsx:update add-price-pipeline` |
| **You** | Type the skill, describe the new decision (e.g. "we changed the Thread C trim strategy to consumer-position-driven"). |
| **Claude** | Edits the affected artifacts and re-checks that proposal / specs / design / tasks still agree. |
| **CLI run** | `openspec status ...`, `openspec instructions ...` for the touched artifacts, `openspec validate ...` |
| **Artifacts** | whichever artifacts the new decision touches |

---

### Step 9 - Sync specs to main (optional, before implementation is done)

| | |
|---|---|
| **Skill in use** | `/opsx:sync add-price-pipeline` |
| **You** | Type the skill when you want the delta specs reflected in `openspec/specs/` without archiving yet. |
| **Claude** | Merges each delta `spec.md` into the corresponding main spec. |
| **CLI run** | `openspec status ...`, `openspec validate --specs` |
| **Artifacts** | `openspec/specs/**` (created/updated). Change stays active. |

Skippable - `/opsx:archive` does this same sync as its first step.

---

### Step 10 - Implement (leaves explore mode)

| | |
|---|---|
| **Skill in use** | `/opsx:apply add-price-pipeline` |
| **You** | Exit `/opsx:explore` first (explore mode never writes code). Type the skill. Review the code it produces. |
| **Claude** | Works through `tasks.md`, writes source code, checks off each task. |
| **CLI run** | `openspec status ...`, `openspec instructions apply --change ...` |
| **Artifacts** | source code + `tasks.md` checkboxes |

---

### Step 11 - Archive

| | |
|---|---|
| **Skill in use** | `/opsx:archive add-price-pipeline` |
| **You** | Type the skill once implementation is complete and verified. |
| **Claude** | Syncs delta specs into main specs (if not already done), then moves the change to `openspec/changes/archive/`. |
| **CLI run** | `openspec archive "add-price-pipeline"` (plus the sync + validate it wraps) |
| **Artifacts** | `openspec/specs/**` finalized; `openspec/changes/add-price-pipeline/` moved under `archive/` |

---

### Thread E as a separate change

If synthetic-feed fidelity is scoped out of `add-price-pipeline`, repeat Steps 2-7 (or a single
`/opsx:propose add-synthetic-feed-fidelity`) for a second change - its `design.md` covers the
fidelity target and calibration source, its `spec.md` covers the acceptance checks (statistical
properties the generator must reproduce).

---

## Read-only commands you might type yourself

These don't change anything - safe to run at the Claude Code prompt with `! ` prefix, or ask Claude to run them:

| Command | Purpose |
|---|---|
| `! openspec list --json` | Show active changes |
| `! openspec status --change "add-price-pipeline" --json` | Which artifacts are ready / blocked / done |
| `! openspec show add-price-pipeline` | Display the change |
| `! openspec validate --change "add-price-pipeline"` | Check the change is well-formed |
| `! openspec view` | Interactive dashboard of specs + changes |

Everything that *writes* (`openspec new change`, `openspec instructions`-driven drafting, `openspec archive`) goes through an `/opsx:*` skill.

Artifact dependency order (schema `spec-driven`): `proposal.md` -> `specs/*/spec.md` -> `design.md` -> `tasks.md`.
