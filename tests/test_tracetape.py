"""Tests for TraceTape format validation rules (each firing and not
firing), per-run isolation, summary math, CLI exit codes, and
scaffolding."""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from tracetape import parse_lines, summarize_parsed, validate_text

CLI = Path(__file__).parent.parent / "src" / "tracetape.py"
EXAMPLES = Path(__file__).parent.parent / "examples"


def run_cli(*argv, cwd=None):
    return subprocess.run(
        [sys.executable, str(CLI), *argv],
        capture_output=True, text=True, cwd=cwd)


def ev(seq, kind, run="r1", name="step", ts=None, **extra):
    e = {"v": 1, "ts": ts or f"2026-10-20T09:00:{seq:02d}Z",
         "run": run, "seq": seq, "kind": kind, "name": name}
    e.update(extra)
    return e


def tape(*events):
    return "\n".join(json.dumps(e) for e in events) + "\n"


def good_run(run="r1"):
    return tape(
        ev(1, "run_start", run=run, name="agent"),
        ev(2, "prompt", run=run, name="user-prompt", tokens_in=50),
        ev(3, "tool_call", run=run, name="search"),
        ev(4, "tool_result", run=run, name="search", parent=3,
           dur_ms=120, ok=True),
        ev(5, "message", run=run, name="reply", tokens_in=300,
           tokens_out=80, cost="0.0020"),
        ev(6, "run_end", run=run, name="agent", ok=True),
    )


def classes(report, level="errors"):
    return [f["cls"] for f in report[level]]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_good_tape_is_valid():
    rep = validate_text(good_run())
    assert rep["ok"] and rep["errors"] == [] and rep["warnings"] == []
    assert rep["events"] == 6 and rep["runs"] == ["r1"]


def test_example_good_tape_is_valid():
    rep = validate_text((EXAMPLES / "good.jsonl").read_text())
    assert rep["ok"], rep["errors"]
    assert len(rep["runs"]) == 2 and rep["events"] == 17


def test_blank_lines_tolerated():
    text = "\n\n" + good_run().replace("\n", "\n\n") + "\n"
    rep = validate_text(text)
    assert rep["ok"] and rep["blank_lines"] > 0


# ---------------------------------------------------------------------------
# Line-level rules
# ---------------------------------------------------------------------------

def test_bad_json_line_reports_line_number():
    text = good_run() + "{not json\n"
    rep = validate_text(text)
    assert "bad-json" in classes(rep)
    f = [f for f in rep["errors"] if f["cls"] == "bad-json"][0]
    assert f["line"] == 7


def test_non_object_line():
    rep = validate_text("[1, 2, 3]\n" + good_run())
    assert "not-an-object" in classes(rep)


def test_missing_required_field():
    e = ev(1, "run_start", name="agent")
    del e["name"]
    rep = validate_text(tape(e, ev(2, "run_end", name="agent")))
    assert "missing-field" in classes(rep)


def test_wrong_field_types():
    e = ev(1, "run_start", name="agent")
    e["v"] = "1"
    e["seq"] = "one"
    rep = validate_text(tape(e))
    assert classes(rep).count("field-type") >= 2


def test_bool_is_not_an_int():
    e = ev(1, "run_start", name="agent")
    e["seq"] = True
    rep = validate_text(tape(e))
    assert "field-type" in classes(rep)


def test_unsupported_version():
    e = ev(1, "run_start", name="agent")
    e["v"] = 2
    rep = validate_text(tape(e, ev(2, "run_end", name="agent")))
    assert "unsupported-version" in classes(rep)


def test_unknown_kind():
    rep = validate_text(tape(
        ev(1, "run_start", name="agent"),
        ev(2, "teleport", name="x"),
        ev(3, "run_end", name="agent")))
    assert "unknown-kind" in classes(rep)


def test_bad_timestamp_string():
    rep = validate_text(tape(
        ev(1, "run_start", name="agent", ts="next Tuesday-ish"),
        ev(2, "run_end", name="agent")))
    assert "bad-timestamp" in classes(rep)


def test_optional_field_shapes_enforced():
    rep = validate_text(tape(
        ev(1, "run_start", name="agent"),
        ev(2, "message", name="m", tokens_in=-5, dur_ms=-1,
           cost="not-a-price", ok="yes", data=[1, 2]),
        ev(3, "run_end", name="agent")))
    cls = classes(rep)
    assert cls.count("field-value") >= 2   # negative tokens + bad cost
    assert cls.count("field-type") >= 2    # ok not bool, data not object
    assert rep["ok"] is False


def test_empty_file_is_an_error():
    rep = validate_text("")
    assert rep["ok"] is False
    assert "empty" in classes(rep)


