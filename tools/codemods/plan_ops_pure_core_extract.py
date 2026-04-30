#!/usr/bin/env python3
"""Extract pure ``_run_*`` cores from ``plan_ops.py`` command handlers."""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import pathlib
import sys
from dataclasses import dataclass
from typing import Any


VALUE_KINDS = {"path", "string", "int", "bool", "json-string", "csv-string"}
PATH_OVERRIDES = {
    "update_schedule_state",
    "from_schedule_state",
    "repo_root",
    "plans_dir",
    "task_file",
    "analyst_annotations",
    "dispatch_context",
    "report_file",
    "baseline_file",
    "out",
    "baseline",
}
STRING_OVERRIDES = {
    "output",
    "task_id",
    "target_task_id",
    "run_id",
    "parent_run_id",
}
JSON_STRING_OVERRIDES = {
    "fields_json",
    "rows_json",
    "reviewer_minor_findings",
}
CSV_STRING_OVERRIDES = {
    "dismissed_finding_ids",
    "filter_ids",
}
SKIP_REASONS = {
    "cmd_gates": "mutex-group",
    "cmd_filter_schedule": "mixed-pattern",
    "cmd_commit_task": "mutex-group",
    "cmd_fail_task": "direct-stdout-exit",
    "cmd_audit": "mixed-pattern",
    "cmd_resolve_read_targets": "direct-stdout-exit",
    "cmd_build_claude_dispatch_input": "private-terminator",
    "cmd_build_codex_dispatch_input": "private-terminator",
    "cmd_build_gemini_dispatch_input": "private-terminator",
}
PRIVATE_TERMINATORS = {
    "_bcdi_emit_error",
    "_bcdi_emit_envelope",
    "_bcdi_resolve_policy_or_die",
}


@dataclass(frozen=True)
class ArgSpec:
    flag: str
    dest: str
    type: str | None
    default: str | None
    required: bool
    value_kind: str
    source: str

    def as_report(self) -> dict[str, Any]:
        return {
            "flag": self.flag,
            "dest": self.dest,
            "required": self.required,
            "value_kind": self.value_kind,
            "source": self.source,
        }


def _literal(node: ast.AST | None) -> Any:
    if node is None:
        return None
    try:
        return ast.literal_eval(node)
    except Exception:
        return None


