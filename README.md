# Kakarot — your own local AI assistant

Kakarot runs **100% on your Windows PC**. Its brain is a free open-source AI model
(`qwen3:8b` by default) running through **Ollama**. It does not use Claude, ChatGPT or any
paid service, and there are no subscriptions. The only internet it uses is for the web tasks
you ask it to do.

## What it can do

| Area | Examples |
|---|---|
| Files & documents | "Make a folder Invoices and move all PDFs from Downloads into it", "Summarize report.docx", "Write a to-do list file" |
| Web | "Search the web for …", "Open this page and tell me the price", "Take a screenshot of my website" |
| Website monitoring | "Check lalsobujdata.site and tell me if anything changed" |
| Scheduled jobs | "Every day at 9am check my website is up", "Every Sunday at 8pm search for news about …" |
| Reminders & notes | "Remind me in 30 minutes to call Rahim", "Add a note: pay internet bill" |
| Memory | "Remember that my office starts at 9am", and it keeps that in mind in future chats |
| Lal Sobuj trips | "Dhaka to Noakhali tomorrow" (one route) or "Check every rotation for today" (all routes, saved as a CSV in `reports`) |
| Chats | Every conversation is saved in `data\\kakarot.db`; reopen them from the **Chats** tab |

## Power features

* **Use websites like a person.** Kakarot keeps its own browser open and can click buttons, type in boxes, pick
  dropdown options and go through multi-page forms. Example: "Open lalsobujbus.com, search Dhaka to Sylhet for
  tomorrow and list the bus times". Set `"browser_visible": true` in `config.json` to watch it work.
* **Office files.** "Make an Excel sheet of …", "Write a Word letter …", "Make a PDF report of …". It can also read
  `.xlsx`, `.csv`, `.docx` and `.pdf` files. Files are saved in `Documents\KakarotWorkspace`.
* **PC commands, with your OK.** Kakarot can run commands or open files and apps, but first a yellow card appears
  in the chat with **Approve / Deny**. Nothing runs until you click Approve. Scheduled tasks can never run commands.
* **🧠 Smart mode.** Tap **Smart** next to the message box for hard jobs. It uses a bigger brain (`qwen3:14b`) and
  thinks first. It's slower (about 1–3 minutes). Run **`get-smart-brain.bat`** once to download it (about 9 GB).

## Advanced features

* **📎 Sees pictures.** Tap 📎 next to the message box to attach a photo (on the phone it can open the camera), a screenshot, a scanned PDF, or a Word/Excel/PDF file. Then ask, for example, "What is the total on this receipt?" or "Read this ticket". Pictures are read by a local picture brain (`qwen2.5vl:3b`).
* **📂 Knows your files.** Kakarot indexes the documents in your allowed folders (Word, PDF, Excel, text) every 3 hours. Ask "What does my rent agreement say about payment?" and it finds the right passages and names the files. Use **Status → Re-index now** after adding many files.
* **⚡ Live replies.** Answers appear word by word, and you can watch each action as it happens (✓ when done). If your phone loses the connection, Kakarot keeps working and saves the answer in the chat.
* **🎯 Missions.** For big jobs, open the **Missions** tab (or say "do this as a mission"). Kakarot makes a plan, does each step on its own in the background and puts the final report in your **Inbox**. You can keep chatting meanwhile. A mission continues after a restart.
* **🚌 Lal Sobuj morning brief.** Every day at 07:00, Kakarot checks every route for today and tells you what changed since the last brief: new or stopped routes, trip counts and almost-full trips. The CSV is saved in `reports`. Change the time in `config.json` → `daily_brief`, or tap **Status → Run brief now**.
* **🛡️ Website watchdog.** Kakarot checks `lalsobujdata.site` every 5 minutes. After 2 failed checks in a row, you get an alert in the Inbox and a Windows pop-up, and another when it's back up. It also warns you 14 days before the HTTPS certificate expires. You can add or remove sites in the **Status** tab.

