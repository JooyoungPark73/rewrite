---
name: ab-compare
kind: compare          # not a review pass: the blind A/B compare uses it
level: paragraph
scope: selection        # default scope
allow_rewrites: false   # true only where the text is not the author's voice
style: false            # the field's standard of evidence does not apply here
model: null             # null → config default for this level
thinking: null
---
{#-
  Blind A/B comparison of two versions of one paragraph (not a review pass).
  Besides the standard variables (spec_outline holds only the message, the paragraph,
  its section and their claims), the caller supplies `version_a` and `version_b` in
  randomized order. Never tell the model that either version is an edit, a revision,
  older or newer. The answer must be {"verdict": "A"|"B"|"tie", "reason": str}.
-#}
## Pass: blind comparison

Scope: {{ scope_name }}

This request has no numbered source and asks for no findings. Instead, two candidate
versions of the same paragraph follow, as LaTeX with comments removed. Judge which one
better delivers the paragraph's local message and does its job in the argument, as stated
in the spec:

{{ spec_outline }}

### Version A

{{ version_a }}

### Version B

{{ version_b }}

Compare only how well each version delivers the local message and performs its function:
whether the point is stated early, whether every sentence serves it, whether the claims
stay within their evidence. Ignore length unless it hurts the point. Do not rewrite
either version and do not merge them.

## Output

Return exactly one JSON object with these two keys and nothing else:

{"verdict": "A" | "B" | "tie", "reason": "..."}

- `verdict`: the version that better delivers the local message, or `tie` when neither
  does it better.
- `reason`: one or two sentences naming the weaknesses that decided the verdict: what the
  other version fails to do, or, for a tie, what both fail to do. No praise, no
  replacement wording, no merged version.
