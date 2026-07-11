"""LM Studio helper: generate Instagram comments from image + caption + username."""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import httpx
from openai import OpenAI

from config import API_KEY, BASE_URL, MODEL, ensure_llm_ready, load_env

load_env()

COMMENT_RULES = """
You write Instagram comments for Cognitive Technologies (CTech).
Your main purpose is to increase company presence in technology and related industries, among government entities and businesses.

Company text content is:
Problem
Solving information security problems for standardization in governance to achieve digital sovereignty.

Acronym Breakdown
Computing Technology for Enhancing Creativity and Happiness
Creating Technologies that Enhance Creations and Hobbies

Mission Statement
Cognitive Technologies (CTech) is a future-focused company empowering talent to drive cutting-edge research towards innovation and development of transformative technology. 

Vision
CTech provides services to business for development using information technology. We have a range of offerings for single users to large organizations. Beginning with AI powered personal companion, automated residential systems, Autonomous Enterprise, and Smart City Infrastructure. All of our operating principles are based on being a technology aggregator with sustainability in mind.

Taglines
Increasing harmony in society using technology and automation.
Man and machine in perfect harmony.
With sustainability, to singularity.


Reply with a single JSON object only. No markdown, no code fences, no extra text.
Exact shape:
{"relevant": true, "comment": "...", "reason": "short why"}

Rules:
- Keep comments positive, on-topic, 1-2 sentences
- If Arabic captions only → comment in Arabic
- If caption is in English or mixed → comment in English
- If irrelevant → {"relevant": false, "comment": "", "reason": "..."}

Content guidelines:
- UAE government / policy / strategy: CTech is proud of UAE 💛, we are working towards the same goals.
- Training / workshop / learning: CTech loves enhancing and training talent, as they are the future.
- Community awareness / social development: CTech loves increasing harmony in society using technology and automation.
- Popular big-tech business: comment on how CTech utilizes their technology.
- Anything else: relevant=false
"""


def _client() -> OpenAI:
    return OpenAI(base_url=BASE_URL, api_key=API_KEY or "lm-studio")


def _image_to_data_url(image: bytes | str | Path) -> str:
    if isinstance(image, (str, Path)):
        data = Path(image).read_bytes()
    else:
        data = image
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def generate_comment(
    *,
    username: str,
    caption: str,
    image: bytes | str | Path,
    model: str | None = None,
) -> dict:
    """
    Ask LM Studio for a comment given post username, caption, and image bytes/path.
    Returns {"relevant": bool, "comment": str, "reason": str}.
    """
    model_id = model or ensure_llm_ready(ping=False)
    user_text = (
        f"Post author: @{username.lstrip('@')}\n"
        f"Caption:\n{caption or '(no caption)'}\n\n"
        "Decide if this post matches CTech content rules and write the comment."
    )

    response = _client().chat.completions.create(
        model=model_id,
        messages=[
            {"role": "system", "content": COMMENT_RULES.strip()},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": _image_to_data_url(image)}},
                ],
            },
        ],
        temperature=0.3,
        max_tokens=600,
    )
    raw = (response.choices[0].message.content or "").strip()
    return _parse_json(raw)


def _strip_fences(text: str) -> str:
    text = text.strip()
    # Closed ```json ... ``` or bare ``` ... ```
    closed = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if closed:
        return closed.group(1).strip()
    # Unclosed fence (common when the model truncates)
    unclosed = re.match(r"```(?:json)?\s*([\s\S]*)", text, re.IGNORECASE)
    if unclosed:
        return unclosed.group(1).strip()
    return text


def _extract_object(text: str) -> str:
    start = text.find("{")
    if start == -1:
        return text
    depth = 0
    in_str = False
    escape = False
    for i, ch in enumerate(text[start:], start):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    # Truncated JSON — take from first { to last }
    end = text.rfind("}")
    if end > start:
        return text[start : end + 1]
    return text[start:]


def _parse_json(raw: str) -> dict:
    text = _extract_object(_strip_fences(raw))
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Last resort: greedy { ... } slice
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return {
                    "relevant": False,
                    "comment": "",
                    "reason": f"Invalid LLM JSON: {raw[:300]}",
                }
        else:
            return {
                "relevant": False,
                "comment": "",
                "reason": f"Invalid LLM JSON: {raw[:300]}",
            }
    return {
        "relevant": bool(data.get("relevant")),
        "comment": (data.get("comment") or "").strip(),
        "reason": (data.get("reason") or "").strip(),
    }
