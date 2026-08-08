"""What the assistant can find out, as opposed to what it can say.

Each module here is a plain Python object with plain Python methods, knows
nothing about the language model, and can be exercised from a REPL. That is the
whole design rule: the model calls these, but they do not depend on it, so a
broken weather feed is a broken weather feed rather than a broken assistant,
and each was unit-tested before it was ever spoken to.

`aipi5/llm/tools.py` is the adapter that describes them to the model.
"""
