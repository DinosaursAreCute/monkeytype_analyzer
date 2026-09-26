#!/usr/bin/env python3
"""View-model bridge between mt_analysis (unchanged) and the dabt TUI frontend.

Runs as one long-lived process so pandas/statsmodels are imported once. The bash frontend drops request files into
RUN/req/ and polls RUN/out/<seq>/done. Nothing here computes statistics: it calls mt_analysis and turns the results
into what the panes need.

Request file:  key=value lines, last line "end=1" (a file without it is still being written).
Response dir:  status        one line for the status bar
               err           error message (the request failed)
               note          "level<TAB>message" toast
               p_<pane>      pane content: records split by RS (0x1e), fields by US (0x1f):
                               t US text                     literal text (may hold ANSI)
                               r US renderer US arg US ...   call <renderer>_string with these args
               t_<table>     table: header line, then rows, cells split by |
               k_<table>     one key per table row (what the row stands for)
               meta          filter options etc., lines of US separated fields
               done          written last

Data: every *.csv of the folders and files listed in SOURCES_FILE (one path per line), deduplicated by real path and
linked into RUN/data, which is what mt_analysis.load reads. "reload" re-reads the list without restarting.

Usage: mt_bridge.py RUN_DIR SOURCES_FILE STATE_JSON PARENT_PID
"""
import glob
import os
import shutil
import sys
import textwrap
import time
import traceback

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]  # the packaged copy first, then the dev checkout (../mt_analysis.py)
import mt_analysis as A  # noqa: E402

US, RS = "\x1f", "\x1e"
E = "\033["
RST, BOLD, DIMC = E + "0m", E + "1m", E + "2m"
CY, YE, RE, GR, MA, GY = E + "36m", E + "33m", E + "31m", E + "32m", E + "35m", E + "90m"
VIEWS = {"overview", "groups", "scan", "detect", "dynamics", "data", "meta", "count", "sources"}
DIMS = [("files", "Files"), ("cond", "Condition"), ("difficulty", "Difficulty"), ("tag", "Tag combo"),
        ("label", "Label")]


# --------------------------------------------------------------------------- formatting helpers
def f(v, dec=2):
    if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
        return "–"
    if isinstance(v, (pd.Timestamp,)):
        return "" if pd.isna(v) else v.strftime("%Y-%m-%d %H:%M")
    if isinstance(v, (int, np.integer)):
        return str(int(v))
    if isinstance(v, (float, np.floating)):
        return f"{v:.1e}" if (abs(v) < 1e-3 and v != 0) else f"{v:.{dec}f}"
    return str(v)


def fp(p):
    if p is None or (isinstance(p, float) and np.isnan(p)):
        return "–"
    s = f"{p:.2g}" if p >= 1e-4 else f"{p:.0e}"
    return s + ("***" if p < .001 else "**" if p < .01 else "*" if p < .05 else "")


def pstar(p):
    if p is None or np.isnan(p):
        return "–"
    txt = f"p={p:.2g}"
    return (GR + txt + " (significant)" + RST) if p < .05 else (GY + txt + " (n.s.)" + RST)


def wrap(text, w, indent=""):
    return "\n".join(textwrap.fill(p, width=max(w, 20), subsequent_indent=indent) if p else "" for p in text.split("\n"))


def h(title):
    return BOLD + CY + title + RST


def kv(rows, w):
    kw = max(len(k) for k, _ in rows) + 1
    out = []
    for k, v in rows:
        lines = textwrap.wrap(v, max(w - kw - 1, 20)) or [""]
        out.append(BOLD + k.ljust(kw) + RST + " " + lines[0])
        out += [" " * (kw + 1) + ln for ln in lines[1:]]
    return "\n".join(out)


def clean(s):
    return str(s).replace("|", "¦").replace("\n", " ")


def date_ticks(ts, n=5):
    lo, hi = ts.min(), ts.max()
    return "|".join((lo + (hi - lo) * i / (n - 1)).strftime("%b %d") for i in range(n))


def days(ts, t0):
    return ((ts - t0).dt.total_seconds() / 86400).round(3)


def pts(x, y):
    return " ".join(f"{a:g},{b:.3f}" for a, b in zip(x, y) if pd.notna(a) and pd.notna(b))


# --------------------------------------------------------------------------- response
class Out:
    def __init__(self):
        self.files = {}

    def pane(self, name, blocks):
        recs = []
        for b in blocks:
            recs.append(US.join(str(x) for x in b))
        self.files["p_" + name] = RS.join(recs)

    def table(self, name, header, rows, keys):
        self.files["t_" + name] = "\n".join(["|".join(map(clean, header))] + ["|".join(map(clean, r)) for r in rows])
        self.files["k_" + name] = "\n".join(str(k).replace("\n", " ") for k in keys)

    def write(self, out_dir, seq):
        d = os.path.join(out_dir, seq)
        os.makedirs(d, exist_ok=True)
        for k, v in self.files.items():
            with open(os.path.join(d, k), "w") as fh:
                fh.write(v)
        open(os.path.join(d, "done"), "w").close()


