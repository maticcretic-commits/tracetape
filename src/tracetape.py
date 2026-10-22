#!/usr/bin/env python3
"""
TraceTape — "One tape every agent run leaves behind."

Every agent run produces the same evidence in a different shape: a
transcript here, a log file there, a billing export somewhere else.
When something goes wrong — or costs too much, or changes behaviour —
you end up reverse-engineering four formats before you can answer one
question.

TraceTape defines ONE canonical format for that evidence: trace.jsonl,
one JSON event per line, with a small fixed vocabulary (run_start,
prompt, tool_call, tool_result, message, error, run_end). Any agent
that writes the tape can be replayed, audited, costed and
regression-checked by any tool that reads it. The full format is
specified in TRACE_FORMAT.md.

This tool defines the format, validates tapes against it, and
summarizes them:

    tracetape validate <file-or-dir> [--format text|json] [--strict]
                                     [--allow-incomplete]
    tracetape summary <file> [--format text|json]
    tracetape handoffs <file> [--format text|json]
    tracetape bundle <tape> [--claims F] [--skill DIR] [--cost F]
                            [--schema F] [--trustbadge F]
                            [--report F ...] [--out bundle.json]
                            [--format text|json]
    tracetape verify <bundle.json> [--format text|json]
    tracetape init

`handoffs` extracts the multi-agent handoff chain from a tape (see
TRACE_FORMAT.md, "Handoff events"). `bundle` seals a tape plus its
companion evidence — claims, a skill manifest, tool reports, a cost
record, a schema contract, a TrustBadge score — into one proof
envelope with a SHA-256 over every artifact; `verify` re-checks a
bundle against the files on disk and fails if any sealed evidence
has gone missing or changed.

Exit codes: 0 = clean (validate: no errors; summary: printed),
1 = validation errors found (or, with --strict, warnings too),
2 = usage / IO error. Stdlib only, no network, fully deterministic.

HONESTY NOTE: validation is STRUCTURAL, not semantic. TraceTape can
prove a tape is well-formed, ordered and complete; it cannot prove
the events are true. A tape that lies fluently passes validate. What
it guarantees is narrower and more useful than magic: if a run
claims the tape format, its gaps, dangling references, reordered
events and unfinished runs are found and named, precisely, every
time.
"""

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

VERSION = "1.1.0"
FORMAT_VERSION = 1
BUNDLE_VERSION = 1

KIND_VOCAB = ("run_start", "prompt", "tool_call", "tool_result",
              "message", "error", "run_end")
REQUIRED_FIELDS = ("v", "ts", "run", "seq", "kind", "name")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def _is_num(x):
    return (isinstance(x, (int, float)) and not isinstance(x, bool)
            and math.isfinite(x))


def parse_ts(raw):
    """Parse an ISO-8601 timestamp. Returns an aware datetime or None.

    Naive timestamps are assumed UTC (the format spec recommends an
    explicit offset; ordering must still be decidable)."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s:
        return None
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _cost_decimal(value):
    """Decimal for a spec-legal cost value, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            d = Decimal(value.strip())
        except InvalidOperation:
            return None
        return d if d.is_finite() else None
    return None


def _fmt_decimal(d):
    if d == d.to_integral_value():
        return str(int(d))
    return format(d.normalize(), "f")


def _fmt_span(seconds):
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{int(m)}m {s:.0f}s"
    h, m = divmod(m, 60)
    return f"{int(h)}h {int(m)}m"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_lines(text):
    """Split tape text into (line_no, obj-or-None) pairs plus the
    findings produced while parsing (bad JSON, non-object lines).
    Blank lines are tolerated and counted, never findings."""
    parsed = []
    findings = []
    blank = 0
    for line_no, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip():
            blank += 1
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            findings.append({
                "line": line_no, "level": "error", "cls": "bad-json",
                "message": f"line is not valid JSON ({e.msg})"})
            parsed.append((line_no, None))
            continue
        if not isinstance(obj, dict):
            findings.append({
                "line": line_no, "level": "error", "cls": "not-an-object",
                "message": "line is valid JSON but not an object — "
                           "every tape event must be a JSON object"})
            parsed.append((line_no, None))
            continue
        parsed.append((line_no, obj))
    return parsed, findings, blank


# ---------------------------------------------------------------------------
# Handoff events (trust layer L9 — see TRACE_FORMAT.md)
#
# A handoff is any event whose data carries a "handoff" object:
# {"from": agent, "to": agent, "context_sha256": hash, ...}. The
# format stays v1 — handoffs are a documented data convention, not a
# new kind — but the tooling understands them: validate warns on
# malformed payloads, summary lists them per run, and the `handoffs`
# command audits the whole chain (attestation, continuity, answers).
# ---------------------------------------------------------------------------

HANDOFF_HASH_KEYS = ("context_sha256", "sha256")


def _handoff_payload(obj):
    data = obj.get("data")
    if isinstance(data, dict) and "handoff" in data:
        return data["handoff"]
    return None


def _handoff_problems(obj):
    """Shape problems of one event's handoff payload ([] when the
    event carries no handoff, or a well-formed one)."""
    h = _handoff_payload(obj)
    if h is None:
        return []
    if not isinstance(h, dict):
        return ["handoff payload is not an object"]
    problems = []
    for key in ("from", "to"):
        v = h.get(key)
        if not (isinstance(v, str) and v):
            problems.append(
                f"handoff.{key} is missing or not a non-empty string")
    for key in HANDOFF_HASH_KEYS:
        if key in h:
            if not (isinstance(h[key], str) and h[key]):
                problems.append(
                    f"handoff {key} is not a non-empty string")
            break
    return problems


def extract_handoffs(parsed):
    """All handoff records on a tape, in file order:
    {run, seq, line, kind, from, to, context_sha256, attested,
    problems}. from/to/context_sha256 are None when absent or
    malformed; `attested` is True only for a well-formed payload
    carrying a context hash."""
    out = []
    for line_no, obj in parsed:
        if obj is None or _handoff_payload(obj) is None:
            continue
        h = _handoff_payload(obj)
        rec = {"run": obj.get("run") if isinstance(obj.get("run"), str)
               else None,
               "seq": obj.get("seq") if _is_int(obj.get("seq")) else None,
               "line": line_no,
               "kind": obj.get("kind") if isinstance(obj.get("kind"), str)
               else None,
               "from": None, "to": None, "context_sha256": None,
               "attested": False, "problems": _handoff_problems(obj)}
        if isinstance(h, dict):
            for key in ("from", "to"):
                v = h.get(key)
                if isinstance(v, str) and v:
                    rec[key] = v
            for key in HANDOFF_HASH_KEYS:
                v = h.get(key)
                if isinstance(v, str) and v:
                    rec["context_sha256"] = v
                    break
        rec["attested"] = (not rec["problems"]
                           and rec["context_sha256"] is not None)
        out.append(rec)
    return out


