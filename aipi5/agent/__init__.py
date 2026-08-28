"""The Agent layer: investigate, plan, act, verify, recover.

Separate from the assistant on purpose, and the separation is the design.

`aipi5/llm/tools.py` carries a written guarantee — the model emits a name and a
JSON blob, a fixed table maps it to a Python function, and there is no path from
model output to a shell, a filesystem path or a command line. That guarantee is
what makes the assistant safe to leave listening in a room, and sixteen tests in
`tests/test_tool_safety.py` hold it in place.

An agent contradicts it. So the agent's tools are a **sibling** of that module
and never an entry in its dispatch table, the two live in different processes
under different Unix users, and a test asserts the tables are disjoint. If
`tests/test_tool_safety.py` ever needs editing to accommodate something here,
the wiring is wrong: the guarantee would still be written down, and would no
longer be true.

Three processes:

    aipi5.service            fuwenxu   the assistant; also the phone's door
    aipi5-agent.service      aipi5-agent   this package's runtime, no sudo
    aipi5-agent-helper.service  root    `helper/`, a short list of operations

`helper/` is deliberately not a package. It is copied to
`/usr/local/lib/aipi5-agent/` by `scripts/install-agent.sh` and run by the
system interpreter, so nothing it does depends on this checkout or on anything
`pip` installed.
"""
