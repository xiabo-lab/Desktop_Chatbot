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
  if (!["call", "approval", "reminder"].includes(data.type)) return;

  // An approval is the opposite kind of notification to a ring. A missed call
  // is worthless afterwards, so it clears itself; a missed approval is a
  // change that did not happen and a person who should know why, so it stays
  // on the lock screen until it is dealt with.
  const approval = data.type === "approval";
  const reminder = data.type === "reminder";
  const title = data.title || (approval ? "AIPI5 needs your approval"
                             : reminder ? "AIPI5 reminder"
                                        : "AIPI5 is calling");
  const body = data.body || (approval ? "Tap to see what it wants to change"
                           : reminder ? ""
                                      : "Tap to answer");

  event.waitUntil(self.registration.showNotification(title, {
    body,
    icon: "/icon-180.png",
    badge: "/icon-180.png",
    // `renotify` with a stable tag: a second push for the same thing replaces
    // the first rather than stacking, and still alerts. Without the tag a
    // retried push would leave a column of identical notifications.
    // A reminder gets its own tag per reminder, so two set for the same
    // morning do not replace each other -- unlike a ring, where a second push
    // for the same call should collapse into the first.
    tag: approval ? "aipi5-approval"
       : reminder ? "aipi5-reminder-" + (data.token || "")
                  : "aipi5-call",
    renotify: true,
    // A missed call is worthless afterwards. A missed reminder is the whole
    // point, so it stays on the lock screen until it is dealt with.
    requireInteraction: approval || reminder,
    data: { session: data.session || "", at: Date.now(),
            kind: data.type, token: data.token || "" },
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
        const info = event.notification.data || {};
        client.postMessage(
          (info.kind === "approval" || info.kind === "reminder")
            ? { type: "open-agent", token: info.token || "" }
            : { type: "answer-call", session: info.session });
        return client.focus();
      }
    }
    // Nothing open. The start URL carries the token — see the note in
    // phone.html about why the fragment is not stripped — so a cold open from
    // here lands on a page that can authenticate.
    if (self.clients.openWindow) {
      const info = event.notification.data || {};
      return self.clients.openWindow(
        (info.kind === "approval" || info.kind === "reminder")
          ? "/?agent=1" : "/?ring=1");
    }
  })());
});