# ---------------------------------------------------------------------------
# Run-level rules
# ---------------------------------------------------------------------------

def test_duplicate_seq():
    rep = validate_text(tape(
        ev(1, "run_start", name="agent"),
        ev(2, "prompt", name="p"),
        ev(2, "message", name="m"),
        ev(3, "run_end", name="agent")))
    assert "duplicate-seq" in classes(rep)


def test_seq_goes_backwards():
    rep = validate_text(tape(
        ev(1, "run_start", name="agent"),
        ev(3, "prompt", name="p"),
        ev(2, "message", name="m"),
        ev(4, "run_end", name="agent")))
    assert "seq-order" in classes(rep)


def test_seq_gap_is_a_warning_not_an_error():
    rep = validate_text(tape(
        ev(1, "run_start", name="agent"),
        ev(4, "message", name="m"),
        ev(5, "run_end", name="agent")))
    assert rep["ok"] is True
    assert "seq-gap" in classes(rep, "warnings")


def test_dangling_parent():
    rep = validate_text(tape(
        ev(1, "run_start", name="agent"),
        ev(2, "tool_result", name="t", parent=99),
        ev(3, "run_end", name="agent")))
    assert "dangling-parent" in classes(rep)


def test_parent_cannot_point_forward():
    rep = validate_text(tape(
        ev(1, "run_start", name="agent"),
        ev(2, "tool_result", name="t", parent=3),
        ev(3, "tool_call", name="t"),
        ev(4, "run_end", name="agent")))
    assert "dangling-parent" in classes(rep)


def test_parent_cannot_point_at_itself():
    rep = validate_text(tape(
        ev(1, "run_start", name="agent"),
        ev(2, "tool_result", name="t", parent=2),
        ev(3, "run_end", name="agent")))
    assert "dangling-parent" in classes(rep)


def test_timestamp_disorder():
    rep = validate_text(tape(
        ev(1, "run_start", name="agent", ts="2026-10-20T09:00:10Z"),
        ev(2, "message", name="m", ts="2026-10-20T09:00:05Z"),
        ev(3, "run_end", name="agent", ts="2026-10-20T09:00:12Z")))
    assert "timestamp-order" in classes(rep)


def test_missing_run_start():
    rep = validate_text(tape(
        ev(1, "prompt", name="p"),
        ev(2, "run_end", name="agent")))
    assert "missing-start" in classes(rep)


def test_event_after_run_end():
    rep = validate_text(tape(
        ev(1, "run_start", name="agent"),
        ev(2, "run_end", name="agent"),
        ev(3, "message", name="late")))
    assert "event-after-end" in classes(rep)


def test_incomplete_run_is_an_error_with_own_class():
    rep = validate_text(tape(
        ev(1, "run_start", name="agent"),
        ev(2, "message", name="m")))
    assert rep["ok"] is False
    assert "incomplete" in classes(rep)
    assert rep["incomplete_runs"] == ["r1"]


def test_allow_incomplete_downgrades_to_warning():
    rep = validate_text(tape(
        ev(1, "run_start", name="agent"),
        ev(2, "message", name="m")), allow_incomplete=True)
    assert rep["ok"] is True
    assert "incomplete" in classes(rep, "warnings")


# ---------------------------------------------------------------------------
# Multi-run isolation
# ---------------------------------------------------------------------------

def test_seq_restart_per_run_is_fine():
    text = good_run("run-a") + good_run("run-b")
    rep = validate_text(text)
    assert rep["ok"], rep["errors"]
    assert rep["runs"] == ["run-a", "run-b"]


def test_parent_cannot_cross_runs():
    text = tape(
        ev(1, "run_start", run="a", name="agent"),
        ev(2, "tool_call", run="a", name="t"),
        ev(1, "run_start", run="b", name="agent"),
        ev(2, "tool_result", run="b", name="t", parent=2),
        ev(3, "run_end", run="b", name="agent"),
        ev(3, "run_end", run="a", name="agent"))
    rep = validate_text(text)
    assert "dangling-parent" in classes(rep)


def test_timestamp_order_is_per_run():
    text = tape(
        ev(1, "run_start", run="a", name="agent",
           ts="2026-10-20T10:00:00Z"),
        ev(2, "run_end", run="a", name="agent",
           ts="2026-10-20T10:05:00Z"),
        ev(1, "run_start", run="b", name="agent",
           ts="2026-10-20T09:00:00Z"),
        ev(2, "run_end", run="b", name="agent",
           ts="2026-10-20T09:01:00Z"))
    rep = validate_text(text)
    assert rep["ok"], rep["errors"]


