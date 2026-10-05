# Provider-reported cost

Tamias stores OpenRouter's provider-reported `usage.cost` as
`provider_cost_usd` when the response supplies a numeric USD value. It remains
SQL NULL when absent; NULL means UNKNOWN, never zero.

OpenRouter documents that `usage: {"include": true}` requests usage in the
response, and that it computes cost from provider token usage. Source:
[OpenRouter support: billing and usage](https://openrouter.ai/support/).

For streaming responses Tamias reads the final SSE usage object. OpenRouter's
free-model documentation says usage information arrives in the final chunk;
the image-generation API reference also explicitly documents `usage.cost` in a
completed streaming event. Sources:
[Free Models Router](https://openrouter.ai/openrouter/free/apps) and
[Image Generation](https://openrouter.ai/docs/guides/overview/multimodal/image-generation).

`tamias serve --request-usage-cost` is opt-in and adds `usage.include=true` to
the forwarded JSON. With the flag off, the body is forwarded byte-for-byte.