def analyze_handoffs(parsed):
    """Chain audit over a tape's handoffs. Returns (records,
    findings) — findings use the validate finding shape and are all
    warnings: a handoff problem never makes a tape structurally
    invalid, but it is always named."""
    records = extract_handoffs(parsed)
    findings = []

    def warn(cls, line, msg):
        findings.append({"line": line, "level": "warning",
                         "cls": cls, "message": msg})

    for rec in records:
        who = (f"run '{rec['run']}': handoff at seq {rec['seq']}")
        for p in rec["problems"]:
            warn("handoff-shape", rec["line"], f"{who} — {p}")
        if not rec["problems"] and rec["context_sha256"] is None:
            warn("handoff-unattested", rec["line"],
                 f"{who} ({rec['from']} → {rec['to']}) carries no "
                 f"context hash — what the receiving agent got is "
                 f"unattested")

    by_run = {}
    for rec in records:
        by_run.setdefault(rec["run"], []).append(rec)
    for run, chain in by_run.items():
        for prev, nxt in zip(chain, chain[1:]):
            if prev["to"] and nxt["from"] and nxt["from"] != prev["to"]:
                warn("handoff-chain", nxt["line"],
                     f"run '{run}': handoff chain breaks — "
                     f"{prev['from']} → {prev['to']} is followed by "
                     f"{nxt['from']} → {nxt['to']}")

    starts = [(line_no, obj.get("name")) for line_no, obj in parsed
              if obj is not None and obj.get("kind") == "run_start"]
    run_agent = {}
    for line_no, obj in parsed:
        if obj is not None and obj.get("kind") == "run_start" \
                and isinstance(obj.get("run"), str) \
                and isinstance(obj.get("name"), str):
            run_agent.setdefault(obj["run"], obj["name"])
    seen_pairs = set()
    for idx, rec in enumerate(records):
        if not rec["to"]:
            continue
        pair = (rec["from"], rec["to"])
        if pair in seen_pairs:
            continue  # receipt echo of an already-tracked handoff
        seen_pairs.add(pair)
        if run_agent.get(rec["run"]) == rec["to"]:
            continue  # recorded inside the receiver's own run
        answered = any(later["from"] == rec["to"]
                       for later in records[idx + 1:])
        if not answered:
            answered = any(name == rec["to"] and line_no > rec["line"]
                           for line_no, name in starts)
        if not answered:
            warn("handoff-unanswered", rec["line"],
                 f"run '{rec['run']}': handoff to '{rec['to']}' at "
                 f"seq {rec['seq']} is never answered on this tape — "
                 f"no later handoff from '{rec['to']}' and no run of "
                 f"theirs starts")
    return records, findings


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _check_event_fields(obj, line_no, findings):
    """Field-level checks for one event object. Appends findings;
    returns the subset of structural facts the run-level checks can
    rely on (each None when unusable)."""
    facts = {"run": None, "seq": None, "kind": None, "ts": None}

    def err(cls, msg):
        findings.append({"line": line_no, "level": "error",
                         "cls": cls, "message": msg})

    for field in REQUIRED_FIELDS:
        if field not in obj:
            err("missing-field", f"missing required field '{field}'")

    if "v" in obj:
        if not _is_int(obj["v"]):
            err("field-type", f"'v' must be an integer, "
                              f"got {type(obj['v']).__name__}")
        elif obj["v"] != FORMAT_VERSION:
            err("unsupported-version",
                f"'v' is {obj['v']} — this tool reads format "
                f"version {FORMAT_VERSION}")

    if "run" in obj:
        if not isinstance(obj["run"], str) or not obj["run"]:
            err("field-type", "'run' must be a non-empty string")
        else:
            facts["run"] = obj["run"]

    if "seq" in obj:
        if not _is_int(obj["seq"]):
            err("field-type", f"'seq' must be an integer, "
                              f"got {type(obj['seq']).__name__}")
        else:
            facts["seq"] = obj["seq"]

    if "kind" in obj:
        if not isinstance(obj["kind"], str):
            err("field-type", "'kind' must be a string")
        elif obj["kind"] not in KIND_VOCAB:
            err("unknown-kind",
                f"unknown kind '{obj['kind']}' — vocabulary is: "
                + ", ".join(KIND_VOCAB))
        else:
            facts["kind"] = obj["kind"]

    if "name" in obj:
        if not isinstance(obj["name"], str) or not obj["name"]:
            err("field-type", "'name' must be a non-empty string")

    if "ts" in obj:
        if not isinstance(obj["ts"], str):
            err("field-type", "'ts' must be an ISO-8601 string")
        else:
            dt = parse_ts(obj["ts"])
            if dt is None:
                err("bad-timestamp",
                    f"'ts' is not a parseable ISO-8601 timestamp: "
                    f"{obj['ts']!r}")
            else:
                facts["ts"] = dt

    # Optional fields: wrong shapes are errors — a tape that carries a
    # field must carry it correctly, or consumers silently misread it.
    if "parent" in obj and not _is_int(obj["parent"]):
        err("field-type", "'parent' must be an integer seq")
    if "dur_ms" in obj:
        if not _is_num(obj["dur_ms"]):
            err("field-type", "'dur_ms' must be a number")
        elif obj["dur_ms"] < 0:
            err("field-value", "'dur_ms' must not be negative")
    for tf in ("tokens_in", "tokens_out"):
        if tf in obj:
            if not _is_int(obj[tf]):
                err("field-type", f"'{tf}' must be an integer")
            elif obj[tf] < 0:
                err("field-value", f"'{tf}' must not be negative")
    if "cost" in obj and _cost_decimal(obj["cost"]) is None:
        err("field-value",
            "'cost' must be a decimal string or a number, "
            f"got {obj['cost']!r}")
    if "ok" in obj and not isinstance(obj["ok"], bool):
        err("field-type", "'ok' must be a boolean")
    if "data" in obj and not isinstance(obj["data"], dict):
        err("field-type", "'data' must be an object")

    return facts


