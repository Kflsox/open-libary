# open-libary-berichte

A small Cloudflare Worker that receives bug reports from Open Libary and files each one as an issue in the
private GitHub repo `jyxpi/open-libary-berichte`. GitHub then notifies the owner by mail.

The app never talks to GitHub. Only this Worker holds the token, as a Cloudflare secret.

## Files

| File | Purpose |
|---|---|
| `worker.js` | Entry point: request handler and the `Zaehler` Durable Object (rate limit) |
| `bericht.js` | Validation, issue title, labels and body, request flow. No Workers imports, testable in Node |
| `test.mjs` | Tests without network: `node test.mjs` |
| `wrangler.toml` | Worker config |

## Request

```
POST /bericht
Content-Type: application/json

{"app": "open-libary", "version": "1.2.2", "system": "macOS 15.1 arm64", "art": "crash",
 "text": "what happened", "kontakt": "optional mail or reddit name", "snapshot": "plain text or null"}
```

| Field | Rule |
|---|---|
| `app` | exactly `open-libary` |
| `version` | `1.2` or `1.2.2` |
| `system` | required, at most 100 characters |
| `art` | `crash`, `freeze` or `manual` |
| `text` | required, at most 5000 characters |
| `kontakt` | optional (missing, null or string), at most 200 characters |
| `snapshot` | null or string, at most 50,000 characters |

The whole body may be at most 256 KB.

| Status | Body | Meaning |
|---|---|---|
| 200 | `{"ok": true}` | issue created |
| 400 | `{"ok": false, "fehler": "..."}` | invalid request, the message says why |
| 404 / 405 | `{"ok": false, ...}` | wrong path or method (only `POST /bericht`) |
| 429 | `{"ok": false, ...}` plus `Retry-After` | more than 5 reports from this client in 10 minutes, or 200 reports today from everyone |
| 502 | `{"ok": false, ...}` | GitHub did not accept the issue (details only in the Worker log) |
| 503 | `{"ok": false, ...}` | rate limiter unavailable, try again later |

## The issue

- Title: `[Crash] 1.2.2 · macOS 15.1 arm64: first 60 characters of the text`
  (`Crash`, `Freeze` or `Report` for manual reports).
- Labels: the `art` (`crash`, `freeze`, `manual`) and the system family (`mac`, `windows`, `linux`, `other`).
- Body: the user's text and contact inside fenced code blocks whose fence is longer than any backtick run in the
  content, so they cannot @mention anyone, inject HTML or break the layout. Version and system as a short list.
  The snapshot sits in a collapsed `<details>` block. When the body would exceed 65,000 characters
  (GitHub allows 65,536), the snapshot keeps its beginning (version, system, incident stacks) and loses its
  end, with a note.

## Rate limiting

Two limits, both answered with 429 and `Retry-After`. Invalid requests count towards neither.

- **Per client:** at most 5 accepted reports within a sliding 10-minute window. A client is one IPv4 address
  or one IPv6 /64 prefix (taken from `CF-Connecting-IP`, expanded and lowercased first), because a single
  household or server usually owns a whole /64 and could otherwise rotate addresses. IPv4-mapped IPv6
  addresses count as their IPv4 address.
- **All clients together:** at most 200 accepted reports per UTC day. Checked only after the per-client limit
  passed, so a client that is already refused cannot use it up.

Both use the same SQLite-backed Durable Object class. Each client gets its own instance (`idFromName` of the
key), which stores only the timestamps of accepted reports and deletes itself 10 minutes after the last one.
The address itself is not stored. The daily count lives in the single instance named `gesamt`.

Why not the Workers Rate Limiting binding: its window can only be 10 or 60 seconds, so "5 per 10 minutes" cannot
be expressed, its counts are per Cloudflare location and approximate, and the docs do not say whether it is
available on the Workers Free plan. SQLite-backed Durable Objects are documented as available on Free
(100,000 requests per day) and count exactly. KV would be eventually consistent and is limited to
1,000 writes per day on Free.

The endpoint is public by design (the app is open source, so a shared secret would not protect anything).
The daily cap bounds what many addresses at once can do: at most 200 issues per day. If spam fills that,
real reports wait until the next UTC day; delete the Worker or rotate the token if it keeps happening.

## Test locally

Needs Node 22.15 or later.

```sh
npm ci
node test.mjs                    # unit tests and the handler with a mocked fetch
npx wrangler deploy --dry-run    # checks config and bundle, needs no login
npx wrangler dev                 # local server on http://localhost:8787
```

Without a token, `wrangler dev` answers valid reports with 502 (no GitHub call is made); validation, 404/405
and the 429 after five reports can be tried with the curl command below against `http://localhost:8787`.

## Deploy (owner, once)

1. **GitHub repo and labels.** Create the private repo `jyxpi/open-libary-berichte` if it does not exist yet.
   Create the labels once so they get colors (GitHub may also create missing ones itself):

   ```sh
   for l in crash freeze manual mac windows linux other; do gh label create "$l" -R jyxpi/open-libary-berichte; done
   ```

2. **Cloudflare.** Create a free account at dash.cloudflare.com, then log in from this folder:

   ```sh
   npx wrangler login
   ```

3. **Deploy.**

   ```sh
   npx wrangler deploy
   ```

   On the first deploy Wrangler may ask you to pick a `workers.dev` subdomain. It prints the URL, for example
   `https://open-libary-berichte.<subdomain>.workers.dev`. The app's report URL is that plus `/bericht`.

4. **GitHub token.** github.com > Settings > Developer settings > Personal access tokens > Fine-grained tokens >
   Generate new token:
   - Resource owner: `jyxpi`
   - Repository access: Only select repositories > `open-libary-berichte`
   - Permissions > Repository permissions > Issues: Read and write (Metadata: Read-only is added automatically)
   - Expiration: your choice; note the date, reports fail with 502 once it expires.

   Store it as a Worker secret (Wrangler asks for the value, it never goes into a file):

   ```sh
   npx wrangler secret put GITHUB_TOKEN
   ```

5. **Send a test report.**

   ```sh
   curl -sS -X POST "https://open-libary-berichte.<subdomain>.workers.dev/bericht" \
     -H "Content-Type: application/json" \
     -d '{"app":"open-libary","version":"1.2.2","system":"macOS 15.1 arm64","art":"manual","text":"Test report, please ignore.","kontakt":null,"snapshot":"line 1\nline 2"}'
   ```

   Expected answer: `{"ok":true}`, and a new issue `[Report] 1.2.2 · macOS 15.1 arm64: Test report, please ignore.`
   with the labels `manual` and `mac`. If you get 502, watch the log while sending again:

   ```sh
   npx wrangler tail
   ```

   `GitHub answered 401` means a wrong or expired token, `403` or `404` usually means the token lacks the
   Issues permission or the repo.

## Maintenance

- New token: `npx wrangler secret put GITHUB_TOKEN` again, no redeploy needed.
- Stop receiving reports: `npx wrangler delete`.
- After changing code: `node test.mjs`, then `npx wrangler deploy`.

## License

AGPL-3.0-only, like Open Libary.
