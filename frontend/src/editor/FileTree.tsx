// FileTree: the .tex files of the paper, as resolved by the backend from the
// main file's \input / \include commands (GET /api/files).
//
// Files are listed depth-first starting at the main file and indented by how
// deep their include chain is (main.tex > sections/intro.tex > ...). A file
// whose parent is not in the list cannot be placed in the tree; it is shown
// unindented at the end with its parent's name as a faint hint.
// Clicking a file opens it in the editor.
import { useMemo } from 'react'
import type { FileEntry } from '../api/client'
import { useStore } from '../state/store'

interface TreeRow {
  path: string
  depth: number
  /** Parent name shown as a hint when the parent is not itself in the list. */
  hint: string | null
}

function basename(path: string): string {
  return path.split('/').pop() ?? path
}

/** Order files depth-first from the main file and compute indentation. */
function buildRows(files: FileEntry[], main: string | null): TreeRow[] {
  const known = new Set(files.map((f) => f.path))
  const children = new Map<string, string[]>()
  for (const f of files) {
    if (f.included_by && known.has(f.included_by) && f.included_by !== f.path) {
      const list = children.get(f.included_by) ?? []
      list.push(f.path)
      children.set(f.included_by, list)
    }
  }

  const rows: TreeRow[] = []
  const seen = new Set<string>()
  const visit = (path: string, depth: number) => {
    if (seen.has(path)) return // guards against include cycles
    seen.add(path)
    rows.push({ path, depth, hint: null })
    for (const child of children.get(path) ?? []) visit(child, depth + 1)
  }

  if (main && known.has(main)) visit(main, 0)
  // Roots other than main (no parent), then anything still unplaced.
  for (const f of files) if (!f.included_by) visit(f.path, 0)
  for (const f of files) {
    if (!seen.has(f.path)) {
      seen.add(f.path)
      rows.push({ path: f.path, depth: 0, hint: f.included_by ? basename(f.included_by) : null })
    }
  }
  return rows
}

export default function FileTree() {
  const files = useStore((s) => s.files)
  const mainFile = useStore((s) => s.mainFile)
  const openPath = useStore((s) => s.activeFile?.path ?? null)
  const openDirty = useStore((s) => s.activeFile?.dirty ?? false)
  const openFile = useStore((s) => s.openFile)

  const rows = useMemo(() => buildRows(files, mainFile), [files, mainFile])

  return (
    <nav className="flex h-full flex-col overflow-y-auto bg-base-200/60 py-2 text-sm" data-testid="file-tree">
      <div className="px-3 pb-1 text-xs font-semibold tracking-wide text-base-content/50 uppercase">Files</div>
      {rows.length === 0 && <div className="px-3 py-2 text-base-content/50">No files</div>}
      <ul>
        {rows.map((row) => {
          const active = row.path === openPath
          return (
            <li key={row.path}>
              <button
                type="button"
                data-path={row.path}
                title={row.path}
                onClick={() => void openFile(row.path)}
                className={`flex w-full items-center gap-1 truncate py-1 pr-2 text-left hover:bg-base-300 ${
                  active ? 'bg-base-300 font-medium' : ''
                }`}
                // 12px base padding plus 14px per include level.
                style={{ paddingLeft: 12 + row.depth * 14 }}
              >
                <span className="truncate">{basename(row.path)}</span>
                {row.path === mainFile && <span className="badge badge-xs badge-primary badge-soft">main</span>}
                {active && openDirty && <span className="text-warning" aria-label="unsaved changes">●</span>}
                {row.hint && <span className="truncate text-xs text-base-content/40">← {row.hint}</span>}
              </button>
            </li>
          )
        })}
      </ul>
    </nav>
  )
}
