---
name: ml-systems
title: ML systems (systems papers about training or serving models)
description: A systems paper whose workload is a model; both cultures' evidence is required.
---
## The field's question

Constraint, with a machine-learning workload: what survives real conditions, where the
conditions include the model, the data and the accuracy the user needs? A systems gain that
changes the model's output is not a free gain.

## What counts as evidence

Systems evidence: throughput, latency, memory and cost under a stated workload (model,
batch, sequence length, precision) on stated hardware, against tuned baselines. Plus the
accuracy side: the same model quality, measured, before and after, or the exact loss stated.
The boundary of a claim is "for models of class C, workload W, on hardware H, at quality Q".

## What performance and contribution mean

Performance is the systems metric at unchanged (or explicitly bounded) model quality. A
contribution is a mechanism (scheduling, memory, parallelism, compilation, quantisation,
caching) that removes a cost the previous stack hid, shown at scale with the quality effect
measured.

## What a reviewer here looks for first

- a speedup with the accuracy effect unmeasured or measured on a proxy;
- a baseline with a different precision, batch size, framework version or hardware;
- scale claimed from one GPU or one model size;
- hidden cost moved to warm-up, compilation, memory or the host;
- "lossless" or "no accuracy drop" from one benchmark or one seed;
- a workload that favours the mechanism (sequence lengths, sparsity) and nothing else.

## Vocabulary

Fits: at batch B, precision P, on H, same quality within ε, end-to-end, amortised over,
degrades when. Misleads: free, lossless (without the measurement), efficient, scalable,
negligible overhead, orders of magnitude (without both numbers).