def validate_parsed(parsed, source, allow_incomplete=False):
    """Run-level structural checks over parsed (line_no, obj) pairs."""
    findings = []
    runs = {}          # run id -> state
    run_order = []

    for line_no, obj in parsed:
        if obj is None:
            continue
        f = _check_event_fields(obj, line_no, findings)
        run, seq, kind = f["run"], f["seq"], f["kind"]
        if run is None:
            continue
        st = runs.get(run)
        if st is None:
            st = {"started": False, "ended": False, "seen": set(),
                  "prev_seq": None, "last_ts": None, "last_line": line_no,
                  "events": 0, "start_flagged": False}
            runs[run] = st
            run_order.append(run)
        st["events"] += 1
        st["last_line"] = line_no

        if st["ended"]:
            findings.append({
                "line": line_no, "level": "error", "cls": "event-after-end",
                "message": f"run '{run}': event after its run_end "
                           f"(seq {seq})"})
        elif not st["started"] and not st["start_flagged"] and kind != "run_start":
            findings.append({
                "line": line_no, "level": "error", "cls": "missing-start",
                "message": f"run '{run}': events present but no "
                           f"run_start seen"})
            st["start_flagged"] = True

        if seq is not None:
            if seq in st["seen"]:
                findings.append({
                    "line": line_no, "level": "error",
                    "cls": "duplicate-seq",
                    "message": f"run '{run}': duplicate seq {seq}"})
            elif st["prev_seq"] is not None and seq < st["prev_seq"]:
                findings.append({
                    "line": line_no, "level": "error", "cls": "seq-order",
                    "message": f"run '{run}': seq goes backwards "
                               f"({st['prev_seq']} → {seq})"})
            elif st["prev_seq"] is not None and seq > st["prev_seq"] + 1:
                missing = ", ".join(
                    str(s) for s in range(st["prev_seq"] + 1, seq))
                findings.append({
                    "line": line_no, "level": "warning", "cls": "seq-gap",
                    "message": f"run '{run}': seq jumps "
                               f"{st['prev_seq']} → {seq} "
                               f"(no event with seq {missing})"})
            st["seen"].add(seq)
            st["prev_seq"] = seq

            parent = obj.get("parent")
            if _is_int(parent):
                if parent == seq or parent not in st["seen"] - {seq}:
                    findings.append({
                        "line": line_no, "level": "error",
                        "cls": "dangling-parent",
                        "message": f"run '{run}': parent {parent} does "
                                   f"not match any earlier seq in "
                                   f"this run"})

        if f["ts"] is not None:
            if st["last_ts"] is not None and f["ts"] < st["last_ts"]:
                findings.append({
                    "line": line_no, "level": "error",
                    "cls": "timestamp-order",
                    "message": f"run '{run}': timestamp goes backwards "
                               f"({st['last_ts'].isoformat()} → "
                               f"{f['ts'].isoformat()})"})
            st["last_ts"] = f["ts"]

        if kind == "run_start":
            if st["started"] and not st["ended"]:
                findings.append({
                    "line": line_no, "level": "error",
                    "cls": "duplicate-start",
                    "message": f"run '{run}': second run_start before "
                               f"run_end"})
            st["started"] = True
        elif kind == "run_end":
            st["ended"] = True

        # Handoff payloads (data convention, TRACE_FORMAT.md): shape
        # problems are warnings — the tape stays structurally valid,
        # but a malformed handoff is always named.
        for problem in _handoff_problems(obj):
            findings.append({
                "line": line_no, "level": "warning",
                "cls": "handoff-shape",
                "message": f"run '{run}': {problem}"})

    incomplete = []
    for run in run_order:
        st = runs[run]
        if st["started"] and not st["ended"]:
            incomplete.append(run)
            findings.append({
                "line": st["last_line"],
                "level": "warning" if allow_incomplete else "error",
                "cls": "incomplete",
                "message": f"run '{run}': run_start with no matching "
                           f"run_end — the tape stops mid-run "
                           f"({st['events']} events)"})

    errors = [f for f in findings if f["level"] == "error"]
    warnings = [f for f in findings if f["level"] == "warning"]
    return {
        "file": source,
        "events": sum(1 for _, o in parsed if o is not None),
        "lines": len(parsed),
        "runs": run_order,
        "incomplete_runs": incomplete,
        "errors": errors,
        "warnings": warnings,
        "ok": not errors,
    }


def validate_text(text, source="<text>", allow_incomplete=False):
    parsed, parse_findings, blank = parse_lines(text)
    report = validate_parsed(parsed, source,
                             allow_incomplete=allow_incomplete)
    report["errors"] = parse_findings + report["errors"]
    report["warnings"] = report["warnings"]
    # Keep findings in line order (parse findings carry line numbers
    # too); stable sort by line, errors before warnings on ties.
    report["errors"].sort(key=lambda f: f["line"])
    report["warnings"].sort(key=lambda f: f["line"])
    report["blank_lines"] = blank
    if report["events"] == 0 and not report["errors"]:
        report["errors"].append({
            "line": 0, "level": "error", "cls": "empty",
            "message": "tape contains no events"})
        report["ok"] = False
    report["ok"] = not report["errors"]
    return report


