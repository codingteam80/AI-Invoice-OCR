import json

import pytest
import requests

from ai import extractor, llm_client, search_interpreter, vision_verifier
from config.settings import settings


class FakeResp:
    def __init__(self, status=200, body=None, text="", headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = text or json.dumps(self._body)
        self.headers = headers or {}
        self.ok = status < 400

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.HTTPError(f"{self.status_code}")
            err.response = self
            raise err


def chat_body(text, model="vendor/some-free-model:free", finish="stop"):
    return {
        "model": model,
        "choices": [{"message": {"content": text}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }


@pytest.fixture
def openrouter(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PROVIDER", "openrouter")
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setattr(settings, "OPENROUTER_MODEL", "openrouter/free")
    monkeypatch.setattr(settings, "OPENROUTER_MAX_RETRIES", 2)
    monkeypatch.setattr(settings, "LLM_FALLBACK_TO_OLLAMA", False)
    monkeypatch.setattr(settings, "OPENROUTER_TIMEOUT_RETRIES", 1)
    monkeypatch.setattr(settings, "OPENROUTER_RETRY_BUDGET_SECONDS", 300)
    monkeypatch.setattr("ai.llm_client.time.sleep", lambda s: None)


def test_extractor_uses_chat_completions_with_bearer_key(openrouter, monkeypatch):
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, payload=json, headers=headers)
        return FakeResp(body=chat_body('{"invoice_number": "A1"}'))

    monkeypatch.setattr(requests, "post", fake_post)
    trace = {}
    raw = extractor._call_ollama("hello", trace)
    assert json.loads(raw) == {"invoice_number": "A1"}
    assert seen["url"].endswith("/chat/completions")
    assert seen["headers"]["Authorization"] == "Bearer sk-or-test"
    assert seen["payload"]["model"] == "openrouter/free"
    assert seen["payload"]["response_format"] == {"type": "json_object"}
    assert seen["payload"]["max_tokens"] == settings.LLM_MAX_TOKENS
    assert trace["usage"]["routed_model"] == "vendor/some-free-model:free"
    assert trace["provider"] == "openrouter"


def test_429_is_retried_then_succeeds(openrouter, monkeypatch):
    responses = iter([FakeResp(429, text="slow down", headers={"Retry-After": "1"}), FakeResp(body=chat_body("{}"))])
    monkeypatch.setattr(requests, "post", lambda *a, **k: next(responses))
    assert extractor._call_ollama("x", {}) == "{}"


def test_json_mode_rejected_falls_back_without_response_format(openrouter, monkeypatch):
    payloads = []

    def fake_post(url, json=None, headers=None, timeout=None):
        payloads.append(dict(json))
        if "response_format" in json:
            return FakeResp(400, text='{"error":"response_format not supported"}')
        return FakeResp(body=chat_body("{}"))

    monkeypatch.setattr(requests, "post", fake_post)
    assert extractor._call_ollama("x", {}) == "{}"
    assert "response_format" in payloads[0] and "response_format" not in payloads[1]


def test_http_200_with_error_body_raises_extraction_error(openrouter, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(body={"error": {"message": "upstream down"}}))
    with pytest.raises(extractor.ExtractionError):
        extractor._call_ollama("x", {})


def test_missing_key_fails_clearly(openrouter, monkeypatch):
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "")
    with pytest.raises(extractor.ExtractionError) as e:
        extractor._call_ollama("x", {})
    assert "LLM_API_KEY" in str(e.value)


def test_timeout_is_reported(openrouter, monkeypatch):
    def boom(*a, **k):
        raise requests.exceptions.ReadTimeout("t")

    monkeypatch.setattr(requests, "post", boom)
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: (calls.append(1), boom())[1])
    with pytest.raises(extractor.ExtractionError) as e:
        extractor._call_ollama("x", {})
    assert "timed out" in str(e.value)
    assert len(calls) == 2  # first try + OPENROUTER_TIMEOUT_RETRIES


def test_vision_sends_image_data_url(openrouter, monkeypatch):
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(payload=json, headers=headers)
        return FakeResp(body=chat_body('{"mismatches":[]}'))

    monkeypatch.setattr(requests, "post", fake_post)
    out = vision_verifier._post_generate("check", "iVBORw0KGgo=", {})
    assert out == '{"mismatches":[]}'
    parts = seen["payload"]["messages"][0]["content"]
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert seen["headers"]["Authorization"].startswith("Bearer ")
    assert vision_verifier._post_chat("check", "iVBORw0KGgo=", {}) is None  # no duplicate request


def test_search_interpreter_uses_openrouter(openrouter, monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(body=chat_body("{}")))
    monkeypatch.setattr(search_interpreter, "_build_prompt", lambda *a, **k: "p")
    monkeypatch.setattr(search_interpreter, "normalize_search_plan", lambda parsed, page: ({"ok": 1}, []))
    monkeypatch.setattr(search_interpreter, "_augment_explicit_presence", lambda q, p, plan: plan)
    plan, warnings = search_interpreter.interpret_natural_language_search("any", page="history")
    assert plan == {"ok": 1}


def test_provider_defaults_follow_key_presence():
    # Mirrors the expression in config/settings.py
    pick = lambda prov, key: (prov or ("openrouter" if key.strip() else "ollama")).strip().lower()
    assert pick("", "sk-or-x") == "openrouter"
    assert pick("", "") == "ollama"
    assert pick("ollama", "sk-or-x") == "ollama"


# ---------------------------------------------------------------- fallback
OLLAMA_OK = {"response": '{"from": "ollama"}'}


@pytest.fixture
def with_fallback(openrouter, monkeypatch):
    monkeypatch.setattr(settings, "LLM_FALLBACK_TO_OLLAMA", True)
    monkeypatch.setattr(settings, "OPENROUTER_COOLDOWN_SECONDS", 600)
    monkeypatch.setattr(settings, "OPENROUTER_SHORT_COOLDOWN_SECONDS", 60)


def router(openrouter_resp, ollama_resp=None, calls=None):
    """Fake requests.post dispatching on URL and recording which backend ran."""
    calls = calls if calls is not None else []

    def fake_post(url, json=None, headers=None, timeout=None):
        if "openrouter" in url:
            calls.append("openrouter")
            if isinstance(openrouter_resp, Exception):
                raise openrouter_resp
            return openrouter_resp
        calls.append("ollama")
        if isinstance(ollama_resp, Exception):
            raise ollama_resp
        return ollama_resp if ollama_resp is not None else FakeResp(body=OLLAMA_OK)

    return fake_post, calls


def test_fallback_on_402_uses_ollama_and_skips_openrouter_afterwards(with_fallback, monkeypatch):
    fake, calls = router(FakeResp(402, text="insufficient credits"))
    monkeypatch.setattr(requests, "post", fake)
    trace = {}
    assert json.loads(extractor._call_ollama("x", trace)) == {"from": "ollama"}
    assert calls == ["openrouter", "ollama"]
    assert trace["fallback_from"]["provider"] == "openrouter"
    # cooldown: next request goes straight to Ollama
    extractor._call_ollama("y", {})
    assert calls == ["openrouter", "ollama", "ollama"]
    assert llm_client.pick_provider() == "ollama"


def test_fallback_after_429_exhausts_retries(with_fallback, monkeypatch):
    fake, calls = router(FakeResp(429, text="limit"))
    monkeypatch.setattr(requests, "post", fake)
    assert json.loads(extractor._call_ollama("x", {})) == {"from": "ollama"}
    assert calls.count("openrouter") == 3 and calls[-1] == "ollama"  # 1 + 2 retries, then local


def test_timeout_is_retried_once_then_falls_back(with_fallback, monkeypatch):
    fake, calls = router(requests.exceptions.ReadTimeout("t"))
    monkeypatch.setattr(requests, "post", fake)
    assert json.loads(extractor._call_ollama("x", {})) == {"from": "ollama"}
    assert calls == ["openrouter", "openrouter", "ollama"]


def test_fallback_when_key_missing(with_fallback, monkeypatch):
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "")
    fake, calls = router(FakeResp(body=chat_body("{}")))
    monkeypatch.setattr(requests, "post", fake)
    assert json.loads(extractor._call_ollama("x", {})) == {"from": "ollama"}
    assert calls == ["ollama"]


