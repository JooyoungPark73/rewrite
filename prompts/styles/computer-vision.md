---
name: computer-vision
title: Computer vision
description: Truth depends on the task; one number rarely captures geometry, views and robustness.
---
## The field's question

Does the representation capture the visual structure the paper claims it captures? Success
depends on the task: recognition, detection, segmentation, reconstruction, tracking, and
neural rendering each have their own notion of being right.

## What counts as evidence

Several metrics rather than one, because one number rarely covers geometry, novel views,
occlusion, lighting and motion at once; standard benchmarks with their protocols; qualitative
results that show failure cases as well as successes; robustness to the perturbations the
application will see. The boundary of a claim is the task, the dataset, the capture
conditions and the metric set.

## What performance and contribution mean

Performance is the task metric (mAP, IoU, PSNR/SSIM/LPIPS, pose or depth error, tracking
accuracy) under stated conditions, often with speed and memory alongside. A contribution is a
representation or method whose gain holds across the metrics the task needs and across
conditions, or a dataset or benchmark that changes what can be measured.

## What a reviewer here looks for first

- a single metric standing in for a multi-faceted task (PSNR without perceptual metrics,
  accuracy without geometry);
- cherry-picked qualitative examples with no failure cases;
- evaluation only on the capture conditions the method was tuned for;
- "3D", "geometry" or "understanding" claimed from 2D image metrics;
- comparisons at different resolutions, inputs or pre-training without saying so;
- a real-time claim without the hardware and resolution.

## Vocabulary

Fits: on dataset D, at resolution R, under occlusion, across views, per-pixel, per-instance,
degrades under. Misleads: photorealistic, understands the scene, general-purpose, works on
in-the-wild images (without an in-the-wild evaluation), real-time (without the device).
