---
name: pl-formal-methods
title: Programming languages and formal methods
description: Truth by soundness, semantics and invariants; an implementation is not a guarantee.
---
## The field's question

Principle: what property is guaranteed, and by what? The link between a specification and
the guarantee it buys is the thing the field protects. A working implementation shows that
something can be built; it does not show that a property holds.

## What counts as evidence

Formal statements of the property (type soundness, memory safety, equivalence, termination,
noninterference), a semantics precise enough to state them, and a proof or a mechanised
proof. For tools: what the checker guarantees when it says yes, what it may miss, and
whether the trusted computing base is named. The boundary of a claim is the language,
fragment, semantics and assumptions it is proved for.

## What performance and contribution mean

Performance means expressiveness, precision, the size of the trusted base, annotation
burden and, for tools, verification time and false-positive rate. A contribution is a new
guarantee, a weaker assumption for the same guarantee, a mechanisation, or a language design
whose property is established, not merely implemented.

## What a reviewer here looks for first

- "guarantees" or "ensures" for a property that is only tested or only intended;
- a soundness claim whose semantics, fragment or assumptions are not stated;
- the implementation and the formal model drifting apart without saying which was proved;
- a type system or analysis whose unsoundness cases are not named;
- evaluation on examples that the calculus does not cover;
- "verified" without saying what, in which logic, and what is trusted.

## Vocabulary

Fits: sound, complete, well-typed, preserves, invariant, formalise, mechanise, admits, is
modelled as, assuming. Misleads: safe (without the property), correct (without the spec),
robust, handles all cases, in practice never happens.
