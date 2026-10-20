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
    tracetape init

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
import json
import math
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

VERSION = "1.0.0"
FORMAT_VERSION = 1

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

    i = sub.add_parser("init", help="scaffold a sample tape + EXPECTED.md")
    i.add_argument("--force", action="store_true")
    i.add_argument("--dir", help="directory to write the sample "
                                 "files into")
    i.set_defaults(fn=cmd_init)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
