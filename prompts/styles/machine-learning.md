---
name: machine-learning
title: Machine learning
description: Truth by benchmarks, generalisation tests and distribution shift; gains can be leakage.
---
## The field's question

Mostly constraint, with a principle corner: does the learned behaviour persist beyond the
examples the paper happened to test? A benchmark number is a measurement of one sample from
one distribution.

## What counts as evidence

Held-out evaluation on named benchmarks with the split, the protocol and the number of runs
stated; generalisation under distribution shift; ablations that isolate the claimed
component; calibration and robustness where the claim implies them; scaling behaviour when
the claim is about scale. The boundary of a claim is the data distribution, model family,
compute budget and metric it was measured on. Error bars or seeds are part of the result.

## What performance and contribution mean

Performance is accuracy, loss, robustness, calibration or scaling behaviour on a stated
benchmark under a stated budget. A contribution is a method whose gain survives ablation,
seeds and a shift the paper did not tune on, or an analysis that explains a behaviour; a gain
that disappears without one undisclosed trick is not one.

## What a reviewer here looks for first

- test-set contamination, leakage, or tuning on the evaluation split;
- a gain within seed variance reported as an improvement;
- the proposed method tuned harder than the baselines;
- a claim about "generalisation", "understanding" or "reasoning" from in-distribution
  accuracy alone;
- a shortcut the benchmark allows (surface features, label priors) that is not ruled out;
- compute, data or parameters that differ between the compared systems and are not reported;
- a mechanism asserted ("the model learns to ...") without an ablation or probe behind it.

## Vocabulary

Fits: on benchmark B, under shift S, across N seeds, within variance, ablating, held out,
improves by X points. Misleads: state of the art (without the table and date), understands,
learns to reason, robust (without the perturbation), significantly (without a test),
outperforms (without the setting).