def _keyword(call: ast.Call, name: str) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _nameish(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _nameish(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Constant):
        return repr(node.value)
    return ast.unparse(node)


def _is_method_call(node: ast.AST, method: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == method
    )


def _parser_var(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
        return node.func.value.id
    return None


def _derive_dest(flag: str) -> str:
    return flag.lstrip("-").replace("-", "_")


def _classify_value_kind(dest: str, type_name: str | None, action: str | None) -> tuple[str, str]:
    if dest in PATH_OVERRIDES:
        return "path", "override"
    if dest in JSON_STRING_OVERRIDES:
        return "json-string", "override"
    if dest in CSV_STRING_OVERRIDES:
        return "csv-string", "override"
    if dest in STRING_OVERRIDES:
        return "string", "override"
    if action in {"store_true", "store_false"}:
        return "bool", "argparse-type"
    if type_name in {"int", "builtins.int"}:
        return "int", "argparse-type"
    if type_name in {"Path", "pathlib.Path"}:
        return "path", "argparse-type"
    if dest.endswith(("_file", "_path", "_dir")):
        return "path", "suffix-heuristic"
    return "string", "argparse-type" if type_name in {"str", None} else "suffix-heuristic"


def extract_arg_specs(tree: ast.Module) -> tuple[dict[str, list[ArgSpec]], dict[str, str], list[str]]:
    """Return ``subcommand -> ArgSpec[]`` and ``cmd_X -> subcommand`` mappings."""
    build_parser = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "build_parser"),
        None,
    )
    if build_parser is None:
        return {}, {}, ["build_parser() not found"]

    parser_vars: dict[str, str] = {}
    command_for_var: dict[str, str] = {}
    func_for_var: dict[str, str] = {}
    specs_by_var: dict[str, list[ArgSpec]] = {}
    warnings: list[str] = []

    for node in ast.walk(build_parser):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if not _is_method_call(call, "add_parser"):
            continue
        if not node.targets or not isinstance(node.targets[0], ast.Name):
            continue
        command = _literal(call.args[0]) if call.args else None
        if not isinstance(command, str):
            continue
        var = node.targets[0].id
        parser_vars[var] = command
        command_for_var[var] = command
        specs_by_var.setdefault(var, [])

    for node in ast.walk(build_parser):
        if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        var = _parser_var(call)
        if var not in parser_vars:
            continue
        if _is_method_call(call, "add_argument"):
            if not call.args:
                continue
            flag_values = [_literal(arg) for arg in call.args]
            flags = [f for f in flag_values if isinstance(f, str)]
            if not flags:
                continue
            option_flags = [f for f in flags if f.startswith("-")]
            flag = option_flags[0] if option_flags else flags[0]
            dest_literal = _literal(_keyword(call, "dest"))
            dest = dest_literal if isinstance(dest_literal, str) else _derive_dest(flag)
            type_name = _nameish(_keyword(call, "type"))
            action_literal = _literal(_keyword(call, "action"))
            action = action_literal if isinstance(action_literal, str) else None
            required = bool(_literal(_keyword(call, "required")))
            default_node = _keyword(call, "default")
            default = ast.unparse(default_node) if default_node is not None else None
            value_kind, source = _classify_value_kind(dest, type_name, action)
            if value_kind not in VALUE_KINDS:
                warnings.append(f"{command_for_var[var]}:{flag} resolved invalid kind {value_kind!r}")
                value_kind = "string"
            specs_by_var[var].append(
                ArgSpec(
                    flag=flag,
                    dest=dest,
                    type=type_name,
                    default=default,
                    required=required,
                    value_kind=value_kind,
                    source=source,
                )
            )
        elif _is_method_call(call, "set_defaults"):
            func_node = _keyword(call, "func")
            if isinstance(func_node, ast.Name):
                func_for_var[var] = func_node.id

    specs: dict[str, list[ArgSpec]] = {}
    cmd_to_command: dict[str, str] = {}
    for var, command in command_for_var.items():
        specs[command] = specs_by_var.get(var, [])
        func = func_for_var.get(var)
        if func:
            cmd_to_command[func] = command

    # Most parsers in plan_ops use set_defaults in a compact block after all
    # add_argument calls, but fall back to the command naming convention.
    for var, command in command_for_var.items():
        cmd_to_command.setdefault(f"cmd_{command.replace('-', '_')}", command)

    return specs, cmd_to_command, warnings


class CmdBodyTransformer(ast.NodeTransformer):
    def __init__(self) -> None:
        self.emit_die_count = 0
        self.stdin_read = False
        self.fstring_args_uses = 0

    def visit_JoinedStr(self, node: ast.JoinedStr) -> ast.AST:
        for child in ast.walk(node):
            if (
                isinstance(child, ast.Attribute)
                and isinstance(child.value, ast.Name)
                and child.value.id == "args"
            ):
                self.fstring_args_uses += 1
        return self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> ast.AST:
        if self._is_stdin_read(node):
            self.stdin_read = True
            return ast.copy_location(
                ast.Subscript(
                    value=ast.Name(id="payload", ctx=ast.Load()),
                    slice=ast.Constant(value="stdin_text"),
                    ctx=ast.Load(),
                ),
                node,
            )
        return self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        node = self.generic_visit(node)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "args":
            return ast.copy_location(
                ast.Subscript(
                    value=ast.Name(id="payload", ctx=ast.Load()),
                    slice=ast.Constant(value=node.attr),
                    ctx=ast.Load(),
                ),
                node,
            )
        return node

    def visit_Expr(self, node: ast.Expr) -> ast.AST:
        if isinstance(node.value, ast.Call) and self._terminator_name(node.value) in {"_emit", "_die"}:
            return self._return_result(node.value)
        return self.generic_visit(node)

    def _return_result(self, call: ast.Call) -> ast.Return:
        name = self._terminator_name(call)
        self.emit_die_count += 1
        expr = call.args[1] if len(call.args) >= 2 else ast.Constant(value={})
        exit_code: ast.AST = ast.Constant(value=0 if name == "_emit" else 1)
        for kw in call.keywords:
            if kw.arg == "exit_code":
                exit_code = kw.value
                break
        result_call = ast.Call(
            func=ast.Name(id="_result", ctx=ast.Load()),
            args=[self.visit(expr)],
            keywords=[ast.keyword(arg="exit_code", value=self.visit(exit_code))],
        )
        return ast.copy_location(ast.Return(value=result_call), call)

    @staticmethod
    def _terminator_name(call: ast.Call) -> str | None:
        if isinstance(call.func, ast.Name):
            return call.func.id
        return None

    @staticmethod
    def _is_stdin_read(call: ast.Call) -> bool:
        return (
            isinstance(call.func, ast.Attribute)
            and call.func.attr == "read"
            and isinstance(call.func.value, ast.Attribute)
            and call.func.value.attr == "stdin"
            and isinstance(call.func.value.value, ast.Name)
            and call.func.value.value.id == "sys"
        )


