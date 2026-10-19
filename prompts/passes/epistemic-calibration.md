---
name: epistemic-calibration
level: sentence
scope: section          # default scope
allow_rewrites: false   # true only where the text is not the author's voice
model: null             # null → config default for this level
thinking: null
---
{# Sentences stronger or weaker than their evidence; vague quantities; unsupported statements about prior work. #}
## Pass: epistemic calibration

Scope: {{ scope_name }}

### The spec

{{ spec_outline }}

The CLAIMS part of the outline says what the paper can support and within what scope.

### What to check

Read each sentence that asserts something about results, causes, generality or prior
work. Report sentences whose strength does not match the evidence available in the
source and the claim bank:
- stronger than the evidence: "always", "proves", "eliminates", "orders of magnitude",
  causal language for a correlation, generalisation beyond the measured setting;
- weaker than the evidence: a measured, central result buried under hedges;
- vague quantities where the paper has the number ("significantly", "much faster");
- statements about prior work that the text does not support with a citation.

### Source

{{ source }}

### Output

Return one JSON object matching this schema, and nothing else:

{{ findings_schema }}

Use `"level": "sentence"` and one of these categories: `overclaim`, `underclaim`,
`vague-quantity`, `unsupported`. Quote the sentence or clause. Set `json_ref` to the
claim id the sentence relates to, or null. If you find no problems, return
`{"findings": []}`.
