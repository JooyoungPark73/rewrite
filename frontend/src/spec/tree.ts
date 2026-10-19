// Pure helpers for walking the paper.json tree. No React and no fetching, so
// the store, the outliner and the drag-and-drop code can all share them.
//
// Shape reminder (see api/client.ts): spec.tree is the root (level "message",
// same id as spec.message); its children are sections; a section's children
// are paragraphs. Claims live in the flat list spec.claims.
import type { Node, NodeLevel, PaperSpec } from '../api/client'

/** A node's place in the tree: its parent and its index among the parent's children. */
export interface TreeLocation {
  parent: Node
  index: number
}

/** Find a section or paragraph by id (the root and claims are not searched). */
export function findNode(spec: PaperSpec, id: string): Node | null {
  const loc = locate(spec, id)
  return loc ? loc.parent.children[loc.index] : null
}

/** Find a node's parent and index, searching below the tree root. */
export function locate(spec: PaperSpec, id: string): TreeLocation | null {
  const visit = (parent: Node): TreeLocation | null => {
    for (let i = 0; i < parent.children.length; i++) {
      const child = parent.children[i]
      if (child.id === id) return { parent, index: i }
      const deeper = visit(child)
      if (deeper) return deeper
    }
    return null
  }
  return visit(spec.tree)
}

/** Find a claim by id. */
export function findClaim(spec: PaperSpec, id: string): Node | null {
  return spec.claims.find((c) => c.id === id) ?? null
}

/**
 * Ids of the rows in the order they appear on screen: each section, then its
 * paragraphs unless the section is collapsed. Used for Arrow-Up/Down focus
 * moves and for "focus the previous row" after a delete.
 */
export function visibleRowIds(spec: PaperSpec, collapsed: Record<string, boolean>): string[] {
  const ids: string[] = []
  for (const section of spec.tree.children) {
    ids.push(section.id)
    if (!collapsed[section.id]) for (const paragraph of section.children) ids.push(paragraph.id)
  }
  return ids
}

/** How many sections and paragraphs link to each claim id. */
export function claimUsage(spec: PaperSpec): Record<string, number> {
  const usage: Record<string, number> = {}
  const visit = (node: Node) => {
    for (const id of node.claims) usage[id] = (usage[id] ?? 0) + 1
    node.children.forEach(visit)
  }
  spec.tree.children.forEach(visit)
  return usage
}

// ---------------------------------------------------------------------------
// Colors
// ---------------------------------------------------------------------------
// Tailwind only generates classes it can see written out in full in the
// source, so every class name below is spelled out literally.

/** Left color band and soft background per level ("color-coded by level"). */
export const LEVEL_STYLE: Record<NodeLevel, { band: string; tint: string; label: string }> = {
  message: { band: 'border-l-purple-500', tint: 'bg-purple-50', label: 'message' },
  claim: { band: 'border-l-amber-500', tint: 'bg-amber-50', label: 'claim' },
  section: { band: 'border-l-indigo-500', tint: 'bg-indigo-50/70', label: 'section' },
  paragraph: { band: 'border-l-teal-500', tint: 'bg-base-100', label: 'paragraph' },
}

const CLAIM_COLORS = [
  'bg-sky-100 text-sky-900 border-sky-300',
  'bg-rose-100 text-rose-900 border-rose-300',
  'bg-lime-100 text-lime-900 border-lime-300',
  'bg-violet-100 text-violet-900 border-violet-300',
  'bg-orange-100 text-orange-900 border-orange-300',
  'bg-teal-100 text-teal-900 border-teal-300',
  'bg-fuchsia-100 text-fuchsia-900 border-fuchsia-300',
  'bg-yellow-100 text-yellow-900 border-yellow-300',
]

/**
 * Chip colors for a claim id. "c1", "c2", ... take consecutive colors so
 * neighbours differ; any other id is hashed.
 */
export function claimColor(id: string): string {
  const numbered = /^c(\d+)$/.exec(id)
  let n: number
  if (numbered) {
    n = Number(numbered[1]) - 1
  } else {
    n = 0
    for (const ch of id) n = (n * 31 + ch.charCodeAt(0)) >>> 0
  }
  return CLAIM_COLORS[((n % CLAIM_COLORS.length) + CLAIM_COLORS.length) % CLAIM_COLORS.length]
}

/**
 * Human label for a `fields` key. The keys in paper.json stay as they are (the prompts
 * and every existing spec use them); only the words on screen change: `stakes` reads
 * "Why it matters" and `thesis` reads "Argument". Other keys: "figure_story" -> "figure story".
 */
const FIELD_LABELS: Record<string, string> = {
  stakes: 'Why it matters',
  thesis: 'Argument',
}

export function fieldLabel(key: string): string {
  return FIELD_LABELS[key] ?? key.replace(/_/g, ' ')
}
