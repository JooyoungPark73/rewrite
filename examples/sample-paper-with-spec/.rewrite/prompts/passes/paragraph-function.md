---
name: paragraph-function
level: paragraph
scope: section          # default scope
allow_rewrites: false   # true only where the text is not the author's voice
model: null             # null → config default for this level
thinking: null
---
{# One job per paragraph, topic sentences, misplaced paragraphs: each paragraph against its local message and function. #}
## Pass: paragraph function

Scope: {{ scope_name }}

### The spec

{{ spec_outline }}

In the outline, each paragraph line is that paragraph's local message: what the
paragraph must deliver. Its `function` (when given) is the job it does in the argument.
In the source, a paragraph that the spec describes is preceded by its `% @p <id>` line.

### What to check

For each paragraph in {{ scope_name }}, compare it with its entry in the outline; for a
paragraph without a `% @p` line, compare it with the promise of its section. Report:
- a paragraph that does more than one job, or whose job differs from its `function`;
- a paragraph whose first sentence does not state its local message, so the reader
  learns the point only at the end or not at all;
- a paragraph whose content does not deliver the local message in the spec;
- a paragraph that would serve the argument better in another place (name the spec id or
  section it belongs with);
- a paragraph with no job in the argument at all.

Judge structure and function only; sentence style is another pass.

### Source

{{ source }}

### Output

Return one JSON object matching this schema, and nothing else:

{{ findings_schema }}

Use `"level": "paragraph"` and one of these categories: `multiple-jobs`, `function`,
`topic-sentence`, `off-message`, `misplaced`, `no-job`. Quote the sentence that shows the
problem (for `topic-sentence`, the paragraph's first sentence). Set `json_ref` to the
paragraph's spec id from its `% @p` line, or null for a paragraph without one. If you
find no problems, return `{"findings": []}`.