def _has_args_access(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Attribute)
        and isinstance(child.value, ast.Name)
        and child.value.id == "args"
        for child in ast.walk(node)
    )


def _is_already_shim(func: ast.FunctionDef) -> bool:
    body = func.body[:]
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
        if isinstance(body[0].value.value, str):
            body = body[1:]
    if len(body) != 3:
        return False
    first, second, third = body
    return (
        isinstance(first, ast.Assign)
        and len(first.targets) == 1
        and isinstance(first.targets[0], ast.Name)
        and first.targets[0].id == "payload"
        and isinstance(first.value, ast.Call)
        and isinstance(first.value.func, ast.Name)
        and first.value.func.id.startswith("_args_to_payload_")
        and isinstance(second, ast.Assign)
        and len(second.targets) == 1
        and isinstance(second.targets[0], ast.Name)
        and second.targets[0].id == "result"
        and isinstance(second.value, ast.Call)
        and isinstance(second.value.func, ast.Name)
        and second.value.func.id.startswith("_run_")
        and isinstance(third, ast.Expr)
        and isinstance(third.value, ast.Call)
        and isinstance(third.value.func, ast.Name)
        and third.value.func.id == "_emit_or_die"
    )


def _detect_skip(func: ast.FunctionDef) -> tuple[str, str] | None:
    if func.name in SKIP_REASONS:
        return SKIP_REASONS[func.name], f"{func.name}:{func.lineno}"
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                if (
                    isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "sys"
                    and node.func.attr in {"exit", "stdout.write"}
                ):
                    return "direct-stdout-exit", f"{func.name}:{getattr(node, 'lineno', func.lineno)}"
                if (
                    isinstance(node.func.value, ast.Attribute)
                    and isinstance(node.func.value.value, ast.Name)
                    and node.func.value.value.id == "sys"
                    and node.func.value.attr == "stdout"
                    and node.func.attr == "write"
                ):
                    return "direct-stdout-exit", f"{func.name}:{getattr(node, 'lineno', func.lineno)}"
            if isinstance(node.func, ast.Name) and node.func.id in PRIVATE_TERMINATORS:
                return "private-terminator", f"{func.name}:{getattr(node, 'lineno', func.lineno)}"
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "os"
            and node.value.attr == "environ"
        ):
            return "env-var-read", f"{func.name}:{getattr(node, 'lineno', func.lineno)}"
    return None


def _cmd_suffix(cmd_name: str) -> str:
    return cmd_name.removeprefix("cmd_")


def _payload_helper(cmd_name: str, specs: list[ArgSpec]) -> str:
    suffix = _cmd_suffix(cmd_name)
    lines = [f"def _args_to_payload_{suffix}(args: argparse.Namespace) -> dict:", "    payload = {"]
    for spec in specs:
        if spec.value_kind == "path":
            value = f'pathlib.Path(args.{spec.dest}) if args.{spec.dest} else None'
        else:
            value = f"args.{spec.dest}"
        lines.append(f'        "{spec.dest}": {value},')
    lines.append("    }")
    if any(spec.dest == "stdin" for spec in specs):
        lines.append('    payload["stdin_text"] = _read_stdin_text()')
    lines.append("    return payload")
    return "\n".join(lines)


