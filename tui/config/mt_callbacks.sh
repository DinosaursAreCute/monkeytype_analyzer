#!/usr/bin/env bash
# mt_callbacks.sh - behaviour of every page. Only public tui.* calls and renderers.
# Talks to mt_bridge.py: a request is a key=value file in $MT_RUN/req/<seq>, the answer a directory
# $MT_RUN/out/<seq>/ that mt_poll picks up (a 0.1 s timer) and hands to the page's handler.

tui.require terminal_renderer
US=$'\x1f'
MT_DIMS=(files cond difficulty tag label)
MT_FACTORS=("Tag combo:tag_combo" "Label:label" "Difficulty:difficulty" "Condition:cond" "Funbox:funbox"
	"Hour of day:hour_bin" "Weekday:weekday" "Month:month" "Position in session:pos_bin" "Gap before test:gap_bin"
	"Restarts before test:restart_bin" "Regime (auto):_regime" "Cluster (auto):_cluster")

# ── state: lives for the whole run (this file is re-sourced on every page load) ─────────────────────────────────
if ! declare -p MT_SEQ >/dev/null 2>&1; then
	declare -g MT_SEQ=0 MT_PAGE="" MT_META_OK=0 MT_FILTER_LOADED=0 MT_DEAD=0 MT_SPIN=0
	declare -g MT_METRIC=wpm MT_DATE_FROM="" MT_DATE_TO="" MT_AFK=0 MT_OUTL=0 MT_SENS=1 MT_K=0
	declare -g MT_FACTOR=tag_combo MT_FACTOR_LABEL="Tag combo" MT_DT_VIEW=Regimes MT_KEY="" MT_LAST_STATUS=""
	declare -gA MT_X=() MT_PEND=() MT_TKEYS=() MT_OPTD=() MT_OPTN=() MT_METRIC_NAME=()
	declare -ga MT_PANES=() MT_LABELNAMES=() MT_METRIC_KEYS=() MT_LBL_ARGS=()
	declare -ga MT_OPTS_files=() MT_OPTS_cond=() MT_OPTS_difficulty=() MT_OPTS_tag=() MT_OPTS_label=()
	MT_FILTER_FILE="$TUI_APP_CONF/filters.state"
fi

# ── persistence of the filter between runs ──────────────────────────────────────────────────────────────────────
mt_filter_save() {
	local k out=""
	out+="metric	$MT_METRIC"$'\n'"from	$MT_DATE_FROM"$'\n'"to	$MT_DATE_TO"$'\n'"afk	$MT_AFK"$'\n'
	out+="outliers	$MT_OUTL"$'\n'"sens	$MT_SENS"$'\n'"k	$MT_K"$'\n'"factor	$MT_FACTOR_LABEL"$'\n'
	for k in "${!MT_X[@]}"; do out+="x	${k%%"$US"*}	${k#*"$US"}"$'\n'; done
	printf '%s' "$out" >"$MT_FILTER_FILE.tmp" && mv -f "$MT_FILTER_FILE.tmp" "$MT_FILTER_FILE"
}
mt_filter_load() {
	MT_FILTER_LOADED=1
	[[ -r "$MT_FILTER_FILE" ]] || return 1
	local tag a b
	MT_X=()
	while IFS=$'\t' read -r tag a b; do
		case "$tag" in
			metric) MT_METRIC="$a" ;; from) MT_DATE_FROM="$a" ;; to) MT_DATE_TO="$a" ;; afk) MT_AFK="$a" ;;
			outliers) MT_OUTL="$a" ;; sens) MT_SENS="$a" ;; k) MT_K="$a" ;;
			factor) mt_factor_set "$a" ;;
			x) MT_X["$a$US$b"]=1 ;;
		esac
	done <"$MT_FILTER_FILE"
	return 0
}

