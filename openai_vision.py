import base64
import json
import os
from typing import Any, Dict, List

from openai import OpenAI

# -------------------------------------------------
# Client
# -------------------------------------------------
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# -------------------------------------------------
# Environment-backed configuration
# -------------------------------------------------
DEFAULT_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4o")

# Vision detail must be: low | high | auto
_raw_detail = (os.getenv("OPENAI_VISION_DETAIL", "auto") or "").strip().lower()
if _raw_detail not in {"low", "high", "auto"}:
    _raw_detail = "auto"
DEFAULT_DETAIL = _raw_detail

# Max images to send
try:
    DEFAULT_MAX_IMAGES = int(os.getenv("OPENAI_VISION_MAX_IMAGES", "10"))
except ValueError:
    DEFAULT_MAX_IMAGES = 10

# -------------------------------------------------
# System prompt
# -------------------------------------------------
SYSTEM_PROMPT = """You are a reserve study assistant performing IMAGE-GROUNDED analysis.

CRITICAL RULES (follow strictly):
- You may ONLY identify components that are DIRECTLY VISIBLE in the provided image(s).
- You may estimate costs ONLY when the image provides sufficient visual evidence
  (e.g., visible material type, scale, repetition, extent across facade).
- Do NOT assume building age, number of buildings, or hidden systems.
- Do NOT include components that are not visible in the image frame.
- Do NOT use “typical” or “standard” costs without tying them to visible evidence.
- If estimation is coarse, lower confidence and explain why.
- Every estimated value MUST be justified by what is visible.

You are NOT a human inspector.
You are an image-based estimation assistant.

Return ONLY valid JSON (no markdown, no commentary) matching this exact shape:

{
  "components": [
    {
      "name": "Exterior masonry walls",
      "quantity": null,
      "current_replacement_cost": 120000,
      "useful_life_years": null,
      "remaining_life_years": null,
      "cycle_years": null,
      "confidence": 0.75,
      "evidence": "Multi-story masonry facade visible across entire image with widespread surface deterioration and patching"
    }
  ],
  "notes": "Only observations and estimates supported by visible evidence",
  "missing_info_questions": []
}

Guidelines:
- Costs should be rough, order-of-magnitude estimates in TODAY dollars.
- Use integers for costs and years.
- If cost cannot be reasonably estimated from the image, set it to null (not zero).
- Confidence reflects certainty of BOTH identification and estimation.


"""

# -------------------------------------------------
# Helpers
# -------------------------------------------------
def _b64_data_url(image_bytes: bytes, mime: str) -> str:
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:{mime};base64,{b64}"

def _extract_output_text(resp: Any) -> str:
    """
    Compatible with older SDK response shapes.
    """
    # Newer SDKs
    text = getattr(resp, "output_text", None)
    if text:
        return text

    # Older SDKs: walk the response
    try:
        output = resp.output  # type: ignore
        chunks = []
        for item in output:
            for c in getattr(item, "content", []) or []:
                if hasattr(c, "text") and c.text:
                    chunks.append(c.text)
        if chunks:
            return "\n".join(chunks)
    except Exception:
        pass

    raise RuntimeError("OpenAI response did not contain text output")

def _safe_json_parse(text: str) -> Dict[str, Any]:
    """
    Attempts strict JSON parse, then falls back to trimming noise.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            raise
        return json.loads(text[start : end + 1])

# -------------------------------------------------
# Main entry point
# -------------------------------------------------
def suggest_components_from_images(
    images: List[Dict[str, Any]],
    address_context: str = "",
    property_type_context: str = "",
) -> Dict[str, Any]:
    """
    images: list of {"bytes": b"...", "mime": "image/jpeg", "label": "Photo 1"}
    """

    content: List[Dict[str, Any]] = [
        {
            "type": "input_text",
            "text": (
                f"{SYSTEM_PROMPT}\n\n"
                f"Context:\n"
                f"- Address/Location: {address_context or 'Unknown'}\n"
                f"- Property type: {property_type_context or 'Unknown'}\n\n"
                f"Analyze the photos and respond with JSON only."
            ),
        }
    ]

    for img in images[:DEFAULT_MAX_IMAGES]:
        content.append(
            {
                "type": "input_image",
                "image_url": _b64_data_url(img["bytes"], img["mime"]),
                "detail": DEFAULT_DETAIL,
            }
        )

    resp = client.responses.create(
        model=DEFAULT_MODEL,
        input=[{"role": "user", "content": content}],
    )

    text = _extract_output_text(resp)
    data = _safe_json_parse(text)

    # Normalize expected fields
    if not isinstance(data, dict):
        data = {}

    if "components" not in data or not isinstance(data["components"], list):
        data["components"] = []

    if "missing_info_questions" in data and not isinstance(data["missing_info_questions"], list):
        data["missing_info_questions"] = []

    if "notes" in data and not isinstance(data["notes"], str):
        data["notes"] = str(data["notes"])

    return data

