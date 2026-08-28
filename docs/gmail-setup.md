# Letting the Pi send you email

Reminders already work without this — they arrive as a notification on your
phone. This adds a second way to deliver them, and it is worth having for the
things a notification is bad at: something you want to find again by searching
your mail, something that should reach a laptop, or something with a bit of
detail in it.

Twenty minutes, mostly waiting for Google's console to load.

---

## What you already have

Checked on the device on 2026-08-27:

| | |
|---|---|
| Google Cloud project | **`ai-assistant-photo`** |
| OAuth client | Desktop app, `5667150559-…` |
| Current scope | `photospicker.mediaitems.readonly` |
| Refresh token | obtained 2026-08-13, **still working 14 days later** |

That last row matters. A consent screen left in **Testing** expires refresh
tokens after seven days. Yours has survived fourteen, so the screen is almost
certainly already **In production** — which is the step that usually catches
people out, and you appear to have done it already. Step 1 below is just
confirming that.

**Kodama-Lite's login cannot be used for this.** It is a browser session — the
cookies that keep YouTube Music signed in. The Gmail API needs an OAuth token
with a send scope, and there is no way to turn one into the other. It is not a
shortcut that was missed; it is a different kind of credential.

---

## What you are granting

`gmail.send` and nothing else. Worth being precise, because "give the Pi access
to my Gmail" sounds larger than what this is:

**It can** compose and send mail as you.

**It cannot** read your mail, list it, search it, or see who has written to
you. `gmail.send` is write-only — there is no read call it authorises.

**The realistic bad day**: if the Pi were compromised, someone could send mail
that appears to come from you. They could not read your inbox. If that
possibility bothers you, use a separate Google account for the device instead
and tell me — it is the same work, in a new project.

---

## Step 1 — check the consent screen is published

<https://console.cloud.google.com/auth/overview?project=ai-assistant-photo>

Under **Audience**, the publishing status should say **In production**.

- If it does: nothing to do. Carry on.
- If it says **Testing**: press **Publish app**. Without this the token expires
  after seven days and a reminder set eight days out silently never sends —
  the failure being that nothing happens, which is the hardest kind to notice.

Google may warn about verification. Ignore it: verification is for apps with
outside users, and this one has exactly one.

---

## Step 2 — add the send scope

<https://console.cloud.google.com/auth/scopes?project=ai-assistant-photo>

**Add or remove scopes** → filter for `gmail.send` → tick
`https://www.googleapis.com/auth/gmail.send` → **Update** → **Save**.

You should end with two scopes: the photos picker one and this.

---

## Step 3 — turn the Gmail API on

<https://console.cloud.google.com/apis/library/gmail.googleapis.com?project=ai-assistant-photo>

Press **Enable**. If it already says *Manage*, it is on.

This is separate from the scope, and forgetting it produces
`403 Gmail API has not been used in project …`, which reads like a permissions
problem rather than a switch nobody flipped.

---

## Step 4 — link the account, on the Pi

I will have installed `scripts/link-gmail.sh` by then. From your laptop:

```sh
ssh -N -L 8095:127.0.0.1:8095 aipi5     # in one terminal, and leave it
ssh aipi5 './AIPI5/scripts/link-gmail.sh'
```

It prints a URL. Open it in a browser **signed in as the account you want the
Pi to send from**, approve the two scopes, and the redirect lands back through
the tunnel. The script writes the token to
`~/.config/aipi5/gmail-token.json`, mode 0600, and prints the address it will
send from.

This mirrors `scripts/link-google-photos.sh`, which you have run before; the
tunnel is there for the same reason — Google's redirect goes to `localhost`,
and on this device the only browser is a kiosk with no address bar.

---

## Step 5 — tell me

I switch `deliver` on, and `"email me tomorrow at 8"` starts working alongside
the notification. Nothing else changes.

---

## What to expect afterwards

- The Pi sends from the account you linked. Mail to yourself lands in your own
  inbox, usually filed under Sent as well.
- The default recipient is that same account. Sending anywhere else asks you
  first, on the phone, the same way a configuration change does.
- If the token is ever revoked, reminders fall back to the phone notification
  and the agent says the mail could not be sent — it does not fail silently.

## If something goes wrong

| What you see | What it means |
|---|---|
| `403 Gmail API has not been used` | Step 3 was skipped |
| `invalid_grant` after about a week | Step 1 was skipped: the screen is still in Testing |
| `Request had insufficient authentication scopes` | Step 2 saved, but the token predates it — re-run step 4 |
| The redirect never arrives | The `ssh -L` tunnel is not up, or is on a different port |