# ── requests ────────────────────────────────────────────────────────────────────────────────────────────────────
# mt_req PAGE HANDLER CMD [KEY=VALUE]...   PAGE "*" = handle on any page, otherwise dropped when the page changed
mt_req() {
	local page="$1" handler="$2" cmd="$3" seq body kv dim key p
	shift 3
	# the page-cache warm-up runs on_visit in subshells: only the app process itself talks to the bridge
	[[ "$BASHPID" == "${MT_MAIN_BASHPID:-$BASHPID}" ]] || return 0
	((MT_SEQ++))
	printf -v seq '%s_%08d' "$BASHPID" "$MT_SEQ"
	body="cmd=$cmd"$'\n'"seq=$seq"$'\n'"slot=$MT_PAGE"$'\n'"metric=$MT_METRIC"$'\n'"date_from=$MT_DATE_FROM"$'\n'
	body+="date_to=$MT_DATE_TO"$'\n'"afk=$MT_AFK"$'\n'"outliers=$MT_OUTL"$'\n'"sens=$MT_SENS"$'\n'"k=$MT_K"$'\n'
	body+="factor=$MT_FACTOR"$'\n'"factor_label=$MT_FACTOR_LABEL"$'\n'
	for dim in "${MT_DIMS[@]}"; do
		local -n _opts="MT_OPTS_$dim"
		local ex=""
		for key in "${_opts[@]}"; do [[ -n "${MT_X["$dim$US$key"]:-}" ]] && ex+="$key$US"; done
		body+="x_$dim=${ex%"$US"}"$'\n'
		unset -n _opts
	done
	for p in "${MT_PANES[@]}"; do
		tui.pane_size "$p" 2>/dev/null && body+="w_$p=$TUI_PANE_COLS"$'\n'"h_$p=$TUI_PANE_ROWS"$'\n'
	done
	for kv in "$@"; do body+="${kv//$'\n'/ }"$'\n'; done
	printf '%send=1\n' "$body" >"$MT_RUN/req/$seq"
	MT_PEND[$seq]="$page|$handler"
}

# timer: collect finished answers, keep the busy indicator and the status line current
mt_poll() {
	local seq d h page msg
	local -a l
	for seq in "${!MT_PEND[@]}"; do
		d="$MT_RUN/out/$seq"
		[[ -e "$d/done" ]] || continue
		page="${MT_PEND[$seq]%%|*}" h="${MT_PEND[$seq]#*|}"
		unset 'MT_PEND[$seq]'
		[[ -e "$d/superseded" ]] && continue
		if [[ -e "$d/status" ]]; then
			mapfile -t l <"$d/status"
			MT_LAST_STATUS="${l[0]}"
		fi
		if [[ -e "$d/err" ]]; then
			mapfile -t l <"$d/err"
			tui.notify "Analysis failed: ${l[0]}" error 8
			continue
		fi
		if [[ -e "$d/note" ]]; then
			mapfile -t l <"$d/note"
			tui.notify "${l[0]#*	}" "${l[0]%%	*}" 4
		fi
		[[ "$page" == "*" || "$page" == "$MT_PAGE" ]] && [[ -n "$h" ]] && "$h" "$d"
	done
	mt_status_draw
}

