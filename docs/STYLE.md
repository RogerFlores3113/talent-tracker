# Talent Search: style and working choices

Roger made these choices while building the app. Follow them in new work so they don't have to be made again. When a change goes against one, say so in the PR and update this file in the same PR.

## Look

- Color marks state only: the primary action, the current place (nav, section index), focus, selection, and errors or warnings. Metadata is muted text separated by middots, never a colored pill or badge per category.
- No gradients, glows, emoji or decorative icons on new elements. The header gradient is the theme's and stays the one exception.
- Use the CSS tokens in `static/style.css` (`--surface`, `--line`, `--muted`, `--accent`, the `--space-*` scale) so every theme follows. Never hard-code a color.
- A floating element (save bar, update toast) is a plain panel: `--surface` background, `--line` border, `--shadow-up`. It sits in the bottom right, `var(--space-5)` from the edges, and spans the width on screens 640px wide or less.

## Words on screen

- Cut text that does not change what the person does next. Taglines, restatements of a setting ("Set up for hiring for clients"), and reassurance lines don't earn their place.
- Name things by what the person sees, not by how they are built: **History**, not "AI changes".
- One short sentence per message. A notice says what happened and, only if needed, the one next step.
- The footer is two centered lines: `Talent Search vX.Y.Z`, then the links.

## Patterns

- **Notifications are toasts.** Something the person can act on later (a new version) shows as a toast in the bottom right with a close X and its actions as buttons, never as a line of page text. X hides it for the browser session. A "Not now" action hides it until something new comes.
- **Settings save once.** Settings forms have no Save button of their own. Any change brings up the save bar in the bottom right with Save and Discard. It hides again when the forms are back as they were, and its Save sends every changed form.
- **Long pages get a section index.** Settings lists its sections on the left, like tabs in Google Docs. The section in view is bright with an accent bar, the rest are faded, hover brightens one, and a click jumps there. On narrow screens the list wraps above the page.
- **Demo data is for exploring.** The tour can load made-up people. They stay after the tour, and a **Clear demo data** button in the top right removes all of them after a backup. The person's own records are never touched.
- **One connect step.** Connecting an AI app is one click per app, picked from a menu in the tour. Anything the AI app needs (skills, playbooks, prompts) comes with the connection. Never send the person to upload or copy files by hand.

## Setup and updates

- A person who isn't technical installs by pointing Claude or Codex at the repo, or by running the one-line install command. Each release carries downloadable app files for Mac, Windows and Linux plus `SHA256SUMS.txt`, built by CI from the tag.
- The app checks GitHub once a day for a new version, on by default, with a switch in Settings to turn it off. Privacy text says exactly what the check sends.
- **Update** reruns the official installer. It must work hands-off on every platform. Test it from inside the installed app, not only from a terminal.

## Working on this repo

- Coding runs in poteto mode (`/poteto-mode`).
- Each change goes through a PR. A PR whose checks all pass may be merged without asking.
- A release is a separate PR that bumps the version in `pyproject.toml`, `src/talent/__init__.py`, `uv.lock` and `packaging/talent-search.spec` and turns `## [Unreleased]` in `CHANGELOG.md` into the new version heading. After it merges, push the `vX.Y.Z` tag. Tag only when Roger asks for the release.
- Verify UI changes in a real browser against the running app (headless Chrome through Playwright works), not only with template tests.
