---
name: browser
description: Real browser access — drives Bob's own headless Chrome (persistent profile at workspace/browser-profile, CDP port 9223) via the browser-use CLI. Use for JS-rendered pages curl can't read, clicking, form filling, Bob's own logged-in sessions, and page screenshots.
trigger: when a task needs a real browser — a JS-heavy page that returns a shell to curl/fetch, clicking buttons or links, filling forms, logging into sites under Bob's own profile, taking screenshots of pages, or any "open / go to / do X on this website" request needing interaction
---

# browser

Bob drives its **own headless Chrome** (user `bob`, not Mike's desktop browser) through the
`browser-use` CLI. The browser keeps a **persistent profile** at `/home/bob/workspace/browser-profile`,
so cookies and logins survive across sessions. Connection is CDP on `127.0.0.1:9223`.

**Escalation rule:** try `curl`/plain fetch first. Only use the browser when the task needs
interaction, JS rendering, login state, or a screenshot. If a plain fetch already worked, don't browse.

## 1. Ensure the browser is running

Check it's up before the first browser command in a task:

```bash
curl -fsS http://127.0.0.1:9223/json/version | head -2
```

If that fails, start Chrome as a **managed background process** (your bash shell can't
launch Chrome itself — it's sandboxed, and Chrome needs things the sandbox denies):

```
bg_start(name="bob-chrome",
         command="/usr/bin/google-chrome --headless=new --remote-debugging-port=9223 --remote-debugging-address=127.0.0.1 --user-data-dir=/home/bob/workspace/browser-profile --no-first-run --disable-dev-shm-usage about:blank")
```

Wait ~5 s, then re-run the curl. If the port still doesn't answer, `bg_logs(name="bob-chrome")`.

## 2. Drive it

Always prefix `BU_CDP_URL` (the daemon auto-starts on first call and connects to the browser):

```bash
BU_CDP_URL=http://127.0.0.1:9223 browser-use <<'PY'
new_tab("https://example.com")
wait_for_load()
print(page_info())
PY
```

Helpers are pre-imported — no imports needed. Multi-line Python in the heredoc; print results
you need to see (heredoc output is the only thing returned).

**Key helpers**

| Helper | Purpose |
|---|---|
| `new_tab(url)` / `close_tab(t)` / `list_tabs()` / `switch_tab(t)` | tab lifecycle — first navigation is `new_tab`, not `goto_url` |
| `goto_url(url)` | navigate the current tab |
| `page_info()` | current url, title, viewport, scroll position |
| `js(expr)` | evaluate JS in the page, return the value |
| `cdp("Domain.method", ...)` | raw CDP call |
| `click_at_xy(x, y)` / `fill_input(...)` / `type_text(...)` / `press_key(k)` / `scroll(...)` | input |
| `wait_for_load()` / `wait_for_element(...)` / `wait_for_network_idle()` / `wait(s)` | synchronisation |
| `capture_screenshot(path)` | save PNG (save under `/home/bob/workspace/scratch/`, then view with `read_image`) |

## 3. Element-finding workflow

Prefer the accessibility tree over screenshots — it has every element's role, name, and node id:

```python
nodes = cdp("Accessibility.getFullAXTree")["nodes"]
# filter in Python before printing — the full tree is thousands of nodes
btns = [n for n in nodes if n["role"]["value"] == "button" and n.get("name", {}).get("value")]
print([(n["name"]["value"], n["backendDOMNodeId"]) for n in btns][:10])
```

Then get coordinates and click, and verify the effect:

```python
q = cdp("DOM.getBoxModel", backendNodeId=nid)["model"]["content"]
x, y = sum(q[0::2]) / 4, sum(q[1::2]) / 4   # viewport centre of the element
click_at_xy(x, y)
wait_for_load(); print(page_info())
```

Fall back to `js(...)` when the AX tree lacks the element (canvas, exotic widgets); use
`screenshot` + `read_image` when layout or imagery matters. Tabs marked 🐴 are browser-use's —
`new_tab`/`switch_tab` work in the background and don't steal focus.

## 4. Working fast — method matters more than tools

Every `browser-use` call costs a full LLM round (~10–20s) plus daemon startup.
A turn that micro-steps — one DOM probe per call, a screenshot after every
action — burns 30+ rounds on what a human does in 30 seconds (2026-10-11:
one Gmail compose took 33 rounds, 7 minutes, and still wasn't sent).

- **One script = one UI transaction.** Navigate, act, and verify in a single
  heredoc (`js()` → `type_text()` → `wait()` → done). Three batched scripts
  beat thirty single-step calls every time.
- **Verify at the end, not every step.** `capture_screenshot` + `read_image`
  costs two rounds and vision tokens each time. Screenshot once when the
  transaction is complete — or immediately when something surprising
  happened, not as routine.
- **Prefill instead of typing.** For forms, put the data in the URL. Gmail
  compose deep-link (To/subject/body all prefilled, no typing at all):

  ```
  https://mail.google.com/mail/?view=cm&fs=1&to=support@printful.com&su=Ticket%205807268&body=<urlencoded body>
  ```

  Gmail's To field is chip-based — synthetic typing into it fights
  autocomplete, Chrome autofill ghost chips, and contact matching, and
  loses. With the deep-link the compose window arrives filled in: verify
  once, click Send.
- **More than ~10 rounds on one page = wrong method.** Stop and look for
  the URL-parameter route, a single batched script, or an API instead.
- **When budget forces deferral, say that.** If the self-wrap budget ends a
  flight mid-task, the wrap-up states what is DONE and what the follow-up
  promise will do ("compose corrupted — clean retry at 12:45"), never
  "in progress" when nothing is running.

## 5. Rules

- **Login walls: stop and ask.** Never type passwords, passcodes, or MFA codes unprompted.
  Logging into a site under Bob's own profile is fine **when the user has asked for it** and
  handed over the flow explicitly; otherwise surface the wall and ask.
- **No cloud browsers.** Never call `start_remote_daemon(...)` / `browser-use auth login` —
  they bill money and send traffic through Browser Use Cloud. Local browser only.
- **No recordings** unless the user asks for one.
- **Tidy up:** `close_tab` tabs you opened once the task is done. Leave Chrome itself running
  (staying warm is the point); to restart it after a crash: `bg_restart(name="bob-chrome")` (or `bg_start`
  as in step 1 if it was never registered).
- Downloads land in the profile's download dir — prefer scraping text via `js()` instead.

## 6. Diagnostics

- `browser-use doctor` — install/daemon/browser state
- `BU_CDP_URL=http://127.0.0.1:9223 browser-use --version` — sanity check
- Chrome log: `bg_logs(name="bob-chrome")`; CDP health: `curl -s http://127.0.0.1:9223/json/version`