def test_both_failing_reports_both_errors(with_fallback, monkeypatch):
    fake, calls = router(FakeResp(402, text="no credits"), requests.exceptions.ConnectionError("refused"))
    monkeypatch.setattr(requests, "post", fake)
    with pytest.raises(extractor.ExtractionError) as e:
        extractor._call_ollama("x", {})
    msg = str(e.value)
    assert "LLM API" in msg and "failed" in msg and "Ollama fallback also failed" in msg


def test_openrouter_tried_again_after_cooldown_expires(with_fallback, monkeypatch):
    fake, calls = router(FakeResp(402, text="no credits"))
    monkeypatch.setattr(requests, "post", fake)
    extractor._call_ollama("x", {})
    monkeypatch.setattr(llm_client, "_cooldown_until", 0.0)
    extractor._call_ollama("y", {})
    assert calls == ["openrouter", "ollama", "openrouter", "ollama"]


def test_fallback_disabled_raises_and_never_touches_ollama(openrouter, monkeypatch):
    fake, calls = router(FakeResp(402, text="no credits"))
    monkeypatch.setattr(requests, "post", fake)
    with pytest.raises(extractor.ExtractionError):
        extractor._call_ollama("x", {})
    assert calls == ["openrouter"]


def test_vision_falls_back_to_ollama(with_fallback, monkeypatch):
    fake, calls = router(FakeResp(402, text="no credits"), FakeResp(body={"response": '{"mismatches":[]}'}))
    monkeypatch.setattr(requests, "post", fake)
    trace = {}
    assert vision_verifier._post_generate("p", "iVBORw0KGgo=", trace) == '{"mismatches":[]}'
    assert calls == ["openrouter", "ollama"]
    assert trace["fallback_from"]["provider"] == "openrouter"


