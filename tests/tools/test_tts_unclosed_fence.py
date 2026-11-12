"""Speech skips fenced code even when a response is truncated."""
import pytest
from tools.tts_text_normalize import prepare_spoken_text


@pytest.mark.parametrize(('text','expected'), [
    ('Answer first.\n```python\nprint(1)\n', 'Answer first.'),
    ('Answer first.\n```python\nprint(1)\n```\nNext step.', 'Answer first. Next step.'),
    ('Before.\n```js\nfirst()\n```\nBetween.\n```python\nsecond()', 'Before. Between.'),
    ('Answer first.\r\n```python\r\nprint(1)\r\n', 'Answer first.'),
    ('Before.\r\n```js\r\nx()\r\n```\r\nAfter.', 'Before. After.'),
    ('Inline `status` is useful.', 'Inline status is useful.'),
    ('Nothing after.\n```', 'Nothing after.'),
])
def test_code_fence_is_not_spoken(text, expected):
    assert prepare_spoken_text(text) == expected
