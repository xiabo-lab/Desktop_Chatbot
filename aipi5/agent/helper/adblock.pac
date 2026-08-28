// Ad and tracker blocking for the agent's browser, and nothing else.
//
// A proxy auto-config file rather than Pi-hole, and rather than
// --host-resolver-rules, for three reasons that were each measured on this
// device:
//
//   * **Pi-hole would not fix what this is for.** It blocks by DNS name, and
//     YouTube serves its ads from googlevideo.com -- the same hosts as the
//     video itself. Blocking those does not remove the ad, it removes
//     YouTube. It would also make this Pi the whole household's DNS server,
//     coupling the assistant, the video calls and the kiosk to a service that
//     did not need to exist.
//
//   * **uBlock Origin cannot load here.** Debian ships 1.67 as Manifest V2 and
//     Chromium 151 refuses third-party MV2 extensions -- silently, with no
//     complaint in the log. The ExtensionManifestV2Availability policy no
//     longer overrides it. Both tested.
//
//   * **--host-resolver-rules works but raises Chromium's yellow "unsupported
//     command-line flag" bar** across the top of the window, and the flag that
//     suppresses it, --test-type, also suppresses certificate warnings. A
//     browser that visits arbitrary pages should keep those.
//
// A PAC file is a supported flag, takes wildcards, costs no warning bar, and
// applies to this browser alone -- so a mistake in the list below cannot stop
// the assistant fetching the weather.
//
// **This is installed to /usr/local/lib/aipi5-agent/, owned by root.** The
// agent cannot edit it, for the same reason it cannot edit policy.py.
//
// What it does not do: YouTube's own pre-roll and mid-roll ads. Those are
// stitched into the same stream as the video, from the same host, and no
// domain-level blocker of any kind removes them.

function FindProxyForURL(url, host) {
  var blocked = [
    // Google's ad stack
    "*.doubleclick.net", "doubleclick.net",
    "*.googlesyndication.com", "googlesyndication.com",
    "*.googleadservices.com", "googleadservices.com",
    "pagead*.googlesyndication.com",
    "partner.googleadservices.com",
    "adservice.google.com", "adservice.google.*",

    // The large exchanges and networks
    "*.adnxs.com", "*.rubiconproject.com", "*.pubmatic.com",
    "*.openx.net", "*.criteo.com", "*.criteo.net",
    "*.taboola.com", "*.outbrain.com", "*.sharethrough.com",
    "*.smartadserver.com", "*.adform.net", "*.casalemedia.com",
    "*.33across.com", "*.indexww.com", "*.districtm.io",
    "*.teads.tv", "*.spotxchange.com", "*.adcolony.com",
    "*.applovin.com", "*.unityads.unity3d.com",
    "*.moatads.com", "*.adsafeprotected.com",
    "*.serving-sys.com", "*.flashtalking.com",
    "*.bidswitch.net", "*.lijit.com", "*.gumgum.com",

    // Analytics and behavioural tracking heavy enough to slow a page
    "*.scorecardresearch.com", "*.quantserve.com",
    "*.hotjar.com", "*.mouseflow.com", "*.fullstory.com",
    "*.crazyegg.com", "*.clarity.ms",
    "*.branch.io", "*.appsflyer.com", "*.adjust.com",
    "*.amplitude.com", "*.mixpanel.com", "*.segment.io",
    "*.chartbeat.com", "*.parsely.com",

    // Social widgets that mostly exist to follow people around
    "*.connect.facebook.net", "*.ads-twitter.com",
    "*.analytics.tiktok.com", "*.ads.linkedin.com",

    // Popups, interstitials and the "allow notifications" industry
    "*.onesignal.com", "*.pushcrew.com", "*.pushengage.com",
    "*.propellerads.com", "*.popads.net", "*.popcash.net",
    "*.adsterra.com", "*.exoclick.com", "*.juicyads.com",
    "*.mgid.com", "*.revcontent.com", "*.zergnet.com",
    "*.content-ad.net", "*.adblade.com"
  ];

  for (var i = 0; i < blocked.length; i++) {
    if (shExpMatch(host, blocked[i])) {
      // Port 9 is discard. Nothing is listening, so the request fails at once
      // rather than hanging -- a page waiting on a dead proxy is worse than a
      // page with an advertisement on it.
      return "PROXY 127.0.0.1:9";
    }
  }
  return "DIRECT";
}
