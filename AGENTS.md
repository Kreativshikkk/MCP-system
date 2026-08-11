# AGENTS.md

Context for coding agents (Codex, Claude, etc.) working in this repository.
Read this first. It explains **what this project is**, **how the pipeline is
wired**, and — most importantly — the **invariants you must not break**.

---

## 1. What this project is

This repo builds a **benchmark / evaluation harness for AI software engineers**.

The subject under evaluation is **Air** — a JetBrains coding-agent product. This
harness measures how well Air performs realistic day-to-day engineering tasks
end-to-end, through the same MCP tools a human would use.

The core idea: we run a *simulated AI software company* that does real work
through **MCP tools** (task tracker, codebase, CI, reviews, etc.). The only
durable artifact of that company is a **realistic trace of work in MCP
services**. Into that trace we deliberately introduce defects / tasks, capture
the correct answer (**ground truth, GT**) *at the moment of perturbation*, and
then ask **Air** to complete each task in a clean, sealed harness. An **Oracle**
decides whether Air succeeded.

The whole system is described by the flowchart (`schema`). This file is the
prose companion to that diagram. When something here says "see the *X* block",
it refers to a box in that flowchart.

The value we care about is **decorrelation and anti-circularity**: the process
that *creates* tasks/defects must not be secretly aligned with the process that
*solves* them, and the GT must never be re-invented by an LLM after the fact.

---

## 2. High-level pipeline

Left-to-right through the flowchart:

1. **Inputs** — sampled `Goal`, `MCP set`, `Tech Stack`, `Idea`, `templates`.
2. **Environment + spec** — a Docker environment (deps) and a project spec are
   generated. This exact env is reused later at eval time (see §7).
3. **Director init** — a `Director` agent is initialized with a system prompt.
   The Director is an **annotator of facts, not a generator of ground truth**.
4. **Team + roles** — Director defines roles (Team Lead/PM, Engineers, QA, …)
   and, per role, the exact interaction protocol with the codebase, task
   tracker, other MCP tools, and processes (testing, PRs, reviews, releases).
5. **Milestones** — ~10 milestones/sprints; cadence is **1 release per
   milestone + 3 standups per milestone**. The first milestone produces the
   **repo baseline**.
6. **Per-milestone task/defect sampling** — number `M` is drawn from a Poisson
   distribution with `λ = 1/N` (N = number of sampled simulation templates).
   Then a template is sampled `M` times; in expectation ~1 issue of each type
   per milestone.
7. **Perturbation + GT capture** — Director produces each task/leak and records
   its GT *at perturbation time* via one of two mechanisms (see §3).
8. **Filtering** — only valid, reproducible tasks survive (see §5 / the
   `Filtering` block).
9. **Oracle** — evaluates Air in a hermetic harness and emits a **Final Score**
   (see §6).

Two side outputs: **final tasks (with injections)** flow toward task completion
(→ merge into `main` only if CI is green; otherwise revert to `main`);
**persisting tasks** branch off. The green informational tasks (RN / IT / SU)
produce an artifact (`.md` / tracker fields) whose GT is captured directly.

---

## 3. The two perturbation mechanisms (the unifying principle)

Every one of the nine task types is produced by **one of two mechanisms**. This
is the most important structural fact about the harness — everything downstream
(filtering, oracle) is built on top of it.

**A. Director prompt injection (`director-driven`)**
The Director injects a defect or an instruction into the prompts / PRs / tickets
the simulated team acts on — a logic change, a typo, a dropped change, a planted
review issue. GT = the exact injection (location + diff), recorded at inject
time.

**B. Repo snapshot before/after (`snapshot-driven`)**
We capture two states of a real repo — a **correct** state and a **degraded**
state — and hand Air the degraded one. GT = the correct state (or the diff
between them). Covers: reverting a real fix, stripping tests from a PR, starting
from outdated deps, constructing a merge conflict, fixing the merge-range for
release notes.

> Both mechanisms **record a fact** at perturbation time. Neither lets an LLM
> decide the correct answer later. This is what anti-circularity rests on.

A third GT flavor exists only for the **subjective quality** of informational
artifacts: **judge-rubric** (used for release-notes wording, standup phrasing,
triage analysis) — never for the factual layer.

