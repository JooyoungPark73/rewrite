---
name: graphics
title: Graphics (rendering, geometry processing, interactive and creative tools)
description: Three cultures in one field; evaluations are heterogeneous because papers promise several properties at once.
---
## The field's question

Graphics papers often promise several properties at once, so three evidence cultures meet:
rendering and graphics systems ask what survives the operating regime; geometry processing
asks what is well defined, convergent and stable; interactive and creative graphics ask what
a person can control and edit. Say which claim is which.

## What counts as evidence

- Rendering and graphics systems: systems-style evidence: frame rate, memory, scene
  complexity, quality metrics against a reference, at a stated resolution on stated hardware.
- Geometry processing: formal-style evidence: a well-defined representation, convergence,
  invariants (manifoldness, orientation, topology), numerical stability, and the conditions
  under which they hold.
- Interactive and creative graphics: HCI-style evidence: controllability, editability,
  workflow support, shown with users or with a described interaction, not only with results.

The boundary of a claim is which of these cultures it belongs to and the regime, scene class
or user task it was shown for.

## What performance and contribution mean

Performance means speed and memory at a quality level for systems claims, convergence and
robustness for geometry claims, and control and effort for interaction claims. A
contribution is a method whose gains are shown with the right kind of evidence for each
property it promises.

## What a reviewer here looks for first

- a visual comparison standing in for every claimed property;
- a real-time or memory claim without resolution, scene size and hardware;
- a geometric property asserted (watertight, smooth, stable) without the condition or the
  failure case;
- "controllable" or "intuitive" without a user, a task, or a description of the control;
- evaluation on scenes the method was developed on;
- a heterogeneous evaluation that never says which number supports which promise.

## Vocabulary

Fits: at R resolution on G, converges when, invariant under, degrades on, lets the user,
within budget. Misleads: photorealistic, high quality (without a metric), robust, intuitive,
interactive (without the rate), handles arbitrary scenes.
