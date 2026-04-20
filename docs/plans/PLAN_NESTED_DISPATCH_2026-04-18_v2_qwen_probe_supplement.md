# PLAN — Nested Dispatch v2 qwen probe supplement

**Date:** 2026-04-20 (probes executed same day)
**Supplements:** Appendix A of `PLAN_NESTED_DISPATCH_2026-04-18_v2_probe_supplement.md` (host setup + three call patterns). This document complements Appendix A with empirical measurements against the live Windows-host Ollama + `qwen2.5-coder:14b-instruct-q4_K_M` instance, and extracts the adapter-level findings needed to de-risk TASK-004's `local-llm` backend.
**Driver:** Before committing the `local-llm` adapter spec to code, we need signal on four questions Appendix A left abstract: does schema-constrained decode actually produce schema-valid output, does tool-calling work, how does Ollama bill / cache prompt tokens, and what does the error surface look like under an SDK like `openai`.

---

## Purpose

Capture the eight probes executed against the running Ollama host so that the `local-llm` adapter can be built without re-discovering server behavior. Findings focus on the three primitives the adapter exposes: **structured output**, **tool-use**, and **observability** (latency, tokens, failure modes).

## TL;DR

All blocker probes came back green for the **structured-output** path. Tool-calling is **out of scope for v1** as v2 §7.3 already anticipated; qwen emits well-formed tool-call JSON but neither Ollama's OpenAI-compat shim nor the native client parses it into structured `tool_calls`, so the adapter would have to sniff content-inline JSON to use tools — not worth it for v1. Context overflow is silently clamped.

- **Schema-constrained decode (`format=<schema>` on native ollama client) is the right default path.** Four back-to-back runs against a representative reviewer_report schema produced 4/4 schema-valid outputs with zero retry. Do **not** layer the v2 §8.3 "retry once on invalid" loop on top of this path — it's belt-and-braces but will never fire in practice.
- **OpenAI `response_format={"type":"json_object"}` works but only guarantees *some* JSON.** Schema conformance is not enforced at decode time on this endpoint; caller must still validate. Keep it as a second-tier fallback when the adapter is pointed at an OpenAI-compatible server that isn't Ollama.
- **Retry amplification by the `openai` SDK.** Declared `timeout=0.4` yielded an `APITimeoutError` at 2.56 seconds wall; `timeout=5` on an unreachable port took 16.4s. The SDK silently retries twice by default. The adapter MUST set `max_retries=0` when constructing the `OpenAI` client, else the manifest's `default_timeout_sec` is a lie.
- **Context overflow is silent.** A 67.5k-token blob with `num_ctx=32768` returned HTTP 200 with `prompt_eval_count=32768` — Ollama dropped the overflow without surfacing an error. Adapter must pre-count input tokens or declare a hard upper bound.
- **Generation rate:** ~28 tokens/s on the deployed host for this quantization. A 145-token reviewer report lands in ~5s of eval + 0.6–1.1s of prompt eval; the manifest `default_timeout_sec: 300` is comfortable but not luxurious.
- **Server-side KV cache is real but not exposed.** Prompt-eval latency drops ~47% on the second call with the same system prompt (1146ms → 592ms). No `cache_creation` / `cache_read` counter analogous to Anthropic's — the only observability is `prompt_eval_duration`. Adapter cannot report a TTL; just log durations and let operators infer.

Net recommendation: build TASK-004's `local-llm` adapter around the **native `ollama` client + `format=<schema>`** path. Keep the OpenAI-compat path wired as a second backend kind for non-Ollama endpoints (vLLM, TGI, LM Studio), but treat it as JSON-mode only, with post-hoc schema validation. Tool-use stays explicitly out of scope.

---

## Host state (delta to Appendix A)

Reusing the host described in `PLAN_NESTED_DISPATCH_2026-04-18_v2_probe_supplement.md` Appendix A. Reconfirmed this run:

- `OLLAMA_HOST=http://172.22.192.1:11434` (from `~/.bashrc` default-route resolution; gateway has not moved since last session).
- `GET /api/version` → `{"version":"0.20.7"}`.
- `GET /api/tags` shows both models resident: `qwen2.5-coder:14b-instruct-q4_K_M` (8.99 GB), `lfm2.5-thinking:latest` (0.73 GB).
- All probes target qwen2.5-coder.

**Model metadata newly captured (`POST /api/show` on qwen2.5-coder):**

| Field | Value | Adapter-relevant? |
|---|---|---|
| `capabilities` | `['completion', 'tools', 'insert']` | Tools advertised — but see Probe 8 |
| `qwen2.context_length` | 32768 | Manifest `local_llm.max_context` should match |
| `general.parameter_count` | 14,770,033,664 | — |
| `general.architecture` | qwen2 | — |
| `template` | ChatML with `<tools>…</tools>` + `<tool_call>…</tool_call>` scaffold | Template supports tool-use; bridge does not extract it |
| `parameters` | `None` | No modelfile defaults — adapter MUST set `num_predict`, `num_ctx`, `temperature` |

