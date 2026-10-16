from types import SimpleNamespace

from bulmaai.utils.forwards import forwarded_content, full_attachments, full_content, inline_forwards


def _forward(content="", attachments=()):
    snap = SimpleNamespace(message=SimpleNamespace(content="inner text", attachments=["inner.png"]))
    return SimpleNamespace(content=content, attachments=list(attachments), snapshots=[snap])


def test_forward_only_message_exposes_snapshot_text_and_files():
    message = _forward()
    assert full_content(message) == "inner text"
    assert forwarded_content(message) == "inner text"
    assert full_attachments(message) == ["inner.png"]


def test_own_text_and_files_come_first():
    message = _forward("look", ["mine.log"])
    assert full_content(message) == "look\ninner text"
    assert full_attachments(message) == ["mine.log", "inner.png"]


def test_plain_message_without_snapshots():
    message = SimpleNamespace(content="hi", attachments=[])
    assert full_content(message) == "hi"
    assert full_attachments(message) == []


def test_inline_forwards_folds_the_forward_into_the_message():
    message = _forward("look", ["mine.log"])
    inline_forwards(message)
    assert message.content == "look\n*Forwarded message:*\n> inner text"
    assert message.attachments == ["mine.log", "inner.png"]


def test_inline_forwards_leaves_plain_messages_alone():
    message = SimpleNamespace(content="hi", attachments=[])
    inline_forwards(message)
    assert message.content == "hi"
