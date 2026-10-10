"""`thermaestro rig`: a lever tried on a real Nibe pump, through the core's own write path.

Each check takes a lever over as Thermaestro does in control (the claim, the baseline, the
request, the readback), watches what the pump does, asks a person where one must judge,
puts the lever back as it was found, and ends with a table and a log. What it shows is what
control will do, because it is the same code: the Nibe plugin, the values and the executor,
run as the daemon runs them.

**Read-only unless asked.** Without `write`, the transport has no way to write and the
levers are in shadow: everything before the send runs (the claim and its baseline, the
competing features, the preconditions, the range), and what would be written is shown.
With `write`, the check says what it will write and asks before the first change.

**Put back on every way out.** A check puts each lever back itself; one that ends early (a
failure, Ctrl-C) has the rest put back as it stops. What it took over (the claims, the
hot-water block's release) is kept in the state directory, so a run that was killed is put
back later by `restore`, and no check starts until it has been.

The log names no address or key, as the probe's report doesn't, so a tester can send it.
"""

from .bench import FORMAT, Options, Report, RigError, Step, terminal_ask
from .checks import CHECKS, run

__all__ = ["CHECKS", "FORMAT", "Options", "Report", "RigError", "Step", "run", "terminal_ask"]