`parameters: None` is load-bearing: there is no server-side default safety net. Every dispatch must set `num_predict` (else some Ollama builds cap at 128), `num_ctx` (else some builds default to 2048 and silently clamp larger prompts), and `temperature` (else varies by client). Treat the manifest's `dispatch.local_llm` block as the sole source of these values; do not rely on inheritance from the Modelfile.

---

## Probe 1 — Native `ollama.Client` smoke

Verifies end-to-end reachability and the richer telemetry the native client surfaces.

**Invocation** (Python, `venv/bin/python`, `/tmp/qwen_probe1_native.py`):

```python
from ollama import Client
client = Client(host=os.environ["OLLAMA_HOST"])
resp = client.chat(
    model="qwen2.5-coder:14b-instruct-q4_K_M",
    messages=[{"role": "user",
               "content": "Reply with exactly one token: READY. Do not use any tools."}],
    options={"temperature": 0.0, "num_predict": 8},
)
```

**Result:**

```json
{
  "wall_seconds": 4.045,
  "content": "READY",
  "prompt_eval_count": 43,
  "prompt_eval_duration_ms": 97.9,
  "eval_count": 2,
  "eval_duration_ms": 37.7,
  "total_duration_ms": 4019.8,
  "load_duration_ms": 3878.6,
  "done_reason": "stop"
}
```

**Findings:**

1. `wall_seconds=4.045` is dominated by `load_duration_ms=3878.6` — cold model load. Subsequent probes paid this only once per server-session; steady-state load_duration was 98–124ms across Probe 5+6.
2. Native client surfaces five distinct timers: `load_duration`, `prompt_eval_duration`, `eval_duration`, `total_duration`, plus wall. The OpenAI-compat shim exposes none of these directly — it emits only `usage.prompt_tokens` / `usage.completion_tokens`. **Adapter decision: use native client for observability; OpenAI-compat only for non-Ollama endpoints.**
3. `done_reason: "stop"` is the good case. Others observed later: no "length" in this run, but watch for it when `num_predict` caps before natural stop.
4. `prompt_eval_count` equals the OpenAI-compat `usage.prompt_tokens` for identical input (Probe 2 confirms 43/43 match) — same tokenizer across both transports.

---

## Probe 2 — OpenAI-compat SDK smoke

Same prompt, same model, different transport. Run immediately after Probe 1 so the model is warm.

**Invocation** (`/tmp/qwen_probe2_openai.py`):

```python
from openai import OpenAI
client = OpenAI(base_url=f"{OLLAMA_HOST}/v1", api_key="ollama-stub")
resp = client.chat.completions.create(
    model="qwen2.5-coder:14b-instruct-q4_K_M",
    messages=[{"role": "user", "content": "Reply with exactly one token: READY..."}],
    temperature=0.0, max_tokens=8,
)
```

**Result:**

```json
{
  "wall_seconds": 3.416,
  "content": "READY",
  "finish_reason": "stop",
  "prompt_tokens": 43, "completion_tokens": 2, "total_tokens": 45,
  "system_fingerprint": "fp_ollama",
  "id_prefix": "chatcmpl-688..."
}
```

**Findings:**

1. OpenAI-compat answers with full fidelity on the semantic fields: `content`, `finish_reason`, `usage`. That's enough for a JSON-mode adapter path.
2. `system_fingerprint="fp_ollama"` is the breadcrumb that differentiates an Ollama-backed endpoint from a real OpenAI endpoint or a vLLM/TGI deployment. Adapter can use this for telemetry but must not gate behavior on it (other backends return their own fingerprints).
3. No per-segment timing → structural reason to prefer the native client when the endpoint IS Ollama.

---

## Probe 3 — OpenAI JSON-mode (`response_format: json_object`)

Checks whether Ollama honors OpenAI's JSON-mode flag and whether the output is parseable.

**Invocation** (`/tmp/qwen_probe3_json_mode.py`):

```python
resp = client.chat.completions.create(
    model="qwen2.5-coder:14b-instruct-q4_K_M",
    messages=[
        {"role": "system", "content": "You classify short code review findings. Respond ONLY with JSON having keys 'severity', 'category', 'summary'."},
        {"role": "user", "content": "Finding: off-by-one in the pagination cursor ..."}],
    response_format={"type": "json_object"},
    temperature=0.0, max_tokens=256,
)
```

**Result:**

```json
{
  "wall_seconds": 5.776,
  "raw": "{\n  \"severity\": \"high\",\n  \"category\": \"bug\",\n  \"summary\": \"Off-by-one error in pagination logic skips the last page.\"\n}",
  "parse_ok": true,
  "parsed": { "severity": "high", "category": "bug",
              "summary": "Off-by-one error in pagination logic skips the last page." },
  "finish_reason": "stop",
  "prompt_tokens": 97, "completion_tokens": 34
}
```

**Findings:**

