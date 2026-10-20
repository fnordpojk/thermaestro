"""Whether Thermaestro can control now: what `/ready` answers, beside `/health`, which
says only that the web UI answers.

Ready means all of: the planner checks in and has made a whole round within two slots;
every device plugin (one with levers, or not described yet) is up, with a reading of its
own taken within `INPUT_S`; the value history is being written; no loop of the write path
or the planner has ended; and every lever that was to be put back is. In counts and
states, no names: it answers without a login.
"""

import math
from typing import TYPE_CHECKING, Any

from .host import PluginHost, State
from .values import Values

if TYPE_CHECKING:
    from ..planner import Planner
    from .executor import Executor

INPUT_S = 600.0
"""How old a device's newest reading may be."""
ROUND_S = 1800.0
"""How long since the planner's last whole round: two slots."""


def readiness(
    *,
    executor: "Executor | None",
    planner: "Planner | None",
    values: Values,
    host: PluginHost | None,
    now: float,
) -> dict[str, Any]:
    """The answer, with `ready` and what it rests on."""
    write: dict[str, Any] = (
        executor.readiness()
        if executor is not None
        else {"planner": "none", "not_put_back": 0, "in_flight": 0, "tasks_down": 0}
    )
    planning = planner.readiness(now) if planner is not None else None
    instances = host.instances.items() if host is not None else ()
    devices = [(id, i) for id, i in instances if i.described is None or bool(i.described.levers)]
    down = sum(1 for _, i in devices if i.state is not State.UP)
    newest: dict[str, float] = {}
    for key, envelope in values.latest.items():
        t = (envelope.t_observed or envelope.t_received).timestamp()
        newest[key.instance] = max(t, newest.get(key.instance, -math.inf))
    stale = sum(
        1 for id, i in devices if i.state is State.UP and now - newest.get(id, -math.inf) > INPUT_S
    )
    history = values.writing(now)
    tasks = write["tasks_down"] + (1 if planning is not None and not planning["running"] else 0)
    age = planning["round_age_s"] if planning is not None else None
    ready = (
        write["planner"] == "checking in"
        and age is not None
        and age <= ROUND_S
        and not down
        and not stale
        and history == "writing"
        and not tasks
        and not write["not_put_back"]
    )
    return {
        "ready": ready,
        "planner": write["planner"],
        "round_age_s": None if age is None else round(age),
        "devices_down": down,
        "inputs_stale": stale,
        "history": history,
        "tasks_down": tasks,
        "not_put_back": write["not_put_back"],
        "in_flight": write["in_flight"],
    }
