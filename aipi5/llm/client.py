"""The OpenAI client, built once and kept for the life of the process.

Section 12 of the specification asks for the model to be "loaded to RAM at
boot", which cannot be literally true of a model reached over an API. What is
true, and what this module does, is that everything expensive about talking to
it is done once at startup: the client object, the HTTPS connection pool, the
TLS session. A client constructed per request pays a DNS lookup and a TLS
handshake before it sends a byte, which on a domestic connection is several
hundred milliseconds added to every single answer.

**The model name is configuration, not a constant.** `openai.model` in the YAML
is what the project specification names — `GPT-5.6-Terra` — and this code does
not care what it is. `probe()` asks the API whether it will accept it and says
so plainly at startup, because the failure mode otherwise is an assistant that
boots cleanly, reports healthy, and apologises to the first person who speaks
to it.

**The conversation runs on `/v1/responses`; the agent loop still runs on
`/v1/chat/completions`.** Two endpoints in one file looks like indecision and
is not: the endpoint that answers the person in the room moved because Chat
Completions would not let it use tools at all. The model this project runs
carries a default reasoning effort, and

    400 — Function tools with reasoning_effort are not supported for
    gpt-5.6-luna in /v1/chat/completions. To use function tools, use
    /v1/responses or set reasoning_effort to 'none'.

is what a tool call came back as. `'none'` cleared it, and `_variants` below is
the negotiation that grew out of that — two parameters tried against the model
and remembered, because guessing either from the model name is the guess that
breaks the next time a model is renamed. On `/v1/responses` neither question
exists: `max_output_tokens` is the only spelling, and tools and reasoning are
not in conflict.

The agent's `step()` did not move with it. Its history is a list of chat
messages that `aipi5/agent/loop.py` builds, appends to, and compacts, and the
two endpoints disagree about the shape of exactly the parts that matter — a
tool call and its result. Migrating both in one change would have meant no
working version to compare against on the one path where a bug is a maintenance
run that quietly does the wrong thing.

Nothing here raises into the voice loop. Every method returns a result object
whose failure carries a sentence that can be spoken.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# How many times the model may call tools before the turn is cut off. Two is
# enough for every real case — ask for the weather, then answer; take a
# picture, then answer — and a model looping on a failing tool is the thing
# this bounds. It is a bound on a bug, not a feature limit.
MAX_TOOL_ROUNDS = 3

#: The one tool the startup probe offers. Its only job is to make the probe's
#: request the same *shape* as a real one — a model that takes a plain request
#: and refuses a tools array is the exact failure the probe exists to catch,
#: and it passed cleanly for a week. Named so that a model which calls it
#: anyway is obvious in the log rather than looking like a real tool.
PROBE_TOOL = {
    "type": "function",
    "name": "startup_check",
    "description": "Do not call this. It exists so that the startup probe "
                   "sends the same request shape a real question does.",
    "parameters": {"type": "object", "properties": {}, "required": [],
                   "additionalProperties": False},
    "strict": True,
}

# "not yet negotiated", distinct from None, which is itself a valid setting.
_UNKNOWN = "?"

#: What to ask the API to include in the output.
#:
#: With `store: False` the API keeps nothing, so a reasoning model's own
#: thinking exists only in the response it arrived in. Handed back on the next
#: request it continues that thinking across the tool call; dropped, the model
#: starts again from the text — which does not error, and is why this went
#: unnoticed. It answers slightly worse and nothing says so.
#:
#: Negotiated rather than assumed: an API or a model that does not offer it
#: answers 400, and this device must not stop talking over a field it can do
#: without. See `_include`.
REASONING_INCLUDE = "reasoning.encrypted_content"

#: Item types `/v1/responses` sends back that carry no text and need no answer.
#: Listed so that an unknown one is *logged* rather than silently skipped: the
#: failure being prevented is a model that answered in an item shape this code
#: does not read, which looks from the room like the assistant saying nothing.
QUIET_OUTPUT = frozenset({"reasoning", "web_search_call", "file_search_call",
                          "code_interpreter_call", "computer_call",
                          "image_generation_call"})


@dataclass
class Reply:
    """What came back, and what it cost."""

    text: str = ""
    ok: bool = True
    error: str = ""
    ms: float = 0.0
    tool_calls: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.ok and bool(self.text.strip())


@dataclass
class Step:
    """One request and what came back, for a caller that owns its own history.

    `Reply` is the answer to a *turn* — a sentence, and the tool names that were
    used getting to it. This is the answer to a single *request*: whatever text
    arrived, the tool calls the model wants made, and the assistant message in
    the exact shape the API will take back.
    """

    text: str = ""
    ok: bool = True
    error: str = ""
    ms: float = 0.0
    #: The SDK's own tool-call objects, with `.id` and `.function`.
    tool_calls: list = field(default_factory=list)
    #: `_as_dict(message)` — append this to history verbatim. Rebuilding it
    #: loses the tool-call ids, which the API then rejects.
    message: dict | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def __bool__(self) -> bool:
        return self.ok


class LlmUnavailable(RuntimeError):
    """There is no usable client at all — no key, or the SDK is not installed.

    Distinct from a request that failed. This one is answered at startup by
    running in degraded mode: every Kodama command, the weather, the clock and
    the screensaver work, and conversation says so.
    """


def _needs_responses(client) -> None:
    """Refuse a client that cannot reach `/v1/responses`, and say so plainly.

    The conversation lives on that endpoint. An SDK old enough to lack it still
    imports, still constructs, still satisfies a `>=1.0` pin — and fails on the
    first sentence anybody says, with an `AttributeError` on a private
    attribute that names neither the endpoint nor the version. On a device
    whose only output is a speaker, that arrives as the assistant saying
    nothing.

    Checked once at construction rather than per request: it is a property of
    the installation, and finding out at startup means the preflight report
    says it while somebody is still looking at a terminal.
    """
    if not hasattr(client, "responses"):
        import openai

        raise RuntimeError(
            f"the installed openai package ({getattr(openai, '__version__', '?')}) "
            f"has no Responses API; aipi5 needs at least 1.66 — "
            f"pip install -U 'openai>=1.66,<3'")


class OpenAIClient:
    """One client, one model, and the tool loop around it."""

    def __init__(self, cfg, api_key: str):
        self.cfg = cfg
        self.model = cfg.model
        self._client = None
        self._error: str | None = None
        # None until the first request has told us which spelling this model
        # accepts. See the module docstring.
        self._token_param: str | None = None
        # Whether this model needs `reasoning_effort="none"` alongside tools.
        # Sentinel `_UNKNOWN` rather than None, because None is a real answer
        # here — it means "send no reasoning_effort at all".
        self._tool_effort: str | None = _UNKNOWN
        self._probed: bool | None = None
        #: What to ask `/v1/responses` to include. Emptied on the first refusal
        #: that names it — see `_responses_request`. Set before the early
        #: returns below, because a client with no key is still constructed and
        #: still handed to a test.
        self._include: tuple[str, ...] = (REASONING_INCLUDE,)

        if not api_key:
            self._error = ("no OPENAI_API_KEY in the environment and no key file "
                           "beside the project")
            log.error("conversation is disabled: %s", self._error)
            return

        try:
            from openai import OpenAI
        except ImportError as exc:
            self._error = f"the openai package is not installed ({exc})"
            log.error("conversation is disabled: %s", self._error)
            return

        # Retries are handled here rather than by the SDK's own mechanism so
        # that the timeout is a bound on the *whole* attempt. The SDK's default
        # of two silent retries turns a 20 s timeout into a 60 s wait, which on
        # a device with a 2.5 s target is indistinguishable from a hang.
        self._client = OpenAI(api_key=api_key, timeout=cfg.timeout_s, max_retries=0)
        _needs_responses(self._client)
        log.info("OpenAI client ready for model %r", self.model)

    # ── startup ──────────────────────────────────────────────────────

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def error(self) -> str | None:
        return self._error

    def probe(self) -> tuple[bool, str]:
        """Ask the API whether this model exists and answers. (ok, detail).

        Run at startup, off the voice path, so that a wrong model name is a
        line in the boot log naming the model rather than an apology to the
        first person who speaks. Deliberately a real request rather than a
        `models.retrieve` — a model can be listed and still refuse the request
        shape this assistant sends, and it is the request shape that matters.

        **It sends a tools array.** That is the whole reason this is worth
        doing: the failure it exists to catch was a model that accepted a plain
        completion, reported healthy at boot, and rejected every request that
        offered it a tool — so the first question anybody asked that needed one
        came back as an apology. A probe without tools would have passed that
        day too.
        """
        if self._client is None:
            return False, self._error or "no client"

        started = time.monotonic()
        try:
            response = self._responses_request(
                items=[{"role": "user", "content": "Reply with the word ready."}],
                instructions="Answer in one word.",
                tools=[PROBE_TOOL],
                max_output_tokens=self.cfg.max_output_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            self._probed = False
            detail = _explain(exc, self.model)
            log.error("model %r is not usable: %s", self.model, detail)
            return False, detail

        self._probed = True
        ms = (time.monotonic() - started) * 1000
        text = _output_text(response)
        log.info("model %r answered in %.0f ms (%r)", self.model, ms, text[:40])
        return True, f"answered in {ms:.0f} ms"

    # ── the conversational turn ──────────────────────────────────────

    def respond(self, conversation, system: str, toolbox=None) -> Reply:
        """One turn: send, run any tools, send again, return what to say.

        The loop is bounded and every exit says something. A model that keeps
        asking for tools until `MAX_TOOL_ROUNDS` gets one final request with no
        tools offered, so the turn ends in a sentence rather than in silence.

        The system prompt goes as `instructions` rather than as a first message.
        It is rebuilt every turn — it carries the clock and the language of the
        utterance — so it is not history and putting it in the history would
        mean trimming around it.
        """
        if self._client is None:
            return Reply(ok=False, error=self._error or "no client")

        started = time.monotonic()
        called: list[str] = []
        tools = toolbox.schemas() if toolbox is not None else None

        for round_number in range(MAX_TOOL_ROUNDS + 1):
            # The last round is asked without tools, so the model has no choice
            # but to answer. Without this a tool-happy model can spend the
            # whole budget calling things and never say anything.
            offer = tools if round_number < MAX_TOOL_ROUNDS else None
            try:
                response = self._responses_request(
                    items=conversation.items(),
                    instructions=system,
                    tools=offer,
                    max_output_tokens=self.cfg.max_output_tokens,
                )
            except Exception as exc:  # noqa: BLE001
                detail = _explain(exc, self.model)
                log.warning("request failed: %s", detail)
                return Reply(ok=False, error=detail,
                             ms=(time.monotonic() - started) * 1000,
                             tool_calls=called)

            text, calls, builtins, carry = _read_output(response)
            called.extend(builtins)

            unfinished = _unfinished(response)
            if unfinished:
                # `failed` and `incomplete` both come back with a 200 and an
                # `output` that may be empty or half-formed. Read as an
                # ordinary answer that is one of those is the assistant saying
                # nothing, with the reason sitting unexamined in the response.
                log.warning("the model did not finish: %s", unfinished)
                return Reply(ok=False, error=unfinished,
                             ms=(time.monotonic() - started) * 1000,
                             tool_calls=called)

            if not calls:
                text = text.strip()
                conversation.assistant(text)
                return Reply(text=text, ok=bool(text),
                             error="" if text else "the model returned nothing",
                             ms=(time.monotonic() - started) * 1000,
                             tool_calls=called)

            if toolbox is None:
                # Should not happen — tools were not offered — but a model that
                # calls one anyway must not leave the turn hanging.
                log.warning("the model called a tool that was never offered")
                return Reply(ok=False, error="the model asked for a tool that "
                                             "does not exist here",
                             ms=(time.monotonic() - started) * 1000)

            # Every call is stored before any of them is answered. The API
            # rejects a `function_call_output` whose `call_id` it has not seen,
            # and it rejects a `function_call` left unanswered — so the pair
            # travels together or the next request is a 400 that says nothing
            # about which half was missing.
            # `carry`, not `calls`: the reasoning that led to them travels
            # with them. See `_read_output`.
            conversation.assistant_tool_calls(carry)
            for call in calls:
                called.append(call["name"])
                result = toolbox.call(call["name"], call["arguments"])
                conversation.tool_result(call["call_id"], result)

        # Unreachable: the final round is asked with no tools and therefore
        # cannot come back with tool calls. Kept as a real return rather than
        # an assert so that a future change to the loop fails as a spoken
        # apology instead of an exception in the voice path.
        return Reply(ok=False, error="the conversation did not finish",
                     ms=(time.monotonic() - started) * 1000, tool_calls=called)

    def describe_image(self, data_url: str, instruction: str, question: str = "",
                       ) -> Reply:
        """One vision request. Its own method because nothing about it is a turn.

        Not added to the conversation: a picture is a fact about the room at
        one moment, and carrying a base64 JPEG forward into every subsequent
        request would multiply the cost of the rest of the conversation by a
        large constant for no benefit. The *description* is what goes into the
        history, as ordinary text, by the caller.
        """
        if self._client is None:
            return Reply(ok=False, error=self._error or "no client")

        prompt = question.strip() or "What do you see?"
        started = time.monotonic()
        try:
            response = self._responses_request(
                items=[{"role": "user", "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": data_url},
                ]}],
                instructions=instruction,
                tools=None,
                max_output_tokens=self.cfg.max_output_tokens,
                model=self.cfg.vision,
                timeout=self.cfg.vision_timeout_s,
            )
        except Exception as exc:  # noqa: BLE001
            detail = _explain(exc, self.cfg.vision)
            log.warning("vision request failed: %s", detail)
            return Reply(ok=False, error=detail,
                         ms=(time.monotonic() - started) * 1000)

        text = _output_text(response).strip()
        return Reply(text=text, ok=bool(text),
                     error="" if text else "the model described nothing",
                     ms=(time.monotonic() - started) * 1000)

    # ── the one place a request is actually made ─────────────────────

    def step(self, messages: list[dict], tools: list[dict] | None = None,
             *, max_tokens: int | None = None,
             model: str | None = None, timeout: float | None = None) -> Step:
        """One request. No conversation object, no history management.

        `respond()` owns a *turn*: it holds a `Conversation`, runs the tool loop
        to `MAX_TOOL_ROUNDS` and returns a sentence to speak. The agent needs
        the request without the turn — its history is not a conversation, its
        ceiling is not three, and what it does between calls is its own.

        Everything hard is still shared. `_request` carries the two negotiated
        request shapes, and the second of them matters here more than anywhere:
        `gpt-5.6-luna` refuses a tools array unless `reasoning_effort` is
        `"none"`, and an agent loop is nothing but tool calls. Re-implementing
        this would rediscover that the slow way.

        Never raises. The agent loop has to be able to report a failed step and
        decide whether to try again.
        """
        started = time.monotonic()
        try:
            response = self._request(
                messages=messages,
                tools=tools or None,
                max_tokens=max_tokens or self.cfg.agent_max_output_tokens,
                model=model or self.cfg.agent,
                timeout=timeout if timeout is not None else self.cfg.agent_timeout_s,
            )
        except Exception as exc:                    # noqa: BLE001
            ms = (time.monotonic() - started) * 1000.0
            log.warning("agent step failed after %.0f ms: %s", ms, exc)
            return Step(ok=False, ms=ms,
                        error=_explain(exc, model or self.cfg.agent))

        ms = (time.monotonic() - started) * 1000.0
        try:
            message = response.choices[0].message
        except (AttributeError, IndexError):
            return Step(ok=False, ms=ms,
                        error="the model returned no message")
        usage = getattr(response, "usage", None)
        return Step(
            text=(getattr(message, "content", None) or "").strip(),
            ok=True,
            ms=ms,
            tool_calls=list(getattr(message, "tool_calls", None) or []),
            message=_as_dict(message),
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        )

    def _responses_request(self, *, items, instructions, tools,
                           max_output_tokens, model=None, timeout=None):
        """Send one `/v1/responses` request, retrying the network once.

        One retry rather than two questions. The token-parameter and
        `reasoning_effort` negotiations that `_request` carries do not exist
        here — `max_output_tokens` is the only spelling this endpoint takes,
        and tools and reasoning are not in conflict on it — so all that is left
        is a link that dropped.

        Retries are counted here rather than left to the SDK for the reason
        given at construction: `max_retries=0` makes the configured timeout a
        bound on the *whole* attempt, and the SDK's default of two silent
        retries turns a 20 s timeout into a 60 s wait, which on a device with a
        2.5 s target is indistinguishable from a hang.
        """
        model = model or self.model
        attempts = self.cfg.max_retries + 1
        last: Exception | None = None

        for attempt in range(attempts):
            kwargs = {
                "model": model,
                "input": items,
                "max_output_tokens": max_output_tokens,
                # Nothing on this device wants the API to keep a copy. The
                # conversation is held here, bounded and self-expiring, and a
                # stored response is a transcript of a living room sitting on
                # somebody else's disk with no expiry this code controls.
                "store": False,
            }
            if self._include:
                kwargs["include"] = list(self._include)
            if instructions:
                kwargs["instructions"] = instructions
            if tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            if timeout is not None:
                kwargs["timeout"] = timeout

            try:
                return self._client.responses.create(**kwargs)
            except Exception as exc:
                last = exc
                if self._include and _rejects_include(exc):
                    # Negotiated the way the token parameters are, and for the
                    # same reason: a device that stops answering because one
                    # optional field was not recognised is worse than one that
                    # answers slightly less well. Said once, at INFO, because
                    # it is a property of the model rather than a fault.
                    log.info("this model does not carry reasoning across tool "
                             "calls; continuing without it")
                    self._include = ()
                    continue
                if attempt + 1 < attempts and _is_transient(exc):
                    log.warning("request failed (%s); retrying once",
                                type(exc).__name__)
                    continue
                raise

        if last is not None:
            raise last
        raise RuntimeError("the request could not be sent")

    def _request(self, *, messages, tools, max_tokens, model=None, timeout=None):
        """Send, retrying the token parameter and the network once each.

        Two different retries, deliberately not merged. The token-parameter
        retry happens once in the life of the process and fixes a request that
        was malformed for this model; the network retry happens per call and
        covers a link that dropped. Merging them would retry a malformed
        request against a timeout, which cannot succeed.
        """
        model = model or self.model
        attempts = self.cfg.max_retries + 1
        # Kept so the caller is told what actually went wrong. Falling out of
        # both loops and raising a fresh "could not be sent" would replace a
        # 401 or an unknown-model 404 — the two failures anybody deploying this
        # is most likely to hit — with a sentence that names no cause at all.
        last: Exception | None = None

        for attempt in range(attempts):
            for token_param, effort in self._variants(bool(tools)):
                kwargs = {
                    "model": model,
                    "messages": messages,
                    token_param: max_tokens,
                }
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"
                    if effort is not None:
                        kwargs["reasoning_effort"] = effort
                if timeout is not None:
                    kwargs["timeout"] = timeout

                try:
                    response = self._client.chat.completions.create(**kwargs)
                except Exception as exc:
                    last = exc
                    if _is_token_param_error(exc) and self._token_param is None:
                        log.info("%s: this model wants the other token parameter",
                                 model)
                        continue
                    if _is_reasoning_effort_error(exc) and self._tool_effort is _UNKNOWN:
                        log.info("%s: adjusting reasoning_effort for tool calls",
                                 model)
                        continue
                    if attempt + 1 < attempts and _is_transient(exc):
                        log.warning("request failed (%s); retrying once",
                                    type(exc).__name__)
                        break  # out of the variant loop, into the next attempt
                    raise
                # It worked. Remember both, so this is negotiated once per
                # process rather than once per request.
                if self._token_param is None:
                    self._token_param = token_param
                    log.debug("using %s for this model", token_param)
                if tools and self._tool_effort is _UNKNOWN:
                    self._tool_effort = effort
                    log.info("tool calls on %s use reasoning_effort=%r", model, effort)
                return response

        if last is not None:
            raise last
        raise RuntimeError("the request could not be sent")

    def _variants(self, has_tools: bool):
        """The request shapes to try, best first.

        Two parameters have to be negotiated against the model rather than
        guessed from its name, and this yields their combinations so the
        request loop stays one loop.

        **The token parameter.** `max_completion_tokens` on current models,
        `max_tokens` on older ones, and each rejects the other.

        **`reasoning_effort`, but only alongside tools.** Measured against
        `gpt-5.6-luna`, which is what this project runs: a plain completion is
        accepted with no `reasoning_effort` at all, and the *same* request with
        a `tools` array comes back

            400 — Function tools with reasoning_effort are not supported for
            gpt-5.6-luna in /v1/chat/completions. To use function tools, use
            /v1/responses or set reasoning_effort to 'none'.

        so the model carries a default effort that tools cannot be combined
        with, and `'none'` is the one value that clears it. `'minimal'` and
        `'low'` are both refused. Sending it unconditionally is not an option
        either — a model with no such parameter rejects the field outright —
        which is why this is negotiated and then remembered.
        """
        token_params = ((self._token_param,) if self._token_param is not None
                        # Newest first: `max_completion_tokens` is what current
                        # models want, so the common case costs no extra round
                        # trip.
                        else ("max_completion_tokens", "max_tokens"))

        if not has_tools:
            efforts: tuple[str | None, ...] = (None,)
        elif self._tool_effort is not _UNKNOWN:
            efforts = (self._tool_effort,)
        else:
            # `"none"` first. A model that has no `reasoning_effort` at all
            # rejects it and the second pass sends none, which is one wasted
            # round trip once per process against a failed tool call on every
            # conversation that needs one.
            efforts = ("none", None)

        for token_param in token_params:
            for effort in efforts:
                yield token_param, effort

    def describe(self) -> dict:
        """For the settings page."""
        return {
            "provider": "openai",
            "model": self.model,
            "vision_model": self.cfg.vision,
            "available": self.available,
            "verified": self._probed,
            "error": self._error,
            "context_turns": self.cfg.context_turns,
        }

    def close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            client.close()
        except Exception:
            log.debug("closing the OpenAI client failed", exc_info=True)


# ── reading the SDK's answers and its complaints ─────────────────────


def _content_of(response) -> str | None:
    try:
        return response.choices[0].message.content
    except (AttributeError, IndexError):
        return None


# ── reading a Responses answer ───────────────────────────────────────
#
# **Every output item, not the first one.** `response.output` is a list and
# what is in it varies with the model and the request: a reasoning item, then a
# web search the model ran itself, then two function calls, then the message.
# Reading `output[0]` works until the day a reasoning item is emitted in front
# of the answer, and then the assistant goes quiet with nothing in the log to
# say why.
#
# `output_text` is the SDK's own convenience for the same walk and is used
# where it exists, with the walk as the fallback — the fallback is what the
# tests drive, because a fake response object that has to grow an
# `output_text` property is a fake that stops resembling the thing it stands in
# for.


def _item(entry, name, default=None):
    """One field of an output item, whether it is an object or a dict.

    The SDK hands back typed objects; a `response.model_dump()`, a replay from
    a log, and every fake in the tests are dictionaries. Both are read the
    same way here rather than converting, because converting an item and
    sending it back is how a `call_id` gets regenerated.
    """
    if isinstance(entry, dict):
        return entry.get(name, default)
    return getattr(entry, name, default)


def _output_text(response) -> str:
    """Everything the model said, in order, as one string."""
    text = getattr(response, "output_text", None)
    if isinstance(text, str) and text:
        return text

    parts: list[str] = []
    for entry in getattr(response, "output", None) or []:
        if _item(entry, "type") != "message":
            continue
        for chunk in _item(entry, "content") or []:
            if _item(chunk, "type") in ("output_text", "text"):
                value = _item(chunk, "text") or ""
                if value:
                    parts.append(value)
    return "".join(parts)


def _read_output(response) -> tuple[str, list[dict], list[str], list[dict]]:
    """(text, function calls, built-in tools it ran, items to send back).

    The fourth is what the next request in this turn has to repeat: the
    model's reasoning and the calls it asked for, in the order they arrived.
    Dropping the reasoning does not error — the model simply picks the thread
    up from the text instead of from its own thinking, slightly worse, with
    nothing anywhere saying so.

    The function calls come back as plain dictionaries in the exact shape the
    API takes them *back* in — `{"type", "call_id", "name", "arguments"}` —
    because that is what goes into the conversation, and a rebuilt item with a
    regenerated `call_id` is rejected with an error about mismatched ids that
    says nothing about where the ids came from.
    """
    calls: list[dict] = []
    builtins: list[str] = []
    carry: list[dict] = []
    for entry in getattr(response, "output", None) or []:
        kind = _item(entry, "type")
        if kind == "reasoning":
            # Kept and handed straight back on the next request. With
            # `store: False` the API remembers nothing, so this is the only
            # copy of the model's own thinking, and a tool call in the middle
            # of a turn is exactly where it needs to survive. Verbatim,
            # including the encrypted content: it is not for this process to
            # read, only to return.
            carry.append(_verbatim(entry))
        elif kind == "function_call":
            item = {
                "type": "function_call",
                "call_id": _item(entry, "call_id") or _item(entry, "id") or "",
                "name": _item(entry, "name") or "",
                "arguments": _item(entry, "arguments") or "{}",
            }
            calls.append(item)
            carry.append(item)
        elif kind == "web_search_call":
            # Run by the API, not by this process. Recorded so the turn's log
            # line says a search happened; there is no result to send back.
            builtins.append("web_search")
        elif kind in QUIET_OUTPUT or kind == "message":
            continue
        else:
            # Not an error — the API adds item types — but worth a line,
            # because an answer that arrived in a shape this code does not read
            # looks from the room like the assistant saying nothing at all.
            log.info("ignoring an output item of type %r", kind)
    return _output_text(response), calls, builtins, carry


def _verbatim(entry) -> dict:
    """One output item as a plain dictionary, unchanged.

    Not rebuilt from named parts. The point of these is to go back to the API
    exactly as they arrived — an item this code understood well enough to
    reconstruct would be one it could have left out, and the fields that matter
    here are the ones it deliberately does not read.
    """
    for method in ("model_dump", "to_dict", "dict"):
        convert = getattr(entry, method, None)
        if callable(convert):
            try:
                return {k: v for k, v in convert().items() if v is not None}
            except Exception:                        # noqa: BLE001
                break
    if isinstance(entry, dict):
        return {k: v for k, v in entry.items() if v is not None}
    return {"type": _item(entry, "type") or "reasoning"}


def _as_dict(message) -> dict:
    """An assistant message with tool calls, in the shape the API takes back.

    The SDK's own `model_dump` where it has one, because the `id` on each tool
    call has to survive the round trip exactly — a rebuilt message with a
    regenerated id is rejected with an error about mismatched tool call ids
    that says nothing about where the ids came from.
    """
    if hasattr(message, "model_dump"):
        dumped = message.model_dump(exclude_none=True)
        # `content` is None on a pure tool-call message and some API versions
        # reject its absence rather than its being null.
        dumped.setdefault("content", None)
        return dumped
    return {
        "role": "assistant",
        "content": getattr(message, "content", None),
        "tool_calls": [
            {"id": c.id, "type": "function",
             "function": {"name": c.function.name,
                          "arguments": c.function.arguments}}
            for c in message.tool_calls
        ],
    }


def _is_token_param_error(exc: Exception) -> bool:
    """Is this the API objecting to `max_tokens` vs `max_completion_tokens`?

    Matched on the message rather than on a type, because it arrives as a
    generic `BadRequestError` whose only distinguishing feature is its text.
    Narrow on purpose: it must not swallow other 400s, which are real errors
    that should be reported rather than retried into a second failure.
    """
    text = str(exc).lower()
    return ("max_tokens" in text or "max_completion_tokens" in text) and (
        "unsupported" in text or "not supported" in text or "instead" in text
        or "unrecognized" in text)


def _is_reasoning_effort_error(exc: Exception) -> bool:
    """Is this the API objecting to `reasoning_effort` beside a tool list?

    Matched on the text for the same reason as the token parameter: it arrives
    as a generic `BadRequestError` and the only thing distinguishing it is what
    it says. Narrow deliberately — it must not swallow other 400s, which are
    real errors that should be reported rather than retried into a second
    failure.
    """
    text = str(exc).lower()
    return "reasoning_effort" in text and (
        "not supported" in text or "unsupported" in text
        or "does not support" in text or "unrecognized" in text)


def _is_transient(exc: Exception) -> bool:
    """Worth trying once more, as opposed to worth reporting.

    A timeout, a connection reset, a 429 or a 5xx. Not a 400, not a 401 — those
    will fail identically the second time and the retry only costs the person
    in the room another few seconds of silence.
    """
    name = type(exc).__name__.lower()
    if any(word in name for word in ("timeout", "connection", "apiconnection")):
        return True
    status = getattr(exc, "status_code", None)
    return status in (408, 409, 429, 500, 502, 503, 504)


def _unfinished(response) -> str:
    """Why this response is not an answer, or "" when it is one.

    `/v1/responses` returns 200 for a response that `failed` or stopped
    `incomplete` — out of output tokens, or filtered — with an `output` that
    may be empty or cut short. Read as an ordinary answer, that is the
    assistant saying nothing at all in a room, with the reason sitting
    unexamined in a field nobody looked at.
    """
    status = _item(response, "status") or ""
    if status not in ("failed", "incomplete"):
        return ""
    detail = getattr(response, "incomplete_details", None) or getattr(
        response, "error", None)
    reason = _item(detail, "reason") or _item(detail, "message") or ""
    if "max_output_tokens" in str(reason):
        return "I ran out of room before I finished that answer."
    if reason:
        return f"I could not finish that ({reason})."
    return "I could not finish that answer."


def _rejects_include(exc: Exception) -> bool:
    """Whether this failure is the API refusing the `include` field.

    Narrow on purpose. A 400 that names the field or the value is one to stop
    asking about; every other 400 is a real fault in the request and must
    surface rather than be quietly retried without a field that had nothing to
    do with it.
    """
    said = str(exc).lower()
    if isinstance(exc, TypeError):
        # An SDK old enough not to know the argument at all. It never reaches
        # the network, so there is no status to read.
        return "include" in said
    if getattr(exc, "status_code", None) not in (400, 404, 422):
        return False
    return "include" in said or "encrypted_content" in said


def _explain(exc: Exception, model: str) -> str:
    """An API failure as a sentence somebody could act on.

    The model name is in it for the one failure that is most likely on this
    project and least obvious from the raw error: `GPT-5.6-Terra` is the name
    the specification gives and it is the assistant's job to say clearly that
    the API did not recognise it, rather than to report a 404 and leave
    somebody reading a traceback to work out which of several identifiers was
    wrong.
    """
    status = getattr(exc, "status_code", None)
    text = str(exc)

    if status == 401 or "invalid_api_key" in text or "Incorrect API key" in text:
        return "the OpenAI API key was rejected"
    if status == 404 or "does not exist" in text or "model_not_found" in text:
        return (f"the API does not recognise the model {model!r} — check "
                f"openai.model in config/aipi5.yaml")
    if status == 429:
        return "the OpenAI account is rate limited or out of quota"
    if any(word in type(exc).__name__.lower() for word in ("timeout", "connection")):
        return "the OpenAI API did not answer in time"
    if status and status >= 500:
        return "the OpenAI API returned a server error"
    return f"{type(exc).__name__}: {text[:200]}"