1. Ollama's OpenAI-compat layer accepts `response_format={"type":"json_object"}` and the output is JSON-parseable.
2. **Caveat:** json_object mode does NOT bind to a schema — the model is free to omit or add keys. Keys present here (severity / category / summary) match because the system prompt names them explicitly; a more complex schema would drift. **Adapter must post-hoc-validate when using this path.**
3. Semantic quality was on-target on this trivial case: "off-by-one" → `severity: high, category: bug`. Across probes, qwen is strong on classification and weak on nuanced security severity (see Probe 5+6).

---

## Probe 4 — Native JSON-schema-constrained decoding

The preferred path for the local-llm adapter. Full `reviewer_report.json`-shaped schema handed to the native `format=` parameter.

**Invocation** (`/tmp/qwen_probe4_schema.py`):

```python
SCHEMA = {
  "type": "object",
  "additionalProperties": False,
  "required": ["status", "findings", "summary"],
  "properties": {
    "status":  {"type": "string", "enum": ["approve", "request_changes", "comment"]},
    "summary": {"type": "string", "minLength": 1, "maxLength": 400},
    "findings": {
      "type": "array", "maxItems": 10,
      "items": {
        "type": "object", "additionalProperties": False,
        "required": ["severity", "file", "line", "category", "description"],
        "properties": {
          "severity":    {"type": "string", "enum": ["critical","high","medium","low"]},
          "file":        {"type": "string"},
          "line":        {"type": "integer", "minimum": 1},
          "category":    {"type": "string", "enum": ["bug","style","perf","security","docs"]},
          "description": {"type": "string", "minLength": 1, "maxLength": 500},
        },
      },
    },
  },
}

resp = client.chat(
    model="qwen2.5-coder:14b-instruct-q4_K_M",
    messages=[
      {"role": "system", "content": "You are a terse code reviewer. Return reviewer_report JSON matching the host-supplied schema."},
      {"role": "user", "content": "Review this diff: <pager off-by-one + auth compare_digest→== regression>"}],
    format=SCHEMA,
    options={"temperature": 0.0, "num_predict": 1024},
)
```

**Result:**

```json
{
  "wall_seconds": 10.601,
  "parse_ok": true,
  "schema_ok": true,
  "schema_errors": [],
  "prompt_eval_count": 223,
  "eval_count": 155,
  "eval_duration_ms": 5209.9,
  "parsed": {
    "status": "request_changes",
    "summary": "Security issues found in the diff.",
    "findings": [
      {"severity": "high", "file": "src/pager.py", "line": 14, "category": "security",
       "description": "The calculation of 'pages' is incorrect and can lead to off-by-one errors, potentially exposing sensitive data."},
      {"severity": "critical", "file": "src/auth.py", "line": 6, "category": "security",
       "description": "Using direct string comparison for tokens instead of 'secrets.compare_digest' can lead to timing attacks."}
    ]
  }
}
```

**Findings:**

1. **Schema-constrained decode works end-to-end.** Zero schema-validator errors on a non-trivial nested schema with enums, `additionalProperties: false`, min/max length, `minimum: 1` on integers. `Draft202012Validator(schema).iter_errors(parsed)` returned `[]`.
2. Qwen caught both intended issues: the off-by-one in the pager and the timing-attack regression in auth. Categorization was imperfect (off-by-one labelled `security` rather than `bug`) but severity was reasonable.
3. Tokens: 223 input (system + short schema prompt + diff) → 155 output ≈ 5.2s of generation at ~30 tok/s. `num_predict: 1024` never hit; model stopped at natural boundary.
4. **The schema-bound decoder is the right primary path.** v2 §8.3 specifies a "retry once on schema-invalid, then `status: schema_invalid`" loop. That loop should still exist in the adapter as a safety net, but expect its retry branch to be near-dead code when `format=<schema>` is in use.

---

## Probe 5 + 6 — Realistic reviewer + warm-run repeatability

Four back-to-back calls with a byte-stable system prompt and a non-trivial diff (RedisCache with a pickle injection; auth.token with a `hmac.compare_digest` → `==` regression). Verifies both semantic consistency and Ollama's prompt-eval caching.

**Invocation shape** (full: `/tmp/qwen_probe5_6_reviewer_repeat.py`):

- System prompt: ~260 tokens, security-sensitive code-reviewer role with an explicit severity rubric. Byte-identical across all 4 calls.
- User prompt: `"Review call #{i+1}. Diff:\n\n```diff\n{DIFF}\n```"` — varies only in the index.
- Schema: same shape as Probe 4.

**Per-call telemetry:**

| # | wall_s | prompt_eval_count | prompt_eval_ms | eval_count | eval_ms | load_ms | status | findings | schema_ok |
|---|--------|-------------------|---------------|------------|---------|---------|--------|----------|-----------|
| 1 | 10.099 | 470 | 1146.2 | 144 | 4892.8 | 98.2  | approve | 2 | ✔ |
| 2 | 9.956  | 470 | 591.9  | 146 | 5066.5 | 104.2 | approve | 2 | ✔ |
| 3 | 9.852  | 470 | 608.5  | 143 | 5132.7 | 124.1 | approve | 2 | ✔ |
| 4 | 9.934  | 470 | 623.7  | 144 | 5277.5 | 99.8  | approve | 2 | ✔ |

