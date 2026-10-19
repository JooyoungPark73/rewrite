# Writing styles: the field's standard of evidence

One file per research culture. `[paper] style = "<file name without .md>"` in
`.rewrite/config.toml` (the Settings dialog has a dropdown) picks one; its body is appended to
`system.md` as the section "The field's standard of evidence" in every review pass, so the
passes judge claims, evidence and wording the way a reviewer in that field would. A pass may
opt out with `style: false` in its front matter (`latex-hygiene` and `ab-compare` do).

Each file has front matter (`name`, `title`, `description`) and a body that answers the same
five questions, drawn from Alan Synn, "Same Computer Science, Different Senses of Reality"
(alansynn.com/blog/on-truth-computer-research/):

1. the field's question: what becomes possible, what survives real conditions, or what can
   be stated generally and proved;
2. what counts as evidence, and the boundary every claim needs;
3. what "performance" and "contribution" mean here;
4. the failure modes a reviewer in this field looks for first;
5. the vocabulary that fits and the vocabulary that misleads.

A style describes what a reviewer will not accept. It never prescribes phrasing: the passes
report problems and leave the wording to the authors. Add a file for your own field or venue;
it is picked up without a restart.
