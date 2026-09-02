"""One assistant, however it was asked.

This package is an orchestration layer and not a repository rename. Nothing
moved into it: the voice loop is still `aipi5/main.py`, the model client is
still `aipi5/llm/`, the sandboxed maintenance runs are still
`aipi5-agent.service` behind a Unix socket and a root-owned helper. What lives
here is the small amount of code that makes those look like one thing to the
person in the room — a shared event record, and the coordinator that decides
what to do with a sentence.

The reason it exists is a failure that has no error message. There used to be
two surfaces, *Talk* and *Agent*, and asking the same question of one that you
had asked of the other got a different answer, a different set of tools and a
different policy — with no way for anybody to know which they had reached.
`Coordinator` is the single place a sentence arrives, whether it came from the
wake word, the Listen button, the compose box on the panel, or the phone.
"""

from aipi5.assistant.consent import ConsentDesk, Pending
from aipi5.assistant.coordinator import Coordinator
from aipi5.assistant.events import (AssistantEvent, EventLog, EventSink, KINDS,
                                    SOURCES)

__all__ = ["AssistantEvent", "ConsentDesk", "Coordinator", "EventLog",
           "EventSink", "KINDS", "Pending", "SOURCES"]
