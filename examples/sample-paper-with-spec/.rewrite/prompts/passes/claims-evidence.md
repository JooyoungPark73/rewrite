---
name: claims-evidence
level: claim
scope: paper            # default scope
allow_rewrites: false   # true only where the text is not the author's voice
model: null             # null → config default for this level
thinking: null
---
{# Where each claim's evidence is, and overclaiming against the claim's scope. #}
## Pass: claims and evidence

Scope: {{ scope_name }}

### The spec

{{ spec_outline }}

The CLAIMS part of the outline lists what the paper claims, the evidence each claim
rests on and the scope it is limited to; the `claims:` lines under the message, sections
and paragraphs show where each claim is meant to be argued.

### What to check

For each claim, find where the source argues it and where its evidence appears. Report:
- a claim whose stated evidence does not appear in the source, or appears only as an
  assertion (no experiment, proof, citation or figure behind it);
- overclaiming: the text states a claim more broadly than its scope allows (more
  workloads, settings, guarantees or generality than the evidence covers);
- evidence that does not support the claim it is attached to;
- a claim argued in a place the outline does not assign it to, while missing from the
  place it should be;
- a strong claim in the text that is not in the claim bank at all.

### Source

{{ source }}

### Output

Return one JSON object matching this schema, and nothing else:

{{ findings_schema }}

Use `"level": "claim"` and one of these categories: `missing-evidence`, `overclaim`,
`weak-support`, `misplaced-claim`, `unlisted-claim`. Set `json_ref` to the claim id, or
null for an unlisted claim. If you find no problems, return `{"findings": []}`.
