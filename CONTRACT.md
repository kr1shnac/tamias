PURPOSE: local HTTP proxy between any coding agent and an OpenAI-compatible chat API. It forwards requests, logs token usage and cost, and in shadow mode records the routing decision it WOULD make. It never stores prompt or response text.
TYPES (src/tamias/types.py, frozen dataclasses):
- Usage(input_tokens:int|None, output_tokens:int|None, cached_input_tokens:int|None, cache_write_tokens:int|None, cache_write_1h_tokens:int|None=None)  # None = UNKNOWN; input_tokens = TOTAL input tokens including cached and cache-write tokens
- CostBreakdown(usd:float|None, formula:str, price_sheet_date:str)
- Decision(action:Literal["STAY","SWITCH"], target_model:str|None, reason:str)
- SessionState(session_id:str, request_index:int, current_model:str)
FUNCTIONS:
- pricing.load_price_sheet(path)->PriceSheet; pricing.compute_cost(model, usage, sheet)->CostBreakdown (usd=None if model or any needed field is None; unknown is NEVER treated as 0). compute_cost rules: 0. model not in sheet -> usd=None. 1. If every price of the model is 0 -> usd=0.0, formula "free model: all prices are 0", whatever the usage. 2. A token field is NEEDED only if its price is nonzero. Needed field is None -> usd=None, and formula lists the missing fields. Price 0 contributes 0. 3. If cached_input_tokens is None and cached_input price != input price -> usd=None (missing cached_input_tokens). If prices are equal, treat cached as 0. 4. uncached_input = input_tokens - cached - (cache_write or 0). If any value is negative or cached+write > input -> usd=None, formula "inconsistent usage". 5. 5m_writes = cache_write_tokens - (cache_write_1h_tokens or 0). cost = (uncached*input + cached*cached_input + 5m_writes*cache_write + 1h_writes*cache_write_1h + output*output)/1e6. 6. formula = readable text with the numbers substituted.
- store.Store(db_path) with .log_request(ts, session_id, model_requested, model_used, usage, cost, latency_ms, status, decision) and .rows()
- router.decide(body:dict, state:SessionState)->Decision (pure, never mutates body)
- proxy.create_app(upstream_url, store, sheet, router_mode)->FastAPI app
RULES: tests never use the network; request body is forwarded byte-identical unless router_mode=="active"; no prompt or response text is ever stored; type hints everywhere; ruff clean.