# --------------------------------------------------------------------------- bridge
class Bridge:
    def __init__(self, run, sources_file, state_path):
        self.run, self.sources_file = run, sources_file
        self.st = A.State(state_path)
        self.reload()

    # ---- data sources
    def read_sources(self):
        try:
            with open(self.sources_file) as fh:
                lines = fh.read().splitlines()
        except OSError:
            return []
        return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]

    @staticmethod
    def is_export(path):
        """A Monkeytype results export: its header names _id and wpm."""
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                head = fh.readline().strip().split(",")
        except OSError:
            return False
        return "_id" in head and "wpm" in head

    @staticmethod
    def csvs_of(path):
        if os.path.isdir(path):
            return sorted(glob.glob(os.path.join(path, "*.csv")))
        if os.path.isfile(path) and path.lower().endswith(".csv"):
            return [path]
        return []

    def reload(self):
        link = os.path.join(self.run, "data")
        shutil.rmtree(link, ignore_errors=True)
        os.makedirs(link)
        self.files, seen = {}, set()
        for src in self.read_sources():
            for p in self.csvs_of(src):
                rp = os.path.realpath(p)
                if rp in seen or not self.is_export(rp):  # stray CSVs in a folder are skipped, not fatal
                    continue
                seen.add(rp)
                name, n = os.path.basename(rp), 1
                while name in self.files:  # same file name from another folder: prefix the folder
                    n += 1
                    name = f"{os.path.basename(os.path.dirname(rp))}{'' if n == 2 else n}__{os.path.basename(rp)}"
                self.files[name] = rp
                os.symlink(rp, os.path.join(link, name))
        self.base, self.load_error = None, ""
        if self.files:
            try:
                self.base = A.load(link)
            except Exception as e:  # e.g. a CSV that is not a Monkeytype export
                self.load_error = f"{type(e).__name__}: {e}"
        self.refresh_state()

    def refresh_state(self):
        self.df = A.apply_state(self.base, self.st) if self.base is not None else None
        self._dcache, self._acache = {}, {}

    # ---- filter
    def options(self, dim):
        df = self.df
        if dim == "files":
            vc = pd.Series([x for t in df["files"] for x in t]).value_counts().sort_index()
            return [(k, k, n) for k, n in vc.items()]
        col = {"cond": "cond", "difficulty": "difficulty", "tag": "tag_key", "label": "label"}[dim]
        vc = df[col].value_counts()
        if dim == "tag":  # "" (no tags) travels as "-": empty fields don't survive the frontend's splitting
            return [(k or "-", self.st.combo_name(k), n) for k, n in vc.items()]
        return [(k, k, n) for k, n in vc.items()]

    def spec(self, r):
        s = {}
        for dim, _ in DIMS:
            ex = {x for x in r.get("x_" + dim, "").split(US) if x}
            if not ex:
                continue
            keep = [k for k, _, _ in self.options(dim) if k not in ex]
            if dim == "tag":
                s["tag_combo"] = [self.st.combo_name("" if k == "-" else k) for k in keep]
            else:
                s[dim] = keep
        for k in ("date_from", "date_to"):
            v = r.get(k, "").strip()
            if v:
                s[k] = pd.Timestamp(v)
        s["exclude_afk"] = r.get("afk") == "1"
        s["exclude_outliers"] = r.get("outliers") == "1"
        s["metric"] = self.metric(r)
        return s

    def metric(self, r):
        m = r.get("metric", "wpm")
        return m if m in A.METRICS else "wpm"

    def data(self, r):
        s = self.spec(r)
        key = repr(sorted((k, str(v)) for k, v in s.items()))
        if key not in self._dcache:
            self._dcache = {key: A.filter_df(self.df, s)}
        return self._dcache[key], key

    def auto(self, r):
        d, key = self.data(r)
        m = self.metric(r)
        sens = float(r.get("sens") or 1.0)
        k = int(float(r.get("k") or 0))
        ak = (key, m, sens, k)
        if ak not in self._acache:
            st, info = A.change_points(d, m, sens)
            cl = A.clusters(d, k_fixed=k or None)
            self._acache = {ak: dict(st=st, info=info, cl=cl, regime=A.regimes_per_test(d, st),
                                     anom=A.anomalous_sessions(d, m))}
        return self._acache[ak]

    def status(self, d):
        warn = f"  ⚠ {d['cond'].nunique()} conditions mixed" if d["cond"].nunique() > 1 else ""
        return f"{len(d)} tests · {d['session'].nunique()} sessions · {d['date'].nunique()} days{warn}"

    # ---- dispatch
    NO_DATA_OK = ("meta", "sources", "reload")

    def handle(self, r):
        o = Out()
        cmd = r.get("cmd", "")
        fn = getattr(self, "v_" + cmd, None)
        if fn is None:
            raise ValueError(f"unknown command {cmd!r}")
        if self.df is None and cmd not in self.NO_DATA_OK:
            msg = self.load_error or "No data yet: add Monkeytype CSV exports (files or folders) on the Sources page."
            o.files["status"] = "no data · add files on the Sources page (alt+9)"
            for k in r:
                if k.startswith("w_"):
                    o.pane(k[2:], [("t", wrap(msg, self.w(r, k[2:], 60)))])
            return o
        d = None
        if cmd not in self.NO_DATA_OK:
            d, _ = self.data(r)
            o.files["status"] = self.status(d)
        fn(r, d, o)
        return o

    def w(self, r, pane, default=80):
        return max(int(r.get("w_" + pane) or default), 30)

    # ---- meta: filter options, tags, labels
    def v_meta(self, r, d, o):
        lines = [US.join(["ntests", str(0 if self.df is None else len(self.df))])]
        for k, v in A.METRICS.items():
            lines.append(US.join(["metric", k, v]))
        for name in sorted(self.st.labels):
            lines.append(US.join(["labelname", name]))
        if self.df is None:
            o.files["meta"] = "\n".join(lines)
            o.table("tags", ["Name"], [], [])
            o.table("labels", ["Label"], [], [])
            o.files["status"] = "no data · add files on the Sources page (alt+9)"
            return
        for dim, _ in DIMS:
            for k, disp, n in self.options(dim):
                lines.append(US.join(["opt", dim, k, disp, str(n)]))
        lines.append(US.join(["default_cond", self.df["cond"].value_counts().index[0]]))
        lines.append(US.join(["dates", f"{self.df['date'].min():%Y-%m-%d}", f"{self.df['date'].max():%Y-%m-%d}"]))
        o.files["meta"] = "\n".join(lines)
        df = self.df
        tids = A.all_tag_ids(df)
        has = {t: df["tag_key"].str.contains(t, regex=False) for t in tids}
        rows = []
        for t in tids:
            sub = df[has[t]]
            co = [self.st.tag_name(x) for x in tids if x != t and has[x][has[t]].all()]
            rows.append([self.st.tag_names.get(t, "") or "(unnamed)", t[-6:], len(sub), sub["session"].nunique(),
                         f"{sub['t'].min():%m-%d}→{sub['t'].max():%m-%d}", f(sub["wpm"].median(), 1),
                         ", ".join(co) or "–"])
        o.table("tags", ["Name", "Id", "Tests", "Sess", "Used", "Wpm", "Always with"], rows, tids)
        lrows = []
        for k, ids in sorted(self.st.labels.items()):
            sub = df.loc[df.index.intersection(ids)]
            lrows.append([k, len(ids), f(sub["t"].min()), f(sub["t"].max()),
                          f(sub["wpm"].median(), 1) if len(sub) else "–"])
        o.table("labels", ["Label", "Tests", "First", "Last", "Med wpm"], lrows, sorted(self.st.labels))
        o.files["status"] = self.status(self.df) + "  (all data)"

    def v_count(self, r, d, o):
        pass  # status only

    # ---- sources
    def v_sources(self, r, d, o):
        rows, keys, raw_rows, counted = [], [], 0, set()
        for src in self.read_sources():
            kind = "folder" if os.path.isdir(src) else ("file" if os.path.isfile(src) else "missing")
            files = self.csvs_of(src)
            ids, bad = set(), 0
            for p in files:
                if not self.is_export(p):
                    bad += 1
                    continue
                try:
                    col = pd.read_csv(p, usecols=["_id"], dtype=str)["_id"]
                except Exception:
                    bad += 1
                    continue
                ids |= set(col)
                if os.path.realpath(p) not in counted:
                    counted.add(os.path.realpath(p))
                    raw_rows += len(col)
            status = ("not found" if kind == "missing" else "no CSV files" if not files
                      else f"{bad} skipped: not a Monkeytype export" if bad else "ok")
            home = os.path.expanduser("~")
            shown = "~" + src[len(home):] if src == home or src.startswith(home + "/") else src
            rows.append([shown, kind, len(files), len(ids), status])
            keys.append(src)
        o.table("sources", ["Path", "Kind", "CSV", "Tests", "Status"], rows, keys)
        lines = [h("Loaded data")]
        if self.df is not None:
            lines += [kv([("CSV files", f"{len(self.files)} (each file counted once, even if listed twice)"),
                          ("Unique tests", f"{len(self.df)} of {raw_rows} rows ({raw_rows - len(self.df)} duplicates "
                                           "from overlapping exports merged by test id)"),
                          ("Period", f"{self.df['t'].min():%Y-%m-%d} → {self.df['t'].max():%Y-%m-%d}")],
                         self.w(r, "sr_info", 60))]
        else:
            lines.append(YE + (self.load_error or "nothing loaded yet") + RST)
        dup = [n for n in self.files if "__" in n]
        if dup:
            lines += ["", GY + wrap("Same file name in several folders, shown in the Files filter as: "
                                    + ", ".join(dup), self.w(r, "sr_info", 60)) + RST]
        w = self.w(r, "sr_info", 60)
        lines += ["", h("How sources work"), wrap(
            "A folder contributes every *.csv directly inside it (not its subfolders); a file is used as is. "
            "Monkeytype exports overlap (each export repeats older tests), so tests are merged by their id: adding "
            "every export you have is safe. Paths are stored in " + self.sources_file + " and used on every start. "
            "Browse… walks folders: Enter opens a folder, picks a CSV, or 'Use this folder' adds the whole folder.",
            w), "", GY + "Tag names and labels: " + self.st.path + RST]
        o.pane("sr_info", [("t", "\n".join(lines))])
        o.files["status"] = (f"{len(rows)} sources · {len(self.files)} CSV files · "
                             f"{0 if self.df is None else len(self.df)} unique tests")

    def v_reload(self, r, d, o):
        self.reload()
        if self.load_error:
            o.files["note"] = f"error\tCould not load: {self.load_error}"
        elif self.df is None:
            o.files["note"] = "warn\tNo CSV files in the listed sources"
        else:
            o.files["note"] = f"success\tLoaded {len(self.df)} tests from {len(self.files)} CSV files"

    # ---- overview
    def v_overview(self, r, d, o):
        m, lab = self.metric(r), A.METRICS[self.metric(r)]
        ws, wc = self.w(r, "ov_sum", 50), self.w(r, "ov_charts", 80)
        if len(d) < 10:
            o.pane("ov_sum", [("t", "Fewer than 10 tests match the filter.")])
            o.pane("ov_charts", [("t", "")])
            return
        s = A.summary(d, m)
        tr = s.get("trend")
        rows = [("Tests", f"{s['n']} in {s['sessions']} sessions on {s['days']} days"),
                ("Period", f"{s['first']:%Y-%m-%d} → {s['last']:%Y-%m-%d}"),
                ("Median", f"{s['median']:.2f}   (mean {s['mean']:.2f} ± {s['sd']:.2f} SD)"),
                ("90th pct / best", f"{s['p90']:.2f} / {s['max']:.2f}   · {s['n_pb']} PBs"),
                ("Last 14 days", f"median {s['recent_median']:.2f} vs {s['before_median']:.2f} before")]
        txt = [h(f"{lab} — summary"), kv(rows, ws), ""]
        if not np.isnan(s["icc"]):
            txt += [h("Where the variation comes from"),
                    wrap(f"{BOLD}{s['icc']:.0%}{RST} of the variance is between sessions (day, state, setup), "
                         f"{1 - s['icc']:.0%} is test-to-test noise. Within-session SD {BOLD}{s['sd_within']:.2f}{RST}: "
                         f"one test is only good to ±{1.96 * s['sd_within']:.1f}, a 10-test session mean to "
                         f"±{1.96 * s['sd_within'] / np.sqrt(10):.1f}.", ws), ""]
        if tr:
            txt += [h("Learning trend"),
                    wrap(f"{BOLD}{tr['per_doubling']:+.2f}{RST} per doubling of practice "
                         f"[95% CI {tr['lo']:+.2f}, {tr['hi']:+.2f}], {pstar(tr['p'])}. Controls: position in "
                         f"session, condition, difficulty, tags; SE clustered over {tr['n_sessions']} sessions.",
                         ws), ""]
        txt += [h("Effort"), wrap(f"{s['restarts_per_saved']:.1f} restarts and {s['abandoned_s_per_saved']:.1f}s "
                                  "of abandoned typing per saved test.", ws)]
        if len(s["conds"]) > 1:
            txt += ["", YE + wrap("⚠ Several text conditions are mixed; their levels are not comparable: "
                                  + ", ".join(f"{k} ({v})" for k, v in s["conds"].items()), ws) + RST]
        o.pane("ov_sum", [("t", "\n".join(txt))])

        t0 = d["t"].min()
        sm = d.groupby("session").agg(t=("t", "median"), y=(m, "median"))
        roll = d[m].rolling(50, min_periods=10).quantile(.9)
        pb = d[d["isPb"]]
        cw = wc - 1
        blocks = [("t", h(f"{lab} over time") + GY + "  dots = tests · line = session median · red = rolling p90"
                   + RST),
                  ("r", "scatter", f"Tests:{pts(days(d['t'], t0), d[m])}",
                   f"Session median:{pts(days(sm['t'], t0), sm['y'])}",
                   f"Rolling p90:{pts(days(d['t'], t0), roll)}",
                   f"PB:{pts(days(pb['t'], t0), pb[m])}",
                   "-w", cw, "-h", 12, "-c", "DIM_CYAN,WHITE,RED,YELLOW", "-line", "2,3", "-xt", date_ticks(d["t"])),
                  ("t", "")]
        lx = np.log10(d["practice"].astype(float))
        sx, sy = A.smooth(lx.values, d[m].values.astype(float))
        ticks = "|".join(f"{10 ** v:.0f}" for v in np.linspace(lx.min(), lx.max(), 5))
        blocks += [("t", h("Learning curve") + GY + "  x = cumulative tests (log scale) · line = LOWESS" + RST),
                   ("r", "scatter", f"Tests:{pts(lx, d[m])}", f"LOWESS:{pts(sx, sy)}", "-w", cw, "-h", 9,
                    "-c", "DIM_CYAN,YELLOW", "-line", "2", "-xt", ticks), ("t", "")]
        blocks += [("t", h("Distribution")),
                   ("r", "histogram", " ".join(f"{v:.2f}" for v in d[m].dropna()), "-w", cw, "-h", 7,
                    "-mk", f"{s['median']:.2f}:median:YELLOW,{s['p90']:.2f}:p90:RED"), ("t", "")]
        daily = d.groupby("date").agg(n=(m, "size"), med=(m, "median"))
        full = pd.date_range(daily.index.min(), daily.index.max())
        dn = daily["n"].reindex(full, fill_value=0)
        blocks += [("t", h("Daily volume") + GY + f"  {len(full)} days · max {dn.max()} tests/day · "
                    f"{(dn == 0).sum()} days off" + RST),
                   ("r", "sparkline", " ".join(str(int(v)) for v in dn), "-w", cw - 2, "-c", "GREEN"),
                   ("t", GY + f"{full[0]:%b %d}".ljust(cw - 8) + f"{full[-1]:%b %d}" + RST)]
        o.pane("ov_charts", blocks)

    # ---- groups
    def group_col(self, r, d):
        col = r.get("factor", "tag_combo")
        d = d.copy()
        if col == "_regime":
            d[col] = self.auto(r)["regime"].map(lambda x: f"R{x}" if pd.notna(x) else "(small session)")
        elif col == "_cluster":
            cl = self.auto(r)["cl"]
            d[col] = cl["labels"].reindex(d.index).map(lambda c: f"C{c}" if pd.notna(c) else "n/a") if cl else "n/a"
        elif col not in d:
            col = "tag_combo"
        return d, col

    def v_groups(self, r, d, o):
        m = self.metric(r)
        w = self.w(r, "gr_plot", 60) - 1
        d, col = self.group_col(r, d)
        res = A.compare_groups(d, m, col) if len(d) >= 10 else None
        if res is None:
            o.table("groups", ["Group"], [], [])
            o.pane("gr_plot", [("t", "Only one group (or too few tests) in the current filter.")])
            return
        rows, keys = [], []
        for x in res["rows"]:
            flag = "n<5" if x["n"] < A.MIN_LEVEL_N else ("few sess" if x["sessions"] < 3 else (
                "low overlap" if not np.isnan(x["overlap"]) and x["overlap"] < .2 else ""))
            ci = f"[{x['adj_lo']:+.1f},{x['adj_hi']:+.1f}]" if not np.isnan(x["adj_lo"]) else ""
            rows.append([x["group"], x["n"], x["sessions"], f(x["median"]), f(x["delta"]), f(x["cliff"]),
                         f(x["adj"]), ci, fp(x["adj_p"]), f(x["overlap"]), flag])
            keys.append(x["group"])
        o.table("groups", ["Group", "n", "Sess", "Median", "ΔMed", "Cliff δ", "Adj Δ", "95% CI", "p", "Overlap",
                           "Flag"], rows, keys)
        lab = A.METRICS[m]
        head = [h(f"{lab} by {r.get('factor_label', col)}"),
                kv([("Reference", f"{res['ref']} (largest group)"),
                    ("Adjusted test", f"{pstar(res.get('f_p', np.nan))}, ΔR² = {res.get('dr2', np.nan):.3f} "
                                      "(cluster-robust Wald)"),
                    ("Naive test", f"{pstar(res['kw_p'])} (Kruskal–Wallis, ignores sessions and trend)"),
                    ("Controls", ", ".join(res["controls"]) or "none")], w)]
        if "error" in res:
            head.append(RE + "Model: " + res["error"] + RST)
        boxes = []
        for x in res["rows"]:
            v = d.loc[d[col].astype(str) == x["group"], m].dropna()
            if len(v):
                q = np.percentile(v, [5, 25, 50, 75, 95])
                boxes.append(f"{x['group'][:22]}:{','.join(f'{a:.2f}' for a in q)},{len(v)}")
        ref_med = res["rows"][0]["median"]
        blocks = [("t", "\n".join(head)), ("t", ""),
                  ("t", h("Distribution") + GY + "  whiskers p5–p95, box IQR, ┃ median, ┆ reference median" + RST),
                  ("r", "boxplot", *boxes, "-w", w, "-z", f"{ref_med:.3f}"), ("t", "")]
        fr = [f"{x['group'][:22]}:{x['adj']:.3f},{x['adj_lo']:.3f},{x['adj_hi']:.3f}"
              for x in res["rows"][1:] if not np.isnan(x["adj"])]
        if fr:
            blocks += [("t", h("Adjusted difference vs reference") + GY + "  95% CI; coloured = excludes 0" + RST),
                       ("r", "forest", *fr, "-w", w, "-p", 1), ("t", "")]
        blocks.append(("t", wrap(
            GY + "Δmed = raw median difference. Adj Δ removes practice, session position and known setup, with "
            "SEs clustered by session. Flags: n<5 not modelled · few sess = <3 sessions · low overlap = <20% "
            "practice overlap with the reference (weakly identified). Cliff δ: .15 small, .33 medium, .47 large."
            + (" Regimes/clusters come from the metric itself: their p-values are circular." if col.startswith("_")
               else "") + RST, w)))
        o.pane("gr_plot", blocks)

    # ---- factor scan
    def v_scan(self, r, d, o):
        m = self.metric(r)
        w = self.w(r, "sc_plot", 60) - 1
        sc = A.factor_scan(d, m, self.st.tag_name) if len(d) >= 20 else None
        if sc is None or not len(sc):
            o.table("scan", ["Factor"], [], [])
            o.pane("sc_plot", [("t", "Not enough data for a factor scan.")])
            return
        rows = [[x.factor, x.levels, f(x.dr2, 4), fp(x.p), fp(x.q), fp(x.kw_p), x.strongest, x.min_sessions,
                 "fragile" if x.min_sessions < 3 else ""] for x in sc.itertuples()]
        o.table("scan", ["Factor", "Lv", "ΔR²", "p", "q (FDR)", "p naive", "Largest adjusted contrast", "Min sess",
                         "Flag"], rows, list(sc["factor"]))
        mx = max(sc["dr2"].max(), 1e-9)
        lw = min(max(len(x) for x in sc["factor"]), 22)
        bw = max(w - lw - 10, 10)
        lines = [h("Variance explained beyond the controls (ΔR²)"),
                 GY + "red = q<0.05 after FDR · grey = not significant" + RST, ""]
        for x in sc.itertuples():
            n = int(round(x.dr2 / mx * bw))
            c = RE if x.q < .05 else GY
            lines.append(x.factor[:lw].ljust(lw) + " " + c + "█" * n + RST + " " * (bw - n) + f" {x.dr2:.3f}")
        lines += ["", wrap(GY + "Model per factor: metric ~ factor + ln(practice) + ln(position) + condition + "
                           "difficulty + tags (a control is dropped when it is the factor). p from a session-clustered "
                           "Wald test, q = Benjamini–Hochberg across factors. 'fragile' = the smallest level spans <3 "
                           "sessions." + RST, w)]
        o.pane("sc_plot", [("t", "\n".join(lines))])

    # ---- auto-detect
    def v_detect(self, r, d, o):
        m = self.metric(r)
        w = self.w(r, "dt_plot", 60) - 1
        if len(d) < 30:
            for t in ("regimes", "clusters", "anomalies"):
                o.table(t, ["–"], [], [])
            o.pane("dt_plot", [("t", "Need at least 30 tests for auto-detection.")])
            return
        au = self.auto(r)
        st, info, cl, an = au["st"], au["info"], au["cl"], au["anom"]
        starts = {c["at"]: (i, c) for i, c in enumerate(info)}
        rows, keys = [], []
        for rg, g in st.groupby("regime"):
            ids = d.index[d["session"].isin(g.index)]
            i, c = starts.get(g["start"].min(), (None, None))
            rows.append([f"R{rg}", f"{g['start'].min():%Y-%m-%d}", f"{g['end'].max():%Y-%m-%d}", len(g), len(ids),
                         f(d.loc[ids, m].median()), f(c["after"] - c["before"]) if c else "",
                         ("; ".join(c["changes"]) or "? nothing recorded") if c else "start"])
            keys.append(str(rg))
        o.table("regimes", ["Regime", "From", "To", "Sess", "Tests", "Median", "Shift", "What changed at start"],
                rows, keys)
        if cl:
            t = cl["table"]
            o.table("clusters", ["Cluster", "n", "Share", "wpm", "acc", "cons", "First", "Last", "Over-represented"],
                    [[f"C{x.cluster}", x.n, f"{x.share:.0%}", f(x.wpm, 1), f(x.acc, 1), f(x.consistency, 1),
                      f"{x.first:%m-%d}", f"{x.last:%m-%d}", x.descriptors] for x in t.itertuples()],
                    [str(c) for c in t["cluster"]])
        else:
            o.table("clusters", ["Cluster"], [], [])
        if len(an):
            a = an.reset_index()
            o.table("anomalies", ["Session", "Start", "n", "Median", "Unexplained Δ", "z", "Tags"],
                    [[x.session, f(x.start), x.n, f(x.med), f(x.resid), f(x.z), x.tags] for x in a.itertuples()],
                    [str(s) for s in a["session"]])
        else:
            o.table("anomalies", ["Session"], [], [])

        t0 = st["start"].min()
        lvl = []
        for rg, g in st.groupby("regime"):
            med = g["med"].median()
            lvl += [(days(pd.Series([g["start"].min()]), t0).iloc[0], med),
                    (days(pd.Series([g["end"].max()]), t0).iloc[0], med)]
        lx, ly = zip(*lvl)
        blocks = [("t", h(f"Regimes of session-median {A.METRICS[m]}") + GY
                   + "  binary segmentation, BIC penalty" + RST),
                  ("r", "scatter", f"Session median:{pts(days(st['start'], t0), st['med'])}",
                   f"Regime level:{pts(lx, ly)}", "-w", w, "-h", 10, "-c", "CYAN,YELLOW", "-line", "2",
                   "-xt", date_ticks(st["start"])), ("t", "")]
        cps = [h("Change points")]
        for i, c in enumerate(info):
            why = "; ".join(c["changes"]) if c["changes"] else YE + "nothing recorded — worth a label?" + RST
            cps.append(f"CP{i + 1} {c['at']:%Y-%m-%d}  {c['before']:.1f} → {c['after']:.1f}  " + why)
        if not info:
            cps.append(GY + "none at this sensitivity" + RST)
        blocks += [("t", wrap("\n".join(cps), w, "    ")), ("t", "")]
        if cl:
            lab = cl["labels"]
            dd = d.loc[lab.index]
            ser = [f"C{c}:{pts(dd.loc[lab == c, 'wpm'], dd.loc[lab == c, 'consistency'])}" for c in range(cl["k"])]
            blocks += [("t", h(f"Clusters (k={cl['k']})") + GY + "  x = wpm, y = consistency" + RST),
                       ("r", "scatter", *ser, "-w", w, "-h", 10),
                       ("t", wrap(GY + "BIC by k: " + ", ".join(f"{k}:{v:.0f}" for k, v in cl["bics"].items())
                                  + ". Over-represented = metadata more common in the cluster than overall "
                                    "(share, lift; Fisher q<0.05)." + RST, w)), ("t", "")]
        blocks.append(("t", wrap(GY + "Anomalous sessions: mean residual after practice, position, condition, "
                                      "difficulty and tags, standardised by session size (|z|≥2.5). Select rows in "
                                      "the table and label them to test the idea as a factor." + RST, w)))
        o.pane("dt_plot", blocks)

    # ---- dynamics & relationships
    def v_dynamics(self, r, d, o):
        m = self.metric(r)
        wl, wr = self.w(r, "dy_left", 60) - 1, self.w(r, "dy_right", 60) - 1
        wu = A.warmup(d, m)
        if wu is None:
            o.pane("dy_left", [("t", "Not enough sessions with ≥5 tests.")])
        else:
            p = wu["profile"].dropna()
            p1 = wu["profile"].iloc[0]
            blocks = [("t", h("Warm-up / fatigue") + GY + "  Δ vs the session's own median, mean ± 95% CI" + RST),
                      ("r", "forest", *[f"test {int(x.pos):>2} (n={x.n}):{x.mean:.3f},{x.lo:.3f},{x.hi:.3f}"
                                        for x in p.itertuples()], "-w", wl, "-p", 1),
                      ("t", kv([("Sessions used", f"{wu['n_sessions']} (≥5 tests)"),
                                ("First test", f"{p1['mean']:+.2f} [{p1['lo']:+.2f}, {p1['hi']:+.2f}]"),
                                ("Warm-up slope", f"{wu['slope']:+.2f} per e-fold of position "
                                                  f"[{wu['slope_lo']:+.2f}, {wu['slope_hi']:+.2f}], "
                                                  f"{pstar(wu['p'])}")], wl)), ("t", "")]
            g = wu["gaps"].dropna()
            if len(g):
                blocks += [("t", h("Pause before the test")),
                           ("r", "forest", *[f"{x.gap} (n={x.n}):{x.mean:.3f},{x.lo:.3f},{x.hi:.3f}"
                                             for x in g.itertuples()], "-w", wl, "-p", 1), ("t", "")]
            x, y = wu["tmin"]
            if len(x):
                blocks += [("t", h("Drift over the session") + GY + "  x = minutes into session, LOWESS" + RST),
                           ("r", "scatter", f"Δ vs session median:{pts(x, y)}", "-w", wl, "-h", 7, "-line", "1",
                            "-c", "GREEN", "-xt", f"0|{x.max() / 2:.0f}|{x.max():.0f} min")]
            o.pane("dy_left", blocks)

        sa = A.speed_accuracy(d)
        blocks = []
        if sa:
            wd, bd = sa["within_df"], sa["between_df"]
            blocks += [("t", h("Speed vs accuracy") + GY + "  rawWpm (wpm counts only correct chars)" + RST),
                       ("t", kv([("Overall", f"ρ={sa['total'][0]:+.2f} {pstar(sa['total'][1])}"),
                                 ("Within session", f"ρ={sa['within'][0]:+.2f} {pstar(sa['within'][1])}"),
                                 ("Between sessions", f"ρ={sa['between'][0]:+.2f} {pstar(sa['between'][1])}")], wr)),
                       ("t", GY + "within: deviations from the session mean" + RST),
                       ("r", "scatter", f"within:{pts(wd['rawWpm'], wd['acc'])}", "-w", wr, "-h", 8, "-nolegend",
                        "-c", "CYAN", "-xt", f"{wd['rawWpm'].min():.0f}|0|{wd['rawWpm'].max():+.0f} rawWpm"),
                       ("t", GY + "between: one dot per session (means)" + RST),
                       ("r", "scatter", f"between:{pts(bd['rawWpm'], bd['acc'])}", "-w", wr, "-h", 8, "-nolegend",
                        "-c", "MAGENTA", "-xt", f"{bd['rawWpm'].min():.0f}|{bd['rawWpm'].max():.0f} rawWpm"),
                       ("t", wrap(GY + "Negative within-session ρ = pushing speed costs accuracy. Opposite signs "
                                       "within vs between = Simpson's paradox." + RST, wr)), ("t", "")]
        c = A.corr_matrix(d)
        blocks += [("t", h("Spearman correlations")),
                   ("r", "heatmap", "|" + "|".join(c.columns),
                    *["|".join([n] + [f"{v:.2f}" if not np.isnan(v) else "" for v in c.loc[n]]) for n in c.index],
                    "-cw", 4 if wr < 90 else 5, "-p", 1, "-m", 1, "-w", wr), ("t", "")]
        mo = d.groupby("month")[["c_incorrect", "c_extra", "c_missed"]].sum()
        tot = d.groupby("month")["chars"].sum()
        pct = mo.div(tot, axis=0) * 100
        lines = [h("Errors by month") + GY + "  % of characters" + RST,
                 BOLD + "Month    incorrect  extra  missed  total" + RST]
        for mth, x in pct.iterrows():
            lines.append(f"{mth}  {x.c_incorrect:9.2f}  {x.c_extra:5.2f}  {x.c_missed:6.2f}  {x.sum():5.2f}")
        blocks.append(("t", "\n".join(lines)))
        o.pane("dy_right", blocks)

    # ---- data
    def v_data(self, r, d, o):
        m = self.metric(r)
        dd = d.sort_values("t", ascending=False).head(int(r.get("limit") or 400))
        rows = [[f"{x.t:%Y-%m-%d %H:%M}", f(x.wpm), f(x.rawWpm), f(x.acc), f(x.consistency), f(x.err_rate),
                 x.cond, x.difficulty, x.tag_combo, x.label, x.session, x.pos, "★" if x.isPb else ""]
                for x in dd.itertuples()]
        o.table("data", ["Time", "WPM", "Raw", "Acc", "Cons", "Err%", "Condition", "Diff", "Tags", "Label", "Sess",
                         "Pos", "PB"], rows, list(dd.index))

    def v_export(self, r, d, o):
        path = os.path.expanduser(r.get("path", "").strip() or "~/monkeytype_filtered.csv")
        d.drop(columns=["files", "labels"]).to_csv(path)
        o.files["note"] = f"success\tExported {len(d)} tests to {path}"

    # ---- state changes
    def v_tagname(self, r, d, o):
        tid, name = r.get("id", ""), r.get("name", "").strip()
        if name:
            self.st.tag_names[tid] = name
        else:
            self.st.tag_names.pop(tid, None)
        self.st.save()
        self.refresh_state()
        o.files["note"] = f"success\tTag {tid[-6:]} → {name or '(unnamed)'}"

    def target_ids(self, r, d):
        t = r.get("target", "")
        keys = [k for k in r.get("keys", "").split(US) if k]
        if t == "group":
            d2, col = self.group_col(r, d)
            return d2.index[d2[col].astype(str).isin(keys)]
        if t == "regimes":
            return d.index[self.auto(r)["regime"].isin({int(k) for k in keys}).fillna(False)]
        if t == "clusters":
            lab = self.auto(r)["cl"]["labels"]
            return lab.index[lab.isin({int(k) for k in keys})]
        if t == "sessions":
            return d.index[d["session"].isin({int(k) for k in keys})]
        if t == "range":
            a, b = pd.Timestamp(r["from"]), pd.Timestamp(r["to"])
            if len(r["to"].strip()) <= 10:
                b += pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
            return self.df.index[(self.df["t"] >= a) & (self.df["t"] <= b)]
        if t == "filter":
            return d.index
        raise ValueError(f"unknown label target {t!r}")

    def v_label_add(self, r, d, o):
        name = r.get("name", "").strip()
        ids = list(self.target_ids(r, d))
        if not name or not ids:
            o.files["note"] = "warn\tNothing labelled (no name or no tests selected)"
            return
        self.st.labels[name] = sorted(set(self.st.labels.get(name, [])) | set(ids))
        self.st.save()
        self.refresh_state()
        o.files["note"] = f"success\tLabel '{name}': {len(self.st.labels[name])} tests (+{len(ids)} selected)"

    def v_label_del(self, r, d, o):
        name = r.get("name", "")
        self.st.labels.pop(name, None)
        self.st.save()
        self.refresh_state()
        o.files["note"] = f"success\tDeleted label '{name}'"

    def v_label_rename(self, r, d, o):
        old, new = r.get("name", ""), r.get("new", "").strip()
        if old in self.st.labels and new:
            self.st.labels[new] = sorted(set(self.st.labels.get(new, [])) | set(self.st.labels.pop(old)))
            self.st.save()
            self.refresh_state()
            o.files["note"] = f"success\tRenamed '{old}' → '{new}'"


