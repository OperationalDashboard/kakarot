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

## Safety built in

* It only reads or writes inside `allowed_folders`.
* It never deletes. "Delete" moves files to `_trash` inside the workspace.
* It cannot run programs or system commands.
* Other devices (like your phone) need the PIN. Turn this off with `"phone_access": false`.
* Everything (chats, memory, notes, schedules) stays in `data\kakarot.db` on your PC.

## Tips for good results

* Be specific: "Check https://lalsobujdata.site and tell me if the login page loads."
* Expand the **"N actions"** line under each reply to see exactly what Kakarot did.
* A local 8B model is smart but not as strong as big cloud AIs. Break very big jobs into
  smaller requests.
* Answers take about 10–60 seconds on a 16 GB laptop without a GPU. That is normal.

## Troubleshooting

| Problem | Fix |
|---|---|
| Top bar says *"Ollama isn't running"* | Open the Ollama app from the Start menu. |
| *"Model not downloaded"* | Open Command Prompt and run `ollama pull qwen3:8b`. |
| Phone link doesn't open | Is the PC on and is the Kakarot window open? Run `go-online.bat` again to see the status. |
| Very slow / PC freezes | Close Chrome tabs and other heavy apps, or switch `model` to `qwen3:4b`. |
| Web search returns errors | DuckDuckGo sometimes rate-limits. Wait a minute and try again. |
| "outside the allowed folders" | Add that folder to `allowed_folders` in `config.json`, then restart. |

## Add your own abilities

Open `tools.py` and copy any `@tool(...)` function. Write what it does in the description,
and Kakarot will learn to use it automatically after a restart.
