// Retain the undated slice and the newest dated rows in the compact overview.
// The selectable tree keeps every row.
export function selectJourneyChartRows<T>(rows: T[], buckets: { label: string }[], limit: number): T[] {
  if (rows.length <= limit || limit <= 0) {
    return limit <= 0 ? [] : rows
  }

  if (limit === 1) {
    return rows.slice(-1)
  }

  if (buckets[0]?.label !== 'undated') {
    return rows.slice(-limit)
  }

  return [rows[0]!, ...rows.slice(-(limit - 1))]
}
