---
name: nlp
title: Natural language processing
description: Truth depends on the objective; fluency, correctness and factuality are separate properties.
---
## The field's question

Which objective is being claimed? Translation, retrieval, generation, summarisation,
factuality and dialogue are not interchangeable; a system good at one says little about the
others. "Is the language fluent?" and "Is the content correct?" are different questions.

## What counts as evidence

Evaluation that matches the objective: automatic metrics with their known blind spots named,
human evaluation with the protocol and agreement reported, and separate measurements for
fluency, correctness, factuality, robustness and semantic preservation when the claim
touches more than one. The boundary of a claim is the language(s), domain, dataset, metric
and, for LLM-based work, the model version and prompt.

## What performance and contribution mean

Performance is the objective's metric under its protocol, with human judgement where the
metric is known to diverge from it. A contribution is a method whose gain holds on the
property it claims, a resource that enables a measurement, or an analysis that separates
properties that looked like one.

## What a reviewer here looks for first

- fluency metrics (BLEU, perplexity) offered as evidence of correctness or factuality;
- human evaluation without the instructions, the number of raters or agreement;
- a gain on one language or domain presented as general;
- contamination of the test data in pre-training, not ruled out;
- prompt sensitivity or model-version drift not controlled;
- "understands", "knows" or "reasons" from task accuracy;
- a hallucination or bias claim without a defined measurement.

## Vocabulary

Fits: on task T, in language L, by metric M, judged by N raters with agreement κ, preserves
meaning, factually consistent with. Misleads: natural, human-like, understands, knows,
state of the art (without the leaderboard and date), significantly better (without a test).
