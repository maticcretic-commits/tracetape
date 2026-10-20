# The TraceTape Format

**Version 1.** A `trace.jsonl` file — “a tape” — is the canonical
record of one or more agent runs. One JSON object per line, one event
per object, in the order the events happened.

The format exists so that any tool can work from the same evidence:
replay a run, audit what the agent did, total what it cost, or diff
its behaviour against last week's run — without each tool inventing
its own log shape first.

```json
{"v": 1, "ts": "2026-10-20T08:15:02Z", "run": "checkout-2026-10-20-a", "seq": 3, "kind": "tool_call", "name": "web_search", "data": {"query_ref": "q-881", "results": 10}}
```

## Event anatomy

Every event is a JSON object with six **required** fields:

| Field | Type | Meaning |
|-------|------|---------|
| `v` | integer | Format version. Currently always `1`. Readers must reject other versions rather than guess. |
| `ts` | string | ISO-8601 timestamp of the event, preferably with offset (`2026-10-20T08:15:02Z`). |
| `run` | string | Run identifier. All events of one run share it; events of different runs may be interleaved in one file. |
| `seq` | integer | Sequence number **within the run**. Starts anywhere (1 is customary), strictly increasing, never reused. |
| `kind` | string | One of the seven kinds below. |
| `name` | string | What the event is about: the tool name for `tool_call`/`tool_result`, the step or speaker otherwise. |

And these **optional** fields:

| Field | Type | Meaning |
|-------|------|---------|
| `parent` | integer | `seq` of the enclosing event in the same run — a `tool_result` points at its `tool_call`; a nested step points at its parent step. Must reference an earlier event. |
| `dur_ms` | number | Duration of the step this event closes, in milliseconds. |
| `tokens_in` | integer | Prompt/input tokens attributable to this event. |
| `tokens_out` | integer | Completion/output tokens attributable to this event. |
| `cost` | string or number | Money attributable to this event, as a decimal string (`"0.0114"`) — preferred, exact — or a JSON number. Currency is a property of the tape's consumer agreement, not the event; pick one currency per tape. |
| `ok` | boolean | Outcome, on events that have one (`tool_result`, `error`, `run_end`). `false` means the step failed — the tape records failures, it does not hide them. |
| `data` | object | Free-form payload: arguments, results, hashes, references. See the privacy rule below. |

Writers may add fields beyond these; readers must ignore fields they
do not understand. A field that *is* in this spec must carry the
spec's type — a tape that carries a field carries it correctly.

## The seven kinds

| Kind | Marks |
|------|-------|
| `run_start` | The first event of a run. Exactly one per run. |
| `prompt` | A prompt was given to the agent (user, system, or another agent). |
| `tool_call` | The agent invoked a tool. `name` is the tool. |
| `tool_result` | A tool returned. `parent` is the `tool_call` it answers; `ok` says whether it worked. |
| `message` | The agent produced a message (reply, note, hand-off). |
| `error` | Something failed and was recorded. `parent` points at the failed step when there is one; `data` says what happened and whether it was handled. |
| `run_end` | The last event of a run. Exactly one per *completed* run. |

## Ordering and completeness rules

A conforming tape satisfies all of these (this is exactly what
`tracetape validate` checks):

1. Every non-blank line is a JSON object with the required fields,
   correctly typed.
2. Within a run, `seq` is strictly increasing and never repeated.
   Gaps are legal (a writer may reserve numbers) but are reported as
   warnings — a gap usually means a lost event.
3. `parent` always references an *earlier* `seq` **in the same
   run**. References across runs, forward references, and
   self-references are errors.
4. Within a run, `ts` never goes backwards.
5. Every run has exactly one `run_start`, and no events appear after
   its `run_end`.
6. A run whose `run_start` has no matching `run_end` is
   **incomplete**: the tape stops mid-run. Incomplete is an error —
   reported under its own heading, because a crashed agent and a
   corrupt tape need different responses. (Validators may offer a
   lax mode for tapes still being written.)

An `error` event is **not** a conformance problem. Failure is data;
a tape with recorded, handled errors is a good tape. A tape that
omits the failure is the bad one.

## Multi-run files

A file may hold many runs — batch jobs and nightly suites usually
do. Rules apply per run: `seq` restarts, `parent` never crosses a
run boundary, timestamps only need to be non-decreasing *within* a
run.

## Privacy rule: hashes over payloads

`data` is free-form, and that is a loaded foot-gun: prompts and tool
results routinely carry secrets and personal data, and tapes get
shared, archived and attached to bug reports. The format's standing
advice:

- Record `{"sha256": "…", "chars": 412}` instead of the prompt text.
- Record references (`"url_ref": "result-3"`) instead of raw content.
- If you must record payloads, scan the tape before sharing it
  (that's what [SecretScan](https://github.com/maticcretic-commits/secretscan)
  is for).

## Example

One short run — a prompt, a search, a fetch, a reply:

```json
{"v": 1, "ts": "2026-10-20T08:15:00Z", "run": "r-1", "seq": 1, "kind": "run_start", "name": "research-agent"}
{"v": 1, "ts": "2026-10-20T08:15:01Z", "run": "r-1", "seq": 2, "kind": "prompt", "name": "user-prompt", "tokens_in": 96, "data": {"sha256": "c41d77…"}}
{"v": 1, "ts": "2026-10-20T08:15:02Z", "run": "r-1", "seq": 3, "kind": "tool_call", "name": "web_search"}
{"v": 1, "ts": "2026-10-20T08:15:03Z", "run": "r-1", "seq": 4, "kind": "tool_result", "name": "web_search", "parent": 3, "dur_ms": 640, "ok": true}
{"v": 1, "ts": "2026-10-20T08:15:09Z", "run": "r-1", "seq": 5, "kind": "message", "name": "assistant-reply", "tokens_in": 1520, "tokens_out": 386, "cost": "0.0114"}
{"v": 1, "ts": "2026-10-20T08:15:09Z", "run": "r-1", "seq": 6, "kind": "run_end", "name": "research-agent", "ok": true}
```

## Versioning

`v` versions the *format*, not the tool. Version 1 is frozen: new
optional fields may be documented here over time (old readers ignore
them), but required fields, kinds and rules only change with a new
version number — and readers reject versions they do not know.
