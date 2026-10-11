"""Hot-water use as a household noted it, to score the draws a replay found against.

A CSV file with a header: `time,what` and, if known, `liters`. The time is local, in the
house's time zone (`2026-10-14 07:15`); `what` is anything (`shower`, `bath`,
`dishwasher`). A label is found where a draw started or peaked within an hour of it, each
draw matching one label at most, the nearest first.
"""

import csv
import json
from dataclasses import dataclass
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from ..store import Database, Transaction

WITHIN_S = 3600.0


@dataclass(frozen=True, slots=True)
class Label:
    t: float
    what: str
    liters: float | None = None


def read_labels(path: Path, zone: tzinfo) -> list[Label]:
    out = []
    with path.open(encoding="utf-8", newline="") as f:
        for n, row in enumerate(csv.DictReader(f), start=2):
            try:
                when = datetime.fromisoformat(row["time"].strip())
            except (KeyError, ValueError) as e:
                raise ValueError(f"{path}:{n}: a time such as 2026-10-14 07:15") from e
            if when.tzinfo is None:
                when = when.replace(tzinfo=zone)
            liters = (row.get("liters") or "").strip()
            out.append(
                Label(
                    when.timestamp(),
                    (row.get("what") or "").strip(),
                    float(liters) if liters else None,
                )
            )
    return sorted(out, key=lambda x: x.t)


async def score(db: Database, labels: list[Label], zone: tzinfo) -> dict[str, Any]:
    """The draws found against `labels`, over the labels' span."""

    def read(t: Transaction) -> list[tuple[str, float, str]]:
        return list(t.execute("SELECT scope, t, body FROM learn_events WHERE kind = 'draw'"))

    draws = [(scope, at, json.loads(body)) for scope, at, body in await db.run(read)]
    if labels:
        first, last = labels[0].t - WITHIN_S, labels[-1].t + WITHIN_S
        draws = [d for d in draws if first <= d[1] <= last]
    pairs = sorted(
        (min(abs(d[2]["start"] - x.t), abs(d[1] - x.t)), i, j)
        for i, x in enumerate(labels)
        for j, d in enumerate(draws)
    )
    matched: dict[int, int] = {}
    used: set[int] = set()
    for gap, i, j in pairs:
        if gap > WITHIN_S:
            break
        if i in matched or j in used:
            continue
        matched[i] = j
        used.add(j)
    rows = []
    for i, x in enumerate(labels):
        k = matched.get(i)
        draw = draws[k] if k is not None else None
        rows.append(
            {
                "time": datetime.fromtimestamp(x.t, zone).isoformat(timespec="minutes"),
                "what": x.what,
                "liters": x.liters,
                "found": draw is not None,
                "draw_at": datetime.fromtimestamp(draw[1], zone).isoformat(timespec="minutes")
                if draw
                else None,
                "size": draw[2]["size"] if draw else None,
            }
        )
    return {
        "labels": rows,
        "found": len(matched),
        "missed": len(labels) - len(matched),
        "extra": len(draws) - len(used),
    }
