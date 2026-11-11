#!/bin/sh
# Talent Search installer for macOS (Apple Silicon) and Linux (x86_64).
#
#   curl -fsSL https://raw.githubusercontent.com/RogerFlores3113/talent-tracker/main/install.sh | sh
#
# Options:  --no-open   install but do not open the app
# Testing:  TALENT_INSTALL_FROM=<folder or base URL holding the release files>
#           TALENT_INSTALL_DIR=<folder to install into>
set -eu

BASE_URL="https://github.com/RogerFlores3113/talent-tracker/releases/latest/download"
OPEN_APP=1
UPGRADE=0
for arg in "$@"; do
  case "$arg" in
    --no-open) OPEN_APP=0 ;;
    -h|--help)
      echo "Usage: install.sh [--no-open]"
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 2
      ;;
  esac
done

say() { printf '%s\n' "$*"; }
fail() {
  printf '\nTalent Search was not installed.\n%s\n' "$*" >&2
  exit 1
}

SOURCE="${TALENT_INSTALL_FROM:-$BASE_URL}"
OVERRIDE_DIR="${TALENT_INSTALL_DIR:-}"

OS="$(uname -s)"
ARCH="$(uname -m)"
case "$OS" in
  Darwin)
    case "$ARCH" in
      arm64) ;;
      *) fail "This Mac has an Intel processor. Talent Search is only built for Apple Silicon Macs (M1 or later) for now." ;;
    esac
    ASSET="TalentSearch-mac.zip"
    ;;
  Linux)
    case "$ARCH" in
      x86_64|amd64) ;;
      *) fail "This computer is $ARCH. Talent Search is only built for Linux x86_64 for now." ;;
    esac
    ASSET="TalentSearch-linux.tar.gz"
    ;;
  *)
    fail "This script supports macOS and Linux. On Windows, use install.ps1 (see INSTALL.md)."
    ;;
esac

WORK="$(mktemp -d "${TMPDIR:-/tmp}/talent-install.XXXXXX")"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT INT TERM

fetch() { # fetch <name> <dest>
  case "$SOURCE" in
    http://*|https://*)
      curl -fsSL "${SOURCE%/}/$1" -o "$2" || return 1
      ;;
    *)
      [ -f "$SOURCE/$1" ] || return 1
      cp "$SOURCE/$1" "$2" || return 1
      ;;
  esac
}

say "Installing Talent Search..."
say "Downloading $ASSET"
fetch "$ASSET" "$WORK/$ASSET" || fail "Could not download $ASSET. Check your internet connection and try again."
fetch "SHA256SUMS.txt" "$WORK/SHA256SUMS.txt" || fail "Could not download SHA256SUMS.txt, so the download cannot be checked. Check your internet connection and try again."

say "Checking the download"
if command -v shasum >/dev/null 2>&1 && [ "$OS" = "Darwin" ]; then
  ACTUAL="$(shasum -a 256 "$WORK/$ASSET" | awk '{print $1}')"
elif command -v sha256sum >/dev/null 2>&1; then
  ACTUAL="$(sha256sum "$WORK/$ASSET" | awk '{print $1}')"
elif command -v shasum >/dev/null 2>&1; then
  ACTUAL="$(shasum -a 256 "$WORK/$ASSET" | awk '{print $1}')"
else
  fail "No tool found to check the download (sha256sum or shasum)."
fi
EXPECTED="$(awk -v n="$ASSET" '{f=$2; sub(/^\*/, "", f); if (f == n) {print $1; exit}}' "$WORK/SHA256SUMS.txt" | tr 'A-F' 'a-f')"
[ -n "$EXPECTED" ] || fail "SHA256SUMS.txt does not list $ASSET, so the download cannot be checked."
if [ "$ACTUAL" != "$EXPECTED" ]; then
  fail "The download does not match its checksum, so it was not installed. Expected $EXPECTED but got $ACTUAL. Try again later, and tell the Talent Search maintainer if it keeps happening."
fi
say "Download verified"

mkdir -p "$WORK/unpacked"
if [ "$OS" = "Darwin" ]; then
  ditto -x -k "$WORK/$ASSET" "$WORK/unpacked" || fail "Could not unpack $ASSET."
  [ -d "$WORK/unpacked/Talent Search.app" ] || fail "The download did not contain Talent Search.app."
else
  tar -xzf "$WORK/$ASSET" -C "$WORK/unpacked" || fail "Could not unpack $ASSET."
  [ -d "$WORK/unpacked/Talent Search" ] || fail "The download did not contain the Talent Search folder."
fi

