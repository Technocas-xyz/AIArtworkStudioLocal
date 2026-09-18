# Artwork Studio

A self-contained desktop app for generating DTF (Direct-to-Film) garment artwork
by driving ChatGPT's web UI through a real, logged-in browser on your own PC.

Everything runs **locally in one process**: the web interface, the generation
workflows, and the browser. There is no separate server to host and no network
"agent" to install alongside it. You sign in to ChatGPT **once**, then work from
a web UI in your browser.

Nextcloud (the artwork vault) and the Decoinks / PrintShop backend (live prompts
and save-back) are still reached over the network — the app talks to them
directly. Only the old middle "artwork server" is gone.

---

## For designers — running the packaged app

You get a single ZIP: **`ArtworkStudio.zip`**. No Python, no installs.

1. **Unzip it** anywhere (e.g. your Desktop). Keep the folder together — the app
   needs the files beside the exe.
2. **Add your settings file.** Put a file named `.env` **next to `ArtworkStudio.exe`**
   (see [Configuration](#configuration) below for what goes in it). Your admin can
   give you this file already filled in.
3. **Double-click `ArtworkStudio.exe`.** A small window opens and the app starts a
   local server on your PC.
4. **Click "Sign in to ChatGPT."** A Chrome window opens — log in to ChatGPT
   normally, then leave it open. Your session is saved to a local profile, so you
   stay logged in between runs.
5. **Click "Start."** The Studio opens in your default browser at
   `http://127.0.0.1:8000/` (the port may differ if 8000 is busy — the window
   shows the exact address, and there's an **Open Studio** button).
6. Generate artwork from the web UI as usual. The window minimises to the system
   tray; the tray dot shows status:
   - **green** = running and signed in
   - **amber** = running but not signed in to ChatGPT
   - **red** = error
   - **grey** = stopped

To stop, use **Stop** (leaves the browser open) or **Quit** from the tray (closes
everything).

### Every day

- Double-click `ArtworkStudio.exe`, click **Start**, work in the browser.
- If the tray goes amber ("not signed in"), click **Sign in to ChatGPT** again.

### If something goes wrong

- **"Not signed in to ChatGPT":** click **Sign in to ChatGPT**, log in, leave the
  Chrome window open, then **Start**.
- **The browser didn't open:** click **Open Studio** in the app window, or visit
  the address shown there.
- **A job is stuck:** cancel it from the web UI and start again. Jobs run one at a
  time on your PC.

---

## Architecture

```
Your browser ──▶ Local FastAPI server (127.0.0.1) ──▶ in-process worker ──▶ ChatGPT (Playwright/Chrome)
                        │                                     │
                        └────────── shared in-memory job queue ┘
                        │
              Nextcloud vault  +  Decoinks/PrintShop backend  (over the network)
```

- **`studio_gui.py`** — the tray + window app (the packaged entry point). Owns the
  single Playwright browser thread, drives sign-in, starts the local server and
  the worker.
- **`studio.py`** — a console entry point for the same thing (handy for
  development / headless runs after you've already signed in).
- **`app.py`** — the FastAPI web UI and JSON API. Serves the operator interface,
  owns the in-memory job queue, and talks to Nextcloud / Decoinks / PrintShop.
- **`local_worker.py`** — the in-process worker. Claims queued jobs, runs the
  workflows against the one browser, writes results straight to `./output`, and
  handles operator pauses **in-process** (no HTTP polling, no heartbeat, no
  cross-process reconciliation — the machinery that used to cause session and
  timeout failures is gone).
- **`agent.py` / `src/` / `config/`** — the workflow library (text, extraction,
  artwork, custom operations), the ChatGPT automation, image processing, prompt
  management, and integrations. Reused unchanged; the worker only repoints a few
  seams so the same code runs in one process.

### Workflows

- **Text** — design from wording. Input modes: typed, from an image (extract the
  text first), replace text in an existing design, or wording combined with a
  reference image (as an element or as a style).
- **Artwork Extraction (Mockup)** — pull individual designs out of a mockup /
  contact sheet.
- **Artwork Generation** — batch clean-up and regeneration of supplied artwork.
- **Custom Operation** — a chosen sequence of operations (reconstruct, remove
  background, halo removal, black out, half tone, change object colour, aspect
  ratio) applied to one artwork.

---

## Configuration

Create a **`.env`** file next to the exe (packaged) or in the project root
(source). Only fill in what you use.

```
# --- App login (the web UI) ---
APP_USERNAME=admin
APP_PASSWORD_HASH=            # generate with: python set_password.py
APP_SECRET_KEY=              # any random string, 32+ chars

# --- Local server ---
STUDIO_PORT=8000             # optional; a free port is auto-picked if this is taken
AGENT_NAME=                  # optional label shown in the header (defaults to the PC name)

# --- Nextcloud vault ---
NEXTCLOUD_URL=https://cloud.example.com
NEXTCLOUD_USER=bot@example.com
NEXTCLOUD_APP_PASSWORD=      # Nextcloud > Settings > Security > App password
NEXTCLOUD_ROOT=Leads 2.0
NEXTCLOUD_POLL_SECONDS=4
NEXTCLOUD_INITIAL_TAIL_HOURS=24
NEXTCLOUD_MAX_IMPORT_MB=80

# --- Decoinks Prompt Management + PrintShop backend ---
PRINTSHOP_BACKEND=http://172.17.0.1:8094   # where the backend is reachable from this PC
DECOINKS_SERVICE_SECRET=     # live prompts + run logging (blank = built-in prompts)
PRINTSHOP_JWT_SECRET=        # required only to save artwork back into the shop vault
```

Notes:
- `APP_PASSWORD_HASH` is a PBKDF2 hash — run `python set_password.py` and paste
  the printed value. Never put a plain password here.
- If `DECOINKS_SERVICE_SECRET` is blank, the app runs on the built-in prompt text
  in `config/workflows.py` — generation still works, prompts just aren't managed
  live.
- `PRINTSHOP_BACKEND` defaults to a docker-bridge address. If the PC reaches the
  backend at a different address, set it here.
- The `.env` is not committed (it's gitignored). Keep secrets out of version
  control.

### Where files live

The app writes into folders **next to the exe** (or the project root when run
from source): `input/`, `output/`, `logs/`, `downloads/`, and `profiles/` (the
saved ChatGPT browser session). These are safe to keep between runs; the browser
profile is what keeps you signed in.

---

## For developers — running from source

Requires **Python 3.11+**.

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

Add a `.env` (see above), then run either entry point:

```
python studio_gui.py     # tray + window, with a Sign-in button
python studio.py         # console; auto-opens the browser
```

Sign in to ChatGPT once (the GUI's button, or `python login.py`). The saved
session lives in `profiles/acct1` and persists across runs.

---

## Building the packaged app

Produces a fully-frozen one-dir Windows build with **everything** bundled
(Python, all dependencies including OpenCV, and the Chromium runtime). Nothing
installs at runtime.

```
.venv\Scripts\activate
python build_studio.py
```

Output:
- `dist/ArtworkStudio/ArtworkStudio.exe` — the app (with `_internal/` beside it)
- `dist/ArtworkStudio.zip` — the distributable; hand this to designers

The build is large (~250–300 MB) because it bundles Chromium; that is expected
and is what removes any runtime download. To keep a console window for debugging,
build with `STUDIO_BUILD_CONSOLE=1` set.

> The build script bundles only the exact `chromium-<ver>` folder Playwright
> resolved on the build machine, plus the web UI (`static/`), and points
> Playwright at the bundled browser via a runtime hook (`pyi_rth_playwright.py`).

---

## Which browser the automation uses

The automation drives ChatGPT through a browser it controls (via Playwright). It
does **not** use your everyday browser window, because it has to programmatically
type prompts, click send, and read back generated images — which only works in a
browser Playwright launches and owns.

By default it **attaches to your real installed Google Chrome** over the
DevTools protocol (`STUDIO_BROWSER_CHANNEL=attach`). It starts Chrome as an
ordinary process and connects to it — Playwright does **not** launch it with
automation switches. This is the most reliable path against ChatGPT's Cloudflare
anti-bot check: an ordinary Chrome, using your real network/proxy/VPN/certs, is
not flagged the way a Playwright-launched browser is. (Earlier approaches — a
bundled Chromium, and a Playwright-launched Chrome — hit `about:blank` or a
"Performing security verification" hard block on some machines.)

**Login: automatic combo check.** On sign-in the app:

1. First tries your **real everyday Chrome profile** (where you are most likely
   already logged in to ChatGPT). If it finds a signed-in session, it uses it —
   **no separate login needed.** (This also avoids a fresh-profile quirk where
   OpenAI's login could return a `400 Invalid content type` error.)
   - Your everyday Chrome must be **fully closed** for this, because Chrome locks
     its profile while running. If Chrome is open, the app skips this step.
2. If your real profile isn't signed in (or Chrome was open), it uses a
   **dedicated profile** (`profiles/acct1_chrome`) and asks you to sign in once.
   That session then persists across runs.

Force one behaviour with `STUDIO_USE_DEFAULT_PROFILE` in `.env`:

```
STUDIO_USE_DEFAULT_PROFILE=only    # ONLY use your real everyday Chrome profile (close Chrome first)
STUDIO_USE_DEFAULT_PROFILE=never   # always use the dedicated profile, never your real one
# (unset)                          # the combo behaviour above (recommended)
```

Override the browser mechanism with `STUDIO_BROWSER_CHANNEL`:

```
STUDIO_BROWSER_CHANNEL=attach     # default — start real Chrome + attach over CDP (least detectable)
STUDIO_BROWSER_CHANNEL=chrome     # Playwright launches installed Google Chrome
STUDIO_BROWSER_CHANNEL=msedge     # Playwright launches installed Microsoft Edge
STUDIO_BROWSER_CHANNEL=chromium   # the bundled Chromium
```

If `attach` can't find/start Chrome it falls back to a launched Chrome, then to
the bundled Chromium.

Your **default browser** is still used — but only for the operator UI web page
(`http://127.0.0.1:8000`), which is just a normal page you click around in.

## Notes / limitations

- **One job at a time per PC.** Each machine runs its own local queue; there is no
  cross-designer job sharing (that was a feature of the old shared server, removed
  by design).
- **ChatGPT sign-in is manual and one-time.** The app cannot log in for you; it
  reuses the saved session in its own browser profile.
- The first ChatGPT page load is bounded and non-fatal — the app won't hang on it.
  If sign-in reports it couldn't load chatgpt.com, confirm the PC can open
  `https://chatgpt.com` in a normal browser, then retry.
