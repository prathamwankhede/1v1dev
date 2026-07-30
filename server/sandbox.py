"""Sandbox — async Piston API client for sandboxed code execution.

Wraps the self-hosted Piston REST API (http://localhost:2000) to execute
untrusted code with strict resource limits (CPU, memory, wall-clock timeout).
Piston handles all sandboxing internally via nsjail + cgroups.
"""

import aiohttp

# Map user-facing language names to Piston runtime identifiers and versions.
# Piston identifies runtimes by (language, version) pairs.
# We use "*" to accept whatever version is installed.
LANGUAGE_MAP = {
    "python": {"language": "python", "version": "3.12.0"},
    "javascript": {"language": "javascript", "version": "20.11.1"},
}

# Default resource limits (must stay within Piston's configured limits)
DEFAULT_CPU_LIMIT = 3_000           # 3 seconds CPU time (ms) — Piston default max
DEFAULT_MEMORY_LIMIT = 256_000_000  # 256 MB
DEFAULT_WALL_TIMEOUT = 3_000        # 3 seconds wall-clock (ms) — Piston default max


class Sandbox:
    """Piston API client for sandboxed code execution.

    Usage:
        sandbox = Sandbox()
        result = await sandbox.execute("python", "print('hello')", stdin="")
        # result = { stdout, stderr, exit_code, wall_time_ms, timed_out }
    """

    def __init__(self, piston_url="http://localhost:2000"):
        self.piston_url = piston_url.rstrip("/")
        self._session = None

    async def _get_session(self):
        """Lazy-create an aiohttp session (reused across calls)."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def is_available(self):
        """Check if Piston is reachable and has runtimes installed.

        Returns:
            True if Piston responds to /api/v2/runtimes, False otherwise.
        """
        try:
            session = await self._get_session()
            async with session.get(
                f"{self.piston_url}/api/v2/runtimes", timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def get_runtimes(self):
        """Fetch the list of installed runtimes from Piston.

        Returns:
            list of dicts, each with 'language', 'version', 'aliases', etc.
        """
        session = await self._get_session()
        async with session.get(f"{self.piston_url}/api/v2/runtimes") as resp:
            resp.raise_for_status()
            return await resp.json()

    async def execute(
        self,
        language,
        code,
        stdin="",
        cpu_limit=DEFAULT_CPU_LIMIT,
        memory_limit=DEFAULT_MEMORY_LIMIT,
        wall_timeout=DEFAULT_WALL_TIMEOUT,
    ):
        """Execute code in Piston's sandbox.

        Args:
            language: User-facing language name ("python" or "javascript").
            code: Source code string to execute.
            stdin: Standard input to feed to the program.
            cpu_limit: CPU time limit in milliseconds.
            memory_limit: Memory limit in bytes.
            wall_timeout: Wall-clock timeout in milliseconds.

        Returns:
            dict with keys:
                stdout (str): Program's standard output.
                stderr (str): Program's standard error.
                exit_code (int | None): Process exit code (None if killed).
                wall_time_ms (int): Wall-clock execution time in ms.
                timed_out (bool): True if the process was killed by timeout.

        Raises:
            ValueError: If the language is not supported.
            aiohttp.ClientError: If Piston is unreachable.
        """
        runtime = LANGUAGE_MAP.get(language)
        if not runtime:
            raise ValueError(
                f"Unsupported language: '{language}'. "
                f"Supported: {list(LANGUAGE_MAP.keys())}"
            )

        # Determine file extension for the source file
        ext = "py" if language == "python" else "js"

        payload = {
            "language": runtime["language"],
            "version": runtime["version"],
            "files": [{"name": f"solution.{ext}", "content": code}],
            "stdin": stdin,
            "run_timeout": wall_timeout,
            "compile_timeout": wall_timeout,
            "run_memory_limit": memory_limit,
            "compile_memory_limit": memory_limit,
        }

        session = await self._get_session()
        async with session.post(
            f"{self.piston_url}/api/v2/execute",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=(wall_timeout / 1000) + 15),
        ) as resp:
            data = await resp.json() if resp.content_type == 'application/json' else {}
            if resp.status != 200:
                body_text = await resp.text() if not data else str(data)
                print(f"  [Sandbox] Piston {resp.status}: {body_text}")
                print(f"  [Sandbox] Payload sent: {payload}")
                resp.raise_for_status()

        # Piston returns { "run": { "stdout", "stderr", "code", "signal", "output" }, ... }
        run = data.get("run", {})
        exit_code = run.get("code")
        signal = run.get("signal")
        timed_out = signal == "SIGKILL" or (exit_code is None and signal is not None)

        # Piston doesn't directly report wall time; we estimate from the
        # response.  For accurate timing we'd need to measure client-side,
        # but for judging purposes this is sufficient.
        # We'll measure it from the caller side in the Judge instead.
        return {
            "stdout": run.get("stdout", ""),
            "stderr": run.get("stderr", ""),
            "exit_code": exit_code,
            "wall_time_ms": 0,  # Populated by the Judge wrapper
            "timed_out": timed_out,
        }
