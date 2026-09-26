#!/usr/bin/env bash
# mt_tui.sh - Monkeytype analyzer, terminal frontend (DABT). The statistics live in ../mt_analysis.py; a long-lived
# python bridge (mt_bridge.py) answers requests from the pages through files in a private run directory.
#   bash mt_tui.sh [PATH...]     PATHs (CSV files or folders) are added to the saved data sources (Sources page)
APP_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"

# the installed DABT: "dabt app run" exports TUI_ROOT; run by hand, resolve it through the dabt command
if [[ -z "${TUI_ROOT:-}" ]] && command -v dabt >/dev/null 2>&1; then
	_dabt="$(readlink -f "$(command -v dabt)")"
	TUI_ROOT="$(cd -P "$(dirname "$_dabt")/.." && pwd -P)"
fi
[[ -r "${TUI_ROOT:-}/lib/tui.sh" ]] || { echo "monkeytype: DABT not found. Install it first." >&2; exit 1; }
command -v python3 >/dev/null || { echo "monkeytype: python3 not found" >&2; exit 1; }
MT_APP_DIR="$APP_DIR"
for _d in "${XDG_RUNTIME_DIR:-/tmp}"/mt_tui.*; do # run dirs of sessions that were killed without cleanup
	[[ -r "$_d/pid" ]] && ! kill -0 "$(<"$_d/pid")" 2>/dev/null && rm -rf -- "$_d"
done
MT_RUN="$(mktemp -d "${XDG_RUNTIME_DIR:-/tmp}/mt_tui.XXXXXX")"
mkdir -p "$MT_RUN/req" "$MT_RUN/out"
echo "$$" >"$MT_RUN/pid"

TUI_APP_NAME="${TUI_APP_NAME:-monkeytype}"
TUI_APP_TITLE="Monkeytype Analyzer"
TUI_APP_DESC="Statistics for Monkeytype result exports"
TUI_APP_ENTRY="mt_tui.sh"
source "$TUI_ROOT/lib/tui.sh"

# data sources and tag names/labels live in the config folder ($TUI_APP_CONF survives "dabt app update");
# a dev checkout (../data, ../analyzer_state.json next to this folder) seeds them on the first start
MT_SOURCES_FILE="$TUI_APP_CONF/sources.conf"
MT_STATE_FILE="$TUI_APP_CONF/analyzer_state.json"
mkdir -p "$TUI_APP_CONF"
if [[ ! -e "$MT_SOURCES_FILE" ]]; then
	{ echo "# Monkeytype data sources: one CSV file or folder per line (edit on the Sources page)"
	  [[ -d "$APP_DIR/../data" ]] && (cd -P "$APP_DIR/../data" && pwd -P); } >"$MT_SOURCES_FILE"
fi
[[ ! -e "$MT_STATE_FILE" && -r "$APP_DIR/../analyzer_state.json" ]] && cp "$APP_DIR/../analyzer_state.json" "$MT_STATE_FILE"
for _p in "$@"; do # paths on the command line join the saved sources
	_p="$(realpath -e -- "$_p" 2>/dev/null)" || { echo "monkeytype: not found: $_p" >&2; continue; }
	grep -qxF -- "$_p" "$MT_SOURCES_FILE" || echo "$_p" >>"$MT_SOURCES_FILE"
done

# the bridge outlives page changes; it exits by itself when this process or the run dir disappears
python3 -u "$APP_DIR/mt_bridge.py" "$MT_RUN" "$MT_SOURCES_FILE" "$MT_STATE_FILE" "$$" </dev/null >"$MT_RUN/bridge.log" 2>&1 &
MT_PID=$!

MT_MAIN_BASHPID=$BASHPID
mt_shutdown() { # the exit hook also fires in subshells (e.g. the page-cache warm-up worker): only the app itself cleans up
	[[ "$BASHPID" == "$MT_MAIN_BASHPID" ]] || return 0
	kill "$MT_PID" 2>/dev/null
	rm -rf -- "$MT_RUN"
}
tui.hook.on exit mt_shutdown
tui.hook.on resize mt_on_resize

tui.start_cached "$APP_DIR/config/overview.xml"
