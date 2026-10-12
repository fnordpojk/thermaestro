"""A linear program, built row by row and solved by HiGHS in stages.

Each stage minimizes its own objective; its optimum, with a tolerance in the stage's own
unit, then bounds it while the next is solved, so what an earlier stage reached is never
given up for a later one (a lexicographic solve). A stage that can't be solved within its
tolerance has it widened tenfold, up to three times, and says so.

numpy and HiGHS are imported only here, and only once an optimizer runs.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

INF = math.inf
WIDEN = 10.0
TRIES = 3


@dataclass(frozen=True)
class Stage:
    name: str
    objective: Mapping[int, float]
    """Coefficients by column."""
    tolerance: float
    """How far above its optimum a later stage may let it go, in its own unit."""


@dataclass
class Solved:
    ok: bool
    x: list[float] = field(default_factory=list)
    values: dict[str, float] = field(default_factory=dict)
    """Each stage's optimum, by name."""
    widened: list[str] = field(default_factory=list)
    """Stages whose tolerance had to be widened."""
    why: str = ""


class Program:
    def __init__(self) -> None:
        self.lower: list[float] = []
        self.upper: list[float] = []
        self.names: list[str] = []
        self._rows: list[tuple[float, float, dict[int, float]]] = []

    @property
    def columns(self) -> int:
        return len(self.lower)

    def var(self, lower: float = 0.0, upper: float = INF, name: str = "") -> int:
        self.lower.append(lower)
        self.upper.append(upper)
        self.names.append(name)
        return len(self.lower) - 1

    def row(self, coefs: Mapping[int, float], lower: float = -INF, upper: float = INF) -> None:
        kept = {c: v for c, v in coefs.items() if v != 0.0}
        self._rows.append((lower, upper, kept))

    def equal(self, coefs: Mapping[int, float], value: float) -> None:
        self.row(coefs, value, value)

    def solve(self, stages: Sequence[Stage], time_limit_s: float = 30.0) -> Solved:
        import highspy
        import numpy as np

        h = highspy.Highs()  # type: ignore[no-untyped-call]
        h.setOptionValue("output_flag", False)
        h.setOptionValue("time_limit", float(time_limit_s))
        n = self.columns
        lp = highspy.HighsLp()
        lp.num_col_ = n
        lp.num_row_ = len(self._rows)
        lp.col_cost_ = np.zeros(n)
        lp.col_lower_ = np.array(self.lower, dtype=float)
        lp.col_upper_ = np.array([u if u != INF else highspy.kHighsInf for u in self.upper])
        lp.row_lower_ = np.array(
            [lo if lo != -INF else -highspy.kHighsInf for lo, _, _ in self._rows], dtype=float
        )
        lp.row_upper_ = np.array(
            [hi if hi != INF else highspy.kHighsInf for _, hi, _ in self._rows], dtype=float
        )
        starts, index, value = [0], [], []
        by_column: list[list[tuple[int, float]]] = [[] for _ in range(n)]
        for r, (_, _, coefs) in enumerate(self._rows):
            for c, v in coefs.items():
                by_column[c].append((r, v))
        for column in by_column:
            for r, v in column:
                index.append(r)
                value.append(v)
            starts.append(len(index))
        lp.a_matrix_.format_ = highspy.MatrixFormat.kColwise
        lp.a_matrix_.start_ = np.array(starts, dtype=np.int32)
        lp.a_matrix_.index_ = np.array(index, dtype=np.int32)
        lp.a_matrix_.value_ = np.array(value, dtype=float)
        h.passModel(lp)
        out = Solved(False)
        bounds: list[list[Any]] = []
        """Each stage solved: its bound's row, its optimum, its tolerance, its name."""
        for stage in stages:
            cost = np.zeros(n)
            for c, v in stage.objective.items():
                cost[c] += v
            h.changeColsCost(n, np.arange(n, dtype=np.int32), cost)
            h.run()
            tries = 0
            while h.getModelStatus() != highspy.HighsModelStatus.kOptimal and tries < TRIES:
                # The stages before were held too tightly for this one: let them go a little.
                tries += 1
                for bound in bounds:
                    bound[2] *= WIDEN
                    h.changeRowBounds(bound[0], -highspy.kHighsInf, bound[1] + bound[2])
                    if bound[3] not in out.widened:
                        out.widened.append(bound[3])
                h.run()
            if h.getModelStatus() != highspy.HighsModelStatus.kOptimal:
                out.why = f"{stage.name}: {h.modelStatusToString(h.getModelStatus())}"
                return out
            x = list(h.getSolution().col_value)
            best = sum(v * x[c] for c, v in stage.objective.items())
            out.values[stage.name] = best
            out.x = x
            if stage.objective:
                columns = sorted(stage.objective)
                tolerance = max(stage.tolerance, 1e-6 * abs(best))
                h.addRow(
                    -highspy.kHighsInf,
                    best + tolerance,
                    len(columns),
                    np.array(columns, dtype=np.int32),
                    np.array([stage.objective[c] for c in columns], dtype=float),
                )
                bounds.append([h.getNumRow() - 1, best, tolerance, stage.name])
        out.ok = True
        return out


def value(solved: Solved, column: int) -> float:
    return solved.x[column] if solved.ok else 0.0


def dot(solved: Solved, coefs: Mapping[int, float]) -> float:
    return sum(v * solved.x[c] for c, v in coefs.items())