def test_example_broken_tape_names_every_fault():
    rep = validate_text((EXAMPLES / "broken.jsonl").read_text())
    all_cls = classes(rep) + classes(rep, "warnings")
    assert "seq-gap" in all_cls
    assert "dangling-parent" in all_cls
    assert "bad-timestamp" in all_cls
    assert "incomplete" in all_cls
    assert rep["ok"] is False


# ---------------------------------------------------------------------------
# Summary math
# ---------------------------------------------------------------------------

def summarize(text):
    parsed, _f, _b = parse_lines(text)
    return summarize_parsed(parsed)


def test_summary_tool_counts_and_errors():
    text = tape(
        ev(1, "run_start", name="agent"),
        ev(2, "tool_call", name="search"),
        ev(3, "tool_result", name="search", parent=2, dur_ms=100,
           ok=True),
        ev(4, "tool_call", name="search"),
        ev(5, "tool_result", name="search", parent=4, dur_ms=250,
           ok=False),
        ev(6, "error", name="search", parent=4, ok=False),
        ev(7, "run_end", name="agent", ok=True))
    runs, totals = summarize(text)
    r = runs[0]
    assert r["tools"]["search"] == {"calls": 2, "errors": 1,
                                    "dur_ms": 350}
    # Two error RECORDS (failed result + error event), one failed call.
    assert len(r["errors"]) == 2
    assert [e["seq"] for e in r["errors"]] == [5, 6]


def test_summary_token_and_cost_sums_are_decimal_exact():
    text = tape(
        ev(1, "run_start", name="agent"),
        ev(2, "message", name="a", tokens_in=100, tokens_out=10,
           cost="0.10"),
        ev(3, "message", name="b", tokens_in=50, tokens_out=5,
           cost="0.20"),
        ev(4, "run_end", name="agent"))
    runs, _ = summarize(text)
    r = runs[0]
    assert r["tokens_in"] == 150 and r["tokens_out"] == 15
    assert r["cost"] == "0.3"   # not 0.30000000000000004


def test_summary_longest_step_and_span():
    text = tape(
        ev(1, "run_start", name="agent", ts="2026-10-20T09:00:00Z"),
        ev(2, "tool_call", name="quick", ts="2026-10-20T09:00:01Z"),
        ev(3, "tool_result", name="quick", parent=2, dur_ms=50,
           ts="2026-10-20T09:00:02Z"),
        ev(4, "tool_call", name="slow", ts="2026-10-20T09:00:03Z"),
        ev(5, "tool_result", name="slow", parent=4, dur_ms=4200,
           ts="2026-10-20T09:00:08Z"),
        ev(6, "run_end", name="agent", ts="2026-10-20T09:00:09Z"))
    runs, _ = summarize(text)
    r = runs[0]
    assert r["longest"]["name"] == "slow" and r["longest"]["dur_ms"] == 4200
    assert r["span_seconds"] == 9.0
    assert r["by_kind"]["tool_call"] == 2


def test_summary_multi_run_totals():
    runs, totals = summarize(good_run("a") + good_run("b"))
    assert len(runs) == 2
    assert totals["runs"] == 2 and totals["events"] == 12
    assert totals["tokens_in"] == 700 and totals["cost"] == "0.004"
    assert totals["errors"] == 0


def test_summary_empty_file():
    runs, totals = summarize("")
    assert runs == [] and totals is None