def _unparse_body_as_function(name: str, body: list[ast.stmt]) -> str:
    if not body:
        body = [ast.Pass()]
    fn = ast.FunctionDef(
        name=name,
        args=ast.arguments(
            posonlyargs=[],
            args=[ast.arg(arg="payload", annotation=ast.Name(id="dict", ctx=ast.Load()))],
            kwonlyargs=[],
            kw_defaults=[],
            defaults=[],
        ),
        body=body,
        decorator_list=[],
        returns=ast.Name(id="dict", ctx=ast.Load()),
        type_comment=None,
    )
    mod = ast.Module(body=[fn], type_ignores=[])
    ast.fix_missing_locations(mod)
    return ast.unparse(mod)


def _shim(cmd_name: str, docstring: str | None) -> str:
    suffix = _cmd_suffix(cmd_name)
    lines = [f"def {cmd_name}(args: argparse.Namespace) -> None:"]
    if docstring is not None:
        lines.append(f"    {docstring!r}")
    lines.extend(
        [
            f"    payload = _args_to_payload_{suffix}(args)",
            f"    result = _run_{suffix}(payload)",
            "    _emit_or_die(args, result)",
        ]
    )
    return "\n".join(lines)


def _module_helpers(source: str) -> str:
    helpers: list[str] = []
    if "def _result(" not in source:
        helpers.append(
            "def _result(payload: dict, exit_code: int = 0) -> dict:\n"
            "    if isinstance(payload, dict):\n"
            "        result = dict(payload)\n"
            "    else:\n"
            '        result = {"result": payload}\n'
            '    result.setdefault("exit_code", exit_code)\n'
            "    return result\n"
        )
    if "def _emit_or_die(" not in source:
        helpers.append(
            "def _emit_or_die(args: argparse.Namespace, result: dict) -> None:\n"
            '    exit_code = int(result.get("exit_code", 0))\n'
            "    if exit_code:\n"
            "        _die(args, result, exit_code=exit_code)\n"
            "    _emit(args, result, exit_code=exit_code)\n"
        )
    if "def _read_stdin_text(" not in source:
        helpers.append("def _read_stdin_text() -> str:\n    return sys.stdin.read()\n")
    return "\n\n".join(helpers)


def _line_offsets(source: str) -> list[int]:
    offsets = [0]
    total = 0
    for line in source.splitlines(keepends=True):
        total += len(line)
        offsets.append(total)
    return offsets


def _replace_segment(source: str, node: ast.AST, replacement: str, offsets: list[int]) -> str:
    start = offsets[node.lineno - 1] + node.col_offset
    end = offsets[node.end_lineno - 1] + node.end_col_offset  # type: ignore[arg-type]
    return source[:start] + replacement + source[end:]


