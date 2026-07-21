# Codex App Server GPT Design

```yaml
doc_type: subsystem-design
status: partially-superseded
authority: subsystem
upstream:
  - 2026-07-13-alltonote-knowledge-compiler-architecture-design.md
superseded_by:
  - Knowledge Compiler ModelExecutor / AgentExecutor ownership
implementation_status: codex-client-and-bridge-retained-old-gpt-ownership-superseded
last_verified_at: 2026-07-18
```

> 当前使用规则：本文只保留 Codex app-server 的登录态、协议帧、子进程生命周期和安全边界参考。本文中以 `NoteGenerator -> GPTFactory -> RequestChunker` 为扩展中心的职责划分已经失效；新功能必须服从 Knowledge Compiler 的 `ModelExecutor`、`AgentExecutor`、Coordinator 和 Job 边界。

## Background

BiliNote currently treats every LLM provider as an OpenAI-compatible HTTP API. The backend stores provider `api_key`, `base_url`, and model names, then `GPTFactory` builds a `UniversalGPT` instance that calls `chat.completions.create()`.

The requested feature adds a second connection method for OpenAI/Codex usage: use the local Codex CLI app-server runtime so BiliNote can generate notes through the user's local Codex login and coding-plan quota instead of a manually configured OpenAI API key.

## Confirmed Scope

First version uses Codex only as the note-generation model backend.

BiliNote still owns:

- Video URL parsing.
- Downloading and metadata extraction.
- Subtitle retrieval and speech transcription.
- Transcript chunking.
- Markdown post-processing for table of contents, source links, and screenshots.
- Saving note results.
- RAG indexing and the existing AI Q&A flow.

Codex app-server only replaces the LLM call inside `gpt.summarize(source)`.

## Non-Goals

This design does not turn BiliNote into a full note-generation agent.

First version does not:

- Let Codex control video download, transcription, or file writes.
- Expose shell, `apply_patch`, or `update_plan` as part of note generation.
- Migrate Hermes MCP callbacks.
- Route the right-side AI Q&A panel through Codex app-server.
- Support Docker or remote backend deployments.
- Automatically share auth between OpenAI API keys, Hermes auth, and Codex CLI auth.

## Design Principles

The smallest correct integration point is the GPT abstraction layer. The existing pipeline already produces the exact input needed for note generation: transcript chunks, title, tags, style, format flags, and optional screenshot references. Replacing only the inference backend preserves the rest of BiliNote's behavior and keeps failure modes narrow.

Codex app-server is not OpenAI-compatible REST. It is a local app-server protocol over stdio / JSON-RPC events. The implementation should model it explicitly instead of hiding it behind a fake `base_url`.

## Architecture

Existing path:

```text
NoteGenerator
  -> GPTFactory
  -> UniversalGPT
  -> OpenAI-compatible chat.completions.create()
```

New path:

```text
NoteGenerator
  -> GPTFactory
  -> CodexAppServerGPT
  -> CodexAppServerClient
  -> local `codex app-server --stdio`
```

Provider routing:

```text
provider.id == "codex_app_server"
or provider.type == "codex_app_server"
  -> CodexAppServerGPT

otherwise
  -> UniversalGPT
```

`NoteGenerator` should not need to know which backend is used.

## Provider Configuration

Add a built-in provider:

```text
id       = codex_app_server
name     = Codex App Server
type     = codex_app_server
logo     = OpenAI
api_key  = ""
base_url = "codex_app_server://local"
enabled  = 1
```

`base_url` is only a schema compatibility value. Backend behavior must not depend on parsing this URL.

Models remain in the existing `models` table:

```text
provider_id = codex_app_server
model_name  = gpt-5.5, gpt-5, or the model read from Codex config
```

The first version can seed or suggest the current model from:

```text
%USERPROFILE%\.codex\config.toml
```

If no model can be read, the UI should allow manual entry.

## Backend Components

### `CodexAppServerClient`

File:

```text
backend/app/gpt/codex_app_server_client.py
```

Responsibilities:

- Verify `codex` CLI availability.
- Verify Codex login state by checking `~/.codex/auth.json` and, where possible, app-server account status.
- Start `codex app-server --stdio`.
- Send JSON-RPC requests.
- Read server notifications and responses.
- Extract final Markdown text from agent message events.
- Enforce timeouts.
- Convert protocol failures into clear Python exceptions.

Minimal request sequence:

```text
initialize
thread/start
turn/start
```

`thread/start` should be ephemeral and constrained:

```text
model             = selected BiliNote model_name
cwd               = project/backend working directory
sandbox           = read-only
approvalPolicy    = never
ephemeral         = true
baseInstructions  = "Only generate Markdown notes. Do not call tools or modify files."
```