def test_summary_example_good_tape():
    parsed, _f, _b = parse_lines((EXAMPLES / "good.jsonl").read_text())
    runs, totals = summarize_parsed(parsed)
    by_run = {r["run"]: r for r in runs}
    a = by_run["checkout-2026-10-20-a"]
    assert a["tools"]["fetch_page"]["dur_ms"] == 1240
    b = by_run["digest-2026-10-20-b"]
    assert b["tools"]["summarize"]["calls"] == 2
    assert b["tools"]["summarize"]["errors"] == 1
    assert totals["cost"] == "0.0185"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_validate_good_exit_0(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(good_run())
    assert run_cli("validate", str(p)).returncode == 0


def test_cli_validate_broken_exit_1():
    r = run_cli("validate", str(EXAMPLES / "broken.jsonl"))
    assert r.returncode == 1
    assert "dangling-parent" in r.stdout
    assert "## Incomplete runs" in r.stdout


def test_cli_validate_strict_turns_gap_warning_into_failure(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(tape(ev(1, "run_start", name="agent"),
                      ev(4, "message", name="m"),
                      ev(5, "run_end", name="agent")))
    assert run_cli("validate", str(p)).returncode == 0
    assert run_cli("validate", str(p), "--strict").returncode == 1


def test_cli_validate_allow_incomplete(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(tape(ev(1, "run_start", name="agent"),
                      ev(2, "message", name="m")))
    assert run_cli("validate", str(p)).returncode == 1
    assert run_cli("validate", str(p),
                   "--allow-incomplete").returncode == 0


def test_cli_missing_file_exit_2():
    assert run_cli("validate", "/nonexistent/x.jsonl").returncode == 2
    assert run_cli("summary", "/nonexistent/x.jsonl").returncode == 2


def test_cli_directory_input(tmp_path):
    (tmp_path / "a.jsonl").write_text(good_run("a"))
    (tmp_path / "b.jsonl").write_text(good_run("b"))
    r = run_cli("validate", str(tmp_path))
    assert r.returncode == 0
    assert r.stdout.count("validation report") == 2


def test_cli_validate_json_shape():
    r = run_cli("validate", str(EXAMPLES / "good.jsonl"),
                "--format", "json")
    out = json.loads(r.stdout)
    assert out["tool"] == "tracetape" and out["ok"] is True
    assert out["files"][0]["runs"] == ["checkout-2026-10-20-a",
                                        "digest-2026-10-20-b"]


def test_cli_summary_json_shape():
    r = run_cli("summary", str(EXAMPLES / "good.jsonl"),
                "--format", "json")
    out = json.loads(r.stdout)
    assert out["command"] == "summary"
    assert out["totals"]["events"] == 17
    assert out["runs"][0]["cost"] == "0.0114"


def test_cli_summary_rejects_directory(tmp_path):
    assert run_cli("summary", str(tmp_path)).returncode == 2


# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

def test_init_scaffolds_a_valid_tape(tmp_path):
    r = run_cli("init", "--dir", str(tmp_path))
    assert r.returncode == 0
    tape_file = tmp_path / "trace.jsonl"
    assert tape_file.exists() and (tmp_path / "EXPECTED.md").exists()
    rep = validate_text(tape_file.read_text())
    assert rep["ok"], rep["errors"]


def test_init_refuses_to_overwrite(tmp_path):
    assert run_cli("init", "--dir", str(tmp_path)).returncode == 0
    assert run_cli("init", "--dir", str(tmp_path)).returncode == 2
    assert run_cli("init", "--dir", str(tmp_path),
                   "--force").returncode == 0


def test_init_sample_contains_a_handled_error(tmp_path):
    run_cli("init", "--dir", str(tmp_path))
    parsed, _f, _b = parse_lines((tmp_path / "trace.jsonl").read_text())
    runs, _ = summarize_parsed(parsed)
    r = runs[0]
    assert r["tools"]["run_tests"]["errors"] == 1
    assert r["by_kind"]["error"] == 1


# ---------------------------------------------------------------------------
# Phase 1: envelope, error envelope, --version
# ---------------------------------------------------------------------------

def _stderr_envelope(result):
    for line in result.stderr.splitlines():
        line = line.strip()
        if line.startswith('{"error"'):
            return json.loads(line)["error"]
    raise AssertionError(f"no error envelope in stderr: {result.stderr!r}")


def test_cli_validate_json_has_schema_version_no_findings_summary():
    r = run_cli("validate", str(EXAMPLES / "good.jsonl"),
                "--format", "json")
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["schema_version"] == 1
    assert isinstance(out["schema_version"], int)
    assert "findings_summary" not in out


def test_cli_summary_json_has_schema_version():
    r = run_cli("summary", str(EXAMPLES / "good.jsonl"),
                "--format", "json")
    assert r.returncode == 0
    out = json.loads(r.stdout)
    assert out["schema_version"] == 1
    assert isinstance(out["schema_version"], int)
    assert "findings_summary" not in out


def test_cli_version_flag():
    r = run_cli("--version")
    assert r.returncode == 0
    assert r.stdout.strip() == "tracetape 1.0.0"


def test_cli_summary_missing_file_error_envelope():
    r = run_cli("summary", "/nonexistent/x.jsonl")
    assert r.returncode == 2
    assert "error: no such file: /nonexistent/x.jsonl" in r.stderr
    err = _stderr_envelope(r)
    assert err["code"] == "file_not_found"
    assert err["message"] == "no such file: /nonexistent/x.jsonl"


def test_cli_summary_directory_error_envelope(tmp_path):
    r = run_cli("summary", str(tmp_path))
    assert r.returncode == 2
    err = _stderr_envelope(r)
    assert err["code"] == "not_a_file"


def test_cli_validate_missing_target_error_envelope():
    r = run_cli("validate", "/nonexistent/x.jsonl")
    assert r.returncode == 2
    err = _stderr_envelope(r)
    assert err["code"] == "file_not_found"
    assert err["message"] == "no such file or directory: /nonexistent/x.jsonl"