def transform_source(source: str) -> tuple[str, dict[str, Any]]:
    tree = ast.parse(source)
    arg_specs, cmd_to_command, warnings = extract_arg_specs(tree)
    cmd_funcs = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name.startswith("cmd_")
        and len(node.args.args) == 1
        and node.args.args[0].arg == "args"
    ]
    report_rewritten: list[dict[str, Any]] = []
    report_skipped: list[dict[str, Any]] = []
    replacements: list[tuple[ast.FunctionDef, str]] = []

    for func in cmd_funcs:
        command = cmd_to_command.get(func.name, _cmd_suffix(func.name).replace("_", "-"))
        specs = arg_specs.get(command, [])
        if _is_already_shim(func):
            report_rewritten.append(
                {
                    "function": func.name,
                    "lines": [func.lineno, func.end_lineno],
                    "emit_die_count": 0,
                    "stdin_read": False,
                    "fstring_args_uses": 0,
                    "argparse_flags": [s.flag for s in specs],
                    "already_shimmed": True,
                }
            )
            continue
        skip = _detect_skip(func)
        if skip:
            reason, evidence = skip
            report_skipped.append({"function": func.name, "reason": reason, "evidence": evidence})
            continue

        docstring = ast.get_docstring(func, clean=False)
        body = func.body[:]
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
            if isinstance(body[0].value.value, str):
                body = body[1:]
        transformer = CmdBodyTransformer()
        new_body = [transformer.visit(stmt) for stmt in body]
        ast.fix_missing_locations(ast.Module(body=new_body, type_ignores=[]))
        if any(_has_args_access(stmt) for stmt in new_body):
            warnings.append(f"{func.name}: args access remains after AST rewrite")

        suffix = _cmd_suffix(func.name)
        replacement = "\n\n".join(
            [
                _payload_helper(func.name, specs),
                _unparse_body_as_function(f"_run_{suffix}", new_body),
                _shim(func.name, docstring),
            ]
        )
        replacements.append((func, replacement))
        report_rewritten.append(
            {
                "function": func.name,
                "lines": [func.lineno, func.end_lineno],
                "emit_die_count": transformer.emit_die_count,
                "stdin_read": transformer.stdin_read,
                "fstring_args_uses": transformer.fstring_args_uses,
                "argparse_flags": [s.flag for s in specs],
            }
        )

    new_source = source
    offsets = _line_offsets(source)
    for func, replacement in sorted(replacements, key=lambda item: item[0].lineno, reverse=True):
        new_source = _replace_segment(new_source, func, replacement, offsets)

    if replacements and "import pathlib" not in new_source:
        if "import argparse\n" in new_source:
            new_source = new_source.replace("import argparse\n", "import argparse\nimport pathlib\n", 1)
        else:
            new_source = "import pathlib\n" + new_source

    helpers = _module_helpers(new_source)
    if helpers:
        first_cmd = min((func.lineno for func in cmd_funcs), default=None)
        if first_cmd is not None:
            insert_offsets = _line_offsets(new_source)
            insert_at = insert_offsets[first_cmd - 1]
            new_source = new_source[:insert_at] + helpers + "\n\n" + new_source[insert_at:]
        else:
            new_source += "\n\n" + helpers + "\n"

    report = {
        "schema_version": 1,
        "rewritten": sorted(report_rewritten, key=lambda r: r["function"]),
        "skipped": sorted(report_skipped, key=lambda r: r["function"]),
        "arg_specs": {
            command: [spec.as_report() for spec in specs]
            for command, specs in sorted(arg_specs.items())
        },
        "warnings": warnings,
        "summary": {
            "total_cmd_x": len(cmd_funcs),
            "rewritten_count": len(report_rewritten),
            "skipped_count": len(report_skipped),
            "ambiguous_arg_specs_count": len(warnings),
        },
    }
    return new_source, report


def _write_report(path: pathlib.Path, report: dict[str, Any]) -> None:
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in", dest="input", required=True, help="Input plan_ops.py path")
    parser.add_argument("--out", dest="output", required=True, help="Output plan_ops.py path")
    parser.add_argument("--dry-run", action="store_true", help="Print diff and classification; do not write --out")
    parser.add_argument("--report", help="Write machine-readable JSON report")
    args = parser.parse_args(argv)

    input_path = pathlib.Path(args.input)
    output_path = pathlib.Path(args.output)
    source = input_path.read_text(encoding="utf-8")
    rewritten, report = transform_source(source)

    for skipped in report["skipped"]:
        print(
            f"skip {skipped['function']}: {skipped['reason']} ({skipped['evidence']})",
            file=sys.stderr,
        )

    if args.report:
        _write_report(pathlib.Path(args.report), report)

    if args.dry_run:
        diff = difflib.unified_diff(
            source.splitlines(keepends=True),
            rewritten.splitlines(keepends=True),
            fromfile=str(input_path),
            tofile=str(output_path),
        )
        sys.stdout.writelines(diff)
        print(json.dumps({"rewritten": report["rewritten"], "skipped": report["skipped"]}, indent=2), file=sys.stderr)
        return 0

    if input_path.resolve() == output_path.resolve():
        output_path.write_text(rewritten, encoding="utf-8")
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rewritten, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
