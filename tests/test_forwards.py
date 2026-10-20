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


def test_v2_card_files_come_from_its_components():
    """Discord leaves a V2 card's attachments empty: the files are read off its File / gallery components."""
    from discord.components import _component_factory

    state = SimpleNamespace(http=None)
    card = _component_factory({"type": 17, "components": [
        {"type": 10, "content": "-# ↳ <@42> uploaded **latest.log**"},
        {"type": 13, "name": "latest.log", "size": 345000,
         "file": {"url": "https://cdn.discordapp.com/attachments/1/222/latest.log?ex=1", "attachment_id": "222"}},
        {"type": 12, "items": [
            {"media": {"url": "https://cdn.discordapp.com/attachments/1/333/shot.png", "content_type": "image/png"}},
            {"media": {"url": "https://example.com/not-ours.png"}},
        ]},
    ]}, state=state)
    message = SimpleNamespace(content="", attachments=[], components=[card], _state=state)
    assert [(a.id, a.filename, a.size) for a in full_attachments(message)] == [(222, "latest.log", 345000), (333, "shot.png", 0)]
    inline_forwards(message)
    assert message.content == ""
    assert [a.filename for a in message.attachments] == ["latest.log", "shot.png"]
