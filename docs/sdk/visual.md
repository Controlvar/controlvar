# Visual experiments: the browser script

!!! note "Not yet available"
    The visual editor is not yet available in Controlvar, and nothing in Controlvar offers this script yet.
    This page describes what the script does on your pages, so that it can be reviewed before it is offered.

## The tag

Put the tag first in the `<head>` of each page an experiment may change:

```html
<script src="https://controlvar.example.com/controlvar-visual.js" data-api-url="https://api.controlvar.example.com" data-api-key="YOUR_WEBSITE_KEY" data-require-consent></script>
```

Placed anywhere else (in `<body>`, with `defer` or `async`, or added by a tag manager), the script may not hide the page, so visitors can see the original page before its changes.

| Attribute | What it does |
|---|---|
| `data-api-url` | Your Controlvar API's address. Without it, or without the key, the script logs one error and does nothing. |
| `data-api-key` | An API key with no scopes, made for your website. A key with any scope is refused, because anyone who opens the page can read it, and so is a `staging` or `development` key, because experiments run in production only. |
| `data-require-consent` | The script waits for `controlvar.visual.consent()` before it reads, stores or sends anything about the visitor. |
| `data-timeout-ms` | How long the page may stay hidden while its changes are applied: 0 to 4000 milliseconds, 1000 by default; 0 never hides it. |
| `data-user-id` | Your own id for the visitor, at most 255 characters. Use an id that does not identify a person, such as an account number, not an email address: anyone with your page's key can ask what that id is assigned to. |
| `data-cookie-name` | Without `data-user-id`, the script keeps a random id in a cookie of this name, `controlvar_uid` by default. |
| `data-attributes` | A JSON object of at most 4 KB about the visitor (4096 bytes, as written and as sent), sent with each call for the experiments' audiences. Its keys that start with `web_` are ignored, with one console warning: the script sets those itself. |
| `data-collect` | The website details the script sends, separated by commas: `device`, `browser`, `language`, `visitor`, `referrer`, `utm`, `landing`, or `none`. Without it the script sends all of them but `landing`. See [What the script reads about visitors](#what-the-script-reads-about-visitors). |

If your site sends a Content-Security-Policy, allow the script's address in `script-src` and your Controlvar API in `connect-src`.
The script needs no `'unsafe-inline'` and no `'unsafe-eval'`, and it writes styles through the CSSOM, which `style-src` does not restrict:

```text
script-src 'self' https://controlvar.example.com; connect-src 'self' https://api.controlvar.example.com
```

## What the script sends

On each page an experiment covers, the script makes one call to your Controlvar API with the visitor's id, the page's host and path, the website details described in [What the script reads about visitors](#what-the-script-reads-about-visitors) (as `data-collect` sets them), and `data-attributes` if the tag has it.
It never sends the query string, except three campaign (`utm_`) values, or the part of the address after `#`.
It keeps the page rules of the running experiments for 60 seconds, and a page none of them covers makes no call and is never hidden.

The answer also names the goals of each experiment the visitor is assigned to, the control's visitors included, and the script records them on any page of your site for 30 days after the answer that named them.
A page-view goal records one event for each page view of the goal's page: each page load, and each path change the script follows; a change of only the query, or only the part after `#`, is not a page view.
A click goal records one event per page view when a click a person makes, or Enter or Space on the element, lands on the one element its selector matches, or inside it; a click made by a script counts nothing, and so does a click anywhere else.
A goal counts at most once per page view, and one click counts once for each experiment whose goal it is.
Each event carries the goal's event name, the visitor's id, the experiment's key, an `event_id` made of the page view's random id and the event name, and the page view's website details (as `data-collect` sets them), and nothing else: never the page's address, its text or `data-attributes`.
The script keeps each event in localStorage until your Controlvar API accepts it: at most 50 events, the oldest dropped first, each for at most 24 hours.
An event is dropped if the server refuses it (any 4xx answer but 429); after a 429, a 5xx or a failed call it is sent again with the next goal event or from the next page, with the same `event_id`, and the API stores it once.
Events are sent with `keepalive`, so a click that takes the visitor to another page is still sent as the page goes away.

When a change cannot be applied, the script sends an error report with the experiment's key, a fixed message, the change's position and the reason.
When a click goal's selector matches no element, or more than one, on a click on its page, the script sends one report for that goal per page view, with the goal's position.
A report never holds a feature flag key, the visitor's id, the page's address or anything from the page, so it never counts toward a flag's safety rollback.
At most 5 reports are sent per page load.

`controlvar.visual.track(event, { value })` sends one event for each experiment the visitor was assigned to in the last 30 days, so a purchase on another page counts for the experiment the visitor saw first.

Requests carry no cookies and no Referer.

## What the script reads about visitors

With the call for a page's changes, the script sends the website details `data-collect` turns on, so that an experiment's audience can use them.
Each is sent only in the form below; a detail the script cannot read in that form is left out.
Goal events carry them too; `track()`'s events and the error reports never do.

| Detail | Where it comes from | Example | Sent to Controlvar | Stored and where |
|---|---|---|---|---|
| Device type (`web_device`) | The browser's user agent, whether it says it is a mobile, and its touch points | phone | Yes | No |
| Browser (`web_browser`) | The browser's user agent | chrome | Yes | No |
| Language (`web_language`) | The browser's language, its first part (en for en-US) | en | Yes | No |
| New or returning (`web_visitor`) | Whether this browser was first seen before this visit started | returning | Yes | localStorage: controlvar_seen and controlvar_visit |
| Came from (`web_referrer`) | The host of the page that linked to this one, when it is another host | www.google.com | Yes | No |
| Campaign source (`web_utm_source`) | utm_source in the address of the visit's first page | newsletter | Yes, on every page of the visit | localStorage: controlvar_visit |
| Campaign medium (`web_utm_medium`) | utm_medium in the address of the visit's first page | email | Yes, on every page of the visit | localStorage: controlvar_visit |
| Campaign name (`web_utm_campaign`) | utm_campaign in the address of the visit's first page | spring-sale | Yes, on every page of the visit | localStorage: controlvar_visit |
| Landing page (`web_landing_path`) | The path of the visit's first page, without its query | /sale/shoes | Only with landing in data-collect, on every page of the visit | localStorage: controlvar_visit |

The device type is phone, tablet or desktop; the browser is chrome, edge, firefox, safari, samsung, opera or other; new or returning is new or returning.
The script never asks the browser for its detailed model: the device type comes from the user agent, whether the browser says it is a mobile, and its touch points.
The referring site is a host only, never a path or a query, and is left out when the page that linked here has this page's host name; a link from another of your own subdomains is sent.

A visit ends after 30 minutes without a page view, and every tab on the same origin shares it.
A page view is each page load the script starts on and each path change it follows.
The campaign values and the landing page are the visit's first page's: a later page of the same visit does not change them.

Campaign values are kept only if they are 1 to 100 of the letters A to Z and a to z, the digits, and `.`, `_`, `~` and `-`; a longer value is left out, never cut.
A tag with an @, a space or another character is not recorded, so an email address in a link is never stored.
The landing page is a path of at most 255 characters, with no `@`, `%40`, whitespace or control character; a longer path is left out, never cut.
Because a path can hold an order number or a name, the landing page is sent only when `data-collect` names `landing`.

`data-collect` names the details to send: `device`, `browser`, `language`, `visitor`, `referrer`, `utm` and `landing`, separated by commas.
With `none`, the script sends no website detail and stores no visit.
A value it does not know gets one console warning, and the values it knows still apply.
With `data-require-consent`, the script reads none of these before `consent()`.
In a preview tab and on an editor link it sends no detail and stores no visit.

## What it stores in the browser

| Key | Where | What it holds | How long |
|---|---|---|---|
| `controlvar_uid` | cookie | The visitor's random id, when the page sets no data-user-id. Its name is data-cookie-name's default. | 30 days (Max-Age=2592000, Path=/, SameSite=Lax, Secure on https) |
| `controlvar_visit` | localStorage | The visit's start and last page view, and its first page's campaign values and landing path when the tag collects them, cleaned. | replaced when a visit starts after 30 minutes without a page view; revokeConsent() deletes it |
| `controlvar_seen` | localStorage | When this browser was first seen (milliseconds since 1970), to tell a new visitor from a returning one. | until deleted |
| `controlvar_assigned` | localStorage | The keys of the experiments this visitor was assigned to, each with the time it was last seen, so track() can name them. | 30 days per experiment |
| `controlvar_goals` | localStorage | The goals of the experiments this visitor was assigned to (each goal's event name, kind, selector and page rule), so a goal on another page counts. | 30 days per experiment |
| `controlvar_outbox` | localStorage | Goal events the API has not yet accepted, each with the time it was made, so a later page sends them again. | until the API accepts or refuses it; at most 50 events, each at most 24 hours |
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
Goal events not yet sent are deleted with them.
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
The preview lasts for the tab, turns every other experiment off and records no assignment, no exposure, no event and no goal; a change it cannot apply is still reported.
No change is ever applied to the page's `<html>` element or to the banner.

## Visitor IP addresses

Controlvar's database stores no visitor IP address.
Like any web server, your Controlvar server's access logs (the API's, and the dashboard's nginx for /api/) record each request's address, and so may a proxy in front of it.
The rate limiter counts requests per address in Redis, in counters that expire with their one-minute window.
The logs are kept as long as your deployment keeps them.