## Grows by itself

* **🌙 Learns every night (03:30).** Kakarot reads the day's chats and results, saves new facts about you to Memory, and writes short *lessons* it follows from then on (see **Status → Lessons**; ✕ removes one). The report goes to your Inbox. **Status → Learn now** runs it immediately.
* **⬆️ Installs new versions by itself (04:00 and after every start).** It checks the GitHub repo in `config.json` → `self_update.repo`. If there's a newer version, it backs up the current files to `data\backups`, installs the new version, adds any new settings (your own settings are never changed), installs new packages and restarts when it isn't busy. If the new version doesn't start, `start.bat` puts the previous version back and skips that version. Set `"auto_update_code": false` to only get a notice.
* **🧠 Refreshes its brains weekly (Sunday 05:00).** It downloads newer versions of the models you already have.
* **🧩 Builds its own new abilities.** When it notices something it couldn't do (or when you say "build yourself a tool to …"), its coding brain writes a new tool, tests that it loads, and puts it in **Status → New abilities**. Nothing new runs until you tap **Approve**. Switch abilities off there any time.

## Builds websites for you

Just describe it: *"I want a modern website for my restaurant with a menu, photo gallery, table booking and a contact page. Warm colors, English and Bangla."* You can also attach your logo or photos with 📎, or use **Missions → 🌐 Build website**.

Kakarot then works in the background (Missions tab):
1. **Plans** the site: pages, features, colors and fonts, and every file. It picks a plain website (HTML/CSS/JS, can be hosted anywhere) or a full web app (Flask + database) when you need logins, bookings, an admin panel or saved data.
2. **Writes** every file with the coding brain. Each file sees the files it must match, so class names, links and routes line up.
3. **Tests** it in a real hidden browser and finds broken links, missing pages, JavaScript errors, server crashes and empty pages.
4. **Fixes** each problem with the exact error, then tests again (up to 4 rounds).
5. **Looks** at screenshots (computer and phone) with the picture brain, and polishes the design.
6. **Delivers** a live link (`/sites/<name>/` or `/apps/<name>/`) that also works on your phone through your Kakarot link, plus a `.zip` ready to upload to your hosting (for example Namecheap cPanel → File Manager → public_html). Web apps also get `run-site.bat`.

To change it later: *"update my website green_line: add a fares page and make the header dark green"*. A typical site takes about 30–90 minutes with the 14B coding brain on this PC.

## Checks, fixes and publishes your existing websites

Everything is in the **Websites** tab (or just ask in chat: *"open lalsobujdata.site, check it and fix every problem"*, *"publish it"*, *"undo the last publish"*).

**Open any site.** Give a live address, a GitHub repo (`owner/repo`), or a folder on this PC. Kakarot copies it into the code folder, works out what it is (plain HTML, PHP/WordPress, Flask, React/Vite/Next, Node) and checks it. Save your hosting's **FTP account** first (Websites → Publishing accounts) and Kakarot downloads the **real files** from the server. Without it, a live address only gives a copy of what visitors see.

**Deep check (score out of 100).** Every page is opened in a hidden browser, on a computer and on a phone screen:
- **Errors:** pages that fail (404/500), JavaScript and console errors, files that don't load.
- **Links:** broken links, inside the site and to other sites.
- **Mobile:** content wider than the screen, tiny text, buttons too small to tap, missing viewport tag.
- **SEO:** titles, descriptions, headings, duplicate titles, robots.txt, sitemap.
- **Accessibility:** alt text, form labels, buttons with no name, low-contrast text.
- **Speed:** load time, page size, huge images, scripts that block the page.
- **Security:** HTTPS and its redirect, certificate expiry, security headers, mixed content, exposed `.env`/`.git`, outdated PHP.
- **Code:** syntax of the Python, PHP, JavaScript and JSON files.
- **Design:** the picture brain reviews the desktop and phone screenshots.

Each problem names the file most likely responsible and whether Kakarot can fix it.