**Findings:**

1. **4/4 schema-valid, zero retries.** Reinforces Probe 4: schema-constrained decode is reliable on realistic prompts.
2. **Prompt-eval cache is real.** First call's `prompt_eval_duration_ms = 1146.2`; runs 2–4 median 608.5, a **~47% drop**. Ollama's server-side KV-cache survives across `/api/chat` calls that reuse the same prefix. This is NOT exposed as `cache_read` / `cache_creation` counters the way Anthropic does — the only observability is the duration field itself.
3. **Caveat — cache invalidates on any prefix change.** Same as Anthropic's semantics: interpolating anything varying into the system prompt kills the cache. The adapter should place per-call data in the user message, not system. Mirror the §3.2 prefix-stability discipline from v2.
4. **Steady-state wall is 9.9s** for this workload (470-token system + ~5-token-delta user + 144-token output under schema constraint). At 28 tok/s generation + 100ms load + 600ms warm-cache prompt-eval, this is the realistic floor for a reviewer role on this hardware. The manifest's `default_timeout_sec: 300` has ~30× headroom; a reasonable operator-tunable would be `120` once measured in production.
5. **Semantic quality concern.** All 4 runs classified the `hmac.compare_digest` → `==` regression as `severity: low, category: style` with description "may be less secure in some contexts." The correct reading is `high / security` (timing attack). Schema-constrained decode makes qwen *very* deterministic — same severity every run — but determinism is not correctness. Review agents run on local qwen should be treated as triage, not final sign-off. This is consistent with v2's positioning of local-llm as "bulk review on cheap hardware."
6. **Load duration is noise-level at 98–124ms.** Model stays resident across back-to-back calls; no cold re-hydration penalty during a burst.

**Implications for manifest defaults:**

| Field | Recommended |
|---|---|
| `local_llm.model` | `qwen2.5-coder:14b-instruct-q4_K_M` |
| `local_llm.max_context` | 32768 |
| `default_timeout_sec` | 300 (luxurious) or 120 (tight) |
| `max_output_tokens` | 1024 for reviewer; 2048 for explainer |
| `options.temperature` | 0.0 for reviewer; 0.2 for exploratory |
| `options.num_predict` | set explicitly — do not inherit |

---

## Probe 7 — Error / timeout surface

Four failure modes probed through the `openai` SDK so the adapter knows what exceptions to map.

**Invocation summary** (`/tmp/qwen_probe7_errors.py`):

| Case | Setup | Expected |
|---|---|---|
| a | Bad model name `"qwen-does-not-exist:0.1b"` | HTTP error |
| b | Bad host (port 65111 on same machine, nothing listening) | Connection error |
| c | `timeout=0.4` on a 1024-token generation | Client timeout |
| d | 67.5k-token blob pushed at 32k-context model | Context-length error |

**Results:**

```json
[
  {"label": "a_bad_model",        "wall": 3.089,
   "exception_class": "APIStatusError", "status_code": 404,
   "message": "model 'qwen-does-not-exist:0.1b' not found"},
  {"label": "b_unreachable",      "wall": 16.422,
   "exception_class": "APITimeoutError", "message": "Request timed out."},
  {"label": "c_short_timeout",    "wall": 2.56,
   "exception_class": "APITimeoutError", "message": "Request timed out."},
  {"label": "d_context_overflow", "wall": 8.12,
   "outcome": "unexpected_ok"}
]
```

**Findings:**

1. **Case a — bad model.** Clean `APIStatusError` with `status_code: 404` and a structured message naming the missing model. Adapter maps to `status: backend_error, code: model_not_found, retriable: false`.
2. **Case b — unreachable.** `APITimeoutError` at 16.4s wall despite `timeout=5`. The `openai` SDK defaults to `max_retries=2` (3 attempts total). 5s × ~3 ≈ 15s + backoff. **The adapter MUST pass `max_retries=0` to the `OpenAI` constructor**, else the manifest's declared `default_timeout_sec` becomes a floor, not a ceiling. Map to `status: backend_error, code: endpoint_unreachable, retriable: true`.
3. **Case c — short timeout.** Same retry amplification: `timeout=0.4` → 2.56s wall. Map to `status: timeout` with the observed `duration_ms`. Enforce `max_retries=0` in the same constructor fix.
4. **Case d — context overflow.** HTTP 200 in 8.1 seconds. **There is no error surfaced.** A follow-up native probe confirmed: Ollama silently clamps the input to `num_ctx` (in this case 32768), keeping the tail. `prompt_eval_count` came back as 32768 exactly and `eval_count=3` with content `"OK."`. The adapter MUST:
    - Pre-estimate input tokens (`len(system+user)*1.3` rough, or use `tiktoken` / native tokenizer) and refuse with `status: input_invalid, code: context_overflow` when the estimate exceeds `manifest.local_llm.max_context - manifest.max_output_tokens`.
    - Log a warning if `prompt_eval_count == manifest.local_llm.max_context`, as it strongly correlates with silent truncation.
    - Do NOT rely on the server to flag overflow.