def test_search_falls_back_to_ollama(with_fallback, monkeypatch):
    fake, calls = router(FakeResp(429, text="limit"))
    monkeypatch.setattr(requests, "post", fake)
    monkeypatch.setattr(search_interpreter, "_build_prompt", lambda *a, **k: "p")
    monkeypatch.setattr(search_interpreter, "normalize_search_plan", lambda parsed, page: ({"p": parsed}, []))
    monkeypatch.setattr(search_interpreter, "_augment_explicit_presence", lambda q, p, plan: plan)
    plan, _ = search_interpreter.interpret_natural_language_search("any", page="history")
    assert plan == {"p": {"from": "ollama"}}
    assert calls[-1] == "ollama"


# ------------------------------------------------------------------ retries
def sequence(*items):
    """requests.post stub returning/raising the given items in order."""
    items = list(items)
    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(url)
        item = items.pop(0) if len(items) > 1 else items[0]
        if isinstance(item, Exception):
            raise item
        return item

    return fake_post, calls


def test_500_and_504_are_retried(openrouter, monkeypatch):
    fake, calls = sequence(FakeResp(500, text="oops"), FakeResp(504, text="gw"), FakeResp(body=chat_body('{"a":1}')))
    monkeypatch.setattr(requests, "post", fake)
    trace = {}
    assert extractor._call_ollama("x", trace) == '{"a":1}'
    assert len(calls) == 3
    assert trace["usage"]["retries"] == ["attempt 1: HTTP 500", "attempt 2: HTTP 504"]