**Fixes and changes, safely.**
- **Fix problems** checks the site, fixes, then checks again (several rounds).
- **Exact fixes need no AI:** huge images are compressed, viewport and language tags are added, and security headers and the HTTPS redirect go into `.htaccess` for cPanel hosting.
- **Changes in plain words:** *"Add a prices page with Dhaka–Sylhet 800 Tk"*. Kakarot finds the right files itself. Big files get precise search-and-replace edits instead of being rewritten.
- **Guard rails:**
  - Every edited file is syntax-checked.
  - The site is re-tested after each change. If a change breaks something, Kakarot repairs it or **undoes it automatically**.
  - Every change has a diff and an **Undo** button (and the undo can be undone).

**Publishing (you tap Approve first):**
- **GitHub:** your code, to an existing or new repository.
- **GitHub Pages:** free hosting. React/Vite sites are built and published to a `gh-pages` branch.
- **Your hosting over FTP/cPanel** (for example Namecheap): secure FTPS.
- **Netlify** and **Vercel**.
- **Back to the original folder** on this PC.

**Publishing safety:**
- **Only changed files are sent.** Nothing on the server is deleted unless you ask.
- **Backups:** before a file on your hosting is replaced, Kakarot downloads the old one, so **Undo publish** puts it back. On GitHub it makes an undo commit; on Netlify it restores the earlier version.
- **Conflicts:** if someone changed the same file on GitHub or on the server since Kakarot downloaded it, Kakarot stops instead of overwriting. **Get latest** brings their changes in and keeps both versions when both sides changed.
- **Live check:** afterwards Kakarot opens the live site to confirm the new version is there and the pages work.
- **Private files stay private:** files with passwords or keys never go to GitHub, Netlify or Vercel. That covers `.env`, private keys and databases, and also a `config.php` holding the database password. They still go to your own hosting.

**Accounts.** Add them in **Websites → Publishing accounts**:
- **GitHub:** a fine-grained token with *Contents: Read and write*, plus *Administration* to create repos and *Pages* for GitHub Pages.
- **FTP/cPanel:** the details from cPanel → FTP Accounts. The server is often `ftp.yoursite.com` or `serverNNN.web-hosting.com`, the user looks like `name@yoursite.com`, and the folder is usually `public_html`.
- **Netlify / Vercel:** a personal token.
- **Figma:** a personal access token (figma.com → Settings → Security), to build sites from your designs.

Passwords and tokens are saved only on this PC, **encrypted with your Windows login**. They are never shown to the AI and never written into chats or logs.

Testing PHP sites on this PC needs PHP installed (optional). Without it, Kakarot checks the live site plus the code. React/Vite sites need Node.js to build.

## Designs that don't look AI-made

Kakarot's design rules come from three open-source design skills: Taste Skill, Impeccable, and Emil Kowalski's design-engineering skills. The originals and their licences are in `design_skills/`.

- **The idea comes first.** Before building, Kakarot lists what belongs only to this business: its name, its street, its real prices and hours. It builds the page around one idea from that list and adds one useful interactive thing, such as "see the total" or "open right now?".
- **Real photos.** Kakarot uses real, credited photographs from open collections (Openverse, commercial-use licences) instead of placeholder pictures. Credits go in the footer and in `images/photos/CREDITS.json`.
- **Measured, not guessed.** Every check measures a page in a real browser for the "AI look": gradients, glass panels, eyebrow labels, emoji icons, rows of identical icon cards, violet buttons, 3-line headlines, too many fonts, slow or `ease-in` animations, animations from `scale(0)`, no reduced-motion version, and no press or focus states.
- **Your look stays yours.** On sites Kakarot builds, these problems are fixed automatically. On your existing sites they show as **design tips**, and Kakarot only applies them when you ask ("improve the design of greenwonderland").
- **Figma.** Send a Figma frame link ("build my site from this Figma: …"). Kakarot reads the layout, colours, fonts and exact text, then builds, tests and polishes the site.

