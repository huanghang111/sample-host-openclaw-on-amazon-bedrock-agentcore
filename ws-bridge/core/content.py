"""Content block extraction — extract text from nested JSON content blocks.

Ported from dingtalk-bridge/bridge.py _extract_text_from_content_blocks().
Subagent responses (deep-research-pro, task-decomposer) can wrap content
multiple levels deep as [{type:"text", text:"..."}] JSON arrays.
"""

import json
import re


def extract_text_from_content_blocks(text: str) -> str:
    """Recursively unwrap nested [{type:"text", text:"..."}] JSON structures."""
    if not text or not isinstance(text, str):
        return text
    result = text
    decoder = json.JSONDecoder(strict=False)
    for _ in range(10):
        prev = result
        rebuilt = []
        i = 0
        while i < len(result):
            pos = result.find("[{", i)
            if pos == -1:
                rebuilt.append(result[i:])
                break
            rebuilt.append(result[i:pos])
            try:
                blocks, end = decoder.raw_decode(result, pos)
                if isinstance(blocks, list) and blocks and all(isinstance(b, dict) for b in blocks):
                    has_typed = any(b.get("type") for b in blocks)
                    if has_typed:
                        parts = [b.get("text", "") for b in blocks
                                 if isinstance(b, dict) and b.get("type") == "text"]
                        rebuilt.append("".join(parts))
                        i = end
                        continue
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
            remainder = result[pos:]
            if re.match(r'^\[\{\s*"', remainder) or remainder.strip() == "[{":
                break
            rebuilt.append("[")
            i = pos + 1
        result = "".join(rebuilt)
        if result == prev:
            break
    return result
