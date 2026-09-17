import { describe, expect, it } from 'vitest'
import { replaceSlashQuery, slashQueryAt, slashSearchRank } from './slashPalette'

describe('slash palette queries', () => {
  it('replaces only the query at the caret, preserving the rest of a draft', () => {
    const text = '请分析 /表格 并保留摘要'
    const range = slashQueryAt(text, 7)!
    expect(range.query).toBe('表格')
    expect(replaceSlashQuery(text, range)).toBe('请分析  并保留摘要')
  })
  it.each(['https://example.org/x', '/usr/bin', 'C:\\work\\data', '//literal', './file', 'text /usr/bin'])('ignores path or URL %s', text => {
    expect(slashQueryAt(text)).toBeNull()
  })
  it('ignores a caret inside a path', () => {
    expect(slashQueryAt('/usr/bin', 4)).toBeNull()
  })
  it('ranks exact names, prefixes, contained names and bilingual descriptions', () => {
    expect(slashSearchRank('XLSX', ['xlsx'], [])).toBe(0)
    expect(slashSearchRank('spread', ['spreadsheets'], [])).toBe(1)
    expect(slashSearchRank('sheet', ['spreadsheets'], [])).toBe(2)
    expect(slashSearchRank('表格', ['xlsx'], ['创建与分析表格'])).toBe(3)
    expect(slashSearchRank('unknown', ['xlsx'], ['表格'])).toBe(-1)
  })
})
