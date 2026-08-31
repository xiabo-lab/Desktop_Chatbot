"""Yoga Coach v2 curriculum design.

Design data, not shipping code. Nothing in `aipi5/` imports this package: it
exists so the 21 courses, the pose catalog and the transition graph can be
written down, computed and reviewed *before* any of it is wired into the game
or any video is generated — which is what `yoga study.txt` §32 and §36 ask for.

    design/yoga_v2/catalog.py   the poses, and what is known about each
    design/yoga_v2/courses.py   the 21 courses
    design/yoga_v2/export.py    writes the JSON catalogs the generation and
                                QA tooling in §18 consumes

Python is the source of truth and JSON is an artefact of it, because the
import-time validation that catches a mistyped pose id at start-up rather than
at minute thirteen of somebody's practice is worth keeping.
"""
