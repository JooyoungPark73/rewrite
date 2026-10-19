---
name: theory-algorithms
title: Theory and algorithms
description: Truth by definition, assumption and proof; a counterexample breaks a universal claim.
---
## The field's question

Principle: what can be stated generally, proved, bounded, or falsified? A result is the
relationship between a precisely stated problem, its assumptions and what follows from them.

## What counts as evidence

Theorems with proofs, lower bounds, convergence guarantees, reductions, and counterexamples.
Experiments illustrate; they do not establish. The boundary of a claim is its hypothesis:
"under the following assumptions", "for inputs of this class", "in this model of computation".
A single valid counterexample refutes a universal claim, so every "for all" must say for all
of what.

## What performance and contribution mean

Performance is asymptotic or worst-case behaviour under a stated cost model (time, space,
queries, rounds, approximation ratio), not wall-clock time on one machine. A contribution is a
new bound, a tighter analysis, a new problem made precise, or a separation; "simpler proof" is
a contribution only when the simplification is shown.

## What a reviewer here looks for first

- an assumption used in a proof but absent from the theorem statement;
- a theorem that does not imply what the introduction or abstract claims it implies;
- definitions that shift between sections, or a symbol used for two things;
- a lower bound and an upper bound that do not refer to the same model or input class;
- "optimal", "tight" or "general" without the matching bound or without naming the class;
- empirical numbers standing in for a missing proof step.

## Vocabulary

Fits: prove, show, bound, construct, reduce, characterise, holds for, under the assumption
that, there exists, for every. Misleads: works well, in practice, typically, empirically
observed (unless the paper is explicitly empirical), significant (without a definition).