# --------------------------------------------------------------------------- server loop
def read_req(path):
    try:
        with open(path) as fh:
            lines = fh.read().split("\n")
    except OSError:
        return None
    if "end=1" not in lines:
        return None  # still being written
    r = {}
    for ln in lines:
        if "=" in ln:
            k, v = ln.split("=", 1)
            r[k] = v
    return r


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def main():
    run, sources_file, state_path, ppid = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
    req_dir, out_dir = os.path.join(run, "req"), os.path.join(run, "out")
    os.makedirs(req_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)
    try:
        b = Bridge(run, sources_file, state_path)
    except Exception as e:
        with open(os.path.join(run, "fatal"), "w") as fh:
            fh.write(f"{type(e).__name__}: {e}")
        raise
    open(os.path.join(run, "ready"), "w").close()
    last_clean = time.time()
    while alive(ppid) and os.path.isdir(run):
        names = sorted(n for n in os.listdir(req_dir) if not n.startswith("."))
        reqs = [(n, read_req(os.path.join(req_dir, n))) for n in names]
        reqs = [(n, r) for n, r in reqs if r is not None]
        if not reqs:
            time.sleep(0.04)
            if time.time() - last_clean > 30:
                last_clean = time.time()
                for n in os.listdir(out_dir):
                    p = os.path.join(out_dir, n)
                    if time.time() - os.path.getmtime(p) > 60:
                        for x in os.listdir(p):
                            os.remove(os.path.join(p, x))
                        os.rmdir(p)
            continue
        latest = {}
        for n, r in reqs:
            if r.get("cmd") in VIEWS:
                latest[(r.get("cmd"), r.get("slot", ""))] = n
        for n, r in reqs:
            os.remove(os.path.join(req_dir, n))
            seq = r.get("seq", n)
            if r.get("cmd") in VIEWS and latest[(r.get("cmd"), r.get("slot", ""))] != n:
                o = Out()
                o.files["superseded"] = "1"
                o.write(out_dir, seq)
                continue
            t = time.time()
            try:
                o = b.handle(r)
            except Exception as e:
                traceback.print_exc()
                o = Out()
                o.files["err"] = f"{r.get('cmd')}: {type(e).__name__}: {e}"
            print(f"{seq} {r.get('cmd')} {time.time() - t:.2f}s", flush=True)
            o.write(out_dir, seq)


if __name__ == "__main__":
    main()
