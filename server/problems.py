"""Problem bank — loads problem definitions from JSON files at startup."""

import json
import random
from pathlib import Path
from typing import List, Optional, Union

REQUIRED_FIELDS = ("id", "title", "description", "testCases")


def legacy_solution_path(language):
    """Filename a bare `code` string (the pre-Phase-5 wire shape) maps to —
    matches the one writable file `normalize_bundle` synthesizes from
    `starterCode` for a single-file problem."""
    ext = "py" if language == "python" else "js"
    return f"solution.{ext}"


def normalize_bundle(problem, language):
    """Normalize a problem's declared files into a uniform bundle shape:
    `{"entrypoint": str | None, "files": [{"path", "content", "writable"}]}`.

    Legacy single-file problems (`starterCode` only, no `files`) normalize
    into a one-element bundle, so there is never a dual code path
    downstream. `entrypoint` is `None` here when the problem instead
    relies on `testFiles` for its real judging entrypoint — see
    `Room._assemble_bundle`, which resolves the final entrypoint.
    """
    files_decl = (problem.get("files") or {}).get(language)
    if files_decl:
        return {
            "entrypoint": files_decl.get("entrypoint"),
            "files": [
                {
                    "path": f["path"],
                    "content": f["content"],
                    "writable": bool(f.get("writable")),
                }
                for f in files_decl.get("files", [])
            ],
        }

    path = legacy_solution_path(language)
    content = problem.get("starterCode", {}).get(language, "")
    return {
        "entrypoint": path,
        "files": [{"path": path, "content": content, "writable": True}],
    }


class ProblemBank:
    """Loads and serves problems from a directory of JSON files."""

    def __init__(self, problems_dir: Union[str, Path]):
        self.problems_dir = Path(problems_dir)
        self.problems: List[dict] = []
        self._load()

    def _load(self):
        """Load all .json files from the problems directory."""
        if not self.problems_dir.is_dir():
            raise FileNotFoundError(
                f"Problems directory not found: {self.problems_dir}"
            )
        for path in sorted(self.problems_dir.glob("*.json")):
            with open(path, "r") as f:
                problem = json.load(f)
            for field in REQUIRED_FIELDS:
                if field not in problem:
                    raise ValueError(
                        f"Problem {path.name} missing required field: {field}"
                    )
            self._validate_files(problem, path.name)
            self.problems.append(problem)
        if not self.problems:
            raise ValueError(f"No problems found in {self.problems_dir}")

    def _validate_files(self, problem, filename):
        """Presence-only validation stays the house style, plus these cheap
        checks that catch real authoring mistakes in a multi-file problem.
        """
        has_starter = bool(problem.get("starterCode"))
        files_decl = problem.get("files")
        if not has_starter and not files_decl:
            raise ValueError(f"Problem {filename} has neither 'starterCode' nor 'files'")
        if not files_decl:
            return

        test_files_decl = problem.get("testFiles")
        has_test_files = "testFiles" in problem

        for lang, lang_files in files_decl.items():
            files_list = lang_files.get("files", [])
            paths = [f["path"] for f in files_list]

            for p in paths:
                if "/" in p or ".." in p or p.startswith("."):
                    raise ValueError(
                        f"Problem {filename}: file path '{p}' must be flat "
                        f"(no '/', no '..', no leading '.')"
                    )

            if not any(f.get("writable") for f in files_list):
                raise ValueError(f"Problem {filename}: language '{lang}' has no writable file")

            lang_test_paths = {f["path"] for f in (test_files_decl or {}).get(lang, [])}
            overlap = set(paths) & lang_test_paths
            if overlap:
                raise ValueError(
                    f"Problem {filename}: path(s) {sorted(overlap)} appear in both "
                    f"'files' and 'testFiles' for '{lang}'"
                )

            if has_test_files:
                if lang not in test_files_decl:
                    raise ValueError(
                        f"Problem {filename}: language '{lang}' in 'files' has no "
                        f"matching 'testFiles' entry"
                    )
            else:
                entrypoint = lang_files.get("entrypoint")
                if not entrypoint:
                    raise ValueError(
                        f"Problem {filename}: language '{lang}' missing 'entrypoint' "
                        f"(required when 'testFiles' is absent)"
                    )
                if entrypoint not in paths:
                    raise ValueError(
                        f"Problem {filename}: entrypoint '{entrypoint}' not found in "
                        f"'files' for '{lang}'"
                    )

    def get_random(self) -> dict:
        """Return a random problem from the bank."""
        return random.choice(self.problems)

    def get_by_id(self, problem_id: str) -> Optional[dict]:
        """Return a specific problem by ID, or None if not found."""
        for p in self.problems:
            if p["id"] == problem_id:
                return p
        return None

    def __len__(self):
        return len(self.problems)
