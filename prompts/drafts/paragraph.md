---
name: draft-paragraph
kind: draft             # a drafting template (prompts/drafts/), never a review pass
level: paragraph
scope: selection        # informational: a draft targets one paragraph node
allow_rewrites: true    # drafting writes prose by definition
model: null             # null → config default for this level
thinking: null
---
{#-
  Draft one paragraph from its spec entry (not a review pass; prompts/drafts/system.md
  frames it). Variables beyond the usual: node_id, says, function, section_id, section_says,
  claims_text (the linked claims with evidence and scope), section_source (the section's
  LaTeX as it reads now, unnumbered), current_text (the paragraph's text today, "" when it is
  not written yet), labels, bib_keys, draft_schema.
-#}
## Draft: paragraph {{ node_id }}

### The spec

{{ spec_outline }}

### The target

- id: `{{ node_id }}`, in section `{{ section_id }}` whose promise is: {{ section_says }}
- local message (what the paragraph must say): {{ says }}
- function (its job in the argument): {{ function or "not stated; infer the job from the local message and the section's promise" }}
- linked claims (the only claims the paragraph may make, each within its scope):
{{ claims_text or "  (none: the paragraph argues no claim of the bank; make none)" }}

{% if current_text -%}
### The paragraph as it reads now

The authors have a version; the draft is an alternative that may keep any of it that already
serves the local message:

{{ current_text }}

{% endif -%}
### The section as it reads now

{{ section_source or "(the section has no text yet)" }}

### Labels in the paper

{{ labels or "(none)" }}

### Bibliography keys in the paper

{{ bib_keys or "(none)" }}

### Output

Return exactly one JSON object matching this schema, and nothing else:

{{ draft_schema }}

- `draft`: the paragraph, as LaTeX, with no `% @p` line and no heading.
- `notes`: at most two sentences for the authors: what the spec left open (every `\todo` you
  wrote), or a claim you kept narrower than the local message asked because its scope did
  not cover it. Empty when there is nothing to say.
