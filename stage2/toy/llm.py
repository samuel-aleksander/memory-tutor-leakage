"""One interface for the tutor and rater models, whichever provider serves them.

Providers (set PROVIDER in toy_config.py):
  "vertex_maas"  open models served by Google Cloud (Vertex AI / Gemini Enterprise Agent
                 Platform "Model as a Service"), e.g. DeepSeek. OpenAI-compatible
                 chat-completions endpoint, authenticated with your Google credentials.
                 Needs: pip install openai google-auth
                        gcloud auth application-default login
                        the model enabled in Model Garden for your project
  "anthropic"    Claude through the Anthropic API. Needs ANTHROPIC_API_KEY.

Usage:
    client = llm.make_client(cfg)            # cfg = toy_config module
    conv = llm.Conversation(client, system)   # one dialogue
    reply = conv.send("student message")      # -> Reply
    reply = client.complete(prompt, schema)   # one-off call (rater), JSON in reply.text
"""

import re
import threading
from dataclasses import dataclass, field

THINK_RE = re.compile(r"<think>.*?</think>", re.S)


class TransientError(Exception):
    """The call failed after retries (rate limit, server error, network). Skip and go on."""


class ConfigError(Exception):
    """The request itself is wrong (model name, access, parameters). Stop the run."""


@dataclass
class Reply:
    text: str                      # visible answer only (any reasoning removed)
    reasoning: str = ""            # the model's reasoning, if the provider returns it
    stop_reason: str = ""          # provider's own value, kept as-is
    refused: bool = False          # the provider blocked or declined the reply
    input_tokens: int = 0
    output_tokens: int = 0
    raw: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Google Cloud MaaS (OpenAI-compatible)
