---
name: message-drift
level: message
scope: paper            # default scope
allow_rewrites: false   # true only where the text is not the author's voice
model: null             # null → config default for this level
thinking: null
---
{# Abstract, introduction and conclusion against the spec's message: drift, missing thesis, competing messages. #}
## Pass: message drift

Scope: {{ scope_name }}

### The spec

{{ spec_outline }}

### What to check

Read the abstract, the introduction and the conclusion in the source and compare each
against the spec's message and its problem, stakes and thesis. Report:
- places where the text argues a different main point than the spec's message, or where
  a reader would take away something else as "the real point";
- a problem, stakes or thesis from the spec that the abstract, introduction or
  conclusion never states, or states only after the reader has lost the thread;
- competing messages: a second contribution presented with the same weight as the thesis;
- disagreement among the abstract, introduction and conclusion about what the paper shows.

Do not report sentence-level style; other passes cover it.

### Source

{{ source }}

### Output

Return one JSON object matching this schema, and nothing else:

{{ findings_schema }}

Use `"level": "message"` and one of these categories: `drift`, `missing-element`,
`competing-message`, `inconsistent-framing`. For a missing element, quote the sentence
where it should have appeared. Set `json_ref` to the id of the spec entry the finding is
about (`message` for the message itself), or null. If you find no problems, return
`{"findings": []}`.
