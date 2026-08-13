"""The daytime slideshow's photographs, and where they come from.

**Read this before changing anything here.** The Google Photos API this feature
would obviously have been built on no longer exists in the form the
specification assumes, and the shape of everything in this package follows from
that.

On 31 March 2025 Google removed the `photoslibrary.readonly` scope and the rest
of the Library API's read access to a user's own library. An app may now list,
search and read only the media it created itself. Anything that wants to reach
photographs a person already has must use the **Picker API**: the app opens a
*picking session*, the person is sent to Google Photos to choose, and the app
is handed exactly what they chose and nothing else.

Three consequences run through this package and none of them are choices we
made:

1. **There is no album to subscribe to.** The picker does not offer albums as
   units — a person can search by album name and select the results, but what
   comes back is a set of photographs, fixed at the moment they picked. So the
   thing this project remembers and the settings page calls a *collection* is
   that set. Section 7's promise is still kept, which is the part that matters:
   the choice survives a reboot and is never asked for twice.

2. **Access is temporary; the cache is not.** A picked item is reachable only
   while its session lives, and each download URL is good for sixty minutes.
   The local cache is therefore not an optimisation — section 8 asks for one
   anyway — it is the only durable copy, and a sync that has not finished
   before the session expires has lost those photographs until somebody picks
   again. `service.py` downloads as fast as it sensibly can for that reason.

3. **Picking needs a browser signed in to Google, and this device has no
   keyboard.** So the pairing happens on a phone: the Pi shows the picker's URL
   as a QR code on its own screen and polls the session until the person is
   done. The one-time OAuth consent is done over ssh with
   `./scripts/link-google-photos.sh`, which is the only part that needs a
   computer.

Nothing in here holds a secret in the repository. The OAuth client and the
refresh token are two files under `~/.config/aipi5`, written 0600, and no token
is ever logged — `auth.py` has the details.
"""

from aipi5.photos.auth import GoogleAuth, GoogleAuthError
from aipi5.photos.cache import PhotoCache
from aipi5.photos.service import GooglePhotosService

__all__ = ["GoogleAuth", "GoogleAuthError", "GooglePhotosService", "PhotoCache"]