# ---------------------------------------------------------------------------
class VertexMaaS:
    def __init__(self, cfg):
        project = getattr(cfg, "GCP_PROJECT", "")
        if not project:
            raise ConfigError("Set GCP_PROJECT in toy_config.py to your Google Cloud project ID.")
        location = getattr(cfg, "GCP_LOCATION", "global")
        host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
        self.base_url = f"https://{host}/v1/projects/{project}/locations/{location}/endpoints/openapi"
        self.model = cfg.MODEL
        self.max_tokens = cfg.MAX_TOKENS
        self.temperature = getattr(cfg, "TEMPERATURE", None)
        self.extra_body = getattr(cfg, "EXTRA_BODY", None) or None
        self._lock = threading.Lock()
        self._client = None
        self._token = None
        try:
            import google.auth
            import openai  # noqa: F401
        except ImportError as e:
            raise ConfigError(f"Missing package ({e.name}). Run: pip install openai google-auth")
        try:
            self._creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        except Exception as e:  # google.auth.exceptions.DefaultCredentialsError
            raise ConfigError(f"No Google credentials ({e}). Run: gcloud auth application-default login")

    def _openai_client(self):
        """An OpenAI client with a fresh Google access token (tokens last about an hour)."""
        import google.auth.transport.requests
        import openai
        with self._lock:
            if not self._creds.valid:
                self._creds.refresh(google.auth.transport.requests.Request())
            if self._client is None or self._token != self._creds.token:
                self._token = self._creds.token
                self._client = openai.OpenAI(base_url=self.base_url, api_key=self._token)
            return self._client

    def _call(self, messages, response_format=None):
        import openai
        kwargs = {"model": self.model, "messages": messages, "max_tokens": self.max_tokens}
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if self.extra_body:
            kwargs["extra_body"] = self.extra_body
        if response_format:
            kwargs["response_format"] = response_format
        try:
            resp = self._openai_client().chat.completions.create(**kwargs)
        except (openai.BadRequestError, openai.NotFoundError, openai.PermissionDeniedError,
                openai.AuthenticationError) as e:
            raise ConfigError(repr(e)) from e
        except (openai.APIStatusError, openai.APIConnectionError) as e:
            raise TransientError(repr(e)) from e
        choice = resp.choices[0]
        content = choice.message.content or ""
        extra = getattr(choice.message, "model_extra", None) or {}
        reasoning = extra.get("reasoning_content") or extra.get("reasoning") or ""
        inline = THINK_RE.findall(content)  # some models put reasoning inline in <think> tags
        if inline:
            reasoning = (reasoning + "\n" + "\n".join(inline)).strip()
            content = THINK_RE.sub("", content).strip()
        return Reply(
            text=content,
            reasoning=reasoning,
            stop_reason=choice.finish_reason or "",
            refused=choice.finish_reason == "content_filter",
            input_tokens=getattr(resp.usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(resp.usage, "completion_tokens", 0) or 0,
        )

    def chat(self, system, history):
        """history: list of {"role": "user"|"assistant", "content": str}. Returns (Reply, turn to append)."""
        reply = self._call([{"role": "system", "content": system}] + history)
        # Send back only the visible answer, never the reasoning (the usual rule for these models).
        return reply, {"role": "assistant", "content": reply.text}

    def complete(self, prompt, schema=None):
        fmt = None
        if schema:
            fmt = {"type": "json_schema", "json_schema": {"name": "answer", "schema": schema, "strict": True}}
        return self._call([{"role": "user", "content": prompt}], response_format=fmt)


# ---------------------------------------------------------------------------
# Anthropic API (Claude)
# ---------------------------------------------------------------------------
class AnthropicAPI:
    def __init__(self, cfg):
        import os
        if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
            raise ConfigError("Set ANTHROPIC_API_KEY first (export ANTHROPIC_API_KEY=...).")
        import anthropic
        self.client = anthropic.Anthropic()
        self.model = cfg.MODEL
        self.max_tokens = cfg.MAX_TOKENS
        self.effort = getattr(cfg, "EFFORT", "medium")

    def _call(self, system, messages, output_config):
        import anthropic
        kwargs = {"model": self.model, "max_tokens": self.max_tokens, "messages": messages,
                  # The installed SDK predates the output_config parameter; send it in the body.
                  "extra_body": {"output_config": output_config}}
        if system:
            kwargs["system"] = system
        try:
            resp = self.client.messages.create(**kwargs)
        except (anthropic.BadRequestError, anthropic.NotFoundError, anthropic.PermissionDeniedError,
                anthropic.AuthenticationError) as e:
            raise ConfigError(repr(e)) from e
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            raise TransientError(repr(e)) from e
        reply = Reply(
            text="".join(b.text for b in resp.content if b.type == "text"),
            reasoning="".join(getattr(b, "thinking", "") or "" for b in resp.content if b.type == "thinking"),
            stop_reason=resp.stop_reason or "",
            refused=resp.stop_reason == "refusal",
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )
        return reply, resp

    def chat(self, system, history):
        reply, resp = self._call(system, history, {"effort": self.effort})
        # Claude: send the full content back, thinking blocks included, unchanged.
        return reply, {"role": "assistant", "content": resp.content}

    def complete(self, prompt, schema=None):
        oc = {"effort": "low"}
        if schema:
            oc["format"] = {"type": "json_schema", "schema": schema}
        reply, _ = self._call(None, [{"role": "user", "content": prompt}], oc)
        return reply


PROVIDERS = {"vertex_maas": VertexMaaS, "anthropic": AnthropicAPI}


def make_client(cfg, model=None):
    """Client for cfg.PROVIDER / cfg.MODEL (or another model on the same provider)."""
    if cfg.PROVIDER not in PROVIDERS:
        raise ConfigError(f"Unknown PROVIDER {cfg.PROVIDER!r}; choose one of {list(PROVIDERS)}")
    client = PROVIDERS[cfg.PROVIDER](cfg)
    if model:
        client.model = model
    return client


class Conversation:
    """One multi-turn dialogue with a fixed system prompt."""

    def __init__(self, client, system):
        self.client, self.system, self.history = client, system, []

    def send(self, user_text):
        self.history.append({"role": "user", "content": user_text})
        try:
            reply, assistant_turn = self.client.chat(self.system, self.history)
        except Exception:
            self.history.pop()
            raise
        self.history.append(assistant_turn)
        return reply


def parse_json(text):
    """JSON from a reply, tolerating code fences or text around the object."""
    import json
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise
        return json.loads(m.group(0))