## Writes real code

Ask for any program: "Write a Python script that merges all Excel files in a folder and test it", "Make a small website for my bus counter", "Write a Telegram bot that…". Kakarot creates a project in `Documents\KakarotWorkspace\code\<project>`. Its **coding brain** (`qwen2.5-coder:14b`) writes each file, then Kakarot runs the code and the tests, reads the errors and fixes them until it works. Each project gets its own Python environment (`.venv`), so packages never affect Kakarot.

Code runs **freely inside the code folder** (you chose this). Commands that touch other folders or change Windows settings are blocked. A program Kakarot writes can still do anything a program can, so read what it built before using it for important things. Run **`get-code-brain.bat`** once to download the coding brain (about 9 GB). Big apps work best as a Mission.

Run **`get-advanced-models.bat`** once to download the picture brain (about 3 GB) and the file search brain (about 300 MB).

After updating Kakarot, run **`update.bat`** once to install the new packages.

## Install (one time, about 15 minutes)

1. **Install Python 3.11+** from https://www.python.org/downloads/
   During setup, tick **"Add python.exe to PATH"**.
2. **Install Ollama** from https://ollama.com/download and open it once. It sits in the tray.
3. Unzip this folder somewhere permanent, for example `C:\Kakarot`.
4. Double-click **`install.bat`**. It installs the packages, a hidden browser, and downloads
   the AI model (about 5 GB).

## Run

Double-click **`start.bat`**. A black window opens and keeps Kakarot running, and your browser
opens **http://127.0.0.1:5050**. Chat there.

* Keep the black window open. Reminders and scheduled jobs only run while Kakarot is running.
* **To start Kakarot automatically when Windows starts**, press `Win + R`, type `shell:startup`,
  press Enter, and place a *shortcut* to `start.bat` in that folder.
* The left panel shows the **Inbox** (reminders and scheduled task results), **Scheduled**
  jobs, **Notes** and **Memory**. You also get Windows pop-up notifications.

## Use from your phone, from anywhere

### Option A — a link that works anywhere (recommended)
This gives Kakarot its own private `https://` address that works on any phone, any network
(mobile data, office Wi-Fi), with no app needed on the phone. It uses **Tailscale Funnel**,
which is free, needs no domain, and doesn't require opening your router.

1. Install **Tailscale** on the PC from https://tailscale.com/download and sign in (a Google
   account is fine).
2. Run `start.bat` as usual.
3. Double-click **`go-online.bat`**.
   * The first time, it prints a link. Open it, click **Enable** (this turns on HTTPS + Funnel
     for your account), then run `go-online.bat` again.
4. The window shows your address, like `https://your-pc.tail1234.ts.net`. Open it on your
   phone and log in with the **password** shown in the Kakarot window.
5. On your phone, tap **Add to Home screen** so it opens like an app.

The link stays on, even after a restart, until you run **`go-offline.bat`**.

**Security in this mode** (`"public_access": true`):
* Everyone has to log in, including the PC itself (only once, then it stays logged in for 30 days).
* The password is 10 random characters, created automatically. You can set your own in
  `config.json` (8+ characters).
* Wrong guesses lock that device out for 5 minutes. If many wrong guesses come from anywhere,
  all logins pause for 15 minutes.
* Anyone with the link *and* the password controls Kakarot, so keep the password private.

### Option B — home Wi-Fi only
Set `"public_access": false`. Then open the `http://192.168.x.x:5050` address shown in the
Kakarot window on any device on the same Wi-Fi. It uses a 6-digit PIN, and the PC itself
doesn't need it. The first time, Windows asks to allow Python on networks. Choose
**Private networks** and click **Allow**.

### Keep the PC ready
The brain runs on your PC, so the phone is just a remote control. The PC must be **on, awake
and online** with the Kakarot window open. To stop it sleeping: Settings → System →
Power → Screen and sleep → set "When plugged in, put my device to sleep after" to **Never**.

