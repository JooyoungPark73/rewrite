---
name: triage
kind: triage            # not a review pass: POST /api/runs/{id}/triage uses it
level: message
scope: paper            # informational: a triage uses the scope of the run it judges
allow_rewrites: false   # true only where the text is not the author's voice
model: null             # null → config default for this level
thinking: null
---
{#-
  Triage of one run's findings: which are wrong and which are trivial (not a review pass).
  This is the one prompt that contains earlier findings (PLAN.md "Rule enforcement").
  Besides the standard variables (the source is the judged run's scope as it reads now),
  the caller supplies `findings`, the run's open findings as a numbered list, and
  `findings_schema` holds the verdict schema. The answer must be
  {"verdicts": [{"index": int, "verdict": "keep"|"wrong"|"trivial", "reason": str}]}.
-#}
## Pass: triage of screening findings

Scope: {{ scope_name }}

An automated screening of this draft produced the numbered findings below. Screening is
noisy: some findings are wrong and some are trivial. Judge each finding against the
source and the spec, and say which ones the authors should spend time on.

### The spec

{{ spec_outline }}

### Findings to judge

{{ findings }}

### Source

{{ source }}

### What to decide

Give each numbered finding exactly one verdict:
- `keep`: the problem is real, the quote shows it, and it would matter to a reader or a
  reviewer;
- `wrong`: the finding misreads the source or the spec, its quote does not show the
  problem, it reports something the source does state, or it demands something the spec
  does not ask for;
- `trivial`: the problem is real but too small to be worth the authors' time.

Judge each finding on its own. Do not add new findings, do not merge findings, and do not
propose fixes or replacement wording.

### Output

Return one JSON object matching this schema, and nothing else:

{{ findings_schema }}

`index` is the finding's number in the list above. `reason` is one sentence saying what
decided the verdict. Give one verdict for every finding in the list.
