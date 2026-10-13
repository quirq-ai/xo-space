# Decision models: Levanto Sage and TypeSafe Jev (research, 2026-10-13)

From a web-research subagent. These are public docs only: no accounts were created and no API calls were made. Every claim below cites its source. Anything not taken directly from the docs is marked inference.

## TL;DR
- **Jev exists.** It is TypeSafe AI's "System One" decision model (https://typesafe.ai, docs at https://docs.typesafe.ai). It is in early access. The current model is `jev-1.13.0`, aliased as `jev-latest` and `jev-preview`, released on 2026-09-15.
- **Sage already speaks Jev's wire format.** Sage serves `POST https://sage.levanto.ai/v1/systemone`, which is "the request format Jev uses". Switching between them changes only the base URL, the key and the `model` (https://docs.levanto.ai/systemone.md). So SystemOne is a ready-made common interface for choice, yes/no and score questions.
- **The gap is multi-label tags.** Jev has no `tags` kind, and Sage's SystemOne endpoint has no Tags, Sort, Images or Grounding. Native tags exist only on Sage `/decide`.

## Jev API (https://docs.typesafe.ai/api.md)
- **Endpoint:** `POST https://api.typesafe.ai/v1/systemone`, with `Authorization: Bearer <key>`.
- **Request:** `{state, model, questions}`.
  - `state` can be a string, an object or an array.
  - `questions` is a map from caller-chosen ids to question objects. The ids are never shown to the model.
  - Questions are evaluated in parallel, independently, against the same state.
- **Choice example:**
  ```json
  {"state":"My running shoes arrived in the wrong size. Can I swap them for a size 10?",
   "questions":{"department":{"type":"choice","instructions":"Which team should handle this?",
     "criteria":{"returns":"Exchanges, wrong or damaged items","shipping":"Delivery status, delays, lost packages","billing":"Charges, invoices, payment problems"}}}}
  → {"model":"jev-1.13.0","answers":{"department":{"type":"choice","choice":"returns","confidence":1.0,
      "probabilities":{"shipping":0.0,"returns":1.0,"billing":0.0}}},"usage":{"input_tokens":328,"output_tokens":34}}
  ```
- **Question kinds:**
  - `choice`: up to 255 options, with `criteria` mapping each option to a description (which may be null).
  - `score`: 2–10 ordered levels. Returns the expected value, `confidence`, a `legend` and the probabilities.
  - `noul`: a yes/no question. Returns P(yes) and no confidence.
  - The docs' answer to multi-label is to ask one `noul` per label in the same request.
- **Never abstains.** Jev never returns null, so you set the thresholds yourself. The docs' examples use these:

  | Signal | Act | Ask a human / abstain |
  |---|---|---|
  | choice `confidence` | ≥ 0.9 acts without confirmation | < 0.5 abstains |
  | `noul` P(yes) | ≥ 0.8 means yes, ≤ 0.2 means no | anything between goes to a human |

  The docs also recommend adding your own "none of the above" option to choices.
- **Confidence** (choice): `(p_max − 1/n)/(1 − 1/n)`.
- **Limits:**
  - 64k tokens per request.
  - 32k tokens for `state` plus the longest question.
  - 100K tokens/s and 80 requests/s, described as "adjusting dynamically".
- **Errors:** 401 bad key, 422 validation failure, 429 rate limited, 529 overloaded (retry). There is no 402 / balance code.
- **Latency:** 70–500 ms (vendor). A third-party host reports a 216 ms median.
- **Price:** $0.042 per million input tokens; output tokens are free.
- **SDKs:**
  - Python `typesafe-sdk` (`AsyncTypeSafeClient.system_one`).
  - JS `@typesafe-ai/sdk`.
- **Other hosts:** AI/ML API at `POST https://api.aimlapi.com/v1/decisions` with model `typesafe/jev`.

## Sage (refreshed from https://sage.levanto.ai/openapi.json and https://docs.levanto.ai)
- **Paths:** `/ready`, `/models`, `/decide`, `/decide/batch`, `/v1/models`, `/v1/systemone`, `/usage/estimate`.
- **`/decide` request:** `{content, question, grounding?, reasoning?, latency_mode?}`.
- **`/decide` response `meta`:** `{model, latency_ms, question_count, compute_mode, usage{billed_input_tokens, rendered_tokens, input_tokens, output_tokens, image_*}, reasoning, escalated}`.
- **Kinds:** yesno, choice, scale, sort, tags.
- **Nulls are meaningful.**
  - In a choice, `chosen` is null when the top two options are too close to call.
  - In tags, `applies` can be null, and the docs say "don't read it as false".
- **Limits:** choice 2–120 options, tags 1–120, scale 2–26, context ~32K tokens (128K with extended context). Requests are never truncated.
- **Errors:** the body is `{"detail"}`.
  - `/decide` returns 400 for an invalid request, plus 401 / 402 / 503.
  - 422 is returned only on the `/v1/*` paths.
  - 429 is undocumented, but should still be handled.
- **Sage `/v1/systemone`:**
  - `model` must be `levanto-sage-v1.3` or `sage-latest`; `jev-latest` returns 422.
  - It never returns null.
  - `confidence` = p_max, which is a different formula from Jev's.
  - Reasoning is opt-in.

## Mapping

| Sage `/decide` | Jev `/v1/systemone` |
|---|---|
| `content` | `state` |
| `question.id` | key in the `questions` map (many questions per call) |
| `choice` + `options[{option,description}]` | `choice` + `criteria{option: description}` |
| `result.chosen` (nullable) | `answers[id].choice` (never null) |
| `result.probability` | `probabilities[choice]` (Jev's `confidence` is a different metric) |
| `tags[{id,name}]` → `{probability, applies}` | none. Emulate with one `noul` per tag and derive `applies` from thresholds. Inference: tags are judged jointly, while nouls are independent. |
| `yesno` | `noul` |
| `scale` (≤26) | `score` (2–10) |
| sort / grounding / images / reasoning | none |
| `meta.usage.billed_input_tokens` | `usage.input_tokens` |
| 400 / 401 / 402 / 429 | 422 / 401 / — / 429, plus 529 |

## Design implication (inference)
Build one internal decision interface shaped like SystemOne: `{state, questions{id: choice|noul|score|tags}}` → a normalised answer `{chosen|null, probability, probabilities, usage.input_tokens}`.
- **Abstain:** apply the threshold client-side, so both providers abstain the same way.
- **Tags:** map natively to Sage `/decide`; expand to N nouls on Jev.
- **Confidence:** recompute it locally, never pass a provider's value through.
- **Errors:** handle each provider's error set.
