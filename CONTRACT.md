PURPOSE: local HTTP proxy between coding agents and OpenAI/Anthropic/Responses-compatible upstreams. It forwards requests, logs token usage and cost, and can route models in shadow/active modes. It never stores prompt or response text.
TYPES (src/tamias/types.py, frozen dataclasses):
- Usage(input_tokens:int|None, output_tokens:int|None, cached_input_tokens:int|None, cache_write_tokens:int|None, cache_write_1h_tokens:int|None=None, provider_cost_usd:float|None=None)  # None = UNKNOWN; input_tokens = TOTAL input tokens including cached and cache-write tokens
- CostBreakdown(usd:float|None, formula:str, price_sheet_date:str, source:Literal["computed","unknown"]="computed")
- Decision(action:Literal["STAY","SWITCH"], target_model:str|None, reason:str, target_effort:str|None=None, persisted_only:bool=False)
- SessionState(session_id:str, request_index:int, current_model:str)
- ToolClass = Literal["read","edit","shell","unknown"]
FUNCTIONS:
- pricing.load_price_sheet(path)->PriceSheet; pricing.compute_cost(model, usage, sheet)->CostBreakdown (usd=None if model or any needed field is None; unknown is NEVER treated as 0). compute_cost follows the rules in src/tamias/pricing.py (cached/uncached/write rules; formula always present; source marks computed/unknown).
- store.Store(db_path) with .log_request(... ) and .rows() / session tracking (see src/tamias/store.py)
- router.decide(body:dict, state:SessionState, ...)->Decision (pure where possible; see src/tamias/router.py)
- proxy.build_serve_app(...)->FastAPI app (see src/tamias/proxy.py)
RULES: tests never use the network; request body is forwarded byte-identical unless routing rewrites for active mode; no prompt or response text is ever stored; type hints used throughout (enforcement via CI/checks evolving); ruff clean.
