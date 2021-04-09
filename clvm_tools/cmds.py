import argparse
import importlib
import io
import json
import pathlib
import sys
import time

from clvm import to_sexp_f, KEYWORD_FROM_ATOM, KEYWORD_TO_ATOM, SExp
from clvm.EvalError import EvalError
from clvm.serialize import sexp_from_stream, sexp_to_stream
from clvm.operators import OP_REWRITE

from ir import reader

from . import binutils
from .debug import make_trace_pre_eval, trace_to_text, trace_to_table
from .sha256tree import sha256tree

try:
    from clvm_rs import deserialize_and_run_program, STRICT_MODE
except ImportError:
    deserialize_and_run_program = None


def path_or_code(arg):
    try:
        with open(arg) as f:
            return f.read()
    except IOError:
        return arg


def stream_to_bin(write_f):
    b = io.BytesIO()
    write_f(b)
    return b.getvalue()


def call_tool(tool_name, desc, conversion, input_args):
    parser = argparse.ArgumentParser(description=desc)
    parser.add_argument(
        "-H", "--script-hash", action="store_true", help="Show only sha256 tree hash of program"
    )
    parser.add_argument(
        "path_or_code",
        nargs="*",
        type=path_or_code,
        help="path to clvm script, or literal script",
    )

    sys.setrecursionlimit(20000)
    args = parser.parse_args(args=input_args[1:])

    for program in args.path_or_code:
        if program == "-":
            program = sys.stdin.read()
        sexp, text = conversion(program)
        if args.script_hash:
            print(sha256tree(sexp).hex())
        elif text:
            print(text)


def opc(args=sys.argv):
    def conversion(text):
        try:
            ir_sexp = reader.read_ir(text)
            sexp = binutils.assemble_from_ir(ir_sexp)
        except SyntaxError as ex:
            print("%s" % ex.msg)
            return None, None
        return sexp, sexp.as_bin().hex()

    call_tool("opc", "Compile a clvm script.", conversion, args)


def opd(args=sys.argv):
    def conversion(blob):
        sexp = sexp_from_stream(io.BytesIO(bytes.fromhex(blob)), to_sexp_f)
        return sexp, binutils.disassemble(sexp)
    call_tool("opd", "Disassemble a compiled clvm script from hex.", conversion, args)


def stage_import(stage):
    stage_path = "stages.stage_%s" % stage
    try:
        return importlib.import_module(stage_path)
    except ImportError:
        raise ValueError("bad stage: %s" % stage)


def as_bin(streamer_f):
    f = io.BytesIO()
    streamer_f(f)
    return f.getvalue()


def run(args=sys.argv):
    return launch_tool(args, "run", default_stage=2)


def brun(args=sys.argv):
    return launch_tool(args, "brun")


def calculate_cost_offset(run_program, run_script: SExp):
    """
    These commands are used by the test suite, and many of them expect certain costs.
    If boilerplate invocation code changes by a fixed cost, you can tweak this
    value so you don't have to change all the tests' expected costs.

    Eventually you should re-tare this to zero and alter the tests' costs though.

    This is a hack and need to go away, probably when we do dialects for real,
    and then the dialect can have a `run_program` API.
    """
    null = binutils.assemble("0")
    cost, _r = run_program(run_script, null.cons(null))
    return 53 - cost


