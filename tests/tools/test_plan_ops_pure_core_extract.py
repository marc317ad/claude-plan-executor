from __future__ import annotations

import ast
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parents[2]
CODEMOD_PATH = ROOT / "tools/codemods/plan_ops_pure_core_extract.py"
PLAN_OPS_PATH = ROOT / "plugins/plan-executor/scripts/plan_ops.py"


def load_codemod():
    spec = importlib.util.spec_from_file_location("plan_ops_pure_core_extract", CODEMOD_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_synthetic_rewrite_golden_file_shapes():
    codemod = load_codemod()
    source = '''import argparse
import json
import sys

def _emit(args, result, *, exit_code=0): pass
def _die(args, result, *, exit_code=1): pass
def _result(payload, *, exit_code=0): return payload
def _emit_or_die(args, result): pass

def cmd_demo(args: argparse.Namespace) -> None:
    """Demo docs."""
    if args.name:
        _emit(args, {"message": f"bad {args.name!r}"}, exit_code=2)
    raw = sys.stdin.read()
    parsed = json.loads(raw)
    if args.count > 2:
        _die(args, {"error": "%s" % args.name})
    _emit(args, {"ok": parsed, "path": args.plan_file})

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("demo")
    p.add_argument("--name")
    p.add_argument("--count", type=int, required=True)
    p.add_argument("--plan-file", required=True)
    p.add_argument("--stdin", action="store_true", required=True)
    p.set_defaults(func=cmd_demo)
    return parser
'''
    rewritten, report = codemod.transform_source(source)
    expected = '''import argparse
import pathlib
import json
import sys

def _emit(args, result, *, exit_code=0): pass
def _die(args, result, *, exit_code=1): pass
def _result(payload, *, exit_code=0): return payload
def _emit_or_die(args, result): pass
def _read_stdin_text() -> str:
    return sys.stdin.read()



def _args_to_payload_demo(args: argparse.Namespace) -> dict:
    payload = {
        "name": args.name,
        "count": args.count,
        "plan_file": pathlib.Path(args.plan_file) if args.plan_file else None,
        "stdin": args.stdin,
    }
    payload["stdin_text"] = _read_stdin_text()
    return payload

def _run_demo(payload: dict) -> dict:
    if payload['name']:
        return _result({'message': f"bad {payload['name']!r}"}, exit_code=2)
    raw = payload['stdin_text']
    parsed = json.loads(raw)
    if payload['count'] > 2:
        return _result({'error': '%s' % payload['name']}, exit_code=1)
    return _result({'ok': parsed, 'path': payload['plan_file']}, exit_code=0)

def cmd_demo(args: argparse.Namespace) -> None:
    'Demo docs.'
    payload = _args_to_payload_demo(args)
    result = _run_demo(payload)
    _emit_or_die(args, result)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("demo")
    p.add_argument("--name")
    p.add_argument("--count", type=int, required=True)
    p.add_argument("--plan-file", required=True)
    p.add_argument("--stdin", action="store_true", required=True)
    p.set_defaults(func=cmd_demo)
    return parser
'''
    assert rewritten == expected
    assert report["summary"]["total_cmd_x"] == 1
    assert report["rewritten"][0]["emit_die_count"] == 3
    assert report["rewritten"][0]["stdin_read"] is True
    assert report["rewritten"][0]["fstring_args_uses"] == 1
    assert report["arg_specs"]["demo"][2]["value_kind"] == "path"


def _cmd_function_names(source: str) -> set[str]:
    tree = ast.parse(source)
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("cmd_")
    }


def _run_function_nodes(source: str) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(source)
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_run_")
    }


def _emit_die_expr_count(func: ast.FunctionDef) -> int:
    count = 0
    for node in ast.walk(func):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            if isinstance(node.value.func, ast.Name) and node.value.func.id in {"_emit", "_die"}:
                count += 1
    return count


