# Model Analysis Execution

`Analyzer` executes one explicit text or image analysis task. It does not know
about reports, BDD scenarios, knowledge cases, or Copilot workflows. Consumers
own prompts, evidence selection, redaction, business validation, and rendering.
The LLM integration owns service authentication and wire protocols.

## Configured Client

Use the existing `LLM_SETTING -> Settings` configuration chain. Creating an
Analyzer snapshots the selected connection settings but does not create a
client, validate its connection configuration, or send a request. The first
`analyze` call creates the client; subsequent calls reuse it without adding
conversation history.

```python
from autowork_core.inference import AnalysisRequest, Analyzer
from autowork_core.integrations.llm import ChatMessage
from config.settings import settings

with Analyzer.from_settings(settings) as analyzer:
    result = analyzer.analyze(AnalysisRequest(messages=(
        ChatMessage("system", "Answer briefly using only the supplied evidence."),
        ChatMessage("user", "Explain this explicitly selected observation."),
    )))
    print(result.data)
    print(result.response.model, result.response.usage)
```

Configuration changes after construction do not switch a running Analyzer to
another endpoint or credential. Create a new Analyzer to use new settings.
Closing an unused Analyzer does not create its client.

## Pictures and Structured Output

Images require `supports_images=True`, an explicit declaration by the caller
that the selected model accepts images. This is not model discovery or a server
capability probe. The consumer must authorize the actual images being sent;
Analyzer does not automatically redact screenshots or select local files.

```python
def require_summary(value):
    if not isinstance(value, dict) or not isinstance(value.get("summary"), str):
        raise ValueError("A summary string is required")


with Analyzer.from_settings(settings, supports_images=True) as analyzer:
    result = analyzer.analyze(AnalysisRequest(
        messages=(
            ChatMessage("system", 'Return only JSON with a "summary" string.'),
            ChatMessage("user", "Describe the picture.", images=("capture.png",)),
        ),
        output_format="json",
        validator=require_summary,
    ))
    print(result.data["summary"])
```

`output_format="json"` parses the complete response locally. It does not inject
instructions or send a provider-specific `response_format` parameter. Prompts
must request JSON explicitly. Markdown fences, non-JSON text, NaN and Infinity
are rejected rather than repaired. Any valid JSON value is allowed; a consumer
requiring an object or specific fields must supply its own validator.

A validator receives parsed JSON or the text string and accepts by returning
`None` or `True`. Returning `False`, another value, or raising an exception rejects
the answer. Validation is structural/business checking, not proof that the
model's reasoning is correct.

## Other Models and Resource Ownership

Provide either `Analyzer(client=existing_client)` or
`Analyzer(client_factory=create_selected_client)`. A client must implement
`complete(messages) -> LlmResponse` and `close()`. Other protocols can adapt to
this contract without subclassing the OpenAI-compatible `LlmClient`.

- An injected client is borrowed; its caller closes it.
- A factory-created client is owned and closed by Analyzer.
- Context manager exit closes owned resources, including on request errors.
- `close()` is idempotent; a closed Analyzer or LlmClient cannot be reused.
- Analyzer is synchronous and intended for sequential tasks, not concurrent use.
- Model selection is explicit: no hidden registry, routing, fallback, or retries.
- The current convenience constructor uses one existing Settings configuration;
  named model profiles are not implemented.

The LlmClient likewise closes only its own HTTP Session, not an injected Session.
It releases each HTTP response after parsing, including failed responses.

## Failure Contract

`AnalysisError` exposes `code` and `response` for empty answers, incomplete
responses, invalid JSON, or rejected validation. Error messages omit response
content, but the attached response is sensitive and must not be logged blindly.
Transport errors remain `LlmRequestError`; invalid caller input/configuration
raises `ValueError` or `TypeError`. Analyzer never retries automatically.

Only missing finish reasons or `stop` are accepted. Missing metadata is allowed
for compatible endpoints but cannot prove an answer was not truncated. Reasons
such as `length`, `content_filter`, or `tool_calls` are not completed analyses.

Connection/read timeouts are those of LlmClient; there is no whole-task deadline,
background worker, tool execution, file mutation, or automatic report integration.