if [ "$OS" = "Darwin" ]; then
  if [ -n "$OVERRIDE_DIR" ]; then
    TARGET_DIR="$OVERRIDE_DIR"
  elif [ -w /Applications ]; then
    TARGET_DIR="/Applications"
  else
    TARGET_DIR="$HOME/Applications"
  fi
  mkdir -p "$TARGET_DIR" || fail "Could not create $TARGET_DIR."
  APP="$TARGET_DIR/Talent Search.app"

  if [ -z "$OVERRIDE_DIR" ] && pgrep -f "Talent Search.app/Contents/MacOS" >/dev/null 2>&1; then
    say "Closing Talent Search so it can be updated. Your candidates and settings are stored elsewhere and are kept."
    osascript -e 'tell application "Talent Search" to quit' >/dev/null 2>&1 || true
    i=0
    while pgrep -f "Talent Search.app/Contents/MacOS" >/dev/null 2>&1 && [ "$i" -lt 20 ]; do
      sleep 1
      i=$((i + 1))
    done
    if pgrep -f "Talent Search.app/Contents/MacOS" >/dev/null 2>&1; then
      fail "Talent Search is still open. Please quit it (Talent Search menu, then Quit) and run this again. Your data is safe."
    fi
  fi

  if [ -e "$APP" ]; then
    UPGRADE=1
    say "Replacing the existing copy. Your candidates and settings are kept."
    rm -rf "$APP" || fail "Could not remove the old copy at $APP."
  fi
  ditto "$WORK/unpacked/Talent Search.app" "$APP" || fail "Could not copy the app into $TARGET_DIR."
  chmod +x "$APP/Contents/MacOS/"* 2>/dev/null || true
  say "Installed to $APP"

  if [ "$OPEN_APP" = "1" ]; then
    say "Opening Talent Search"
    open "$APP" || say "Could not open it automatically. Open Talent Search from $TARGET_DIR."
  fi
else
  if [ -n "$OVERRIDE_DIR" ]; then
    TARGET_DIR="$OVERRIDE_DIR"
  else
    TARGET_DIR="$HOME/.local/share/talent-search-app"
  fi
  mkdir -p "$TARGET_DIR" || fail "Could not create $TARGET_DIR."
  APP_DIR="$TARGET_DIR/Talent Search"
  EXE="$APP_DIR/talent-search"

  if [ -z "$OVERRIDE_DIR" ] && pgrep -f "$EXE" >/dev/null 2>&1; then
    say "Closing Talent Search so it can be updated. Your candidates and settings are stored elsewhere and are kept."
    pkill -TERM -f "$EXE" >/dev/null 2>&1 || true
    sleep 2
    if pgrep -f "$EXE" >/dev/null 2>&1; then
      fail "Talent Search is still open. Please close it and run this again. Your data is safe."
    fi
  fi

  if [ -e "$APP_DIR" ]; then
    UPGRADE=1
    say "Replacing the existing copy. Your candidates and settings are kept."
    rm -rf "$APP_DIR" || fail "Could not remove the old copy at $APP_DIR."
  fi
  cp -R "$WORK/unpacked/Talent Search" "$APP_DIR" || fail "Could not copy the app into $TARGET_DIR."
  chmod +x "$EXE" 2>/dev/null || true
  say "Installed to $APP_DIR"

  if [ -z "$OVERRIDE_DIR" ]; then
    mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications"
    if [ ! -e "$HOME/.local/bin/talent-search" ] || [ -L "$HOME/.local/bin/talent-search" ]; then
      ln -sf "$EXE" "$HOME/.local/bin/talent-search"
    else
      say "Note: $HOME/.local/bin/talent-search already exists and is not a link, so I left it alone."
    fi
    cat > "$HOME/.local/share/applications/talent-search.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Talent Search
Comment=A candidate database for recruiters
Exec="$EXE"
Terminal=false
Categories=Office;
DESKTOP
    say "Added a Talent Search entry to your applications menu, and the command talent-search"
  fi

  if [ "$OPEN_APP" = "1" ]; then
    say "Opening Talent Search"
    nohup "$EXE" >/dev/null 2>&1 &
  fi
fi

say ""
say "Done. Your candidates and settings live in their own folder and are never touched by installs or updates."
if [ "$OS" = "Darwin" ]; then
  say "Talent Search is in your Applications folder (or ~/Applications)."
  CLAUDE_CFG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
else
  say "Talent Search is in your applications menu, or run talent-search."
  CLAUDE_CFG="$HOME/.config/Claude/claude_desktop_config.json"
fi
if [ "$OPEN_APP" = "1" ]; then
  say "Talent Search should now be open in your web browser."
fi
if [ "$UPGRADE" = "1" ] && [ -f "$CLAUDE_CFG" ] && grep -q "talent-search" "$CLAUDE_CFG" 2>/dev/null; then
  say "Already connected. Restart Claude Desktop to use the new version."
elif [ "$OPEN_APP" = "1" ]; then
  say "Next: press \"Connect Claude\", then press \"Add skills\"."
else
  say "Next: open Talent Search, then press \"Connect Claude\" and \"Add skills\"."
fi
if [ "$OS" = "Darwin" ]; then
  say "Connect Claude quits and reopens Claude Desktop (never by force), so save anything you are typing there first."
fi
