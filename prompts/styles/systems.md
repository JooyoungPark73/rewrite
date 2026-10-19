---
name: systems
title: Systems, architecture, databases and networking
description: Truth by measurement under a stated workload, system and metric; hidden cost returns.
---
## The field's question

Constraint: what survives real conditions? The field's instinct is that hidden complexity
returns and ignored cost returns. The basic chain of a paper is abstraction, hidden cost,
bottleneck, measurement, redesign.

## What counts as evidence

Measurements under a stated workload, on a stated machine, with a stated metric: throughput,
latency and tail latency, memory, CPU, bandwidth, cost. The boundary of every claim is
"under workload W, on system S, with metric M"; a number without those three is not a
result. Baselines must be the strongest reasonable configuration of the alternative, tuned
the way its authors would tune it. Repetitions, variance and the hardware generation matter.

## What performance and contribution mean

Performance is the measured metric at a stated operating point, including the tail and the
cost paid for it. A contribution is a mechanism that removes a bottleneck or a cost the
previous abstraction hid, shown to hold at scale and under adversity (skew, contention,
failures, cold caches), with the trade-off made explicit.

## What a reviewer here looks for first

- a speedup without the workload, hardware, metric or baseline configuration;
- a median reported where the tail is what the system is for, or the reverse;
- memory, CPU or cost that the design moved somewhere unmeasured;
- a baseline that is untuned, outdated or not the obvious competitor;
- a single machine or a single trace presented as general;
- "lack of" phrased as a flaw of the previous authors rather than a missing mechanism
  (critique here is technical, not moral);
- a cost model that is implied but never written down.

## Vocabulary

Fits: under, on, with, at p99, per core, amortised, saturates, bottleneck, trade, costs,
scales to, degrades when. Misleads: fast, efficient, scalable, lightweight, negligible
overhead (without the number), orders of magnitude (without both numbers), production-ready.