def _read(path):
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def validate_path(path, allow_incomplete=False):
    """Validate one file or every *.jsonl under a directory.
    Returns (reports, fatal) — fatal is an error string for exit 2."""
    p = Path(path)
    if p.is_dir():
        files = sorted(p.rglob("*.jsonl"))
        if not files:
            return None, f"no .jsonl files found under {path}"
    elif p.is_file():
        files = [p]
    else:
        return None, f"no such file or directory: {path}"
    reports = []
    for f in files:
        try:
            text = _read(f)
        except OSError as e:
            return None, f"error: {e}"
        reports.append(validate_text(text, str(f),
                                     allow_incomplete=allow_incomplete))
    return reports, None


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def summarize_parsed(parsed):
    """Profile the runs on a tape. Lenient by design: it profiles
    what parses — run `validate` when you need conformance."""
    runs = {}
    order = []
    for line_no, obj in parsed:
        if obj is None or not isinstance(obj.get("run"), str):
            continue
        run = obj["run"]
        r = runs.get(run)
        if r is None:
            r = {"run": run, "events": 0, "by_kind": {}, "tools": {},
                 "tokens_in": 0, "tokens_out": 0, "cost": Decimal(0),
                 "cost_seen": False, "dur_total_ms": 0,
                 "longest": None, "errors": [], "first_ts": None,
                 "last_ts": None, "by_seq": {}, "failed_calls": set()}
            runs[run] = r
            order.append(run)
        r["events"] += 1
        kind = obj.get("kind")
        if isinstance(kind, str):
            r["by_kind"][kind] = r["by_kind"].get(kind, 0) + 1
        seq = obj.get("seq") if _is_int(obj.get("seq")) else None
        if seq is not None:
            r["by_seq"][seq] = obj
        name = obj.get("name") if isinstance(obj.get("name"), str) else "?"

        dt = parse_ts(obj.get("ts"))
        if dt is not None:
            if r["first_ts"] is None or dt < r["first_ts"]:
                r["first_ts"] = dt
            if r["last_ts"] is None or dt > r["last_ts"]:
                r["last_ts"] = dt

        if _is_int(obj.get("tokens_in")):
            r["tokens_in"] += obj["tokens_in"]
        if _is_int(obj.get("tokens_out")):
            r["tokens_out"] += obj["tokens_out"]
        if "cost" in obj:
            d = _cost_decimal(obj["cost"])
            if d is not None:
                r["cost"] += d
                r["cost_seen"] = True
        dur = obj.get("dur_ms")
        if _is_num(dur):
            r["dur_total_ms"] += dur
            if r["longest"] is None or dur > r["longest"]["dur_ms"]:
                r["longest"] = {"name": name, "seq": seq, "dur_ms": dur}

        if kind == "tool_call":
            t = r["tools"].setdefault(
                name, {"calls": 0, "errors": 0, "dur_ms": 0})
            t["calls"] += 1
            if _is_num(dur):
                t["dur_ms"] += dur
        elif kind == "tool_result" and _is_num(dur):
            # Durations live on results; credit them to the tool
            # whose call the result answers.
            parent = obj.get("parent")
            pobj = r["by_seq"].get(parent) if _is_int(parent) else None
            tname = name
            if pobj is not None and isinstance(pobj.get("name"), str):
                tname = pobj["name"]
            t = r["tools"].setdefault(
                tname, {"calls": 0, "errors": 0, "dur_ms": 0})
            t["dur_ms"] += dur

        failed = obj.get("ok") is False
        if kind == "error" or (kind == "tool_result" and failed):
            # The run's error LIST holds every error record. The tool
            # table's error column counts failed CALLS: a failed
            # result and the error event describing it (both point
            # at the same call via `parent`) are one failure, not two.
            r["errors"].append({"seq": seq, "name": name, "kind": kind})
            parent = obj.get("parent")
            pobj = r["by_seq"].get(parent) if _is_int(parent) else None
            if pobj is not None and pobj.get("kind") == "tool_call":
                r["failed_calls"].add(parent)
            elif kind == "error":
                t = r["tools"].setdefault(
                    name, {"calls": 0, "errors": 0, "dur_ms": 0})
                t["errors"] += 1

    handoffs_by_run = {}
    for rec in extract_handoffs(parsed):
        handoffs_by_run.setdefault(rec["run"], []).append(
            {"seq": rec["seq"], "from": rec["from"], "to": rec["to"],
             "attested": rec["attested"]})

    out = []
    for run in order:
        r = runs[run]
        for call_seq in r["failed_calls"]:
            cobj = r["by_seq"].get(call_seq) or {}
            cname = cobj.get("name")
            if isinstance(cname, str):
                t = r["tools"].setdefault(
                    cname, {"calls": 0, "errors": 0, "dur_ms": 0})
                t["errors"] += 1
        span = None
        if r["first_ts"] is not None and r["last_ts"] is not None:
            span = (r["last_ts"] - r["first_ts"]).total_seconds()
        out.append({
            "run": run,
            "events": r["events"],
            "by_kind": {k: r["by_kind"][k] for k in KIND_VOCAB
                        if k in r["by_kind"]} | {
                k: v for k, v in r["by_kind"].items()
                if k not in KIND_VOCAB},
            "tools": r["tools"],
            "tokens_in": r["tokens_in"],
            "tokens_out": r["tokens_out"],
            "cost": _fmt_decimal(r["cost"]) if r["cost_seen"] else None,
            "dur_total_ms": r["dur_total_ms"],
            "longest": r["longest"],
            "span_seconds": span,
            "errors": r["errors"],
            "handoffs": handoffs_by_run.get(run, []),
        })

    totals = None
    if len(out) > 1:
        tc = Decimal(0)
        any_cost = False
        for r in out:
            if r["cost"] is not None:
                tc += Decimal(r["cost"])
                any_cost = True
        totals = {
            "runs": len(out),
            "events": sum(r["events"] for r in out),
            "tokens_in": sum(r["tokens_in"] for r in out),
            "tokens_out": sum(r["tokens_out"] for r in out),
            "cost": _fmt_decimal(tc) if any_cost else None,
            "errors": sum(len(r["errors"]) for r in out),
        }
    return out, totals


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------

def _finding_line(f):
    where = f"line {f['line']}" if f["line"] else "file"
    return f"  {where:<9} [{f['cls']}] {f['message']}"


def print_validate_text(reports):
    multi = len(reports) > 1
    for rep in reports:
        print("=" * 68)
        print("TraceTape — validation report")
        print("=" * 68)
        print(f"File:   {rep['file']}")
        print(f"Events: {rep['events']} · Runs: {len(rep['runs'])}"
              f" · Blank lines: {rep['blank_lines']}")
        verdict = "✅ VALID" if rep["ok"] else "❌ INVALID"
        print(f"Result: {verdict} — {len(rep['errors'])} error(s), "
              f"{len(rep['warnings'])} warning(s)")
        errors = [f for f in rep["errors"] if f["cls"] != "incomplete"]
        incomplete = [f for f in rep["errors"] + rep["warnings"]
                      if f["cls"] == "incomplete"]
        if errors:
            print("\n## Errors")
            for f in errors:
                print(_finding_line(f))
        if incomplete:
            print("\n## Incomplete runs")
            for f in incomplete:
                print(_finding_line(f))
        if rep["warnings"]:
            warns = [f for f in rep["warnings"] if f["cls"] != "incomplete"]
            if warns:
                print("\n## Warnings")
                for f in warns:
                    print(_finding_line(f))
        if multi:
            print()


def _print_run_summary(r):
    span = _fmt_span(r["span_seconds"]) if r["span_seconds"] is not None \
        else "—"
    print(f"Run: {r['run']}  ({r['events']} events · {span} wall clock)")
    kinds = " · ".join(f"{k} {v}" for k, v in r["by_kind"].items())
    print(f"  Kinds:  {kinds}")
    if r["tools"]:
        print("  Tools:")
        print(f"    {'name':<24} {'calls':>5} {'errors':>6} {'total ms':>9}")
        for name in sorted(r["tools"]):
            t = r["tools"][name]
            print(f"    {name:<24} {t['calls']:>5} {t['errors']:>6} "
                  f"{t['dur_ms']:>9}")
    print(f"  Tokens: in {r['tokens_in']:,} · out {r['tokens_out']:,}")
    if r["cost"] is not None:
        print(f"  Cost:   {r['cost']}")
    if r["longest"]:
        lg = r["longest"]
        print(f"  Longest step: {lg['name']} (seq {lg['seq']}) — "
              f"{lg['dur_ms']} ms")
    if r["errors"]:
        print("  Errors:")
        for e in r["errors"]:
            print(f"    seq {e['seq']} · {e['name']} ({e['kind']})")
    else:
        print("  Errors: none")
    if r.get("handoffs"):
        print("  Handoffs:")
        for h in r["handoffs"]:
            mark = "attested" if h["attested"] else "UNATTESTED"
            print(f"    seq {h['seq']} · {h['from']} → {h['to']} "
                  f"({mark})")