mt_status_draw() {
	local spin=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏) busy=""
	if ((!MT_DEAD)) && ! kill -0 "$MT_PID" 2>/dev/null; then
		MT_DEAD=1
		tui.notify "The analysis engine stopped. See $MT_RUN/bridge.log (Methods page shows it)" error 30
	fi
	if ((MT_DEAD)); then
		busy="engine stopped"
	elif [[ ! -e "$MT_RUN/ready" ]]; then
		busy="${spin[MT_SPIN++ % 10]} starting engine"
	elif ((${#MT_PEND[@]})); then
		busy="${spin[MT_SPIN++ % 10]} computing"
	fi
	tui.update mt_busy "$busy"
	tui.update mt_status "${MT_METRIC_NAME[$MT_METRIC]:-$MT_METRIC} · ${MT_LAST_STATUS:-…}"
}

# ── rendering answers ───────────────────────────────────────────────────────────────────────────────────────────
# pane content = records (RS) of fields (US): "t" TEXT, or "r" RENDERER ARG... → RENDERER_string ARG...
mt_show_pane() {
	local pane="$1" f="$2/p_$1" rec part s out=""
	local -a recs args
	[[ -r "$f" ]] || return 0
	mapfile -d $'\x1e' -t recs <"$f"
	for rec in "${recs[@]}"; do
		if [[ "$rec" == t"$US"* ]]; then
			part="${rec#t"$US"}"
		else
			mapfile -d "$US" -t args <<<"${rec#r"$US"}"
			args[-1]="${args[-1]%$'\n'}"
			s="$("${args[0]}_string" "${args[@]:1}")"
			printf -v part '%b' "$s"
		fi
		out+="$part"$'\n'
	done
	# a reset inside a chart (ESC[0m, or colors.sh's ESC[0;33m form) would drop the pane background: restore it
	tui.class.sgr panel
	out="${out//$'\e[0m'/$'\e[0m'$TUI_SGR}"
	out="${out//$'\e[0;'/$'\e[0m'$TUI_SGR$'\e['}"
	tui.output "$pane" "$out"
}

# table widget ← t_NAME (header + rows), row keys ← k_NAME
mt_show_table() {
	local id="$1" name="$2" dir="$3"
	local -a lines keys
	[[ -r "$dir/t_$name" ]] || return 0
	mapfile -t lines <"$dir/t_$name"
	mapfile -t keys <"$dir/k_$name"
	tui.table.set "$id" "${lines[@]}"
	((${#lines[@]} > 1)) || tui.table.clear "$id"
	local IFS="$US"
	MT_TKEYS[$id]="${keys[*]}"
}

# MT_KEY ← key of the selected row of table ID; returns 1 when nothing is selected
mt_table_key() {
	local i
	local -a keys
	i="$(tui.table.selected "$1")"
	IFS="$US" read -ra keys <<<"${MT_TKEYS[$1]:-}"
	((i >= 0 && i < ${#keys[@]})) || return 1
	MT_KEY="${keys[i]}"
}

# ── page plumbing ───────────────────────────────────────────────────────────────────────────────────────────────
# mt_page NAME PANE...   every page's on_visit starts here
mt_page() {
	MT_PAGE="$1"
	shift
	MT_PANES=("$@")
	((MT_FILTER_LOADED)) || mt_filter_load
	tui.every 0.1 mt_poll mt_poll
	mt_status_draw
}

# the first view of a run needs the filter options (default condition) before it can ask for anything
mt_with_meta() {
	if ((MT_META_OK)); then
		"$@"
	else
		MT_AFTER_META=("$@")
		mt_req "*" mt_meta_apply meta
	fi
}

mt_meta_apply() {
	local tag a b c d n dim first=0
	local -a lines
	mapfile -t lines <"$1/meta"
	for dim in "${MT_DIMS[@]}"; do local -n _o="MT_OPTS_$dim"; _o=(); unset -n _o; done
	MT_LABELNAMES=() MT_METRIC_KEYS=()
	((MT_META_OK)) || { [[ -r "$MT_FILTER_FILE" ]] || first=1; }
	for n in "${lines[@]}"; do
		IFS="$US" read -r tag a b c d <<<"$n"
		case "$tag" in
			opt)
				local -n _o="MT_OPTS_$a"
				_o+=("$b")
				unset -n _o
				MT_OPTD["$a$US$b"]="$c" MT_OPTN["$a$US$b"]="$d"
				;;
			default_cond) MT_DEFAULT_COND="$a" ;;
			dates) MT_DATE_MIN="$a" MT_DATE_MAX="$b" ;;
			metric) MT_METRIC_KEYS+=("$a"); MT_METRIC_NAME[$a]="$b" ;;
			labelname) MT_LABELNAMES+=("$a") ;;
		esac
	done
	if ((first)); then # default: only the dominant text condition is directly comparable
		for b in "${MT_OPTS_cond[@]}"; do [[ "$b" == "$MT_DEFAULT_COND" ]] || MT_X["cond$US$b"]=1; done
		mt_filter_save
	fi
	MT_META_OK=1
	[[ "$MT_PAGE" == tags ]] && mt_tags_show "$1"
	if ((${#MT_AFTER_META[@]})); then
		local -a next=("${MT_AFTER_META[@]}")
		MT_AFTER_META=()
		"${next[@]}"
	fi
}

mt_refresh() { # re-ask the bridge for whatever the current page shows
	case "$MT_PAGE" in
		overview) mt_req overview mt_overview_show overview ;;
		groups) mt_req groups mt_groups_show groups ;;
		scan) mt_req scan mt_scan_show scan ;;
		detect) mt_req detect mt_detect_show detect ;;
		dynamics) mt_req dynamics mt_dynamics_show dynamics ;;
		data) mt_req data mt_data_show data ;;
		tags) mt_req "*" mt_meta_apply meta ;;
		filters) mt_filters_show; mt_req filters "" count ;;
	esac
}
mt_on_resize() { [[ -n "$MT_PAGE" ]] && tui.after 0.3 mt_refresh mt_resize; }

