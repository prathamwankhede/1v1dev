"""Problem bank — loads problem definitions from JSON files at startup."""

import json
import random
from pathlib import Path
from typing import List, Optional, Union


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
            # Validate required fields
            for field in ("id", "title", "description", "starterCode", "testCases"):
                if field not in problem:
                    raise ValueError(
                        f"Problem {path.name} missing required field: {field}"
                    )
            self.problems.append(problem)
        if not self.problems:
            raise ValueError(f"No problems found in {self.problems_dir}")

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
