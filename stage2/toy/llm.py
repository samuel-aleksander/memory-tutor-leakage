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

import random
import re
import sys
import threading
import time
from dataclasses import dataclass, field

THINK_RE = re.compile(r"<think>.*?</think>", re.S)
EMPTY_REPLY_RETRIES = 3

# Retries for transient failures (network, rate limits, server errors), on top of the SDKs'
# own 2 quick retries. In the 2026-11-22 batch, 5 of 40 dialogues lost their first call to a
# connection error that outlasted those. Waits (seconds, +-20% jitter) before each retry;
# about 4 minutes in all before a call is given up as a TransientError.
RETRY_WAITS = (5, 15, 30, 60, 120)
RETRY_STATUSES = (408, 409, 429)  # plus any 5xx


def _is_transient(e, sdk):
    """A connection problem, rate limit or server error from the given SDK module."""
    if isinstance(e, sdk.APIConnectionError):  # includes timeouts
        return True
    return isinstance(e, sdk.APIStatusError) and (e.status_code in RETRY_STATUSES or e.status_code >= 500)


def _with_retries(call, transient):
    """call() -> (result, retries used). Waits and tries again while transient(error) holds."""
    for attempt, wait in enumerate((*RETRY_WAITS, None)):
        try:
            return call(), attempt
        except Exception as e:
            if wait is None or not transient(e):
                raise
            wait *= random.uniform(0.8, 1.2)
            print(f"    retry {attempt + 1}/{len(RETRY_WAITS)} after {type(e).__name__}, "
                  f"waiting {wait:.0f}s", file=sys.stderr, flush=True)
            time.sleep(wait)


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
    retries: int = 0               # transient failures retried before this reply came back
    raw: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Google Cloud MaaS (OpenAI-compatible)
# ---------------------------------------------------------------------------
class VertexMaaS:
    def __init__(self, cfg):
        import os
        try:
            import google.auth
            import openai  # noqa: F401
        except ImportError as e:
            raise ConfigError(f"Missing package ({e.name}). Run: pip install openai google-auth")
        try:
            self._creds, adc_project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        except Exception as e:  # google.auth.exceptions.DefaultCredentialsError
            raise ConfigError(f"No Google credentials ({e}). Run: gcloud auth application-default login")
        # Project: the config, else $GOOGLE_CLOUD_PROJECT, else the one your gcloud login set.
        # Leaving it out of the config keeps your project ID out of the repo.
        project = (getattr(cfg, "GCP_PROJECT", "") or os.environ.get("GOOGLE_CLOUD_PROJECT")
                   or adc_project or getattr(self._creds, "quota_project_id", None))
        if not project:
            raise ConfigError("No Google Cloud project found. Set GCP_PROJECT in toy_config.py "
                              "or export GOOGLE_CLOUD_PROJECT=<your project ID>.")
        self.project = project
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
        import google.auth.exceptions

        def transient(e):  # a token refresh can also fail on the network
            return _is_transient(e, openai) or isinstance(e, google.auth.exceptions.TransportError)

        retries = 0
        for attempt in range(1, EMPTY_REPLY_RETRIES + 1):
            try:
                resp, used = _with_retries(
                    lambda: self._openai_client().chat.completions.create(**kwargs), transient)
                retries += used
            except google.auth.exceptions.TransportError as e:
                raise TransientError(repr(e)) from e
            except (openai.BadRequestError, openai.NotFoundError, openai.PermissionDeniedError,
                    openai.AuthenticationError) as e:
                # Google puts the useful explanation in the body (e.g. "API not enabled").
                raise ConfigError(f"{type(e).__name__} {e.status_code}: {e.body or e.message}") from e
            except openai.APIStatusError as e:
                raise TransientError(f"{type(e).__name__} {e.status_code}: {e.body or e.message}") from e
            except openai.APIConnectionError as e:
                raise TransientError(repr(e)) from e
            if resp.choices:
                break
            # Seen in practice: a 200 response with no choices. Retry, then give up on this turn.
            if attempt == EMPTY_REPLY_RETRIES:
                body = getattr(resp, "model_extra", None) or {}
                raise TransientError(f"no choices in reply after {attempt} attempts: {str(body)[:300]}")
            time.sleep(2 * attempt)
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
            retries=retries,
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
            resp, retries = _with_retries(lambda: self.client.messages.create(**kwargs),
                                          lambda e: _is_transient(e, anthropic))
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
            retries=retries,
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
