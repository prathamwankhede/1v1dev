"""Judge — evaluates code submissions against test cases.

Uses the Sandbox (Piston) to execute code for each test case and
compares stdout to expected output. Produces a structured verdict.
"""

import asyncio
import time

# Test cases for one submission run concurrently, but bounded so a race
# (two players, each retrying) can't flood Piston's job queue.
MAX_CONCURRENT_TESTS = 4


class Judge:
    """Evaluates code submissions against problem test cases.

    Usage:
        judge = Judge(sandbox)
        verdict = await judge.evaluate(code, "python", test_cases)
        # verdict = { passed, pass_count, total, results: [...] }
    """

    def __init__(self, sandbox):
        """Initialize with a Sandbox instance.

        Args:
            sandbox: A Sandbox instance for code execution.
        """
        self.sandbox = sandbox

    async def evaluate(self, code, language, test_cases):
        """Run code against all test cases and produce a verdict.

        Args:
            code: Source code string.
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
                        code=code,
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
        }
