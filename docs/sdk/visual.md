# Visual experiments: the browser script

!!! note "Not yet available"
    The visual editor is not yet available in Controlvar, and nothing in Controlvar offers this script yet.
    This page describes what the script does on your pages, so that it can be reviewed before it is offered.

## The tag

Put the tag first in the `<head>` of each page an experiment may change:

```html
<script src="https://controlvar.example.com/controlvar-visual.js" data-api-url="https://api.controlvar.example.com" data-api-key="YOUR_WEBSITE_KEY" data-require-consent></script>
```

| Attribute | What it does |
|---|---|
| `data-api-url` | Your Controlvar API's address. Without it, or without the key, the script logs one error and does nothing. |
| `data-api-key` | An API key with no scopes, made for your website. A key with any scope is refused, because anyone who opens the page can read it. |
| `data-require-consent` | The script waits for `controlvar.visual.consent()` before it reads, stores or sends anything about the visitor. |
| `data-timeout-ms` | How long the page may stay hidden while its changes are applied: 0 to 4000 milliseconds, 1000 by default; 0 never hides it. |
| `data-user-id` | Your own id for the visitor, at most 255 characters. Use an id that does not identify a person, such as an account number, not an email address: anyone with your page's key can ask what that id is assigned to. |
| `data-cookie-name` | Without `data-user-id`, the script keeps a random id in a cookie of this name, `controlvar_uid` by default. |
| `data-attributes` | A JSON object of at most 4 KB about the visitor, sent with each call for the experiments' audiences. |

If your site sends a Content-Security-Policy, allow the script's address in `script-src` and your Controlvar API in `connect-src`.
The script needs no `'unsafe-inline'` and no `'unsafe-eval'`, and it writes styles through the CSSOM, which `style-src` does not restrict:

```text
script-src 'self' https://controlvar.example.com; connect-src 'self' https://api.controlvar.example.com
```

## What the script sends

On each page an experiment covers, the script makes one call to your Controlvar API with the visitor's id, the page's host and path, and `data-attributes` if the tag has it.
It never sends the query string or the part of the address after `#`.
It keeps the page rules of the running experiments for 60 seconds, and a page none of them covers makes no call and is never hidden.

When a change cannot be applied, the script sends an error report with the experiment's key, a fixed message, the change's position and the reason.
A report never holds a feature flag key, the visitor's id, the page's address or anything from the page, so it never counts toward a flag's safety rollback.
At most 5 reports are sent per page load.

`controlvar.visual.track(event, { value })` sends one event for each experiment the visitor was assigned to in the last 30 days, so a purchase on another page counts for the experiment the visitor saw first.

Requests carry no cookies and no Referer.

## What it stores in the browser

| Key | Where | What it holds | How long |
|---|---|---|---|
| `controlvar_uid` | cookie | The visitor's random id, when the page sets no data-user-id. Its name is data-cookie-name's default. | 30 days (Max-Age=2592000, Path=/, SameSite=Lax, Secure on https) |
| `controlvar_assigned` | localStorage | The keys of the experiments this visitor was assigned to, each with the time it was last seen, so track() can name them. | 30 days per experiment |
| `controlvar_pages` | localStorage | The page rules of the running visual experiments on this site (match and path), so a page none of them covers makes no call. | 60 seconds |
| `controlvar_backoff` | sessionStorage | The time until which the script sends nothing, set after a refused or failed call. | 5 minutes, in this tab |
| `controlvar_preview` | sessionStorage | A preview link's token, so the preview lasts for the tab. | this tab |
| `controlvar_editor_open` | sessionStorage | "1" once a visual editor link was opened in this tab. | this tab |

Every key starts with `controlvar_`, and `revokeConsent()` deletes all of them, with the keys later versions of the script will use.

## Consent

If your page waits for consent (`data-require-consent`), the script reads, stores and sends nothing about your visitors before `consent()`.
The one exception is a visual-editor link that someone on your team opens from Controlvar: the script then sends only that link's one-time sign-in, opens the editor's frame and stores the editor's own key, and nothing about any visitor.
Call `controlvar.visual.consent()` once the visitor agrees, and the script applies the page's changes.
A `consent()` made while the page's `<head>` is still loading (a stored consent read there) hides the page as usual; a later one applies the changes in view, without hiding the page.

`controlvar.visual.revokeConsent()` stops the script: nothing more is sent from this page, and it deletes every cookie it set and every `controlvar_` key in this site's localStorage and sessionStorage.
Changes already shown stay until the visitor leaves the page.
With `data-require-consent`, later pages wait for `consent()` again; without it the next page starts again, so a site that lets visitors withdraw consent sets `data-require-consent`.
A [Split URL](../api/split-url.md#the-visitor-id-cookie) server integration given a cookie domain sets `controlvar_uid` for that parent domain, which `revokeConsent()` does not delete, so a later `consent()` uses the same id.
Have your server expire that cookie too when a visitor withdraws consent.
Events already sent are not deleted; they stay in your Controlvar database.

## While the page is hidden

While the script hides the page (until its changes are applied, and at most `data-timeout-ms`, 1 second by default), the browser paints nothing, so First Contentful Paint and Largest Contentful Paint are reported that much later.
Clicks and Tab reach none of the page's controls until it shows.

On Next.js's App Router, React places the framework's own scripts ahead of this one in `<head>`, and in development React logs "Encountered a script tag" for it.

## Preview links

A preview link opens your page with one variant's changes and a banner with an Exit preview button.
The preview lasts for the tab, turns every other experiment off and records no assignment, no exposure and no event; a change it cannot apply is still reported.
No change is ever applied to the page's `<html>` element or to the banner.

## Visitor IP addresses

Controlvar's database stores no visitor IP address.
Like any web server, your Controlvar server's access logs (the API's, and the dashboard's nginx for /api/) record each request's address, and so may a proxy in front of it.
The rate limiter counts requests per address in Redis, in counters that expire with their one-minute window.
The logs are kept as long as your deployment keeps them.
