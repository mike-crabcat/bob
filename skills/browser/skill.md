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

Run this before the first browser command in a task (idempotent — takes ~5s when starting):

```bash
curl -fsS http://127.0.0.1:9223/json/version >/dev/null 2>&1 || setsid nohup /usr/bin/google-chrome --headless=new --remote-debugging-port=9223 --remote-debugging-address=127.0.0.1 --user-data-dir=/home/bob/workspace/browser-profile --no-first-run --disable-dev-shm-usage about:blank >/tmp/bob-chrome.log 2>&1 < /dev/null & sleep 5; curl -fsS http://127.0.0.1:9223/json/version | head -2
```

If the port still doesn't answer after that, check `tail -5 /tmp/bob-chrome.log`.

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

## 4. Rules

- **Login walls: stop and ask.** Never type passwords, passcodes, or MFA codes unprompted.
  Logging into a site under Bob's own profile is fine **when the user has asked for it** and
  handed over the flow explicitly; otherwise surface the wall and ask.
- **No cloud browsers.** Never call `start_remote_daemon(...)` / `browser-use auth login` —
  they bill money and send traffic through Browser Use Cloud. Local browser only.
- **No recordings** unless the user asks for one.
- **Tidy up:** `close_tab` tabs you opened once the task is done. Leave Chrome itself running
  (staying warm is the point); to restart it after a crash: `pkill -f remote-debugging-port=9223`,
  then run the step-1 snippet again.
- Downloads land in the profile's download dir — prefer scraping text via `js()` instead.

## 5. Diagnostics

- `browser-use doctor` — install/daemon/browser state
- `BU_CDP_URL=http://127.0.0.1:9223 browser-use --version` — sanity check
- Chrome log: `/tmp/bob-chrome.log`; CDP health: `curl -s http://127.0.0.1:9223/json/version`