def test_connection_error_is_retried(openrouter, monkeypatch):
    fake, calls = sequence(requests.exceptions.ConnectionError("reset"), FakeResp(body=chat_body("{}")))
    monkeypatch.setattr(requests, "post", fake)
    assert extractor._call_ollama("x", {}) == "{}"
    assert len(calls) == 2


def test_empty_answer_is_retried(openrouter, monkeypatch):
    fake, calls = sequence(FakeResp(body=chat_body("")), FakeResp(body=chat_body('{"ok":true}')))
    monkeypatch.setattr(requests, "post", fake)
    assert json.loads(extractor._call_ollama("x", {})) == {"ok": True}
    assert len(calls) == 2


def test_invalid_json_from_one_free_model_is_retried(openrouter, monkeypatch):
    fake, calls = sequence(FakeResp(body=chat_body("Sure! Here is the invoice...", model="a/chatty:free")),
                           FakeResp(body=chat_body('{"invoice_number":"Z9"}', model="b/obedient:free")))
    monkeypatch.setattr(requests, "post", fake)
    trace = {}
    assert json.loads(extractor._call_ollama("x", trace)) == {"invoice_number": "Z9"}
    assert trace["usage"]["routed_model"] == "b/obedient:free"
    assert len(calls) == 2


def test_http_200_error_body_is_retried(openrouter, monkeypatch):
    fake, calls = sequence(FakeResp(body={"error": {"message": "upstream"}}), FakeResp(body=chat_body("{}")))
    monkeypatch.setattr(requests, "post", fake)
    assert extractor._call_ollama("x", {}) == "{}"
    assert len(calls) == 2


def test_permanent_errors_are_not_retried(openrouter, monkeypatch):
    for status in (401, 402, 403, 404):
        fake, calls = sequence(FakeResp(status, text="nope"))
        monkeypatch.setattr(requests, "post", fake)
        with pytest.raises(extractor.ExtractionError):
            extractor._call_ollama("x", {})
        assert len(calls) == 1, status


def test_retries_are_capped(openrouter, monkeypatch):
    fake, calls = sequence(FakeResp(503, text="busy"))
    monkeypatch.setattr(requests, "post", fake)
    with pytest.raises(extractor.ExtractionError):
        extractor._call_ollama("x", {})
    assert len(calls) == settings.OPENROUTER_MAX_RETRIES + 1


def test_retry_budget_stops_early(openrouter, monkeypatch):
    monkeypatch.setattr(settings, "OPENROUTER_MAX_RETRIES", 10)
    monkeypatch.setattr(settings, "OPENROUTER_RETRY_BUDGET_SECONDS", 0)
    fake, calls = sequence(FakeResp(503, text="busy"))
    monkeypatch.setattr(requests, "post", fake)
    with pytest.raises(extractor.ExtractionError):
        extractor._call_ollama("x", {})
    assert len(calls) == 1


def test_exhausted_bad_json_fails_over_for_initial_but_not_corrections(with_fallback, monkeypatch):
    fake, calls = router(FakeResp(body=chat_body("not json at all")))
    monkeypatch.setattr(requests, "post", fake)
    # initial extraction: strict -> after retries, fail over to Ollama
    assert json.loads(extractor._call_ollama("x", {"kind": "initial"})) == {"from": "ollama"}
    assert calls.count("openrouter") == settings.OPENROUTER_MAX_RETRIES + 1 and calls[-1] == "ollama"
    # correction: lenient -> returns the text so the caller keeps its last good extraction
    llm_client.reset_cooldown()
    calls.clear()
    assert extractor._call_ollama("x", {"kind": "correction"}) == "not json at all"
    assert "ollama" not in calls


