import { describe, expect, it } from 'vitest'

import { summarizeToolRun, type ToolCallLike } from './run-summary'

function tool(toolName: string, args: Record<string, unknown> = {}, result?: unknown): ToolCallLike {
  return { args, result, toolCallId: `${toolName}-${Math.random()}`, toolName }
}

const read = (path: string) => tool('read_file', { path }, { content: '' })
const searched = (query: string) => tool('search_files', { query }, { hits: [] })
const webSearched = (query: string) => tool('web_search', { query }, { web: [] })
const webRead = (url: string) => tool('web_extract', { urls: [url] }, { results: [] })
const researched = (query: string) => tool('web_research', { query }, { sources: [] })
const ran = (command: string) => tool('terminal', { command }, { exit_code: 0 })

const settled = (tools: ToolCallLike[]) => summarizeToolRun(tools, false)
const running = (tools: ToolCallLike[]) => summarizeToolRun(tools, true)

// A run only ever holds ephemeral activity: reads, searches, commands. File
// edits and other cards are split out before a run is summarized, so there is
// no "Edited …" clause to test here — that work shows as its own diff card.
describe('summarizeToolRun', () => {
  it('names a lone target and counts the rest', () => {
    expect(settled([searched('toolRuns'), read('a.ts'), read('b.ts'), read('c.ts')])).toBe('Explored 4 files')
  })

  it('orders clauses explore then run regardless of call order', () => {
    expect(settled([ran('ls'), read('a.ts'), read('b.ts'), ran('pwd'), ran('id')])).toBe(
      'Explored 2 files, ran 3 commands'
    )
  })

  it('counts commands rather than naming them once they have run', () => {
    expect(settled([ran('git status')])).toBe('Ran 1 command')
    expect(settled([read('status.ts'), ran('a'), ran('b'), ran('c'), ran('d'), ran('e')])).toBe(
      'Explored status.ts, ran 5 commands'
    )
  })

  it('puts the running category in the present tense and leaves the rest past', () => {
    expect(running([read('a.ts'), tool('read_file', { path: 'b.ts' }), ran('x'), ran('y')])).toBe(
      'Exploring 2 files, ran 2 commands'
    )
  })

  it('names the command that is still running', () => {
    expect(running([tool('terminal', { command: 'npm run typecheck' })])).toMatch(/^Running /)
  })

  // Sequential calls leave a gap where the run is still going but nothing is
  // pending. Falling back to past tense there contradicted the ticker still
  // scrolling underneath, so the most recent call carries the present tense.
  it('stays in the present tense between two sequential calls', () => {
    expect(running([read('a.ts'), ran('x'), ran('y')])).toBe('Explored a.ts, running 2 commands')
  })

  // A turn can end — or the agent can simply move on — with a call that never
  // got a result. The run is history at that point and has to read as history,
  // or it narrates work that stopped happening and never offers its toggle.
  it('reads a run the turn left unresolved as finished', () => {
    expect(settled([read('a.ts'), tool('search_files', { query: 'toolRuns' })])).toBe('Explored 2 files')
  })

  // Web tools are not file exploration (Skappa 2026-09-10): a turn of web
  // searches used to summarize as "Explored N files" — wrong noun for work
  // that never touched the filesystem. They get their own clause, and
  // web_research reads as research, not search.
  it('summarizes web tools as web searches, not file exploration', () => {
    expect(settled([webSearched('wal'), webSearched('wal2'), webRead('https://x.dev')])).toBe(
      'Searched the web 3 web searches'
    )
  })

  it('keeps web and file clauses apart in one run', () => {
    expect(settled([read('a.ts'), webSearched('q'), ran('ls')])).toBe(
      'Explored a.ts, searched the web q, ran 1 command'
    )
  })

  it('narrates a live web research call as Researching', () => {
    expect(running([tool('web_research', { query: 'x vs y' })])).toBe('Researching x vs y')
  })

  it('settles a web research run in past tense', () => {
    expect(settled([researched('x vs y')])).toBe('Researched x vs y')
  })
})
