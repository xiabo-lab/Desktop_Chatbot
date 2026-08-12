"""File transfer: one folder on the Pi, reachable from the phone.

Three pieces, and the split is deliberate. `store` owns the directory and every
rule about it; `multipart` turns an upload into chunks without holding a file
in memory; `tickets` is what lets Safari download through a plain link without
a bearer token being in it. Neither web server does any filesystem work of its
own — they authenticate, and then they ask this package.
"""

from aipi5.files.store import FileError, FileStore, StoredFile, human_size
from aipi5.files.tickets import Tickets

__all__ = ["FileError", "FileStore", "StoredFile", "Tickets", "human_size"]
