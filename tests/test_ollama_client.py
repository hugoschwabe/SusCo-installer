import pytest

from susco.config import Config
from susco.ollama_client import OllamaClient, OllamaError


def test_consume_stream_collects_content_and_final_metadata():
    lines = [
        '{"message":{"thinking":"first "},"done":false}',
        '{"message":{"thinking":"thought","content":"The answer"},"done":false}',
        '{"message":{"content":" is 18."},"done":false}',
        (
            '{"message":{"content":""},"done":true,"done_reason":"stop",'
            '"eval_count":12,"prompt_eval_count":8,"total_duration":1000000000,'
            '"load_duration":10000000,"prompt_eval_duration":100000000,'
            '"eval_duration":800000000}'
        ),
    ]
    result = OllamaClient._consume_stream(
        lines, model="qwen3:1.7b", think=True, wall_start=0.0
    )
    assert result.thinking == "first thought"
    assert result.content == "The answer is 18."
    assert result.done
    assert result.eval_count == 12
    assert result.prompt_eval_count == 8
    assert result.time_to_first_token_s is not None
    assert result.tokens_per_second == pytest.approx(15.0)


def test_consume_stream_requires_done_event():
    with pytest.raises(OllamaError):
        OllamaClient._consume_stream(
            ['{"message":{"content":"x"},"done":false}'],
            model="m",
            think=False,
            wall_start=0.0,
        )


def test_payload_has_think_flag_and_cpu_option():
    client = OllamaClient(model="qwen3:1.7b")
    payload = client._payload("question", True, Config())
    assert payload["think"] is True
    assert payload["stream"] is True
    assert payload["options"]["num_gpu"] == 0
    assert payload["options"]["temperature"] == 0.0
    assert payload["messages"][-1]["content"] == "question"


def test_prompt_suffix_fallback_payload():
    client = OllamaClient(model="qwen3:1.7b")
    payload = client._payload("question", False, Config(), prompt_suffix=True)
    assert payload["messages"][-1]["content"].endswith("/no_think")