def test_real_file_dry_run_report_and_rewritten_output_compile(tmp_path):
    codemod = load_codemod()
    source = PLAN_OPS_PATH.read_text(encoding="utf-8")
    rewritten, report = codemod.transform_source(source)
    out = tmp_path / "plan_ops.codemod.py"
    out.write_text(rewritten, encoding="utf-8")

    ast.parse(rewritten)
    original_cmds = _cmd_function_names(source)
    reported = {item["function"] for item in report["rewritten"]} | {
        item["function"] for item in report["skipped"]
    }
    assert len(original_cmds) == 38
    assert reported == original_cmds

    original_tree = ast.parse(source)
    original_funcs = {
        node.name: node
        for node in original_tree.body
        if isinstance(node, ast.FunctionDef) and node.name in original_cmds
    }
    skipped = {item["function"] for item in report["skipped"]}
    run_funcs = _run_function_nodes(rewritten)
    for item in report["rewritten"]:
        if item.get("already_shimmed"):
            continue
        original_count = _emit_die_expr_count(original_funcs[item["function"]])
        assert item["emit_die_count"] == original_count
        run_name = "_run_" + item["function"].removeprefix("cmd_")
        assert run_name in run_funcs
        assert "_emit(" not in ast.unparse(run_funcs[run_name])
        assert "_die(" not in ast.unparse(run_funcs[run_name])
        assert not any(
            isinstance(child, ast.Attribute)
            and isinstance(child.value, ast.Name)
            and child.value.id == "args"
            for child in ast.walk(run_funcs[run_name])
        )

    assert skipped == {
        "cmd_audit",
        "cmd_build_claude_dispatch_input",
        "cmd_build_codex_dispatch_input",
        "cmd_build_gemini_dispatch_input",
        "cmd_commit_task",
        "cmd_fail_task",
        "cmd_filter_schedule",
        "cmd_gates",
        "cmd_resolve_read_targets",
    }

    subprocess.run([sys.executable, "-m", "py_compile", str(out)], check=True)


def _help_option_strings(script: pathlib.Path, subcommand: str | None = None) -> list[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PLAN_OPS_PATH.parent)
    cmd = [sys.executable, str(script)]
    if subcommand:
        cmd.append(subcommand)
    cmd.append("--help")
    result = subprocess.run(cmd, text=True, capture_output=True, env=env, check=True)
    return sorted(set(re.findall(r"(?<!\\w)--[a-zA-Z0-9][a-zA-Z0-9-]*", result.stdout)))


def _subcommands_from_help(script: pathlib.Path) -> set[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PLAN_OPS_PATH.parent)
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )
    body = re.search(r"\{([^}]+)\}", result.stdout)
    assert body is not None
    return set(body.group(1).split(","))


def test_real_file_help_surface_matches_after_rewrite(tmp_path):
    codemod = load_codemod()
    source = PLAN_OPS_PATH.read_text(encoding="utf-8")
    rewritten, report = codemod.transform_source(source)
    out = tmp_path / "plan_ops.codemod.py"
    out.write_text(rewritten, encoding="utf-8")

    original_subcommands = _subcommands_from_help(PLAN_OPS_PATH)
    rewritten_subcommands = _subcommands_from_help(out)
    assert len(original_subcommands) == 38
    assert rewritten_subcommands == original_subcommands

    for subcommand in sorted(original_subcommands):
        assert _help_option_strings(out, subcommand) == _help_option_strings(PLAN_OPS_PATH, subcommand)

    report_path = tmp_path / "report.json"
    result = subprocess.run(
        [
            sys.executable,
            str(CODEMOD_PATH),
            "--in",
            str(PLAN_OPS_PATH),
            "--out",
            str(out),
            "--dry-run",
            "--report",
            str(report_path),
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    assert "--- " in result.stdout
    written_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert written_report["summary"] == report["summary"]


def test_release_lock_early_results_do_not_fall_through_after_rewrite():
    codemod = load_codemod()
    source = '''import argparse

def _emit(args, result, *, exit_code=0): pass
def _die(args, result, *, exit_code=1): pass
def _result(payload, *, exit_code=0): return {**payload, "__exit": exit_code}
def _emit_or_die(args, result): pass
def _atomic_write_json(*args, **kwargs): raise AssertionError("fallthrough")

def cmd_release_lock(args: argparse.Namespace) -> None:
    if args.mode == "missing":
        _die(args, {"code": "no-lock-file"})
    if args.mode == "mismatch":
        _die(args, {"code": "run-id-mismatch"})
    _atomic_write_json(args.lock_file, {})
    _emit(args, {"released": True})

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("release-lock")
    p.add_argument("--mode")
    p.add_argument("--lock-file")
    p.set_defaults(func=cmd_release_lock)
    return parser
'''
    rewritten, _report = codemod.transform_source(source)
    namespace: dict[str, object] = {}
    exec(compile(rewritten, "<rewritten>", "exec"), namespace)
    run = namespace["_run_release_lock"]
    assert run({"mode": "missing", "lock_file": "x"})["code"] == "no-lock-file"
    assert run({"mode": "mismatch", "lock_file": "x"})["code"] == "run-id-mismatch"


def test_skipped_fstring_branches_remain_byte_equal_in_real_file():
    source = PLAN_OPS_PATH.read_text(encoding="utf-8")
    codemod = load_codemod()
    rewritten, _report = codemod.transform_source(source)
    snippets = [
        'f"bad --task-id: {args.task_id!r}"',
        'f"bad --task-id: {args.task_id!r}"',
    ]
    assert source.count(snippets[0]) >= 2
    assert rewritten.count(snippets[0]) >= 2
