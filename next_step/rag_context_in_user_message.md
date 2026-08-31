# KG-RAG Context in the User Message

## Approach

LightRAG now has an opt-in `context_in_user_message` query setting for the
knowledge-graph retrieval modes: `local`, `global`, `hybrid`, and `mix`.

The new default setting (`true`) keeps behavioural instructions in the system
message and places retrieved, untrusted source material beside the question in
the final user message:

```text
system: rag_response instructions
user:   retrieved context and user query
```

With the setting disabled (`false`), the established layout is restored:

```text
system: rag_response instructions and retrieved context
user:   user query
```

This makes the message boundary explicit without changing retrieval, graph
storage, embeddings, or document content.

## Added and Updated

- Added `QueryParam.context_in_user_message`, with a default of `true`.
- Added the corresponding REST and WebUI request field.
- Added a WebUI query-settings checkbox for KG-RAG modes. It is disabled for
  `naive` and `bypass`, which are intentionally unchanged.
- Added `PROMPTS["rag_response_user_context"]` for the instruction-only
  system message; the existing `PROMPTS["rag_response"]` remains the legacy
  context-in-system template.
- Updated `kg_query` to construct the final user message when enabled.
- Updated `only_need_prompt` output to label the system and user messages,
  making the selected layout easy to inspect.
- Added the setting to the LLM query cache identity and cache metadata so an
  answer created under one layout cannot be reused by the other.
- Added regression tests for default behaviour, enabled behaviour, final
  provider arguments, and cache-key separation.

## Not Changed

- `naive_query` and `PROMPTS["naive_rag_response"]` are untouched.
- `bypass` remains a direct LLM query with no retrieved context.
- Retrieval ranking, token limits, documents, vectors, and graph storage are
  unchanged.

## Reverting

Turn off **Context in User Message** in WebUI query settings, or send
`"context_in_user_message": false` through the API. This immediately restores
the original system-context message layout.

## Personal Note

I prefer this as an experiment behind a request-level switch rather than a
global prompt rewrite. It lets you compare answer quality and prompt handling
with the same data, and its cache separation prevents a previous answer from
making the comparison misleading.