5. **Exception import contract:** the adapter should catch `APIStatusError`, `APIConnectionError`, `APITimeoutError` from `openai` in that order (specificity first), then a bare `Exception` for the long tail. Same set applies to Anthropic's SDK if we ever backport this to a Claude local-mode.

---

## Probe 8 — Tool-calling (OpenAI-compat AND native)

Can the adapter build an implementer-style agent on qwen2.5-coder? The capability flag says `tools`, the template has the scaffold — does the model actually emit structured calls?

**8a — OpenAI-compat** (`/tmp/qwen_probe8_tools.py`):

```python
resp = client.chat.completions.create(
    model="qwen2.5-coder:14b-instruct-q4_K_M",
    messages=[
      {"role": "system", "content": "You are a code investigator. When needed, call tools."},
      {"role": "user", "content": "I need the contents of src/auth/token.py so I can review it."}],
    tools=[{"type":"function","function":{"name":"get_file_contents", ...}}],
    tool_choice="auto", temperature=0.0, max_tokens=256,
)
```

Result:

```json
{
  "wall": 5.212,
  "finish_reason": "stop",
  "content": "{\n  \"name\": \"get_file_contents\",\n  \"arguments\": {\n    \"path\": \"src/auth/token.py\"\n  }\n}",
  "tool_calls": []
}
```

**8b — Native client** (`/tmp/qwen_probe8b_tools_native.py`):

```python
resp = client.chat(
    model="qwen2.5-coder:14b-instruct-q4_K_M",
    messages=[...],
    tools=[{"type":"function","function":{"name":"get_file_contents", ...}}],
    options={"temperature": 0.0, "num_predict": 256},
)
```

Result:

```json
{
  "wall": 2.156,
  "content": "{\n  \"name\": \"get_file_contents\",\n  \"arguments\": {\n    \"path\": \"src/auth/token.py\"\n  }\n}",
  "tool_calls_raw": null,
  "done_reason": "stop"
}
```

**Findings:**

1. **Qwen produces the function call.** The JSON shape is correct — `{"name": ..., "arguments": {...}}` with the right argument. The model understood the tool, chose it, and supplied the path correctly.
2. **But neither transport extracts it.** OpenAI-compat returns `tool_calls: []`; native returns `tool_calls: null`. `finish_reason: stop` / `done_reason: stop` — not `tool_calls`. In both cases the call lives inline in `content`.
3. **Root cause** (inferred from the template): the Modelfile template emits `<tool_call>...</tool_call>` XML-ish tags around tool responses; for this model + Ollama 0.20.7, qwen is producing raw JSON *without* those tags, so the Ollama post-processor (which looks for the tags to populate `tool_calls`) finds nothing to extract. This is a known pattern for smaller qwen-coder variants on Ollama — a model-fine-tuning quirk rather than a client bug.
4. **v1 decision: keep tool-use out of scope.** v2 §7.3 already states: *"Tool use for local models is out of scope for v1."* This probe confirms the positioning. If tool-use is needed later, options in increasing order of complexity:
    - Ship a content-JSON parser: try `json.loads(content)` after trimming, treat a dict with `{name, arguments}` as a synthesized `tool_calls[0]`. Fragile because the model can ruin it by leaking prose around the JSON, but tractable on a schema-constrained path.
    - Upgrade to a larger qwen-coder (32B-instruct has better tool-call adherence on some reports) or a model known to emit the expected tags (`mistral-7b-instruct-v0.3`, `llama3.1:8b-instruct`). Requires re-probing cache / latency / schema-fidelity.
    - Wait for Ollama to ship a post-processor that accepts raw-JSON tool responses from models that don't wrap them.
5. **Adapter surface:** the `local-llm` backend should reject tool-enabled manifests with `status: manifest_invalid, code: tools_unsupported` at startup — cleaner than letting callers think tool-use works and then silently degrading.

---

## Context-overflow follow-up (telemetry-only)

Called out from Probe 7 case d. Native client invocation to capture what Ollama actually did:

```
blob = "The quick brown fox jumps over the lazy dog. " * 6000   # ≈ 67.5k tokens
resp = client.chat(
    model="qwen2.5-coder:14b-instruct-q4_K_M",
    messages=[{"role": "user", "content": blob + "\n\nRespond with exactly one token: OK."}],
    options={"temperature": 0.0, "num_predict": 8, "num_ctx": 32768},
)
```

Result:

```json
{
  "wall": 128.169,
  "content": "OK.",
  "prompt_eval_count": 32768,
  "eval_count": 3,
  "done_reason": "stop",
  "blob_approx_tokens": 67500
}
```