---

## 4. The nine task types

Mapped 1:1 to the `Automation Templates`. Two natural groups:

### Group I — informational (GT = recorded artifact/fact + rubric on quality)

| # | Template | How it's set up | Ground truth (GT) | Mechanism |
|---|----------|-----------------|-------------------|-----------|
| 1 | **Generate Release Notes** | Fix the last-release merge range | Real merges in range (by SHA); wording scored by rubric | snapshot + rubric |
| 4 | **Issue Triage** | Real issue; Lead records correct triage | Correct labels/fields set by Lead; analysis by rubric | annotated + rubric |
| 7 | **Standup Update** | Real trace over the period | Real events/state in the window; phrasing by rubric | annotated + rubric |

### Group II — defect-injection ("насрать ⇒ fix", GT = mechanical: green CI / diff)

| # | Template | How the defect is created | Ground truth (GT) | Mechanism |
|---|----------|---------------------------|-------------------|-----------|
| 2 | **Fix Bug** | Revert a real fix (snapshot) or inject | Reverting diff; green CI/tests restored | snapshot / injection |
| 3 | **Code Review** | Inject a defect into a PR | The injected defect (location + diff); review must catch it | injection |
| 5 | **Cover PR with Tests** | Strip existing tests from a PR (snapshot) | Removed coverage; re-added tests green + cover the change | snapshot |
| 6 | **Fix Failed CI** | Inject / snapshot a defect that breaks CI | Root-cause fix; green CI restored | injection / snapshot |
| 8 | **Dependency Upgrade** | Start from heavily outdated deps; real upgrade PR **rejected** so the task stays repeatable, and we **do not persist the update** | The deps decision / upgrade (by SHA) | snapshot / annotated |
| 9 | **Resolve Merge Conflicts** | Construct two diverging branches → conflict (snapshot) | Correct merged resolution; clear-cut resolved, ambiguous flagged | snapshot + rubric on flagging |

Notes:
- **Standup (7)** cadence: Lead runs a standup once per *n* days; participants
  are `director/lead` + `engineer`.
- **Dependency Upgrade (8)**: the upgrade is intentionally **not saved** to the
  repo state, so re-running the task always starts from the same outdated deps.

---

## 5. Anti-circularity & filtering (do not break)

**GT is fixed at the moment of perturbation and is never re-invented by an LLM
later.** GT capture flavors: **mechanical** (we know the injected diff / snapshot
diff ⇒ we know GT), **annotated** (a real fact recorded by SHA / by the Lead),
and **judge-rubric** (subjective quality layer only).

### 5.1 Trap-tests
- A **trap-test** is green on the correct code and red under the defect.
- Preferred: `trap_test_ref` points to an **existing test written by the team
  during the simulation**, not a new test authored around the bug. The Director
  *marks which existing tests go red*; it does not write mirror tests.
- A trap-test must **never mirror the defect**.

### 5.2 Filtering gate (the `Filtering` block)
Keep a task only if **all** hold:
- **Differential check** — baseline CI green ∧ perturbed CI red (≥1 trap fails).
  No red ⇒ equivalent / no-op ⇒ **drop**.
- **GT-solvability** — applying GT restores green. GT doesn't fix ⇒ **drop**.
- **Trap-test integrity** — trap pre-exists in correct code, not a mirror.
- **Determinism** — no flaky tests (time/network/order); task must be
  repeatable ⇒ else **drop**.
