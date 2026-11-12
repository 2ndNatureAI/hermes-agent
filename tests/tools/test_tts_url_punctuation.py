"""Bare links do not consume punctuation needed by generated speech."""
import pytest
from tools.tts_text_normalize import prepare_spoken_text


@pytest.mark.parametrize(('text', 'expected'), [
    ('Read https://example.com. Then continue.', 'Read. Then continue.'),
    ('Try https://example.com/search?q=one&sort=asc? Then decide.', 'Try? Then decide.'),
    ('See https://example.com/a_(b)). Next.', 'See). Next.'),
    ('Check https://example.com/path?! Then go.', 'Check?! Then go.'),
    ('See https://example.com/a_(b). Next.', 'See. Next.'),
    ('See https://example.com/search?q=one&sort=asc then decide.', 'See then decide.'),
    ('See (https://example.com/path). Next.', 'See (). Next.'),
    ('See https://example.com/a_(b))]. Next.', 'See)]. Next.'),
    ('Only https://example.com...', 'Only..'),
    ('Dr. Chen cited https://example.com. Next.', 'Dr. Chen cited. Next.'),
    ('Use [the guide](https://example.com). Next.', 'Use the guide. Next.'),
])
def test_sentence_punctuation_after_url(text, expected):
    assert prepare_spoken_text(text) == expected


def test_long_url_suffix_keeps_punctuation_and_closers():
    from tools.tts_text_normalize import strip_markdown_for_tts
    suffix = ')' * 2048 + '.' * 2048
    assert strip_markdown_for_tts('See https://example.com/path' + suffix) == 'See' + suffix
