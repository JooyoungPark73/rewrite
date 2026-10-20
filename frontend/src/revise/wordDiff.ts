// A small word-level diff (longest common subsequence over whitespace-separated tokens),
// enough for a paragraph against its rewrite. Returns runs of equal / removed / added words.
export interface DiffRun {
  kind: 'equal' | 'removed' | 'added'
  text: string
}

export function wordDiff(before: string, after: string): DiffRun[] {
  const a = before.split(/\s+/).filter(Boolean)
  const b = after.split(/\s+/).filter(Boolean)
  const n = a.length
  const m = b.length
  // lcs[i][j] = length of the LCS of a[i:] and b[j:]
  const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0))
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1 : Math.max(lcs[i + 1][j], lcs[i][j + 1])
    }
  }
  const runs: DiffRun[] = []
  const push = (kind: DiffRun['kind'], word: string) => {
    const last = runs[runs.length - 1]
    if (last && last.kind === kind) last.text += ' ' + word
    else runs.push({ kind, text: word })
  }
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      push('equal', a[i])
      i++
      j++
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      push('removed', a[i])
      i++
    } else {
      push('added', b[j])
      j++
    }
  }
  while (i < n) push('removed', a[i++])
  while (j < m) push('added', b[j++])
  return runs
}