- **Non-triviality** — drop trivially detectable (won't build, obvious typo) and
  unsolvable/degenerate; keep the target difficulty band.
- **Dedup & decorrelation** — drop near-duplicates; preserve diversity across
  mechanisms and across attack sources (director / snapshot / mutation).

(Group I informational tasks are graded on artifact/fact match + rubric; the
differential-CI check applies to Group II defect tasks.)

---

## 6. Oracle — how Air is evaluated

Two layers.

### 6.1 Clean harness (constraints on Air)
1. **Air has no access to tests / CI config.** The hidden trap-suite is held out
   of the repo state Air sees and revealed only inside the Oracle.
2. Air works **only via MCP tools**, intercepted exactly as during task creation.
3. The eval env is the **same** env generated at task-creation time (a fresh,
   sealed replica — Air cannot mutate the grader, tests, or GT).

> "No access to tests/CI" is a property of the **Oracle harness relative to
> Air**, *not* a restriction on the simulated Engineers/QA — they see and write
> tests freely during the simulation (§8).

### 6.2 Mechanical verdict (source of truth)
- Verdict = re-run of the pre-captured trap-tests / GT check. The Oracle
  **applies** GT, it does **not** regenerate it.
- Pass criterion is **differential**: baseline green ∧ Air's patch keeps/returns
  the **full suite** green. Binary pass/fail per defect.
- Mechanical **red → fail immediately** (LLM layer not called).

### 6.3 LLM auditor (legitimacy layer, only on a mechanical pass)
The mechanical layer decides *green/red*; the LLM layer only decides whether a
green was obtained **legitimately**. It **never** issues the verdict and **never**
re-derives GT. Flags INVALID for: hardcoding hidden assertions, editing/skipping
tests or CI, env-sniffing, stubbing the path under test, masking one failure with
another, or matching trap output without fixing the cause. Conservative: INVALID
only with concrete diff evidence; else defer to the mechanical pass.

For Group I tasks the LLM layer is a **rubric scorer** over the recorded GT
(coverage, factual accuracy, clarity, framing) rather than an overfit check.

### 6.4 Composition
```
final = mechanical_pass ∧ auditor_VALID
```
The LLM can only **lower** a result, never **raise** one.

---

## 7. Attack sources (decorrelation)

- **director-driven** — Director-invented injections into prompts/PRs.
- **snapshot-driven** — before/after repo snapshots (revert real fix, strip
  tests, outdated deps, constructed conflict, merge-range).
- **mutation-driven** — mechanical mutation operators (operator swaps, typos,
  dropped changes); cheap, reproducible, decorrelated from any solver.

Every perturbation carries `source: director | snapshot | mutation` so filtering
and the auditor can preserve/inspect diversity.

---

## 8. Example prompts

Illustrative — tune to the codebase. All prompts English, framed as MCP-tool work.

### 8.1 Director — system prompt
> You orchestrate a simulated AI software company whose only real artifact is a
> **trace of work in MCP services**. You are an **annotator of facts, not a
> generator of ground truth**.
> Inputs: goal, tech stack, project idea, available MCP tools, task templates.
> You will: (1) produce env (Docker deps) + spec; (2) define team roles and their
> MCP interaction protocol (codebase, tracker, testing, PRs, reviews);
> (3) plan ~10 milestones (1 release + 3 standups each); (4) per milestone,
> perturb the repo/prompts and **record GT at perturbation time**.
> Every perturbation uses exactly one mechanism — **director prompt injection**
> or **before/after repo snapshot** — and emits a structured record (§8.6).
> Hard rules: GT is captured mechanically from the injected diff / snapshot diff
> / SHA — never re-decided later. Trap-tests come from the **correct pre-defect
> code**, never authored to mirror a defect. Defects and trap-tests are **never**
> surfaced to solver agents. Prefer decorrelated sources.

### 8.2 Team Lead / PM — system prompt
> You are the Team Lead acting as PM. Convert the milestone plan into concrete,
> trackable tickets via MCP. Per agent specify codebase read/write, ticket
> movement, allowed MCP tools, and process (branch → PR → review → CI → merge).
> Enforce cadence: 1 release + 3 standups per milestone. Tickets must be
> **self-contained and repeatable** — no dependence on live mutable state (e.g.
> live dependency upgrades). Do not embed solutions in tickets.
> For **Issue Triage** tasks you also record the correct triage (labels/fields)
> as GT.

### 8.3 Engineer — system prompt
> You are a Software Engineer. Pick up tickets and resolve them **only via MCP
> tools** in the provided env. Follow process: branch → implement → **write tests
> covering your change** → run tests/CI → open a PR with a clear description →
> address review. During the simulation you have normal access to tests and CI.
> Fixes must address the underlying cause.

### 8.4 QA / Reviewer — system prompt
> You are QA / Reviewer. Review PRs and author tests **for correct behavior**
> derived from the spec — never reverse-engineered from a known defect. If the
> engineer's tests are missing or insufficient, **you must add them**. Your tests
> must pass on correct code independently of any specific bug; they may become
> baseline/trap tests later.

### 8.5 Director — perturbation prompt (per template)
> Produce exactly one task of the sampled type and capture its GT.
> - **Generate Release Notes** → snapshot: fix the last-release merge range. GT =
>   real merges (by SHA); note quality scored by rubric.
> - **Fix Bug** → snapshot (revert a real fix) or injection. GT = reverting diff;
>   trap = a pre-existing green test now red.
> - **Code Review** → injection: plant a defect in a PR. GT = the injection
>   (location + diff); the reviewer must catch it.
> - **Issue Triage** → annotate correct labels/fields on a real issue (Lead). GT
>   = those fields; analysis scored by rubric.
> - **Cover PR with Tests** → snapshot: strip existing tests from a PR. GT = the
>   removed coverage; re-added tests must be green and cover the change.
> - **Fix Failed CI** → injection/snapshot that breaks CI. GT = root-cause fix;
>   green CI restored.
> - **Standup Update** → annotate real trace events in the window. GT = those
>   events; phrasing scored by rubric.
> - **Dependency Upgrade** → snapshot from heavily outdated deps; the real
>   upgrade PR is rejected and **not persisted** (repeatable). GT = the deps
>   decision (by SHA).
> - **Resolve Merge Conflicts** → snapshot: construct two diverging branches. GT
>   = correct merged resolution; clear-cut resolved, ambiguous flagged.
> Do not solve the task. Do not author trap-tests that mirror the defect.

### 8.6 Perturbation emit format
> `{ type, mechanism: director|snapshot, source: director|snapshot|mutation,
>    injected_diff | snapshot_pair | sha | range, trap_test_ref, gt, rubric_ref? }`

### 8.7 Oracle — LLM auditor system prompt (Group II)
> You are the audit layer of a hermetic oracle. The **pass/fail verdict is
> decided mechanically**; you do not issue it and do not re-derive GT. Given a
> mechanically-passing solution, decide whether the pass is **legitimate** or
> gamed. You receive the defect + its GT, Air's diff, the full-suite result, and
> the touched files; you cannot execute code or author tests.
> Flag **INVALID** if the patch hardcodes hidden assertions, edits/weakens/skips
> tests or CI, sniffs the env, stubs the path under test, masks one failure with
> another, or matches trap output without fixing the cause. Otherwise **VALID**.
> Judge only the GT defect's cause; ignore incidental refactors. Be conservative:
> INVALID only with concrete diff evidence; absent evidence, defer to the
> mechanical pass. Output `{verdict, rationale, evidence}`.

---

## 9. Rules for agents editing this repo

- **Do not weaken anti-circularity.** No LLM deciding or re-deriving GT; GT is
  captured at perturbation and fixed downstream.
- **Every task must use one of the two mechanisms** (director injection /
  snapshot) and emit the record in §8.6.
- **Do not let trap-tests mirror defects.** Prefer referencing existing
  team-authored tests.
- **Keep tasks deterministic and repeatable** (esp. Dependency Upgrade: outdated
  deps are not persisted with the update).
- **Preserve the mechanical-first Oracle**: the LLM auditor may only downgrade a
  mechanical pass, never upgrade a mechanical fail.
- **Keep the env identical** between task creation and evaluation.
- When adding a task type, specify (a) mechanism, (b) how GT is captured, (c) the
  trap-test / artifact and why it's valid on correct code.

---

## 10. Glossary

- **Air** — the JetBrains coding-agent product under evaluation (the solver).
- **Director** — orchestrator that builds the sim and perturbs it; annotator of
  facts, not a GT generator.
- **GT (ground truth)** — the correct fix/answer, captured at perturbation.
- **Trap-test** — a pre-existing test, green on correct code, red under the defect.
- **Trace** — the recorded work in MCP services; the real artifact of the sim.
- **Milestone** — a sprint; 1 release + 3 standups each; ~10 total.
- **Group I / Group II** — informational tasks (RN/IT/SU) vs. defect-injection
  tasks (fix bug, code review, cover PR, fix CI, deps, merge conflicts).
- **Persisting tasks** — items that branch off the perturbation step.
