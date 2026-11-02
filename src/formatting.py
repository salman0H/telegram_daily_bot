"""Render a small Markdown subset to Telegram text/entities, measured in UTF-16."""
import re

TOKEN = re.compile(r"```(?:[^\n`]*\n)?([\s\S]*?)```|`([^`\n]+)`|\*\*([^*]+)\*\*")


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


def render(text):
    parts, entities, cursor, offset = [], [], 0, 0
    for match in TOKEN.finditer(text):
        before = text[cursor:match.start()]
        parts.append(before)
        offset += utf16_length(before)
        kind, content = next((kind, value) for kind, value in zip(
            ("pre", "code", "bold"), match.groups()) if value is not None)
        parts.append(content)
        length = utf16_length(content)
        if length:
            entities.append({"type": kind, "offset": offset, "length": length})
        offset += length
        cursor = match.end()
    parts.append(text[cursor:])
    return "".join(parts), entities


def split_message(text, limit=4000):
    """Split without truncation; intersect formatting spans with each chunk."""
    if limit < 2:
        raise ValueError("Message limit must be at least 2 UTF-16 units")
    text, entities = render(text)
    start, unit_start = 0, 0
    while start < len(text):
        end, length = start, 0
        while end < len(text):
            size = 2 if ord(text[end]) > 0xFFFF else 1
            if length + size > limit:
                break
            length += size
            end += 1
        if end < len(text):
            boundary = text.rfind("\n", start, end)
            if boundary >= start + (end - start) // 2:
                end = boundary + 1
                length = utf16_length(text[start:end])
        unit_end = unit_start + length
        chunk_entities = []
        for entity in entities:
            left = max(unit_start, entity["offset"])
            right = min(unit_end, entity["offset"] + entity["length"])
            if left < right:
                chunk_entities.append({"type": entity["type"], "offset": left - unit_start, "length": right - left})
        yield text[start:end], chunk_entities
        start, unit_start = end, unit_end
