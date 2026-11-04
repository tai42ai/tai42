"""How long a test waits for a deferred retire tail: the hang bound the epoch tests share."""

from __future__ import annotations

# How long a test waits for a deferred retire tail to finish once the work it waits on has ended
# (its driver answered, its waiter left its wait). A tail that never ends (a driver or gate waiter
# never released) fails at this bound instead of hanging the suite. It is a measured hang bound,
# not a multiple of any drain budget: the tails these tests wait on drain an idle or short-lived
# generation, the gate turnover takes about 0.2 s, and the rest is work on the loop that host load
# stretches, above all the certification sweep after the close, which runs a full garbage
# collection of the process while settings of the retired epoch are still alive. That collection
# measures up to 2.67 s with twenty busy processes sharing one core, the whole tail up to 3.2 s;
# the slowest tail, of a reload whose drain ended well short of its 5.0 s budget, measures 5.8 s
# with the host at load 200. The bound leaves headroom past both for a loaded CI runner.
DEFERRED_TAIL_BOUND_S = 10.0