`turn/start` sends one text input:

```json
{
  "type": "text",
  "text": "<prompt>",
  "text_elements": []
}
```

The client collects output from:

- `item/agentMessage/delta` as the streaming source.
- `item/completed` with `type = agentMessage` as a full-text fallback.
- `turn/completed` to decide success or failure.
- `error` notifications for failure details.

### `CodexAppServerGPT`

File:

```text
backend/app/gpt/codex_app_server_gpt.py
```

Responsibilities:

- Implement the existing `GPT` interface.
- Reuse BiliNote's current prompt-building logic for title, transcript text, tags, style, and note format flags.
- Reuse `RequestChunker` so large transcripts behave like existing providers.
- Call `CodexAppServerClient.run_markdown_turn(prompt, model)` for each chunk.
- Reuse the existing merge prompt behavior for multi-chunk notes.
- Return the final Markdown string to `NoteGenerator`.

The model prompt must explicitly ask for Markdown only and must not ask Codex to inspect or modify files.

## Safety Model

First version should behave like a text-generation backend, not an autonomous local agent.

Defaults:

- `sandbox = read-only`
- `approvalPolicy = never`
- ephemeral thread per generated chunk
- no MCP migration
- no shell/file-edit approvals surfaced to the user

If Codex tries to use tools and cannot produce a final message, BiliNote should fail the task with a clear runtime error.

## Frontend UX

The model settings page should special-case the Codex App Server provider.

For ordinary providers, keep the current API key and Base URL form.

For `codex_app_server`, show:

- Codex CLI installed status.
- Codex login status.
- Current Codex default model, if readable.
- Manual model-name input.
- Test connection button that calls a Codex-specific backend health check.

Hide:

- API Key input.
- Base URL input.
- Fetch models from OpenAI-compatible API.

On the home page, the existing model selector can continue listing enabled models. The user should see the Codex model as a normal generation option, for example:

```text
gpt-5.5 - Codex App Server
```

## API Changes

Add a health/status endpoint:

```text
GET /api/codex_app_server/status
```

Example response:

```json
{
  "codex_cli_available": true,
  "codex_version": "codex-cli 0.137.0",
  "auth_available": true,
  "default_model": "gpt-5.5",
  "ready": true
}
```

Add or adapt connection test behavior so `POST /api/connect_test` dispatches by provider type:

```text
codex_app_server -> Codex health/minimal turn test
other providers  -> current OpenAI-compatible test
```

## Error Handling

User-facing errors should distinguish:

- Codex CLI is not installed.
- Codex is installed but `codex login` has not been completed.
- Codex app-server failed to start.
- The selected model failed.
- The turn timed out.
- The turn completed without Markdown output.

The note task should fail explicitly. It should not save an empty Markdown file as a successful note.

## Testing

Backend tests:

- `GPTFactory` returns `CodexAppServerGPT` for `codex_app_server`.
- Existing providers still return `UniversalGPT`.
- Codex health check reports CLI missing, auth missing, and ready states correctly.
- JSON-RPC event parsing extracts text from `item/agentMessage/delta`.
- `item/completed` full-text fallback works.
- turn failure, timeout, and empty output raise clear exceptions.

Integration test:

- Use a short transcript fixture.
- Select `codex_app_server` provider and a model name.
- Generate a note.
- Confirm Markdown is written to the normal note result files.
- Confirm existing post-processing and RAG indexing paths still run.

Frontend tests or manual checks:

- Codex provider hides API fields.
- Codex status renders correctly.
- Manual model add works.
- Home page model selection can choose the Codex model.

## Implementation Phases

### Phase 1: Backend MVP

- Add built-in provider metadata.
- Add Codex status service and endpoint.
- Add `CodexAppServerClient`.
- Add `CodexAppServerGPT`.
- Update `GPTFactory` routing.
- Add focused backend tests.

### Phase 2: Frontend Configuration

- Special-case Codex provider form.
- Display Codex runtime status.
- Allow manual model add.
- Wire connection test to the Codex health path.
- Verify home page model selection.

### Phase 3: Local End-to-End Verification

- Run a short video/transcript note generation with Codex selected.
- Verify generated Markdown appears in BiliNote.
- Verify post-processing and RAG indexing are unchanged.

## Success Criteria

The feature is complete when:

- A user can configure Codex App Server without an OpenAI API key.
- BiliNote can generate notes through local `codex app-server`.
- The generated note is saved and displayed through the existing BiliNote flow.
- Existing API-key providers continue to work unchanged.
- Failures clearly tell the user whether the issue is CLI install, login, runtime startup, model error, timeout, or empty output.
