// The service worker, which exists for exactly one reason: to be running when
// the app is not.
//
// An installed web app is a page, and a page that nobody is looking at does not
// exist. So the phone cannot be ringing when its app is closed — unless
// something else on the phone is listening, and on iOS that something is this
// file. It is registered once, iOS keeps it, and it is woken by the operating
// system when a push arrives.
//
// It deliberately does almost nothing. No caching, no offline page, no fetch
// handler: this app is useless without the Pi anyway, and a service worker that
// serves a cached copy of the page is a service worker that will one day serve
// a stale one — which is a class of bug this project has already paid for once,
// in the assistant's own page.

self.addEventListener("install", (event) => {
  // Take over immediately rather than waiting for every existing tab to close.
  // A registration that only activates "later" is one that is not there the
  // first time somebody is rung.
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (err) {
    data = {};
  }
  if (data.type !== "call") return;

  const title = data.title || "AIPI5 is calling";
  const body = data.body || "Tap to answer";

  event.waitUntil(self.registration.showNotification(title, {
    body,
    icon: "/icon-180.png",
    badge: "/icon-180.png",
    // `renotify` with a stable tag: a second ring for the same call replaces
    // the first rather than stacking, and still alerts. Without the tag a
    // retried push would leave a column of identical notifications.
    tag: "aipi5-call",
    renotify: true,
    // The call is worthless once it has stopped ringing, so the notification
    // should not sit there afterwards inviting a tap that answers nothing.
    requireInteraction: false,
    data: { session: data.session || "", at: Date.now() },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  // Focus the app if it is already open, and only open a window if it is not.
  // Opening unconditionally would leave two copies of the call page running,
  // both polling, both trying to answer the same session.
  event.waitUntil((async () => {
    const all = await self.clients.matchAll({ type: "window",
                                              includeUncontrolled: true });
    for (const client of all) {
      if ("focus" in client) {
        // Tell the page which call this was, so it can pick up without waiting
        // for its next poll.
        client.postMessage({ type: "answer-call",
                             session: (event.notification.data || {}).session });
        return client.focus();
      }
    }
    // Nothing open. The start URL carries the token — see the note in
    // phone.html about why the fragment is not stripped — so a cold open from
    // here lands on a page that can authenticate.
    if (self.clients.openWindow) {
      return self.clients.openWindow("/?ring=1");
    }
  })());
});
