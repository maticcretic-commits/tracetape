# TraceTape 📼

**Every agent run produces the same evidence in a different shape: a transcript here, a log file there, a billing export somewhere else. When something goes wrong — or costs too much, or quietly changes behaviour — you reverse-engineer four formats before you can answer one question.**

TraceTape is a canonical format for that evidence, plus the tool that guards it. A tape is `trace.jsonl`: one JSON event per line, a sequence number and a timestamp on every event (the format is a first-class spec: `TRACE_FORMAT.md` in this repo). `validate` proves a tape's shape, precisely, and names every place it breaks; `summary` profiles the runs on it.

*Part of **AgentForge** — verify AI-agent work before you ship it.*
*The family: [AgentProof](https://github.com/maticcretic-commits/agentproof) · [PromptRE](https://github.com/maticcretic-commits/promptre) · [AgentCI](https://github.com/maticcretic-commits/agentci) · [SecretScan](https://github.com/maticcretic-commits/secretscan) · [CostLedger](https://github.com/maticcretic-commits/costledger) · [SchemaGuard](https://github.com/maticcretic-commits/schemaguard) · [SkillAudit](https://github.com/maticcretic-commits/skillaudit) · [PromptDiff](https://github.com/maticcretic-commits/promptdiff) · [TraceTape](https://github.com/maticcretic-commits/tracetape) · [PromptLint](https://github.com/maticcretic-commits/promptlint) · [ClaimCheck](https://github.com/maticcretic-commits/claimcheck) · [TrustBadge](https://github.com/maticcretic-commits/trustbadge)*

```bash
$ tracetape validate examples/broken.jsonl
```

```
====================================================================
TraceTape — validation report
====================================================================
File:   examples/broken.jsonl
Events: 6 · Runs: 1 · Blank lines: 0
Result: ❌ INVALID — 3 error(s), 1 warning(s)

## Errors
  line 5    [dangling-parent] run 'broken-2026-10-20': parent 42 does not match any earlier seq in this run
  line 6    [bad-timestamp] 'ts' is not a parseable ISO-8601 timestamp: '20 Oct 2026, morning'

## Incomplete runs
  line 6    [incomplete] run 'broken-2026-10-20': run_start with no matching run_end — the tape stops mid-run (6 events)

## Warnings
  line 4    [seq-gap] run 'broken-2026-10-20': seq jumps 3 → 5 (no event with seq 4)
```

*Exit code 1 — the tape is INVALID. The bundled `examples/good.jsonl` validates clean (17 events, 2 runs, exit 0).*

## Install

```bash
pipx install git+https://github.com/maticcretic-commits/tracetape
# or, into the current environment:
pip install git+https://github.com/maticcretic-commits/tracetape
```

Then run it as `tracetape …` (Python 3.9+, no dependencies — stdlib only).
PyPI release planned; until then, the git URL above is the install path.

From source — it is a single file, so no install is needed at all:

```bash
git clone https://github.com/maticcretic-commits/tracetape
cd tracetape
python src/tracetape.py …
```

## The commands

| Command | What it does |
|---------|--------------|
| `validate <file-or-dir>` | Structural conformance check (rules below). Per-file report with every fault named, classed, and given its line number. |
| `validate … --strict` | Exit `1` on warnings too, not just errors. |
| `validate … --allow-incomplete` | Unterminated runs count as warnings — for tapes still being written by a live agent. |
| `validate … --format json` | Machine-readable report: per-file errors / warnings / incomplete runs. |
| `summary <file>` | Per-run profile: event counts by kind, tool usage table, token totals, Decimal-exact cost total, wall-clock span, longest step, and the error list with seq references. Multi-run files get one block per run plus totals. |
| `init` | Scaffold a sample `trace.jsonl` (one complete run, including a *handled* error) + `EXPECTED.md`. |

Exit codes: `0` = clean, `1` = validation errors (or warnings under
`--strict`), `2` = usage or IO error.

## What validate checks

| Check | Class | Level |
|-------|-------|-------|
| Line is not valid JSON / not an object | `bad-json` / `not-an-object` | error |
| Required field missing (`v`, `ts`, `run`, `seq`, `kind`, `name`) | `missing-field` | error |
| Field has the wrong type — including optional fields that are present (`parent`, `dur_ms`, `tokens_*`, `cost`, `ok`, `data`) | `field-type` / `field-value` | error |
| `v` is not format version 1 | `unsupported-version` | error |
| `kind` outside the seven-kind vocabulary | `unknown-kind` | error |
| `ts` is not a parseable ISO-8601 timestamp | `bad-timestamp` | error |
| `seq` repeated within a run / goes backwards | `duplicate-seq` / `seq-order` | error |
| `seq` skips numbers (a lost event, usually) | `seq-gap` | warning |
| `parent` names no earlier event *in the same run* — dangling, forward, self-, or cross-run | `dangling-parent` | error |
| Timestamps go backwards within a run | `timestamp-order` | error |
| Events with no `run_start`, a second `run_start`, or events after `run_end` | `missing-start` / `duplicate-start` / `event-after-end` | error |
| `run_start` with no matching `run_end` | `incomplete` | error, own heading |

Rules apply **per run**: a multi-run file restarts `seq` per run,
and a `parent` may never cross a run boundary. Blank lines are
tolerated. An `error` *event* is never a finding — failure is data,
and a tape that records its failures is a good tape.

## Example

Two runs on one tape (`examples/good.jsonl`) validate clean, and
summarize like this — real captured output:

```
$ tracetape summary examples/good.jsonl
====================================================================
TraceTape — run summary
====================================================================
File: examples/good.jsonl

Run: checkout-2026-10-20-a  (8 events · 9.0s wall clock)
  Kinds:  run_start 1 · prompt 1 · tool_call 2 · tool_result 2 · message 1 · run_end 1
  Tools:
    name                     calls errors  total ms
    fetch_page                   1      0      1240
    web_search                   1      0       640
  Tokens: in 1,616 · out 386
  Cost:   0.0114
  Longest step: fetch_page (seq 6) — 1240 ms
  Errors: none

Run: digest-2026-10-20-b  (9 events · 10.0s wall clock)
  Kinds:  run_start 1 · prompt 1 · tool_call 2 · tool_result 2 · message 1 · error 1 · run_end 1
  Tools:
    name                     calls errors  total ms
    summarize                    2      1      6090
  Tokens: in 1,034 · out 240
  Cost:   0.0071
  Longest step: summarize (seq 7) — 3110 ms
  Errors:
    seq 4 · summarize (tool_result)
    seq 5 · summarize (error)

--------------------------------------------------------------------
Totals: 2 runs · 17 events · tokens in 2,650 / out 626 · cost 0.0185 · 2 error(s)
```

Note run B: `summarize` failed once (seq 4–5), the agent chunked the
input and retried successfully. The tape says so, the tool table
counts **one failed call** (the failed result and its error event
point at the same call), and the run still validates — a handled
failure is history, not a defect.

And a tape that breaks the rules (`examples/broken.jsonl`) — every
fault named, with its line:

```
$ tracetape validate examples/broken.jsonl
====================================================================
TraceTape — validation report
====================================================================
File:   examples/broken.jsonl
Events: 6 · Runs: 1 · Blank lines: 0
Result: ❌ INVALID — 3 error(s), 1 warning(s)

## Errors
  line 5    [dangling-parent] run 'broken-2026-10-20': parent 42 does not match any earlier seq in this run
  line 6    [bad-timestamp] 'ts' is not a parseable ISO-8601 timestamp: '20 Oct 2026, morning'

## Incomplete runs
  line 6    [incomplete] run 'broken-2026-10-20': run_start with no matching run_end — the tape stops mid-run (6 events)

## Warnings
  line 4    [seq-gap] run 'broken-2026-10-20': seq jumps 3 → 5 (no event with seq 4)
```

Incomplete runs get their own heading on purpose: a crashed agent
and a corrupt tape look identical in a line count, and they need
different responses.

## Adopting the format

Emitting a tape is a few lines in any agent loop: open the file at
`run_start`, append one JSON object per step, close with `run_end`.
The full spec — field reference, the seven kinds, ordering rules,
versioning — is **[TRACE_FORMAT.md](TRACE_FORMAT.md)**. Two habits
make tapes worth keeping:

- **Reference from the sibling tools.** A run's token and cost
  fields feed [CostLedger](https://github.com/maticcretic-commits/costledger)
  accounting directly; a tape per run is the regression corpus
  [AgentCI](https://github.com/maticcretic-commits/agentci) diffs
  when behaviour changes; [AgentProof](https://github.com/maticcretic-commits/agentproof)
  verdicts attach to named steps by `seq`.
- **Hashes over payloads.** Record `{"sha256": "…", "chars": 412}`
  in `data`, not the raw prompt — tapes get shared and archived,
  and prompts carry secrets. The spec's privacy rule spells this
  out; if a tape does carry payloads, scan it with
  [SecretScan](https://github.com/maticcretic-commits/secretscan)
  before it leaves the machine.

## Honesty note

Validation is **structural, not semantic**. TraceTape proves a tape
is well-formed, ordered and complete; it cannot prove the events
are true. A run that lies fluently passes `validate`. What it
guarantees is narrower and more useful than magic: claim the
format, and your gaps, dangling references, reordered events and
unfinished runs are found and named — precisely, every time,
before another tool builds a conclusion on top of them.

## Quick start

```bash
# 1. Scaffold the sample run and read it back:
tracetape init
tracetape validate trace.jsonl     # ✅ VALID, exit 0
tracetape summary trace.jsonl      # one run, one handled error

# 2. Gate tapes in CI:
tracetape validate tapes/ --strict

# 3. Watch a live agent's tape without failing on its open run:
tracetape validate live/trace.jsonl --allow-incomplete
```

## Why

An agent ecosystem without a common run record re-derives everything
per tool: the cost tool parses one log shape, the regression harness
another, the auditor a third — and they disagree about what the run
even was. A canonical tape makes the run a shared fact. Tools stay
small and single-purpose because the evidence underneath them is
stable: define the tape once, validate it mechanically, and every
check built on top inherits the same ground truth.

## Exit codes & JSON output

All AgentForge tools share one contract (full text in
[CONVENTIONS.md](CONVENTIONS.md)):

| Code | Meaning |
|------|---------|
| `0` | Clean / PASS — no findings at or above the tool's fail threshold |
| `1` | Findings / FAIL — the tool ran fine; the answer is “no” |
| `2` | Caller error — bad arguments, missing or unreadable input |
| `3` | Environment error — the tool itself could not run |

On a caller error the tool also prints one JSON line to stderr:
`{"error": {"code": "file_not_found", "message": "no such file: x"}}`.

JSON output (`--format json`) always carries the family envelope —
`{"tool": "tracetape", "version": "1.0.0", "schema_version": 1}` — alongside
the tool's own payload keys, unchanged.

## Part of AgentForge

TraceTape is one of the 12 tools in the [AgentForge](https://github.com/maticcretic-commits) family — offline, stdlib-only tools to **verify AI-agent work before you ship it**:

- **AgentProof** — verify agent work — [github.com/maticcretic-commits/agentproof](https://github.com/maticcretic-commits/agentproof)
- **PromptRE** — reverse-engineer agent prompts — [github.com/maticcretic-commits/promptre](https://github.com/maticcretic-commits/promptre)
- **AgentCI** — regression harness for agents — [github.com/maticcretic-commits/agentci](https://github.com/maticcretic-commits/agentci)
- **SecretScan** — scan agent outputs for leaked secrets — [github.com/maticcretic-commits/secretscan](https://github.com/maticcretic-commits/secretscan)
- **CostLedger** — cost accounting for agent runs — [github.com/maticcretic-commits/costledger](https://github.com/maticcretic-commits/costledger)
- **SchemaGuard** — validate agent outputs against their contracts — [github.com/maticcretic-commits/schemaguard](https://github.com/maticcretic-commits/schemaguard)
- **SkillAudit** — audit agent skills for hidden instructions and quality — [github.com/maticcretic-commits/skillaudit](https://github.com/maticcretic-commits/skillaudit)
- **PromptDiff** — diff prompt versions — [github.com/maticcretic-commits/promptdiff](https://github.com/maticcretic-commits/promptdiff)
- **TraceTape** — the canonical run tape (this repo)
- **PromptLint** — lint prompts for quality defects — [github.com/maticcretic-commits/promptlint](https://github.com/maticcretic-commits/promptlint)
- **ClaimCheck** — audit agent reports against their evidence — [github.com/maticcretic-commits/claimcheck](https://github.com/maticcretic-commits/claimcheck)
- **TrustBadge** — one trust score and badge for the whole family — [github.com/maticcretic-commits/trustbadge](https://github.com/maticcretic-commits/trustbadge)

AgentProof checks what an agent *did*; PromptRE checks what it was *told*; AgentCI checks it still *works* after you change it; SecretScan checks that nothing *leaked*; CostLedger checks what it *cost*; SchemaGuard checks the output still *fits the contract*; SkillAudit checks the skill was *safe to install*; PromptDiff checks what the prompt change *changed*; TraceTape keeps the whole run on *tape*; PromptLint keeps the prompt itself *linted clean*; ClaimCheck makes sure every claim is *backed*; TrustBadge puts the family's score *on one badge*.

## License

MIT
