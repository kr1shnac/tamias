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

- `--effort-mode {off,meter,apply}` (default: `off`) — the scope of the
  effort axis, independent of `--router-mode`. `off` computes nothing;
  `meter` records the effort the policy would have used (the
  `decision_target_effort` column) without changing the request; `apply`
  also sends it. `apply` is gated by `--registry`: an effort is sent only
  for a model the registry lists as supporting reasoning, and a level the
  model does not list is downgraded to the nearest one it does.
- `--registry PATH` (default: `$TAMIAS_REGISTRY`, else
  `~/.tamias/models.toml`) — the capability catalogue `apply` consults. The
  default path may be absent (empty registry, so nothing is applied); an
  explicitly named file must exist.
- `--effort-style {openrouter,openai}` (default: `openrouter`) — controls whether the proxy sets `reasoning.effort` (OpenRouter) or `reasoning_effort` (OpenAI) in upstream requests.

Effort switching is off by default, and `apply` stays opt-in until the
benchmark (see the project plan) measures it against a fixed-effort baseline;
`meter` is the evidence-gathering step on the way there.