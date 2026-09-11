"""Judge — evaluates code submissions against test cases.

Uses the Sandbox (Piston) to execute code for each test case and
compares stdout to expected output. Produces a structured verdict.
"""

import asyncio
import time

# Test cases for one submission run concurrently, but bounded so a race
# (two players, each retrying) can't flood Piston's job queue.
MAX_CONCURRENT_TESTS = 4

# A crash string containing one of these looks like an import/syntax error
# rather than ordinary wrong output — see _detect_import_crash.
_IMPORT_CRASH_MARKERS = ("ImportError", "ModuleNotFoundError", "SyntaxError", "IndentationError")


def _detect_import_crash(results):
    """If every test case failed with the exact same crash message and it
    looks like an import/syntax error, surface that as a distinct signal.

    With a harness importing the player's module, a writable file that
    fails to import fails every test identically — without this, players
    stare at N identical wrong-answer rows with no clue why.
    """
    if not results or any(r["passed"] for r in results):
        return None
    errors = {r["error"] for r in results}
    if len(errors) != 1:
        return None
    only_error = next(iter(errors))
    if only_error and any(marker in only_error for marker in _IMPORT_CRASH_MARKERS):
        return only_error
    return None


class Judge:
    """Evaluates code submissions against problem test cases.

    Usage:
        judge = Judge(sandbox)
        bundle = {"entrypoint": "solution.py", "files": [{"path": "solution.py", "content": "..."}]}
        verdict = await judge.evaluate(bundle, "python", test_cases)
        # verdict = { passed, pass_count, total, results: [...], import_error }
    """

    def __init__(self, sandbox):
        """Initialize with a Sandbox instance.

        Args:
            sandbox: A Sandbox instance for code execution.
        """
        self.sandbox = sandbox

    async def evaluate(self, bundle, language, test_cases):
        """Run a file bundle against all test cases and produce a verdict.

        Args:
            bundle: {"entrypoint": str, "files": [{"path", "content"}, ...]}
                — the fully-assembled bundle (locked + writable + hidden
                harness), identical for every test case; only stdin varies.
            language: Language identifier ("python" or "javascript").
            test_cases: List of dicts, each with "input" and "expectedOutput".

        Returns:
            dict with keys:
                passed (bool): True if ALL test cases produced correct output.
                pass_count (int): Number of test cases that passed.
                total (int): Total number of test cases.
                results (list): Per-test detail, each with:
                    input (str), expected (str), actual (str),
                    passed (bool), error (str), timed_out (bool),
                    wall_time_ms (int)
                import_error (str | None): Set when every case crashed with
                    the same import/syntax error, for a distinct UI message.
        """
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_TESTS)

        async def run_one(index, tc):
            """Execute and grade a single test case."""
            tc_input = tc.get("input", "")
            expected = tc.get("expectedOutput", "")

            start = time.monotonic()
            try:
                async with semaphore:
                    exec_result = await self.sandbox.execute(
                        language=language,
                        files=bundle["files"],
                        entrypoint=bundle["entrypoint"],
                        stdin=tc_input,
                    )
            except asyncio.CancelledError:
                # The room cancelled us (race resolved, timed out, or the
                # player disconnected). Propagate so the task really stops.
                raise
            except Exception as e:
                # Sandbox communication failure — treat as a crash
                import traceback
                print(f"  [Judge] Sandbox error on test {index + 1}: {e}")
                traceback.print_exc()
                elapsed_ms = int((time.monotonic() - start) * 1000)
                return {
                    "input": tc_input,
                    "expected": expected,
                    "actual": "",
                    "passed": False,
                    "error": f"Sandbox error: {e}",
                    "timed_out": False,
                    "wall_time_ms": elapsed_ms,
                }

            elapsed_ms = int((time.monotonic() - start) * 1000)
            actual_output = exec_result["stdout"]

            # Compare stripped output
            tc_passed = (
                actual_output.strip() == expected.strip()
                and exec_result["exit_code"] == 0
                and not exec_result["timed_out"]
            )

            print(f"  [Judge] Test {index + 1}: exit={exec_result['exit_code']}, timed_out={exec_result['timed_out']}, passed={tc_passed}")
            if not tc_passed:
                print(f"  [Judge]   expected: {repr(expected.strip())}")
                print(f"  [Judge]   actual:   {repr(actual_output.strip())}")
                if exec_result["stderr"]:
                    print(f"  [Judge]   stderr:   {exec_result['stderr'][:200]}")

            # Build error string from stderr or timeout
            error = ""
            if exec_result["timed_out"]:
                error = "Time limit exceeded"
            elif exec_result["exit_code"] != 0:
                error = exec_result["stderr"].strip() or f"Exit code {exec_result['exit_code']}"

            return {
                "input": tc_input,
                "expected": expected,
                "actual": actual_output.strip(),
                "passed": tc_passed,
                "error": error,
                "timed_out": exec_result["timed_out"],
                "wall_time_ms": elapsed_ms,
            }

        # asyncio.gather preserves argument order, so `results` stays aligned
        # with `test_cases` even though the executions interleave.
        results = list(await asyncio.gather(
            *(run_one(i, tc) for i, tc in enumerate(test_cases))
        ))
        pass_count = sum(1 for r in results if r["passed"])

        total = len(test_cases)
        return {
            "passed": pass_count == total and total > 0,
            "pass_count": pass_count,
            "total": total,
            "results": results,
            "import_error": _detect_import_crash(results),
        }
