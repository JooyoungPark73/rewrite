---
name: latex-hygiene
level: sentence
scope: paper            # default scope
allow_rewrites: true    # LaTeX mechanics are not the author's voice
style: false            # the field's standard of evidence does not apply here
model: null             # null → config default for this level
thinking: null
---
{# LaTeX mechanics: undefined refs, labels, citation keys, notation consistency, typography, hard-coded numbers. #}
## Pass: LaTeX hygiene

Scope: {{ scope_name }}

Check the LaTeX mechanics of the source. This pass may show corrected markup in
`problem` (for example `Figure~\ref{fig:x}`), because markup is not the author's voice.
Keep any correction to the markup itself and do not reword prose.

### What to check

- `\ref`, `\eqref`, `\cref` or `\autoref` targets with no matching `\label` in the source;
- `\label`s that are defined twice, or never referenced;
- `\cite` keys that look malformed or are cited inconsistently (the bibliography itself
  is not shown, so do not guess whether a key exists in it);
- the same quantity written with different notation in different places, or one symbol
  used for two quantities;
- references without a non-breaking space (`Figure \ref{...}` instead of
  `Figure~\ref{...}`), numbers and units without a thin space, math typed outside math
  mode, `"` quotes instead of ``` `` ''```;
- hard-coded numbers for figures, tables, sections or equations instead of `\ref`;
- `\aigen{...}` wrappers and `% aigen-was:` lines left in the source: text a model proposed
  on a comment that the co-authors have not accepted yet (they accept by removing the
  wrapper); a submission must have none.

### Source

{{ source }}

### Output

Return one JSON object matching this schema, and nothing else:

{{ findings_schema }}

Use `"level": "sentence"` and one of these categories: `undefined-ref`, `label`,
`citation`, `notation`, `typography`, `hard-coded-number`, `aigen-leftover`. Quote the markup exactly as it
appears. Set `json_ref` to null unless the finding concerns a specific spec entry. If you
find no problems, return `{"findings": []}`.