def launch_tool(args, tool_name, default_stage=0):
    sys.setrecursionlimit(20000)
    parser = argparse.ArgumentParser(
        description='Execute a clvm script.'
    )
    parser.add_argument(
        "--strict", action="store_true",
        help="Unknown opcodes are always fatal errors in strict mode")
    parser.add_argument(
        "-x", "--hex", action="store_true",
        help="Read program and environment as hexadecimal bytecode")
    parser.add_argument(
        "-s", "--stage", type=stage_import,
        help="stage number to include", default=stage_import(default_stage))
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Display resolve of all reductions, for debugging")
    parser.add_argument(
        "-t", "--table", action="store_true",
        help="Print diagnostic table of reductions, for debugging")
    parser.add_argument(
        "-c", "--cost", action="store_true", help="Show cost")
    parser.add_argument(
        "--time", action="store_true", help="Print execution time")
    parser.add_argument(
        "-m", "--max-cost", type=int, default=10860254871, help="Maximum cost")
    parser.add_argument(
        "-d", "--dump", action="store_true",
        help="dump hex version of final output")
    parser.add_argument(
        "--quiet", action="store_true", help="Suppress printing the program result")
    parser.add_argument(
        "-y", "--symbol-table", type=pathlib.Path,
        help=".SYM file generated by compiler")
    parser.add_argument(
        "-n", "--no-keywords", action="store_true",
        help="Output result as data, not as a program")
    parser.add_argument("--backend", type=str, help="force use of 'rust' or 'python' backend")
    parser.add_argument(
        "-i",
        "--include",
        type=pathlib.Path,
        help="add a search path for included files",
        action="append",
        default=[],
    )
    parser.add_argument(
        "path_or_code", type=path_or_code,
        help="filepath to clvm script, or a literal script")

    parser.add_argument(
        "env", nargs="?", type=path_or_code,
        help="clvm script environment, as clvm src, or hex")

    args = parser.parse_args(args=args[1:])

    keywords = {} if args.no_keywords else KEYWORD_FROM_ATOM

    if hasattr(args.stage, "run_program_for_search_paths"):
        run_program = args.stage.run_program_for_search_paths(args.include)
    else:
        run_program = args.stage.run_program

    input_serialized = None
    input_sexp = None

    time_start = time.perf_counter()
    if args.hex:
        assembled_serialized = bytes.fromhex(args.path_or_code)
        if not args.env:
            args.env = "80"
        env_serialized = bytes.fromhex(args.env)
        time_read_hex = time.perf_counter()

        input_serialized = b"\xff" + assembled_serialized + env_serialized
    else:

        src_text = args.path_or_code
        try:
            src_sexp = reader.read_ir(src_text)
        except SyntaxError as ex:
            print("FAIL: %s" % (ex))
            return -1
        assembled_sexp = binutils.assemble_from_ir(src_sexp)
        if not args.env:
            args.env = "()"
        env_ir = reader.read_ir(args.env)
        env = binutils.assemble_from_ir(env_ir)
        time_assemble = time.perf_counter()

        input_sexp = to_sexp_f((assembled_sexp, env))

    pre_eval_f = None
    symbol_table = None

    log_entries = []

    if args.symbol_table:
        with open(args.symbol_table) as f:
            symbol_table = json.load(f)
        pre_eval_f = make_trace_pre_eval(log_entries, symbol_table)
    elif args.verbose or args.table:
        pre_eval_f = make_trace_pre_eval(log_entries)

    run_script = getattr(args.stage, tool_name)

    cost = 0
    cost_offset = calculate_cost_offset(run_program, run_script)
    try:
        output = "(didn't finish)"

        use_rust = (
            (tool_name != "run")
            and not pre_eval_f
            and (
                args.backend == "rust"
                or (deserialize_and_run_program and args.backend != "python")
            )
        )
        max_cost = max(0, args.max_cost - cost_offset if args.max_cost != 0 else 0)
        if use_rust:
            if input_serialized is None:
                input_serialized = input_sexp.as_bin()

            run_script = run_script.as_bin()
            time_parse_input = time.perf_counter()

            # build the opcode look-up table
            # this should eventually be subsumed by "Dialect" api

            native_opcode_names_by_opcode = dict(
                ("op_%s" % OP_REWRITE.get(k, k), op)
                for op, k in KEYWORD_FROM_ATOM.items()
                if k not in "qa."
            )
            cost, result = deserialize_and_run_program(
                run_script,
                input_serialized,
                KEYWORD_TO_ATOM["q"][0],
                KEYWORD_TO_ATOM["a"][0],
                native_opcode_names_by_opcode,
                max_cost,
                STRICT_MODE if args.strict else 0,
            )
            time_done = time.perf_counter()
            result = sexp_from_stream(io.BytesIO(result), to_sexp_f)
        else:
            if input_sexp is None:
                input_sexp = sexp_from_stream(io.BytesIO(input_serialized), to_sexp_f)

            time_parse_input = time.perf_counter()
            cost, result = run_program(
                run_script, input_sexp, max_cost=max_cost, pre_eval_f=pre_eval_f, strict=args.strict)
            time_done = time.perf_counter()
        if args.cost:
            cost += cost_offset if cost > 0 else 0
            print("cost = %d" % cost)
        if args.time:
            if args.hex:
                print('read_hex: %f' % (time_read_hex - time_start))
            else:
                print('assemble_from_ir: %f' % (time_assemble - time_start))
                print('to_sexp_f: %f' % (time_parse_input - time_assemble))
            print('run_program: %f' % (time_done - time_parse_input))
        if args.dump:
            blob = as_bin(lambda f: sexp_to_stream(result, f))
            output = blob.hex()
        elif args.quiet:
            output = ''
        else:
            output = binutils.disassemble(result, keywords)
    except EvalError as ex:
        result = to_sexp_f(ex._sexp)
        output = "FAIL: %s %s" % (ex, binutils.disassemble(result, keywords))
        return -1
    except Exception as ex:
        output = str(ex)
        raise
    finally:
        print(output)
        if args.verbose or symbol_table:
            print()
            trace_to_text(log_entries, binutils.disassemble, symbol_table)
        if args.table:
            trace_to_table(log_entries, binutils.disassemble, symbol_table)


def read_ir(args=sys.argv):
    parser = argparse.ArgumentParser(
        description='Read script and tokenize to IR.'
    )
    parser.add_argument(
        "script", help="script in hex or uncompiled text")

    args = parser.parse_args(args=args[1:])

    sexp = reader.read_ir(args.script)
    blob = stream_to_bin(lambda f: sexp_to_stream(sexp, f))
    print(blob.hex())


"""
Copyright 2018 Chia Network Inc

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

   http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""