**Findings:**

1. Ollama silently truncated to `num_ctx` (32768). The tail of the prompt survived — the "Respond with exactly one token: OK." instruction was the last 10 tokens of the blob, so it was kept and honored.
2. Cold-eval cost for 32k prompt tokens was **128 seconds** on this hardware. Not a steady-state concern (reviewer payloads fit in well under 10% of that) but a clear signal that `num_ctx` is a ceiling the adapter should never push against.
3. **Truncation direction** is tail-kept / head-dropped in this case, but Ollama's docs do not promise a specific direction across versions. For a reviewer who puts the diff at the end of the user prompt (common pattern) this is benign; for a reviewer who puts task instructions at the start, the instructions would be truncated. **Adapter convention:** put caller instructions at the *end* of the user message, preceded by the variable-length context. Same mental model as Claude's system-prompt-first / user-prompt-last ordering from v2 §3.2, but applied inside the user message.

---

## Cost / latency model (vs. Anthropic table in v2 §3.1)

Dollar cost is $0.00 for every row; local inference is "free" past the hardware cost. The relevant costs are **wall time** and **electricity**:

| Workload | Cold wall | Warm wall | Notes |
|---|---|---|---|
| Trivial (2-token output) | 4.05s | 0.14s (eval) + 0.1s (prompt) + overhead | cold = load_duration_ms ≈ 3878 |
| Small JSON classify (34 tokens, 97 prompt) | 5.78s | ~1.2s eval + prompt | measured once; extrapolated warm |
| Reviewer report (~144 tokens, 470 prompt) | 10.10s (first) | 9.94s median (3 subsequent) | stable; see Probe 5+6 table |
| Context-overflow (32k clamped, 3-token out) | 128.17s | — | cautionary, not a normal workload |

**Reality checks:**

- Ollama's prompt-eval caching saves ~550ms per warm call with a 470-token system prompt. Over a 10-task plan that's ~5 seconds — real but not dominant vs the 5s eval floor per reviewer call.
- Generation rate is ~28 tok/s. Any reviewer output budget `max_output_tokens` directly sets a floor on wall time: `t_eval_min = max_output_tokens / 28`.
- Parallel fanout is constrained by GPU/CPU contention on the server; Ollama will serialize requests internally. The adapter can safely issue N concurrent HTTP calls, but wall time grows ~linearly until the server parallelism limit is hit. Measure before running wide-fanout reviews.

---

## Adapter design implications (concrete delta to v2 TASK-004)

Deltas to `backends/local_llm.py` as specified in v2 §7.3 + TASK-004 acceptance criteria:

1. **Dual transport paths, native-first.**
    - `api_kind: ollama` → native `ollama.Client` with `format=<schema>`.
    - `api_kind: openai-compatible` → `openai.OpenAI(base_url=.../v1, max_retries=0)` with `response_format={"type":"json_object"}` + post-hoc `jsonschema.validate`. Applies to vLLM, TGI, LM Studio.
    - Auto-detect is possible (ping `/api/tags` → Ollama) but manifest-declared is cleaner. Require `api_kind` to be set.

2. **`max_retries=0` is mandatory on the OpenAI client.** Add a constructor-level test that asserts this property, because the SDK default is surprising and the miss mode is silent.

3. **Token-count preflight.** Before spawning the request, estimate `input_tokens` (crude: `len(system+user) / 3.5`, better: tokenizer). If `input_tokens + max_output_tokens > manifest.local_llm.max_context`, refuse with `status: input_invalid, code: context_overflow`. Don't trust the server to complain.

4. **Tool-use rejection.** If a manifest with `backend: local-llm` carries a non-empty `tools_override.allowed` that includes any non-reader tool, refuse at preflight with `status: manifest_invalid, code: tools_unsupported`. Reader-only tool lists (`Read`, `Grep`, `Glob`) are fine because they won't be invoked (local models produce plain text).

5. **Envelope fields specific to local-llm:**
    - `tokens.input` ← `prompt_eval_count` (native) or `usage.prompt_tokens` (openai-compat).
    - `tokens.output` ← `eval_count` / `usage.completion_tokens`.
    - `tokens.cache_read` / `cache_creation` ← `null` (no equivalent metric; document this explicitly).
    - `cost_usd` ← `0.0`.
    - `extra.prompt_eval_duration_ms` + `extra.eval_duration_ms` (native only) for observability.
    - `extra.model_server_version` ← captured once at adapter init from `/api/version` (value `"0.20.7"` at this writing).

6. **Schema-retry loop is not dead code, but nearly is.** Keep v2 §8.3's "retry once on invalid, else `status: schema_invalid`" — it catches the openai-compat fallback path and the rare constrained-decode miss. Instrument the retry with a counter so the adapter can report "schema_retry_rate" over time; if it never fires in production, the primary path is healthy.

