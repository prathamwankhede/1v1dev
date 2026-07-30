"""Judge — evaluates code submissions against test cases.

Uses the Sandbox (Piston) to execute code for each test case and
compares stdout to expected output. Produces a structured verdict.
"""

import time


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
        results = []
        pass_count = 0

        for tc in test_cases:
            tc_input = tc.get("input", "")
            expected = tc.get("expectedOutput", "")

            start = time.monotonic()
            try:
                exec_result = await self.sandbox.execute(
                    language=language,
                    code=code,
                    stdin=tc_input,
                )
            except Exception as e:
                # Sandbox communication failure — treat as a crash
                import traceback
                print(f"  [Judge] Sandbox error on test {len(results)+1}: {e}")
                traceback.print_exc()
                elapsed_ms = int((time.monotonic() - start) * 1000)
                results.append({
                    "input": tc_input,
                    "expected": expected,
                    "actual": "",
                    "passed": False,
                    "error": f"Sandbox error: {e}",
                    "timed_out": False,
                    "wall_time_ms": elapsed_ms,
                })
                continue

            elapsed_ms = int((time.monotonic() - start) * 1000)
            actual_output = exec_result["stdout"]

            # Compare stripped output
            tc_passed = (
                actual_output.strip() == expected.strip()
                and exec_result["exit_code"] == 0
                and not exec_result["timed_out"]
            )

            print(f"  [Judge] Test {len(results)+1}: exit={exec_result['exit_code']}, timed_out={exec_result['timed_out']}")
            print(f"  [Judge]   expected: {repr(expected.strip())}")
            print(f"  [Judge]   actual:   {repr(actual_output.strip())}")
            print(f"  [Judge]   passed:   {tc_passed}")
            if exec_result["stderr"]:
                print(f"  [Judge]   stderr:   {exec_result['stderr'][:200]}")

            if tc_passed:
                pass_count += 1

            # Build error string from stderr or timeout
            error = ""
            if exec_result["timed_out"]:
                error = "Time limit exceeded"
            elif exec_result["exit_code"] != 0:
                error = exec_result["stderr"].strip() or f"Exit code {exec_result['exit_code']}"

            results.append({
                "input": tc_input,
                "expected": expected,
                "actual": actual_output.strip(),
                "passed": tc_passed,
                "error": error,
                "timed_out": exec_result["timed_out"],
                "wall_time_ms": elapsed_ms,
            })

        total = len(test_cases)
        return {
            "passed": pass_count == total and total > 0,
            "pass_count": pass_count,
            "total": total,
            "results": results,
        }