def print_summary_text(file_label, runs, totals):
    print("=" * 68)
    print("TraceTape — run summary")
    print("=" * 68)
    print(f"File: {file_label}")
    print()
    for r in runs:
        _print_run_summary(r)
        print()
    if totals:
        print("-" * 68)
        print(f"Totals: {totals['runs']} runs · {totals['events']} events "
              f"· tokens in {totals['tokens_in']:,} / out "
              f"{totals['tokens_out']:,}"
              + (f" · cost {totals['cost']}" if totals["cost"] else "")
              + f" · {totals['errors']} error(s)")


# ---------------------------------------------------------------------------
# Proof bundles (item: TraceTape as the family's shared proof envelope)
#
# A bundle seals one tape plus its companion evidence into a single
# JSON document in which every artifact is named by the SHA-256 of
# its bytes: the claims file, the skill manifest (per-file hashes),
# sibling tool reports, the cost record, the schema contract and the
# TrustBadge score. `verify` re-reads the files on disk and fails if
# anything sealed has gone missing or changed — the envelope is the
# receipt, the files are the evidence, and the two must agree.
# ---------------------------------------------------------------------------

def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _claim_lines(text):
    """(line_no, stripped) for claim-bearing lines of a report:
    non-empty, outside code fences, not a markdown heading."""
    out = []
    in_fence = False
    for i, raw in enumerate(text.splitlines(), start=1):
        s = raw.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not s or s.startswith("#"):
            continue
        out.append((i, s))
    return out


def _cost_total(doc):
    """Best-effort total from a cost-record JSON document. Returns
    (total-as-string-or-None, key-it-came-from). Recognizes the
    CostLedger ledger shape (totals.cost_usd) plus common flat keys;
    anything else is honestly recorded as unknown, never guessed."""
    candidates = []
    totals = doc.get("totals") if isinstance(doc, dict) else None
    if isinstance(totals, dict):
        candidates += [("totals.cost_usd", totals.get("cost_usd")),
                       ("totals.cost", totals.get("cost")),
                       ("totals.total", totals.get("total"))]
    if isinstance(doc, dict):
        candidates += [("total", doc.get("total")),
                       ("total_cost", doc.get("total_cost")),
                       ("cost", doc.get("cost"))]
    for key, value in candidates:
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, dict):  # {"amount": ..., "currency": ...}
            amount = value.get("amount")
            if isinstance(amount, (int, float, str)) \
                    and not isinstance(amount, bool):
                return str(amount), key
            continue
        if isinstance(value, (int, float, str)):
            return str(value), key
    return None, None


def _json_file_section(path, what):
    """Read + parse a JSON evidence file. Returns (doc, raw_bytes);
    raises ValueError with a caller-facing message on problems."""
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"{what} file not found: {path}")
    raw = p.read_bytes()
    try:
        doc = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        raise ValueError(f"{what} file is not valid JSON: {path} ({e})")
    if not isinstance(doc, dict):
        raise ValueError(f"{what} file must hold a JSON object: {path}")
    return doc, raw


def build_bundle(tape_path, claims=None, skill=None, cost=None,
                 schema=None, trustbadge=None, reports=()):
    """Assemble the proof-bundle dict for a VALID tape. Raises
    ValueError (caller problems → exit 2) for bad companion inputs,
    and TapeInvalid (→ exit 1) when the tape itself has errors."""
    p = Path(tape_path)
    if not p.is_file():
        raise ValueError(f"no such file: {tape_path}")
    raw = p.read_bytes()
    text = raw.decode("utf-8", errors="replace").replace(
        "\r\n", "\n").replace("\r", "\n")
    report = validate_text(text, str(p))
    if report["errors"]:
        raise TapeInvalid(report)
    parsed, _f, _b = parse_lines(text)
    runs, totals = summarize_parsed(parsed)
    evidence_hashes = set()
    for _ln, obj in parsed:
        if obj is None:
            continue
        data = obj.get("data")
        if isinstance(data, dict) and isinstance(data.get("sha256"), str):
            evidence_hashes.add(data["sha256"])
    sections = {
        "tape": {
            "file": str(tape_path),
            "sha256": _sha256(raw),
            "events": report["events"],
            "runs": report["runs"],
            "warnings": len(report["warnings"]),
            "evidence_hashes": sorted(evidence_hashes),
            "summary": {"runs": runs, "totals": totals},
        },
    }
    if claims is not None:
        cp = Path(claims)
        if not cp.is_file():
            raise ValueError(f"claims file not found: {claims}")
        craw = cp.read_bytes()
        ctext = craw.decode("utf-8", errors="replace")
        items = [{"line": n,
                  "sha256": _sha256(s.encode("utf-8"))}
                 for n, s in _claim_lines(ctext)]
        sections["claims"] = {"file": str(claims),
                              "sha256": _sha256(craw),
                              "count": len(items), "items": items}
    if skill is not None:
        sp = Path(skill)
        if not sp.is_dir():
            raise ValueError(f"skill is not a directory: {skill}")
        files = []
        for f in sorted(sp.rglob("*")):
            rel = f.relative_to(sp)
            if any(part in (".git", "__pycache__", "node_modules")
                   for part in rel.parts):
                continue
            if f.is_file():
                data = f.read_bytes()
                files.append({"path": rel.as_posix(),
                              "sha256": _sha256(data),
                              "bytes": len(data)})
        name = None
        smd = sp / "SKILL.md"
        if smd.is_file():
            for line in smd.read_text(encoding="utf-8",
                                      errors="replace").splitlines()[:40]:
                m = line.strip()
                if m.lower().startswith("name:"):
                    name = m.split(":", 1)[1].strip().strip("\"'") or None
                    break
        sections["skill_manifest"] = {
            "dir": str(skill), "name": name, "files": files,
            "file_count": len(files),
            "total_bytes": sum(f["bytes"] for f in files)}
    if reports:
        tool_outputs = []
        for rpath in reports:
            doc, rraw = _json_file_section(rpath, "report")
            entry = {"file": str(rpath), "sha256": _sha256(rraw),
                     "tool": doc.get("tool"),
                     "version": doc.get("version")}
            for key in ("verdict", "ok", "score"):
                if key in doc:
                    entry[key] = doc[key]
            tool_outputs.append(entry)
        sections["tool_outputs"] = tool_outputs
    if cost is not None:
        doc, craw = _json_file_section(cost, "cost record")
        total, total_key = _cost_total(doc)
        sections["cost_record"] = {
            "file": str(cost), "sha256": _sha256(craw),
            "total": total, "total_key": total_key,
            "currency": doc.get("currency")}
    if schema is not None:
        doc, sraw = _json_file_section(schema, "schema")
        sections["schema_contract"] = {
            "file": str(schema), "sha256": _sha256(sraw),
            "title": doc.get("title"), "id": doc.get("$id")}
    if trustbadge is not None:
        doc, traw = _json_file_section(trustbadge, "trustbadge report")
        sections["trustbadge"] = {
            "file": str(trustbadge), "sha256": _sha256(traw),
            "composite": doc.get("composite"),
            "capped": doc.get("capped"),
            "coverage": doc.get("coverage")}
    bundle = {"tool": "tracetape", "version": VERSION,
              "schema_version": 1, "kind": "proof-bundle",
              "bundle_version": BUNDLE_VERSION, "sections": sections}
    bundle["bundle_sha256"] = _bundle_digest(bundle)
    return bundle


