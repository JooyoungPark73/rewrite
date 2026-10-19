---
name: section-promises
level: section
scope: paper            # default scope
allow_rewrites: false   # true only where the text is not the author's voice
model: null             # null → config default for this level
thinking: null
---
{# Does each section deliver its promise; figure story coherence; section order. #}
## Pass: section promises

Scope: {{ scope_name }}

### The spec

{{ spec_outline }}

Each section line in the outline is the section's promise: what the reader should know or
believe after reading it. A section's `figure_story` (when given) says what its figures
and tables are meant to show, in order.

### What to check

For each section in the source, report:
- a promise the section does not deliver, or delivers only partly;
- material that serves no promise of this section (it belongs elsewhere or nowhere);
- a figure or table whose content does not match the figure story, is never referred to
  in the text, or is referred to before the reader can interpret it;
- an order of sections or subsections that forces the reader to use something before it
  is introduced.

### Source

{{ source }}

### Output

Return one JSON object matching this schema, and nothing else:

{{ findings_schema }}

Use `"level": "section"` and one of these categories: `undelivered-promise`,
`off-promise`, `figure-story`, `ordering`. For an undelivered promise, quote the heading
or the sentence where the delivery falls short. Set `json_ref` to the section's spec id,
or null if the section is not in the spec. If you find no problems, return
`{"findings": []}`.