mt_after_state() { mt_req "*" mt_meta_apply meta; mt_refresh; }

# ── labelling (from any page) ───────────────────────────────────────────────────────────────────────────────────
# mt_label_ask DESCRIPTION KEY=VALUE...  asks for a name, then labels the target on the bridge
mt_label_ask() {
	local what="$1"
	shift
	MT_LBL_ARGS=("$@")
	local known="${MT_LABELNAMES[*]}"
	tui.prompt "Label $what as:" mt_label_submit --title "New label" \
		--placeholder "${known:+existing: ${known// /, }}"
}
mt_label_submit() {
	local name="${*: -1}"
	[[ -n "${name// /}" ]] || { tui.notify "No name given" warn 2; return; }
	mt_req "*" mt_after_state label_add "name=$name" "${MT_LBL_ARGS[@]}"
}

# ── global keys ─────────────────────────────────────────────────────────────────────────────────────────────────
mt_metric_choose() {
	local -a names=() k
	for k in "${MT_METRIC_KEYS[@]}"; do names+=("${MT_METRIC_NAME[$k]}"); done
	((${#names[@]})) || return 0
	tui.choose "Metric" mt_metric_pick "${names[@]}"
}
mt_metric_pick() {
	MT_METRIC="${MT_METRIC_KEYS[$1]}"
	mt_filter_save
	mt_refresh
}
mt_methods() {
	local -a l
	mapfile -t l <"$MT_APP_DIR/../METHODS.md"
	if ((MT_DEAD)) && [[ -r "$MT_RUN/bridge.log" ]]; then
		local -a lg
		mapfile -t lg <"$MT_RUN/bridge.log"
		l=("ENGINE LOG ($MT_RUN/bridge.log)" "${lg[@]: -40}" "" "${l[@]}")
	fi
	local IFS=$'\n'
	tui.view "Methods" "${l[*]}" --width 110
}

# ── Overview ────────────────────────────────────────────────────────────────────────────────────────────────────
mt_visit_overview() {
	mt_page overview ov_sum ov_charts
	mt_with_meta mt_req overview mt_overview_show overview
}
mt_overview_show() { mt_show_pane ov_sum "$1"; mt_show_pane ov_charts "$1"; }

# ── Groups ──────────────────────────────────────────────────────────────────────────────────────────────────────
mt_factor_set() { # LABEL
	local f
	for f in "${MT_FACTORS[@]}"; do
		[[ "${f%%:*}" == "$1" ]] && { MT_FACTOR_LABEL="$1" MT_FACTOR="${f#*:}"; return 0; }
	done
	return 1
}
mt_visit_groups() {
	mt_page groups gr_plot
	local f
	local -a names=()
	for f in "${MT_FACTORS[@]}"; do names+=("${f%%:*}"); done
	tui.select.set gr_factor "${names[@]}"
	tui.update gr_factor "$MT_FACTOR_LABEL"
	mt_with_meta mt_req groups mt_groups_show groups
	tui.focus gr_table
}
mt_groups_factor() {
	mt_factor_set "$(tui.get gr_factor)" || return 0
	mt_filter_save
	mt_req groups mt_groups_show groups
}
mt_groups_show() { mt_show_table gr_table groups "$1"; mt_show_pane gr_plot "$1"; }
mt_groups_label() {
	mt_table_key gr_table || { tui.notify "Select a group row first" warn 2; return; }
	mt_label_ask "the tests of group '$MT_KEY'" target=group "keys=$MT_KEY"
}

# ── Factor scan ─────────────────────────────────────────────────────────────────────────────────────────────────
mt_visit_scan() {
	mt_page scan sc_plot
	mt_with_meta mt_req scan mt_scan_show scan
	tui.focus sc_table
}
mt_scan_show() { mt_show_table sc_table scan "$1"; mt_show_pane sc_plot "$1"; }
mt_scan_open() { # Enter on a factor: show it on the Groups page when it is one of the grouping factors
	mt_table_key sc_table || return 0
	mt_factor_set "$MT_KEY" || { tui.notify "'$MT_KEY' is a single tag or label: see Tag combo / Label on Groups" info 4; return; }
	mt_filter_save
	tui.goto groups.xml
}

# ── Auto-detect ─────────────────────────────────────────────────────────────────────────────────────────────────
mt_visit_detect() {
	mt_page detect dt_plot
	tui.update dt_sens "$MT_SENS"
	tui.update dt_k "$MT_K"
	tui.update dt_view "$MT_DT_VIEW"
	mt_with_meta mt_req detect mt_detect_show detect
	tui.focus dt_table
}
mt_detect_show() {
	local name
	case "$MT_DT_VIEW" in Clusters) name=clusters ;; Anomalous*) name=anomalies ;; *) name=regimes ;; esac
	mt_show_table dt_table "$name" "$1"
	mt_show_pane dt_plot "$1"
}
mt_detect_params() { # submit of either input, or the Recompute button
	local s k
	s="$(tui.get dt_sens)" k="$(tui.get dt_k)"
	if [[ "$s" =~ ^[0-9]*\.?[0-9]+$ ]]; then MT_SENS="$s"; else tui.notify "Sensitivity must be a number (1 = BIC)" warn 3; fi
	if [[ "$k" =~ ^[0-9]$ ]]; then MT_K="$k"; else tui.notify "k must be 0-9 (0 = choose by BIC)" warn 3; fi
	tui.update dt_sens "$MT_SENS"
	tui.update dt_k "$MT_K"
	mt_filter_save
	mt_req detect mt_detect_show detect
}
mt_detect_view() { MT_DT_VIEW="$(tui.get dt_view)"; mt_req detect mt_detect_show detect; }
mt_detect_label() {
	mt_table_key dt_table || { tui.notify "Select a row first" warn 2; return; }
	case "$MT_DT_VIEW" in
		Clusters) mt_label_ask "cluster C$MT_KEY" target=clusters "keys=$MT_KEY" ;;
		Anomalous*) mt_label_ask "session $MT_KEY" target=sessions "keys=$MT_KEY" ;;
		*) mt_label_ask "regime R$MT_KEY" target=regimes "keys=$MT_KEY" ;;
	esac
}