class TapeInvalid(Exception):
    """The tape has validation errors — a verdict, not a caller
    error (carries the validate report)."""

    def __init__(self, report):
        super().__init__("tape is invalid")
        self.report = report


def _bundle_digest(bundle):
    body = {k: v for k, v in bundle.items() if k != "bundle_sha256"}
    canon = json.dumps(body, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False)
    return _sha256(canon.encode("utf-8"))


def _resolve_evidence(stored, bundle_dir):
    """Locate a sealed evidence file: as recorded, relative to the
    bundle, or by basename next to the bundle."""
    p = Path(stored)
    for cand in (p, bundle_dir / stored, bundle_dir / p.name):
        if cand.is_file():
            return cand
    return None


def verify_bundle(bundle, bundle_path):
    """Re-check a bundle dict against the files on disk. Returns
    (findings, section_status) — findings use the validate shape;
    every level is 'error' (a failed proof is a failed proof)."""
    findings = []
    status = {}

    def err(cls, msg):
        findings.append({"line": 0, "level": "error", "cls": cls,
                         "message": msg})

    if bundle.get("tool") != "tracetape" \
            or bundle.get("kind") != "proof-bundle":
        err("bundle-structure",
            "not a TraceTape proof bundle (tool/kind mismatch)")
    if bundle.get("bundle_version") != BUNDLE_VERSION:
        err("bundle-structure",
            f"unsupported bundle_version "
            f"{bundle.get('bundle_version')!r} — this tool reads "
            f"version {BUNDLE_VERSION}")
    sections = bundle.get("sections")
    if not isinstance(sections, dict) or "tape" not in sections:
        err("bundle-structure", "bundle has no sections.tape")
        return findings, status
    digest = bundle.get("bundle_sha256")
    if not digest:
        err("bundle-structure", "bundle carries no bundle_sha256")
    elif digest != _bundle_digest(bundle):
        err("bundle-digest",
            "bundle_sha256 does not match the bundle's contents — "
            "the envelope itself was altered after sealing")

    bundle_dir = Path(bundle_path).parent if bundle_path else Path(".")

    def check_hash(name, sec, record=True):
        stored = sec.get("file") or sec.get("dir")
        found = _resolve_evidence(stored, bundle_dir) if stored else None
        if found is None:
            err("missing-evidence",
                f"{name}: sealed file is gone: {stored}")
            if record:
                status[name] = "missing"
            return None
        actual = _sha256(found.read_bytes())
        if actual != sec.get("sha256"):
            err("hash-mismatch",
                f"{name}: {stored} changed since sealing "
                f"(sha256 {actual[:12]}… != sealed "
                f"{str(sec.get('sha256'))[:12]}…)")
            if record:
                status[name] = "mismatch"
            return None
        if record:
            status[name] = "ok"
        return found

    tape_sec = sections["tape"]
    tape_file = check_hash("tape", tape_sec)
    if tape_file is not None:
        text = tape_file.read_bytes().decode(
            "utf-8", errors="replace").replace("\r\n", "\n").replace(
                "\r", "\n")
        rep = validate_text(text, str(tape_file))
        if rep["errors"]:
            err("tape-invalid",
                f"tape: sealed tape no longer validates "
                f"({len(rep['errors'])} error(s), first: "
                f"{rep['errors'][0]['message']})")
            status["tape"] = "invalid"
        elif rep["events"] != tape_sec.get("events") \
                or rep["runs"] != tape_sec.get("runs"):
            err("tape-drift",
                "tape: event/run counts differ from the sealed "
                "summary")
            status["tape"] = "drift"

    if "claims" in sections:
        sec = sections["claims"]
        check_hash("claims", sec)
        found = _resolve_evidence(sec.get("file", ""), bundle_dir)
        if found is not None:
            ctext = found.read_bytes().decode("utf-8", errors="replace")
            items = [{"line": n, "sha256": _sha256(s.encode("utf-8"))}
                     for n, s in _claim_lines(ctext)]
            if items != sec.get("items"):
                err("claims-drift",
                    "claims: claim lines differ from the sealed "
                    "manifest (claims added, removed or edited)")
                status["claims"] = "drift"

    if "skill_manifest" in sections:
        sec = sections["skill_manifest"]
        root = None
        for cand in (Path(sec.get("dir", "")),
                     bundle_dir / sec.get("dir", ""),
                     bundle_dir / Path(sec.get("dir", "")).name):
            if cand.is_dir():
                root = cand
                break
        if root is None:
            err("missing-evidence",
                f"skill_manifest: sealed skill dir is gone: "
                f"{sec.get('dir')}")
            status["skill_manifest"] = "missing"
        else:
            now = {}
            for f in sorted(root.rglob("*")):
                rel = f.relative_to(root)
                if any(part in (".git", "__pycache__", "node_modules")
                       for part in rel.parts):
                    continue
                if f.is_file():
                    now[rel.as_posix()] = _sha256(f.read_bytes())
            sealed = {f["path"]: f["sha256"]
                      for f in sec.get("files", [])}
            if now != sealed:
                gone = sorted(set(sealed) - set(now))
                added = sorted(set(now) - set(sealed))
                changed = sorted(k for k in set(now) & set(sealed)
                                 if now[k] != sealed[k])
                bits = []
                if gone:
                    bits.append(f"removed: {', '.join(gone)}")
                if added:
                    bits.append(f"added: {', '.join(added)}")
                if changed:
                    bits.append(f"changed: {', '.join(changed)}")
                err("manifest-drift",
                    "skill_manifest: skill files differ from the "
                    "sealed manifest (" + "; ".join(bits) + ")")
                status["skill_manifest"] = "drift"
            else:
                status["skill_manifest"] = "ok"

    if "tool_outputs" in sections:
        ok_all = True
        for entry in sections["tool_outputs"]:
            before = len(findings)
            check_hash(f"tool_outputs[{entry.get('tool')}]", entry,
                       record=False)
            if len(findings) > before:
                ok_all = False
        status["tool_outputs"] = "ok" if ok_all else "failed"

    for name in ("cost_record", "schema_contract", "trustbadge"):
        if name in sections:
            check_hash(name, sections[name])

    return findings, status


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _emit_error(code, message):
    """Print the human error line plus the JSON error envelope to
    stderr (CONVENTIONS.md: caller errors carry both)."""
    print(f"error: {message}", file=sys.stderr)
    print(json.dumps({"error": {"code": code, "message": message}}),
          file=sys.stderr)