# ------------------------------------------- local Ollama path is unchanged
def test_ollama_text_request_is_exactly_the_original_payload(monkeypatch):
    seen = {}

    def fake_post(url, json=None, timeout=None, **kw):
        seen.update(url=url, payload=json, timeout=timeout, extra=kw)
        return FakeResp(body={"response": "{}"})

    monkeypatch.setattr(requests, "post", fake_post)
    trace = {}
    assert extractor._call_ollama("PROMPT", trace) == "{}"
    assert seen["url"] == f"{settings.OLLAMA_HOST}/api/generate"
    assert seen["payload"] == {
        "model": settings.OLLAMA_MODEL, "prompt": "PROMPT", "stream": False, "format": "json",
        "keep_alive": settings.OLLAMA_KEEP_ALIVE, "options": {"temperature": settings.LLM_TEMPERATURE, "num_predict": settings.LLM_MAX_TOKENS, "num_ctx": settings.LLM_CONTEXT_TOKENS},
    }
    assert seen["timeout"] == settings.OLLAMA_TIMEOUT_SECONDS and seen["extra"] == {}
    assert trace["provider"] == "ollama" and "fallback_from" not in trace


def test_ollama_timeout_behaviour_and_messages_are_the_original(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: (calls.append(1), (_ for _ in ()).throw(requests.exceptions.ReadTimeout("t")))[1])
    monkeypatch.setattr(settings, "OLLAMA_TIMEOUT_RETRIES", 1)
    with pytest.raises(extractor.ExtractionError) as e:
        extractor._call_ollama("x", {})
    assert len(calls) == 2
    assert "Ollama request timed out after 2 attempt(s)" in str(e.value)
    assert f"configured request limit={settings.OLLAMA_TIMEOUT_SECONDS}s" in str(e.value)


def test_ollama_search_request_is_exactly_the_original_payload(monkeypatch):
    seen = {}

    def fake_post(url, json=None, timeout=None, **kw):
        seen.update(url=url, payload=json, timeout=timeout, extra=kw)
        return FakeResp(body={"response": "{}"})

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(search_interpreter, "_build_prompt", lambda *a, **k: "P")
    monkeypatch.setattr(search_interpreter, "normalize_search_plan", lambda parsed, page: ({}, []))
    monkeypatch.setattr(search_interpreter, "_augment_explicit_presence", lambda q, p, plan: plan)
    search_interpreter.interpret_natural_language_search("any", page="history")
    assert seen["payload"] == {
        "model": settings.SEARCH_LLM_MODEL, "prompt": "P", "stream": False, "format": "json",
        "keep_alive": settings.OLLAMA_KEEP_ALIVE, "options": {"temperature": 0.0},
    }
    assert seen["timeout"] == settings.SEARCH_LLM_TIMEOUT_SECONDS and seen["extra"] == {}


def test_ollama_vision_request_is_exactly_the_original_payload(monkeypatch):
    seen = {}

    def fake_post(url, json=None, timeout=None, **kw):
        seen.update(url=url, payload=json, timeout=timeout, extra=kw)
        return FakeResp(body={"response": "{}"})

    monkeypatch.setattr(requests, "post", fake_post)
    assert vision_verifier._post_generate("P", "IMG", {}) == "{}"
    assert seen["url"] == f"{settings.OLLAMA_HOST}/api/generate"
    assert seen["payload"] == {
        "model": settings.VISION_MODEL, "prompt": "P", "images": ["IMG"], "stream": False, "format": "json",
        "keep_alive": settings.OLLAMA_KEEP_ALIVE,
        "options": {"temperature": settings.LLM_TEMPERATURE, "num_ctx": settings.VISION_NUM_CTX},
    }
    assert seen["timeout"] == settings.VISION_TIMEOUT_SECONDS and seen["extra"] == {}


def test_ollama_provider_never_contacts_openrouter(monkeypatch):
    urls = []
    monkeypatch.setattr(requests, "post", lambda url, **k: (urls.append(url), FakeResp(body={"response": "{}"}))[1])
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "sk-or-present-but-unused")
    extractor._call_ollama("x", {})
    assert urls and all("openrouter" not in u for u in urls)