7. **Session-level warm-up (optional, later).** A one-shot `client.chat(..., options={"num_predict": 1})` at adapter-init warms the KV cache for the system prompt slot, paying the 1146ms cost once per run instead of on the first reviewer call. Defer — not a v1 requirement.

8. **Error-to-envelope mapping table:**

    | Exception / condition | envelope |
    |---|---|
    | `APIStatusError` 404 with `not_found_error` | `status: backend_error, code: model_not_found, retriable: false` |
    | `APIStatusError` 400 / 422 | `status: backend_error, code: bad_request, retriable: false` |
    | `APIStatusError` 5xx | `status: backend_error, code: server_error, retriable: true` |
    | `APIConnectionError` | `status: backend_error, code: endpoint_unreachable, retriable: true` |
    | `APITimeoutError` | `status: timeout, retriable: true` |
    | Post-hoc schema validation fails twice | `status: schema_invalid, retriable: false` |
    | Preflight token-count overflow | `status: input_invalid, code: context_overflow` |
    | Preflight tool-use declared | `status: manifest_invalid, code: tools_unsupported` |

---

## Risk and caveat register (qwen-specific additions)

| Risk | Status | Mitigation |
|---|---|---|
| Tool-calling not parsed from content | Confirmed (Probe 8 + 8b) | Manifest preflight rejects; documented out-of-scope |
| Context overflow silently truncated | Confirmed (Probe 7d + overflow follow-up) | Preflight token count; log on `prompt_eval_count == num_ctx` |
| openai SDK retry amplification | Confirmed (Probe 7b + 7c) | `max_retries=0` on client constructor |
| Semantic quality drift on security-sensitive diffs | Observed (Probe 5+6 all 4 runs mis-classified compare_digest regression) | Position local-llm reviewer as triage / bulk only; manifest description should say "secondary review"; gate critical paths on a Claude or Codex reviewer |
| Server-side cache not observable | Confirmed | Log `prompt_eval_duration` only; do not try to report cache hit/miss |
| Parameter defaults absent | Confirmed (Probe 0) | Adapter sets all `options.*` from manifest — no fallthrough |
| WSL gateway IP shifts on `wsl --shutdown` | Documented in Appendix A | Resolve at request time for long-lived processes |
| Parallel dispatches serialize on GPU | Not probed | Measure before wide fanout; sequential for v1 |

---

## Integration pointers — revised from Appendix A

Reconfirms and extends the Appendix A pointers for the TASK-004 `local-llm` adapter:

- **Primary path: native ollama client + `format=<schema>`**. Reserves the OpenAI-compat SDK for non-Ollama endpoints.
- **`base_url` convention:** for OpenAI-compat, the endpoint in the manifest **carries `/v1`** — caller supplies `http://host:11434/v1`, adapter passes it through. For native ollama, the endpoint is the bare host (`http://host:11434`) per `ollama.Client(host=...)`.
- **`api_key`:** required by `openai` SDK but ignored by Ollama. Pass a literal `"ollama-stub"`. **Do not** source from the caller's `OPENAI_API_KEY` env — it would leak real credentials into a local call for no benefit (reconfirming Appendix A).
- **Timeouts:** `default_timeout_sec: 300` in the manifest. The adapter clamps `min(overrides.timeout_sec, manifest.default_timeout_sec)`. `max_retries=0` ensures the wall-time cap is respected.
- **`total_cost_usd` is 0.0.** Record `tokens.input` / `tokens.output` for observability.
- **No tool use in v1.** Preflight refusal.
- **Envelope mapping (reconfirmed from Appendix A):** `status: ok` when (a) HTTP 200 / no exception, (b) content parses as JSON *and* validates against `output_instructions.schema_path` (on schema-constrained decode this is always true, on json_object fallback it sometimes is not).

---

## Still-open probes (non-blocking)

