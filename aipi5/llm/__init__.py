"""The conversational layer: the OpenAI client, the context, and the tools.

Reached only when the deterministic router has already declined. Everything in
here is on the slow path by construction — see `aipi5/main.py`, where the fast
path returns before any of this is imported into the turn.
"""
