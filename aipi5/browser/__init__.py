"""Opening a website by voice, in a browser window of its own.

One command per site, from a fixed table. Not a spoken URL bar: what the model
may reach is decided in source and not in an utterance, which is the same rule
`aipi5/llm/tools.py` follows and the reason there is no `{site}` slot here.

This is not the agent's browser (`aipi5/agent/helper/browser.py`). That one is
driven over a CDP pipe held by root, exists to be *automated*, and is only
reachable through the agent runtime — a separate user, a separate service, and
one that is normally not running. This is a window for a person to use with
their hands, started by the assistant as the user it already is.
"""
