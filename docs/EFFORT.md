# Reasoning-Effort Switching

This feature adds optional `reasoning.effort` (OpenRouter style) or `reasoning_effort` (OpenAI style) to chat completions request bodies, allowing the router to control how much effort the upstream model spends on internal reasoning.

## OpenRouter Documentation

- **Request field**: `"reasoning": {"effort": "low"|"medium"|"high"}` — documented at https://openrouter.ai/docs/parameters under "Reasoning"
- **OpenAI-style alternative**: `"reasoning_effort"` — documented at https://openrouter.ai/docs/parameters under "Reasoning Effort", enum `(xhigh, high, medium, low, minimal, none)`
- **Model support**: Determined by `supported_parameters` in the model list API. Models that support reasoning have a `reasoning` config field with `supported_efforts` listing allowed values (e.g. `["high", "medium", "low", "minimal"]). See https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties

## Fields Verified

| Field | Source | Verified |
|-------|--------|----------|
| `reasoning.effort` | OpenRouter "Reasoning" parameter | yes |
| `reasoning_effort` | OpenRouter "Reasoning Effort" parameter | yes |
| `supported_parameters` | Model list API | yes |

## Fields UNVERIFIED

- End-to-end effort switching with real OpenRouter models (not measured here)
- Provider-specific behavior when `reasoning.effort` is sent to models that do not support it

## Warning

- Providers may ignore the `reasoning.effort` or `reasoning_effort` field entirely.
- Effort changes may invalidate the prompt cache on some providers (not measured here).
- Effort switching has NOT been demonstrated end-to-end.

## CLI Flags

- `--effort-policy {off,easy-low}` (default: `off`) — enables effort-based routing decisions in the router.
- `--effort-style {openrouter,openai}` (default: `openrouter`) — controls whether the proxy sets `reasoning.effort` (OpenRouter) or `reasoning_effort` (OpenAI) in upstream requests.