def _fatal_code(fatal):
    """Map a validate_path fatal string to an error-envelope code."""
    if fatal.startswith("no such file or directory:"):
        return "file_not_found"
    if fatal.startswith("no .jsonl files found under"):
        return "invalid_input"
    if fatal.startswith("error:"):
        return "read_error"
    return "invalid_input"


def cmd_validate(args):
    if args.target == "-":
        # Pipe composability (CONVENTIONS.md): one tape arrives on
        # stdin and is validated in memory — no temp file needed.
        reports = [validate_text(sys.stdin.read(), "<stdin>",
                                 allow_incomplete=args.allow_incomplete)]
        fatal = None
    else:
        reports, fatal = validate_path(
            args.target, allow_incomplete=args.allow_incomplete)
    if fatal:
        _emit_error(_fatal_code(fatal), fatal)
        return 2
    if args.format == "json":
        out = {"tool": "tracetape", "version": VERSION,
               "schema_version": 1,
               "command": "validate",
               "ok": all(r["ok"] for r in reports),
               "files": reports}
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print_validate_text(reports)
    any_error = any(r["errors"] for r in reports)
    any_warning = any(r["warnings"] for r in reports)
    if any_error or (args.strict and any_warning):
        return 1
    return 0


def cmd_summary(args):
    if args.file == "-":
        # Pipe composability (CONVENTIONS.md): summarize stdin.
        text = sys.stdin.read().replace("\r\n", "\n").replace(
            "\r", "\n")
        label = "<stdin>"
    else:
        p = Path(args.file)
        if p.is_dir():
            _emit_error("not_a_file",
                        "summary takes a single tape file, not a "
                        "directory")
            return 2
        if not p.is_file():
            _emit_error("file_not_found", f"no such file: {args.file}")
            return 2
        try:
            text = _read(p)
        except OSError as e:
            _emit_error("read_error", f"{e}")
            return 2
        label = args.file
    parsed, _findings, _blank = parse_lines(text)
    runs, totals = summarize_parsed(parsed)
    if args.format == "json":
        out = {"tool": "tracetape", "version": VERSION,
               "schema_version": 1,
               "command": "summary", "file": label,
               "runs": runs, "totals": totals}
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print_summary_text(label, runs, totals)
    return 0


def cmd_handoffs(args):
    if args.file == "-":
        text = sys.stdin.read().replace("\r\n", "\n").replace(
            "\r", "\n")
        label = "<stdin>"
    else:
        p = Path(args.file)
        if not p.is_file():
            _emit_error("file_not_found", f"no such file: {args.file}")
            return 2
        try:
            text = _read(p)
        except OSError as e:
            _emit_error("read_error", f"{e}")
            return 2
        label = args.file
    parsed, _f, _b = parse_lines(text)
    records, findings = analyze_handoffs(parsed)
    if args.format == "json":
        out = {"tool": "tracetape", "version": VERSION,
               "schema_version": 1, "command": "handoffs",
               "file": label, "ok": not findings,
               "handoffs": records, "findings": findings}
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print("=" * 68)
        print("TraceTape — handoff chain")
        print("=" * 68)
        print(f"File: {label}")
        if records:
            for rec in records:
                mark = "attested" if rec["attested"] else "UNATTESTED"
                print(f"  run {rec['run']} · seq {rec['seq']} · "
                      f"{rec['from']} → {rec['to']} ({mark})")
        else:
            print("  (no handoff events on this tape)")
        if findings:
            print("\n## Findings")
            for f in findings:
                print(_finding_line(f))
        verdict = "✅ CLEAN" if not findings else "⚠️  FINDINGS"
        print(f"\nResult: {verdict} — {len(records)} handoff(s), "
              f"{len(findings)} finding(s)")
    return 1 if findings else 0


def cmd_bundle(args):
    try:
        bundle = build_bundle(
            args.tape, claims=args.claims, skill=args.skill,
            cost=args.cost, schema=args.schema,
            trustbadge=args.trustbadge, reports=args.report or ())
    except TapeInvalid as e:
        rep = e.report
        print(f"error: tape is invalid — refusing to seal a proof "
              f"bundle over {len(rep['errors'])} validation "
              f"error(s); first: {rep['errors'][0]['message']}",
              file=sys.stderr)
        return 1
    except ValueError as e:
        msg = str(e)
        code = "file_not_found" if "not found" in msg else \
            "invalid_json" if "not valid JSON" in msg else \
            "not_a_directory" if "not a directory" in msg else \
            "invalid_input"
        _emit_error(code, msg)
        return 2
    payload = json.dumps(bundle, indent=2, ensure_ascii=False)
    if args.out:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
    if args.format == "json":
        if not args.out:
            print(payload)
        else:
            print(json.dumps(
                {"tool": "tracetape", "version": VERSION,
                 "schema_version": 1, "command": "bundle",
                 "out": args.out,
                 "bundle_sha256": bundle["bundle_sha256"],
                 "sections": sorted(bundle["sections"])},
                indent=2, ensure_ascii=False))
    else:
        secs = bundle["sections"]
        print("=" * 68)
        print("TraceTape — proof bundle")
        print("=" * 68)
        print(f"Tape:     {secs['tape']['file']} "
              f"({secs['tape']['events']} events, "
              f"{len(secs['tape']['runs'])} run(s))")
        for name in ("claims", "skill_manifest", "cost_record",
                     "schema_contract", "trustbadge"):
            if name in secs:
                print(f"Sealed:   {name}")
        if "tool_outputs" in secs:
            tools = [e.get("tool") for e in secs["tool_outputs"]]
            print(f"Sealed:   tool_outputs ({', '.join(map(str, tools))})")
        print(f"Digest:   {bundle['bundle_sha256']}")
        if args.out:
            print(f"Written:  {args.out}")
    return 0


def cmd_verify(args):
    p = Path(args.bundle)
    if not p.is_file():
        _emit_error("file_not_found", f"no such file: {args.bundle}")
        return 2
    try:
        bundle = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _emit_error("invalid_json",
                    f"bundle file is not valid JSON: {args.bundle} "
                    f"({e})")
        return 2
    if not isinstance(bundle, dict):
        _emit_error("invalid_input",
                    "bundle file must hold a JSON object")
        return 2
    findings, status = verify_bundle(bundle, str(p))
    if args.format == "json":
        out = {"tool": "tracetape", "version": VERSION,
               "schema_version": 1, "command": "verify",
               "bundle": args.bundle, "ok": not findings,
               "sections": status, "findings": findings}
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print("=" * 68)
        print("TraceTape — bundle verification")
        print("=" * 68)
        print(f"Bundle: {args.bundle}")
        for name, st in status.items():
            print(f"  {name:<16} {st}")
        if findings:
            print("\n## Findings")
            for f in findings:
                print(f"  [{f['cls']}] {f['message']}")
        verdict = "✅ VERIFIED" if not findings else "❌ FAILED"
        print(f"\nResult: {verdict} — {len(findings)} finding(s)")
    return 1 if findings else 0


