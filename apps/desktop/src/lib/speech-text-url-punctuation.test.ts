import { describe, expect, it } from 'vitest'
import { sanitizeTextForSpeech } from './speech-text'

describe('URL punctuation in speech', () => {
  it.each([
    ['Read https://example.com. Then continue.', 'Read link. Then continue.'],
    ['Try https://example.com/search?q=one&sort=asc? Then decide.', 'Try link? Then decide.'],
    ['See https://example.com/a_(b). Next.', 'See link. Next.'],
    ['See https://example.com/a_(b))]. Next.', 'See link)]. Next.'],
    ['Check https://example.com/path?! Then go.', 'Check link?! Then go.'],
    ['Dr. Chen cited https://example.com. Next.', 'Dr. Chen cited link. Next.'],
    ['Use [the guide](https://example.com). Next.', 'Use the guide. Next.'],
    ['See https://example.com/search?q=one&sort=asc then decide.', 'See link then decide.']
  ])('keeps sentence cues in %s', (input, expected) => {
    expect(sanitizeTextForSpeech(input)).toBe(expected)
  })
  it('handles a bounded long suffix', () => {
    const suffix = ')'.repeat(2048) + '.'.repeat(2048)
    expect(sanitizeTextForSpeech('See https://example.com/path' + suffix)).toBe('See link' + suffix)
  })
})