## Start automatically with Windows

Double-click **`add-to-startup.bat`** once. To undo it, run **`remove-from-startup.bat`**.

## Settings — `config.json`

| Setting | Meaning |
|---|---|
| `model` | The AI brain. `qwen3:8b` is the best fit for 16 GB RAM. For faster but less clever, use `qwen3:4b`. After changing it, run `ollama pull <name>`. |
| `allowed_folders` | The **only** folders Kakarot may touch. The first one is its main workspace. Add more folders if needed (use `\\` in paths). |
| `max_steps` | How many actions Kakarot may take for one request. |
| `port` | Web page port (default 5050). |
| `name` | Rename your assistant. |
| `phone_access` | `true` lets devices on your Wi-Fi connect (PIN required). `false` = this PC only. |
| `public_access` | `true` = ready for the anywhere link (`go-online.bat`): everyone logs in, with a stronger password. |
| `pin` | Your PIN/password. If it's empty, one is created on first start. |
| `publish_needs_approval` | `true` (default) = you tap Approve before any website is published or a publish is undone. |
| `site_check_pages` | How many pages a website check opens (default 20). |
| `site_check_external_links` | Also test links to other websites (default `true`). |
| `site_fix_rounds` | Check → fix → check rounds when building or fixing a site (default 4). |
| `site_import_max_mb` / `site_import_max_files` | Size limits when opening a site (default 400 MB / 8000 files). |

## Safety built in

* It only reads or writes inside `allowed_folders`.
* It never deletes. "Delete" moves files to `_trash` inside the workspace.
* It runs PC commands or opens files **only after you click Approve**. Scheduled jobs and missions can never run commands.
* Other devices (like your phone) need the PIN. Turn this off with `"phone_access": false`.
* Everything (chats, memory, notes, schedules) stays in `data\kakarot.db` on your PC.
* Websites are only published after you tap Approve. Server files are backed up first, conflicts stop the publish, and
  files with passwords/keys never go to public places. Publishing passwords/tokens are encrypted with your Windows login.

## Tips for good results

* Be specific: "Check https://lalsobujdata.site and tell me if the login page loads."
* Expand the **"N actions"** line under each reply to see exactly what Kakarot did.
* A local 8B model is smart but not as strong as big cloud AIs. Break very big jobs into
  smaller requests.
* Answers take about 10–60 seconds on a 16 GB laptop without a GPU. That is normal.

## Troubleshooting

| Problem | Fix |
|---|---|
| "picture brain isn't downloaded" | Run `get-advanced-models.bat`. |
| File search says "keyword search only" | Run `get-advanced-models.bat`, then **Status → Re-index now**. |
| Coding is slow | The 14B coding brain writes about one file per 1–3 minutes on this PC. For quicker, simpler code set `code_model` to `qwen2.5-coder:7b` and run `get-code-brain.bat`. |
| Kakarot keeps restarting after an update | Close the window, run `start.bat`; if it still fails, copy the files from the newest `data\backups` folder back. |
| Pictures are slow | Reading a picture takes about 30–90 seconds on a PC without a graphics card. Use a cropped photo for faster results. |
| Top bar says *"Ollama isn't running"* | Open the Ollama app from the Start menu. |
| *"Model not downloaded"* | Open Command Prompt and run `ollama pull qwen3:8b`. |
| Phone link doesn't open | Is the PC on and is the Kakarot window open? Run `go-online.bat` again to see the status. |
| Very slow / PC freezes | Close Chrome tabs and other heavy apps, or switch `model` to `qwen3:4b`. |
| Web search returns errors | DuckDuckGo sometimes rate-limits. Wait a minute and try again. |
| "outside the allowed folders" | Add that folder to `allowed_folders` in `config.json`, then restart. |

## Add your own abilities

Open `tools.py` and copy any `@tool(...)` function. Write what it does in the description,
and Kakarot will learn to use it automatically after a restart.