SAMPLE_TAPE = """\
{"v": 1, "ts": "2026-10-20T09:00:00Z", "run": "sample-run-001", "seq": 1, "kind": "run_start", "name": "support-agent"}
{"v": 1, "ts": "2026-10-20T09:00:01Z", "run": "sample-run-001", "seq": 2, "kind": "prompt", "name": "user-prompt", "tokens_in": 182, "data": {"sha256": "9f2c1a…", "chars": 412}}
{"v": 1, "ts": "2026-10-20T09:00:02Z", "run": "sample-run-001", "seq": 3, "kind": "tool_call", "name": "read_file", "data": {"path_ref": "ticket-4821.txt"}}
{"v": 1, "ts": "2026-10-20T09:00:02Z", "run": "sample-run-001", "seq": 4, "kind": "tool_result", "name": "read_file", "parent": 3, "dur_ms": 84, "ok": true}
{"v": 1, "ts": "2026-10-20T09:00:03Z", "run": "sample-run-001", "seq": 5, "kind": "tool_call", "name": "run_tests", "data": {"suite": "billing"}}
{"v": 1, "ts": "2026-10-20T09:00:09Z", "run": "sample-run-001", "seq": 6, "kind": "tool_result", "name": "run_tests", "parent": 5, "dur_ms": 5930, "ok": false}
{"v": 1, "ts": "2026-10-20T09:00:09Z", "run": "sample-run-001", "seq": 7, "kind": "error", "name": "run_tests", "parent": 5, "ok": false, "data": {"error": "2 tests failed", "handled": true, "action": "reported to user, run continued"}}
{"v": 1, "ts": "2026-10-20T09:00:11Z", "run": "sample-run-001", "seq": 8, "kind": "message", "name": "assistant-reply", "tokens_in": 640, "tokens_out": 148, "cost": "0.0042", "data": {"sha256": "b71e09…"}}
{"v": 1, "ts": "2026-10-20T09:00:11Z", "run": "sample-run-001", "seq": 9, "kind": "run_end", "name": "support-agent", "ok": true}
"""

EXPECTED_TEMPLATE = """\
# What to expect

`trace.jsonl` is one complete run of a fictional support agent, in
the TraceTape format (see TRACE_FORMAT.md in the repo):

    tracetape validate trace.jsonl        # ✅ VALID, exit 0
    tracetape summary  trace.jsonl        # profile of the run

The run contains a *handled* error: `run_tests` fails (seq 6–7),
the agent records it and finishes the run anyway. That is legal
tape — `error` is an event kind, not a format violation. Summary
shows it under the run's error list and in the tool table's error
column for `run_tests`.

Try breaking it: delete the `run_end` line and validate again —
the run is reported as *incomplete*. Point `seq` 8's parent at 99
and you get a *dangling parent*. The validator names each fault,
with its line number.
"""


def cmd_init(args):
    base = Path(args.dir) if args.dir else Path.cwd()
    targets = {base / "trace.jsonl": SAMPLE_TAPE,
               base / "EXPECTED.md": EXPECTED_TEMPLATE}
    for p in targets:
        if p.exists() and not args.force:
            print(f"{p} exists — use --force to overwrite")
            return 2
    for p, content in targets.items():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    print(f"created trace.jsonl and EXPECTED.md in {base}")
    print("try it:  tracetape validate trace.jsonl")
    print("         tracetape summary trace.jsonl")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="tracetape",
        description="Define, validate and summarize the canonical "
                    "agent run tape (trace.jsonl). One tape every "
                    "agent run leaves behind.")
    ap.add_argument("--version", action="version",
                    version=f"tracetape {VERSION}")
    ap.add_argument("--no-color", action="store_true",
                    help="disable ANSI color output (this tool emits "
                         "no color; accepted harmlessly)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="check a tape (or a directory "
                                        "of tapes) against the format")
    v.add_argument("target", help="a .jsonl tape file, or a directory "
                                  "of them ('-' reads one tape from "
                                  "stdin)")
    v.add_argument("--format", choices=("text", "json"), default="text")
    v.add_argument("--strict", action="store_true",
                   help="exit 1 on warnings as well as errors")
    v.add_argument("--allow-incomplete", action="store_true",
                   help="treat unterminated runs as warnings, not "
                        "errors (for tapes still being written)")
    v.set_defaults(fn=cmd_validate)

    s = sub.add_parser("summary", help="profile the runs on a tape")
    s.add_argument("file", help="the .jsonl tape file to summarize "
                                "('-' reads stdin)")
    s.add_argument("--format", choices=("text", "json"), default="text")
    s.set_defaults(fn=cmd_summary)

    h = sub.add_parser("handoffs", help="audit the multi-agent "
                                        "handoff chain on a tape")
    h.add_argument("file", help="the .jsonl tape file ('-' reads "
                                "stdin)")
    h.add_argument("--format", choices=("text", "json"), default="text")
    h.set_defaults(fn=cmd_handoffs)

    b = sub.add_parser("bundle", help="seal a tape + companion "
                                      "evidence into a proof bundle")
    b.add_argument("tape", help="the .jsonl tape file to seal "
                                "(must validate clean)")
    b.add_argument("--claims", help="claims/report file to seal")
    b.add_argument("--skill", help="skill directory to manifest + seal")
    b.add_argument("--cost", help="cost-record JSON file to seal")
    b.add_argument("--schema", help="schema-contract JSON file to seal")
    b.add_argument("--trustbadge", help="TrustBadge report JSON to seal")
    b.add_argument("--report", action="append",
                   help="sibling tool report JSON to seal "
                        "(repeatable)")
    b.add_argument("--out", help="write the bundle JSON to this path")
    b.add_argument("--format", choices=("text", "json"), default="json")
    b.set_defaults(fn=cmd_bundle)

    vf = sub.add_parser("verify", help="re-check a proof bundle "
                                       "against the files on disk")
    vf.add_argument("bundle", help="the bundle JSON written by "
                                   "`tracetape bundle`")
    vf.add_argument("--format", choices=("text", "json"), default="text")
    vf.set_defaults(fn=cmd_verify)

    i = sub.add_parser("init", help="scaffold a sample tape + EXPECTED.md")
    i.add_argument("--force", action="store_true")
    i.add_argument("--dir", help="directory to write the sample "
                                 "files into")
    i.set_defaults(fn=cmd_init)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
