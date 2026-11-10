# Installing Talent Search (instructions for an AI assistant)

A person asked you to install Talent Search from this repository. You are probably
Claude in the Claude desktop app (Code tab or Cowork), Claude Code or Codex, running on
their own computer. Follow these steps exactly. Talk to the person in plain, friendly
words; they may not be technical.

## 1. Detect the operating system

- macOS on Apple Silicon (`uname -m` prints `arm64`): use the shell script.
- Linux x86_64 (`uname -m` prints `x86_64`): use the shell script.
- Windows 10 or 11, 64-bit: use the PowerShell script.
- Intel Mac or anything else: stop and go to "If it fails".

## 2. Run the installer

macOS or Linux, from any folder:

```sh
curl -fsSL https://raw.githubusercontent.com/RogerFlores3113/talent-tracker/main/install.sh | sh
```

Windows, in PowerShell:

```powershell
irm https://raw.githubusercontent.com/RogerFlores3113/talent-tracker/main/install.ps1 | iex
```

If this repository is already cloned on the computer, run the local script instead:
`sh install.sh` (macOS, Linux) or `powershell -ExecutionPolicy Bypass -File install.ps1`
(Windows; this only applies to this one script run).

The script downloads the latest release for this computer, checks it against
`SHA256SUMS.txt`, installs it, and opens Talent Search. Add `--no-open` (`-NoOpen` on
Windows) only if the person does not want the app to open.

Where it installs:

- macOS: `/Applications/Talent Search.app`, or `~/Applications` if `/Applications` is not writable
- Windows: `%LOCALAPPDATA%\Programs\Talent Search`
- Linux: `~/.local/share/talent-search-app/Talent Search`, with a `talent-search` command in `~/.local/bin`

If Talent Search is already installed, the script closes it and replaces it. The
person's candidates and settings are stored in a separate folder and are kept.

## 3. What success looks like

The script prints "Download verified", then "Installed to ...", then a closing message,
and exits with status 0. Talent Search opens in the person's web browser at
`http://127.0.0.1:8765`.

Then tell the person, in plain words: "Talent Search is installed and open in your
browser. Press **Connect Claude**, then press **Add skills**." Those two buttons are in
the Talent Search window that just opened. **Connect Claude** links the app to Claude,
and **Add skills** adds the Talent Search skills. You do not need to edit any
configuration files yourself.

## If it fails

Stop and tell the person what happened, in one or two plain sentences. Do not try to
work around it.

- Intel Mac: only Apple Silicon builds exist. Say so and stop.
- No network, or a download error: say so, and suggest they check their connection and ask again.
- Checksum mismatch: the file was not installed on purpose. Report it and stop. Never
  skip or edit the check, and never install a file that failed it.
- Anything else: show the script's last message and stop.

## Do not

- Do not change Gatekeeper, SmartScreen, antivirus, firewall or any other security setting,
  and do not disable any security feature, even if something asks you to.
- Do not remove macOS quarantine flags or run the installer with elevated permissions
  (no `sudo`, no "Run as administrator").
- Do not download the app from anywhere other than this repository's GitHub releases.
- Do not delete the person's Talent Search data folder.
