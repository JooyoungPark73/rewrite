---
name: robotics
title: Robotics
description: Truth by physical execution; the physical world tests the assumptions hidden in the model.
---
## The field's question

Constraint, drawing on several cultures at once (perception, learning, planning, control,
systems, HCI): does it work on the robot, repeatably, when the world does not match the
model? Simulation results are a hypothesis about hardware.

## What counts as evidence

Physical execution with task success rates over stated numbers of trials, tracking or pose
error, control stability, recovery from disturbances, and repeatability across runs, objects
and conditions; sim-to-real transfer shown, not assumed; for learning-based work, the
training conditions and the gap to the test conditions. The boundary of a claim is the
platform, the environment, the objects, the number of trials and the disturbances tried.

## What performance and contribution mean

Performance is task success and precision under stated conditions, with time and compute on
the robot's hardware. A contribution is a capability that holds on hardware across the
variation the task entails, a controller with a stability argument, or a system whose
integration is shown to matter, with failure modes reported.

## What a reviewer here looks for first

- results only in simulation, or hardware results on a handful of hand-picked trials;
- success rates without the number of trials, the objects or the environments;
- no failure cases or recovery behaviour reported;
- an assumption of the model (known dynamics, perfect perception, static scene) that the
  experiments never violate;
- a learning method whose test conditions match its training conditions exactly;
- "autonomous", "general" or "robust" without the variation that was tested.

## Vocabulary

Fits: on platform P, over N trials, under disturbance D, transfers to, recovers from,
success rate, tracking error. Misleads: robust, general, real-world (for a lab bench),
autonomous (with a human in the loop), works reliably (without the rate).
