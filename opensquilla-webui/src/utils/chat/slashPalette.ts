export interface SlashQueryRange { start: number; end: number; query: string }

export function shortSlashDescription(description: string, limit = 64): string {
  const firstLine = description.trim().split(/\r?\n/, 1)[0] || ''
  const sentence = firstLine.replace(/`/g, '').match(/^.*?(?:[。！？!?]|\.(?=\s|$)|$)/u)?.[0] || ''
  const characters = Array.from(sentence)
  return characters.length > limit ? characters.slice(0, limit - 1).join('').trimEnd() + '…' : sentence
}

/** Only a standalone slash token at the caret is a palette query. */
export function slashQueryAt(text: string, caret = text.length): SlashQueryRange | null {
  const before = text.slice(0, caret)
  const match = /(?:^|\s)\/([^\s/\\:]*)$/.exec(before)
  if (!match) return null
  const start = caret - match[1]!.length - 1
  // A caret in the middle of a URL/path must not turn its prefix into a query.
  const suffix = text.slice(caret).match(/^[^\s]*/)?.[0] || ''
  if (/[/\\:]/.test(suffix)) return null
  return { start, end: caret, query: match[1]! }
}

export function slashSearchRank(query: string, names: readonly string[], descriptions: readonly string[]): number {
  const needle = query.trim().toLocaleLowerCase()
  if (!needle) return 0
  const keys = names.map(name => name.replace(/^\//, '').toLocaleLowerCase())
  if (keys.some(key => key === needle)) return 0
  if (keys.some(key => key.startsWith(needle))) return 1
  if (keys.some(key => key.includes(needle))) return 2
  return descriptions.some(description => description.toLocaleLowerCase().includes(needle)) ? 3 : -1
}

export function replaceSlashQuery(text: string, range: SlashQueryRange, replacement = ''): string {
  return text.slice(0, range.start) + replacement + text.slice(range.end)
}
