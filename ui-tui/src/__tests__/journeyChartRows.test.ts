import { describe, expect, it } from 'vitest'

import { selectJourneyChartRows } from '../lib/journeyChartRows.js'

describe('journey chart overview', () => {
  it('keeps undated and the newest dated rows within the limit', () => {
    const rows = ['unknown', ...Array.from({ length: 9 }, (_, i) => `dated-${i}`)]
    const buckets = rows.map((_, i) => ({ label: i === 0 ? 'undated' : 'dated' }))
    expect(selectJourneyChartRows(rows, buckets, 8)).toEqual([
      'unknown', 'dated-2', 'dated-3', 'dated-4', 'dated-5', 'dated-6', 'dated-7', 'dated-8'
    ])
  })

  it('preserves all rows that fit and uses the latest rows without undated', () => {
    expect(selectJourneyChartRows(['one', 'two'], [{ label: 'undated' }, { label: 'dated' }], 8)).toEqual(['one', 'two'])
    expect(selectJourneyChartRows(['one', 'two', 'three'], [{ label: 'dated' }], 2)).toEqual(['two', 'three'])
    expect(selectJourneyChartRows(['one'], [{ label: 'undated' }], 0)).toEqual([])
    expect(selectJourneyChartRows(['one', 'two'], [{ label: 'undated' }, { label: 'dated' }], 1)).toEqual(['two'])
  })
})