- **Probe 9 — 32B qwen-coder.** Does `qwen2.5-coder:32b-instruct-q4_K_M` correctly emit `<tool_call>` tags and surface structured `tool_calls`? If yes, tool-use may be re-scopable for a v2.
- **Probe 10 — Parallel-fanout throughput.** Fire N concurrent requests against the same model and measure wall-time scaling. Would establish a safe concurrency ceiling for multi-reviewer plans.
- **Probe 11 — Alternative models.** Briefly probe `mistral-7b-instruct-v0.3` and `llama3.1:8b-instruct` for schema-constrained decode quality and tool-call fidelity on the same Diff fixtures.
- **Probe 12 — lfm2.5-thinking pass-through.** The other resident model has a "thinking" capability — does Ollama expose it via a separate response field? Possible for a scratchpad reviewer mode.
- **Probe 13 — GPU vs CPU tokens/sec.** The 28 tok/s observed here — is qwen running on GPU (Windows host's graphics) or CPU? `ollama ps` can tell. Relevant if the adapter should refuse slow hardware.
- **Probe 14 — Long-running process cache behavior.** Hold an `ollama.Client` for 30 min with gaps between calls; does the server evict KV pages after idle? Would inform warm-up strategy.

None of these gate TASK-004 the v1 adapter build; they tune what's already working.

---

## Acceptance-criteria delta for TASK-004 `local-llm` adapter

Augments the v2 TASK-004 criteria:

- [ ] Native-path adapter round-trips a schema-validated `reviewer_report.json` against a running `qwen2.5-coder:14b-instruct-q4_K_M`.
- [ ] OpenAI-compat-path adapter round-trips the same request against the same endpoint with `response_format={"type":"json_object"}` + post-hoc validation, and matches the native-path envelope shape.
- [ ] `OpenAI` client is constructed with `max_retries=0`. Unit test asserts the instance attribute.
- [ ] Preflight rejects tool-enabled manifests with `status: manifest_invalid, code: tools_unsupported`.
- [ ] Preflight rejects input where `estimated_input_tokens + max_output_tokens > max_context` with `status: input_invalid, code: context_overflow`.
- [ ] Timeout path (client-timeout-short on a long generation) emits `status: timeout` with observed elapsed time; wall time is within 1.2× of the declared `timeout_sec` (validates `max_retries=0`).
- [ ] Endpoint-down path (wrong port) emits `status: backend_error, code: endpoint_unreachable, retriable: true`.
- [ ] Bad-model path emits `status: backend_error, code: model_not_found, retriable: false`.
- [ ] Envelope carries `extra.prompt_eval_duration_ms` and `extra.eval_duration_ms` on native-path runs.
- [ ] Envelope `cost_usd = 0.0` and `tokens.cache_read = null`, `tokens.cache_creation = null` are set explicitly (not omitted), so downstream consumers distinguish "no metric" from "zero metric."

---

## Appendix A — Probe artifacts

Scripts used (all in `/tmp/` on the run host; deleted post-probe):

- `/tmp/qwen_probe1_native.py` — Probe 1
- `/tmp/qwen_probe2_openai.py` — Probe 2
- `/tmp/qwen_probe3_json_mode.py` — Probe 3
- `/tmp/qwen_probe4_schema.py` — Probe 4
- `/tmp/qwen_probe5_6_reviewer_repeat.py` — Probes 5 + 6
- `/tmp/qwen_probe7_errors.py` — Probe 7
- `/tmp/qwen_probe8_tools.py` — Probe 8 (OpenAI-compat)
- `/tmp/qwen_probe8b_tools_native.py` — Probe 8b (native)
- `/tmp/qwen_probe_overflow.py` — overflow follow-up

The scripts are self-contained; each prints a JSON dict to stdout. They depend only on `openai`, `ollama`, `jsonschema`, `pyyaml` (installed into the project venv at probe time).

To replay:

```bash
/mnt/d/claude-plan-executor/venv/bin/pip install openai ollama jsonschema pyyaml
source ~/.bashrc                          # sets $OLLAMA_HOST
/mnt/d/claude-plan-executor/venv/bin/python /tmp/qwen_probe4_schema.py
```

## Appendix B — Minimal adapter skeleton (sketch)

Illustrative only. Final shape lives in `plugins/plan-executor/scripts/backends/local_llm.py` per TASK-004.

```python
class LocalLLMBackend:
    def __init__(self, manifest):
        self.manifest = manifest
        cfg = manifest["dispatch"]["local_llm"]
        self.api_kind = cfg["api_kind"]                       # "ollama" | "openai-compatible"
        self.endpoint = cfg["endpoint"]                       # resolved from env/manifest
        self.model = cfg["model"]
        self.max_ctx = cfg["max_context"]
        if self.api_kind == "ollama":
            from ollama import Client
            self.client = Client(host=self.endpoint)
        else:
            from openai import OpenAI
            self.client = OpenAI(base_url=self.endpoint,
                                 api_key="ollama-stub",
                                 max_retries=0)               # probe-7 mitigation

    def invoke(self, *, system_prompt, user_prompt,
               output_schema, timeout_sec, max_output_tokens):
        est_in = (len(system_prompt) + len(user_prompt)) // 3
        if est_in + max_output_tokens > self.max_ctx:
            return self._envelope_input_invalid("context_overflow")

        if self.api_kind == "ollama":
            return self._invoke_native(system_prompt, user_prompt,
                                       output_schema, timeout_sec,
                                       max_output_tokens)
        return self._invoke_openai(system_prompt, user_prompt,
                                   output_schema, timeout_sec,
                                   max_output_tokens)

    def _invoke_native(self, sys_p, usr_p, schema, timeout_s, max_tok):
        # happy path: native + format=schema
        # on APIStatusError / requests exceptions → envelope error map
        # on success: extract eval_count / prompt_eval_count, validate, return envelope
        ...

    def _invoke_openai(self, sys_p, usr_p, schema, timeout_s, max_tok):
        # happy path: response_format=json_object + post-hoc validate
        # on schema-invalid: retry once with error quote in prompt
        # on second failure: status=schema_invalid
        ...
```
