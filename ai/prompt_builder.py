"""Builds prompts sent to the local LLM (via Ollama) for invoice extraction."""
import json
from config.constants import EXTRACTION_SCHEMA

SYSTEM_PROMPT = """You are an expert invoice-data-extraction assistant.
You will be given raw OCR text from a scanned invoice. Extract the requested
fields as accurately as possible. Follow these rules strictly:

1. Respond with ONLY a single valid JSON object — no markdown, no commentary.
2. If a field is not present in the text, use null (or an empty list for line_items).
3. Numbers must be plain numeric values (no currency symbols, no commas).
4. Dates must be formatted as YYYY-MM-DD.
5. currency must be a 3-letter ISO 4217 code (e.g. USD, EUR, PHP). Infer it
   from currency symbols/context if not explicitly stated.
6. line_items is a list of objects: description, quantity, unit_price, amount.
7. Do not hallucinate values that are not supported by the text.
8. On receipts, several money amounts often appear close together (TOTAL,
   CASH/TENDERED, CHANGE, AMOUNT PAID). total_amount must ALWAYS be the
   TOTAL/AMOUNT DUE/AMOUNT PAYABLE — the actual price of the transaction.
   Never use CASH or TENDERED (what the customer handed over) or CHANGE
   (money handed back) as total_amount, even if that number is larger or
   appears more prominently on the receipt.
"""


def build_extraction_prompt(ocr_text: str) -> str:
    schema_str = json.dumps(EXTRACTION_SCHEMA, indent=2)
    return f"""{SYSTEM_PROMPT}

JSON schema to fill:
{schema_str}

OCR TEXT:
\"\"\"
{ocr_text}
\"\"\"

Return only the JSON object matching the schema above.
"""


def build_correction_prompt(ocr_text: str, previous_json: dict, issues: list[str]) -> str:
    """Used when validation finds problems and we want the LLM to self-correct."""
    return f"""{SYSTEM_PROMPT}

Your previous extraction had these issues:
{chr(10).join(f"- {i}" for i in issues)}

Previous JSON:
{json.dumps(previous_json, indent=2)}

Original OCR TEXT:
\"\"\"
{ocr_text}
\"\"\"

Fix the issues and return only the corrected JSON object.
"""