# ── Dynamics ────────────────────────────────────────────────────────────────────────────────────────────────────
mt_visit_dynamics() {
	mt_page dynamics dy_left dy_right
	mt_with_meta mt_req dynamics mt_dynamics_show dynamics
}
mt_dynamics_show() { mt_show_pane dy_left "$1"; mt_show_pane dy_right "$1"; }

# ── Data ────────────────────────────────────────────────────────────────────────────────────────────────────────
mt_visit_data() {
	mt_page data
	mt_with_meta mt_req data mt_data_show data
	tui.focus da_table
}
mt_data_show() { mt_show_table da_table data "$1"; }
mt_data_export() {
	tui.prompt "Export the filtered tests (all columns) to:" mt_data_export_to --title "Export CSV" \
		--value "$HOME/monkeytype_filtered.csv"
}
mt_data_export_to() { mt_req "*" "" export "path=${*: -1}"; }

# ── Filters ─────────────────────────────────────────────────────────────────────────────────────────────────────
mt_visit_filters() {
	mt_page filters
	local dim p
	for dim in "${MT_DIMS[@]}"; do
		p="fl_p_$dim"
		tui.bind a "mt_filters_all $dim 0" --pane "$p" --page --desc "Filter: include all"
		tui.bind n "mt_filters_all $dim 1" --pane "$p" --page --desc "Filter: include none"
	done
	tui.update fl_from "$MT_DATE_FROM"
	tui.update fl_to "$MT_DATE_TO"
	tui.set fl_afk "$MT_AFK"
	tui.set fl_outl "$MT_OUTL"
	mt_with_meta mt_filters_ready
}
mt_filters_ready() {
	local -a names=() k
	for k in "${MT_METRIC_KEYS[@]}"; do names+=("${MT_METRIC_NAME[$k]}"); done
	tui.select.set fl_metric "${names[@]}"
	tui.update fl_metric "${MT_METRIC_NAME[$MT_METRIC]}"
	tui.update fl_range "data: $MT_DATE_MIN … $MT_DATE_MAX"
	mt_filters_show
	mt_req filters "" count
	tui.focus fl_t_cond
}
mt_filters_show() {
	local dim
	for dim in "${MT_DIMS[@]}"; do mt_filters_table "$dim"; done
}
mt_filters_table() { # DIM
	local dim="$1" key mark on=0
	local -a rows=()
	local -n _o="MT_OPTS_$dim"
	for key in "${_o[@]}"; do
		if [[ -n "${MT_X["$dim$US$key"]:-}" ]]; then mark=" "; else mark="✓"; ((on++)); fi
		rows+=("$mark|${MT_OPTD["$dim$US$key"]//|/¦}|${MT_OPTN["$dim$US$key"]}")
	done
	tui.table.set "fl_t_$dim" "✓|Value ($on/${#_o[@]} on)|n" "${rows[@]}"
	unset -n _o
}
mt_filters_toggle() { # table action: ID
	local dim="${1#fl_t_}" i key
	local -n _o="MT_OPTS_$dim"
	i="$(tui.table.selected "$1")"
	((i >= 0 && i < ${#_o[@]})) || return 0
	key="${_o[i]}"
	unset -n _o
	if [[ -n "${MT_X["$dim$US$key"]:-}" ]]; then unset 'MT_X["$dim$US$key"]'; else MT_X["$dim$US$key"]=1; fi
	mt_filters_table "$dim"
	tui.table.select "$1" "$i"
	mt_filter_save
	mt_req filters "" count
}
mt_filters_all() { # DIM 0|1  (0 = include everything, 1 = exclude everything)
	local dim="$1" key
	local -n _o="MT_OPTS_$dim"
	for key in "${_o[@]}"; do
		if (($2)); then MT_X["$dim$US$key"]=1; else unset 'MT_X["$dim$US$key"]'; fi
	done
	unset -n _o
	mt_filters_table "$dim"
	mt_filter_save
	mt_req filters "" count
}
mt_filters_metric() {
	local k
	for k in "${MT_METRIC_KEYS[@]}"; do [[ "${MT_METRIC_NAME[$k]}" == "$(tui.get fl_metric)" ]] && MT_METRIC="$k"; done
	mt_filter_save
	mt_status_draw
}
mt_filters_dates() { # submit of a date input, and Apply
	local a b re='^([0-9]{4}-[0-9]{2}-[0-9]{2})?$'
	a="$(tui.get fl_from)" b="$(tui.get fl_to)"
	a="${a// /}" b="${b// /}"
	[[ "$a" =~ $re && "$b" =~ $re ]] || { tui.notify "Dates as YYYY-MM-DD (or empty)" warn 3; return 1; }
	MT_DATE_FROM="$a" MT_DATE_TO="$b"
	mt_filter_save
	mt_req filters "" count
}
mt_filters_flag() { # checkbox action: ID VALUE
	case "$1" in fl_afk) MT_AFK="$2" ;; fl_outl) MT_OUTL="$2" ;; esac
	mt_filter_save
	mt_req filters "" count
}
mt_filters_apply() { mt_filters_dates && tui.goto overview.xml; }
mt_filters_reset() {
	local b
	MT_X=() MT_DATE_FROM="" MT_DATE_TO="" MT_AFK=0 MT_OUTL=0
	for b in "${MT_OPTS_cond[@]}"; do [[ "$b" == "$MT_DEFAULT_COND" ]] || MT_X["cond$US$b"]=1; done
	tui.update fl_from ""
	tui.update fl_to ""
	tui.set fl_afk 0
	tui.set fl_outl 0
	mt_filters_show
	mt_filter_save
	mt_req filters "" count
}

# ── Tags & labels ───────────────────────────────────────────────────────────────────────────────────────────────
mt_visit_tags() {
	mt_page tags
	tui.update tg_from "${MT_DATE_MIN:-}"
	tui.update tg_to "${MT_DATE_MAX:-}"
	mt_req "*" mt_meta_apply meta
	tui.focus tg_tags
}
mt_tags_show() { mt_show_table tg_tags tags "$1"; mt_show_table tg_labels labels "$1"; }
mt_tag_rename() {
	mt_table_key tg_tags || { tui.notify "Select a tag first" warn 2; return; }
	MT_TAG_ID="$MT_KEY"
	tui.prompt "Name for tag …${MT_KEY: -6} (empty = unnamed):" mt_tag_rename_to --title "Name tag" \
		--placeholder "e.g. keyboard, layout, setup"
}
mt_tag_rename_to() { mt_req "*" mt_after_state tagname "id=$MT_TAG_ID" "name=${*: -1}"; }
mt_label_rename() {
	mt_table_key tg_labels || { tui.notify "Select a label first" warn 2; return; }
	MT_LBL_OLD="$MT_KEY"
	tui.prompt "Rename label '$MT_KEY' to:" mt_label_rename_to --title "Rename label" --value "$MT_KEY"
}
mt_label_rename_to() { mt_req "*" mt_after_state label_rename "name=$MT_LBL_OLD" "new=${*: -1}"; }
mt_label_delete() {
	mt_table_key tg_labels || { tui.notify "Select a label first" warn 2; return; }
	MT_LBL_OLD="$MT_KEY"
	tui.confirm "Delete label '$MT_KEY'? The tests stay, only the label goes." mt_label_delete_yes \
		--danger --yes Delete --no Keep --title "Delete label"
}
mt_label_delete_yes() { mt_req "*" mt_after_state label_del "name=$MT_LBL_OLD"; }
mt_label_range() {
	local a b re='^[0-9]{4}-[0-9]{2}-[0-9]{2}( [0-9]{2}:[0-9]{2})?$'
	a="$(tui.get tg_from)" b="$(tui.get tg_to)"
	[[ "$a" =~ $re && "$b" =~ $re ]] || { tui.notify "Use YYYY-MM-DD or YYYY-MM-DD HH:MM" warn 3; return; }
	mt_label_ask "all tests from $a to $b" target=range "from=$a" "to=$b"
}
mt_label_filter() { mt_label_ask "every test in the current filter" target=filter; }
