#!/usr/bin/env python3
"""Monkeytype results analyzer — standalone Qt desktop app.

Usage:  python3 mt_app.py [data_dir]        (default: ./data next to this script)
User tag names and labels are stored in analyzer_state.json next to the data folder.
"""
import os
import sys

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("QtAgg")
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from PyQt6 import QtCore, QtGui  # noqa: E402
from PyQt6 import QtWidgets as W  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402

import mt_analysis as A  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PALETTE = matplotlib.colormaps["tab10"].colors + matplotlib.colormaps["Dark2"].colors
WARN, BAD = QtGui.QColor(255, 240, 180), QtGui.QColor(255, 205, 190)


# --------------------------------------------------------------------------- widgets
class Plot(W.QWidget):
    def __init__(self, h=6):
        super().__init__()
        self.fig = Figure(figsize=(10, h), layout="constrained")
        self.canvas = FigureCanvasQTAgg(self.fig)
        lay = W.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(NavigationToolbar2QT(self.canvas, self))
        lay.addWidget(self.canvas)

    def clear(self):
        self.fig.clear()
        return self.fig

    def draw(self):
        self.canvas.draw_idle()


class CheckList(W.QWidget):
    """Checkable list with all/none buttons. selected() is None when everything is checked."""

    def __init__(self, title):
        super().__init__()
        lay = W.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        row = W.QHBoxLayout()
        row.addWidget(W.QLabel(f"<b>{title}</b>"))
        row.addStretch()
        for txt, val in (("all", True), ("none", False)):
            b = W.QPushButton(txt)
            b.setFixedWidth(42)
            b.clicked.connect(lambda _, v=val: self.set_all(v))
            row.addWidget(b)
        lay.addLayout(row)
        self.list = W.QListWidget()
        self.list.setMaximumHeight(120)
        lay.addWidget(self.list)

    def populate(self, counts):
        self.list.clear()
        for val, n in counts.items():
            it = W.QListWidgetItem(f"{val}  ({n})")
            it.setData(Qt.ItemDataRole.UserRole, val)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked)
            self.list.addItem(it)

    def set_all(self, on):
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)

    def check_only(self, vals):
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setCheckState(Qt.CheckState.Checked if it.data(Qt.ItemDataRole.UserRole) in vals
                             else Qt.CheckState.Unchecked)

    def selected(self):
        items = [self.list.item(i) for i in range(self.list.count())]
        sel = [it.data(Qt.ItemDataRole.UserRole) for it in items if it.checkState() == Qt.CheckState.Checked]
        return None if len(sel) == len(items) else sel


def fmt(v, dec=2):
    if v is None:
        return ""
    if isinstance(v, (pd.Timestamp,)):
        return v.strftime("%Y-%m-%d %H:%M") if not pd.isna(v) else ""
    if isinstance(v, (float, np.floating)):
        if np.isnan(v) or np.isinf(v):
            return "–"
        if abs(v) < 1e-3 and v != 0:
            return f"{v:.1e}"
        return f"{v:.{dec}f}"
    return str(v)


class NumItem(W.QTableWidgetItem):
    def __init__(self, v, dec):
        super().__init__(fmt(v, dec))
        self.v = v

    def __lt__(self, other):
        a, b = self.v, getattr(other, "v", None)
        try:
            return float(a) < float(b)
        except (TypeError, ValueError):
            return str(a) < str(b)


def fill_table(tw, df, dec=2, row_color=None, editable=()):
    tw.setSortingEnabled(False)
    tw.clear()
    tw.setRowCount(len(df))
    tw.setColumnCount(len(df.columns))
    tw.setHorizontalHeaderLabels([str(c) for c in df.columns])
    for r, (_, row) in enumerate(df.iterrows()):
        color = row_color(row) if row_color else None
        for c, col in enumerate(df.columns):
            v = row[col]
            it = NumItem(v if not isinstance(v, pd.Timestamp) else v.value, dec) \
                if isinstance(v, (int, float, np.number, pd.Timestamp)) and not isinstance(v, bool) \
                else W.QTableWidgetItem(fmt(v, dec))
            if isinstance(v, pd.Timestamp):
                it.setText(fmt(v))
            if col not in editable:
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if color is not None:
                it.setBackground(color)
                it.setForeground(QtGui.QColor("black"))
            tw.setItem(r, c, it)
    tw.setSortingEnabled(True)
    tw.resizeColumnsToContents()
    for c in range(tw.columnCount()):
        tw.setColumnWidth(c, min(tw.columnWidth(c), 420))


def html_table(rows):
    return "<table cellspacing=0 cellpadding=3>" + "".join(
        f"<tr><td><b>{k}</b></td><td>{v}</td></tr>" for k, v in rows) + "</table>"


def pstar(p):
    if p is None or np.isnan(p):
        return "–"
    return f"p={p:.2g}" + (" ***" if p < .001 else " **" if p < .01 else " *" if p < .05 else " (n.s.)")


# --------------------------------------------------------------------------- main window
class App(W.QMainWindow):
    def __init__(self, data_dir):
        super().__init__()
        self.setWindowTitle("Monkeytype Analyzer")
        self.resize(1500, 950)
        self.data_dir = data_dir
        self.st = A.State(os.path.join(os.path.dirname(os.path.abspath(data_dir)), "analyzer_state.json"))
        self.base = A.load(data_dir)
        self.df = A.apply_state(self.base, self.st)
        self.d = self.df
        self._auto = None
        self.dirty = set()

        self._build_filters()
        self.tabs = W.QTabWidget()
        self.setCentralWidget(self.tabs)
        self.pages = {}
        for name, builder in [("Overview", self._tab_overview), ("Groups", self._tab_groups),
                              ("Factor scan", self._tab_scan), ("Auto-detect", self._tab_auto),
                              ("Session dynamics", self._tab_dynamics), ("Relationships", self._tab_rel),
                              ("Tags & labels", self._tab_tags), ("Data", self._tab_data),
                              ("Methods", self._tab_methods)]:
            w, render = builder()
            self.pages[name] = render
            self.tabs.addTab(w, name)
        self.tabs.currentChanged.connect(lambda _: self._render_current())
        self._refresh_filter_lists()
        self.apply_filters()

    # ---------------------------------------------------------------- filters
    def _build_filters(self):
        dock = W.QDockWidget("Filters", self)
        dock.setFeatures(W.QDockWidget.DockWidgetFeature.DockWidgetMovable)
        inner = W.QWidget()
        lay = W.QVBoxLayout(inner)
        lay.addWidget(W.QLabel("<b>Metric</b>"))
        self.metric = W.QComboBox()
        for k, v in A.METRICS.items():
            self.metric.addItem(v, k)
        self.metric.currentIndexChanged.connect(lambda _: self.apply_filters())
        lay.addWidget(self.metric)
        self.fl = {k: CheckList(t) for k, t in [("files", "Files"), ("cond", "Condition (mode)"),
                                                 ("difficulty", "Difficulty"), ("tag_combo", "Tag combo"),
                                                 ("label", "Label")]}
        for w in self.fl.values():
            lay.addWidget(w)
        grid = W.QFormLayout()
        self.date_from, self.date_to = W.QDateEdit(), W.QDateEdit()
        for de in (self.date_from, self.date_to):
            de.setCalendarPopup(True)
            de.setDisplayFormat("yyyy-MM-dd")
        grid.addRow("From", self.date_from)
        grid.addRow("To", self.date_to)
        lay.addLayout(grid)
        self.cb_afk = W.QCheckBox("Exclude tests with AFK time")
        self.cb_out = W.QCheckBox("Exclude outliers (|robust z|>4 per condition)")
        lay.addWidget(self.cb_afk)
        lay.addWidget(self.cb_out)
        row = W.QHBoxLayout()
        b = W.QPushButton("Apply filters")
        b.setStyleSheet("font-weight:bold; padding:6px")
        b.clicked.connect(self.apply_filters)
        row.addWidget(b)
        b2 = W.QPushButton("Reset")
        b2.clicked.connect(self.reset_filters)
        row.addWidget(b2)
        lay.addLayout(row)
        self.status = W.QLabel()
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        lay.addStretch()
        sc = W.QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(inner)
        sc.setMinimumWidth(330)
        dock.setWidget(sc)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)

    def _refresh_filter_lists(self):
        df = self.df
        self.fl["files"].populate(pd.Series([f for t in df["files"] for f in t]).value_counts().sort_index().to_dict())
        for k in ("cond", "difficulty", "tag_combo", "label"):
            self.fl[k].populate(df[k].value_counts().to_dict())
        mn, mx = df["date"].min(), df["date"].max()
        for de in (self.date_from, self.date_to):
            de.setDateRange(QtCore.QDate(mn.year, mn.month, mn.day), QtCore.QDate(mx.year, mx.month, mx.day))
        self.date_from.setDate(QtCore.QDate(mn.year, mn.month, mn.day))
        self.date_to.setDate(QtCore.QDate(mx.year, mx.month, mx.day))
        # sensible default: only the dominant text condition is directly comparable
        top = df["cond"].value_counts().index[0]
        self.fl["cond"].check_only([top])

    def reset_filters(self):
        self._refresh_filter_lists()
        self.cb_afk.setChecked(False)
        self.cb_out.setChecked(False)
        self.apply_filters()

    @property
    def m(self):
        return self.metric.currentData()

    def spec(self):
        s = {k: w.selected() for k, w in self.fl.items()}
        s["date_from"] = self.date_from.date().toPyDate()
        s["date_to"] = self.date_to.date().toPyDate()
        s["exclude_afk"] = self.cb_afk.isChecked()
        s["exclude_outliers"] = self.cb_out.isChecked()
        s["metric"] = self.m
        return s

    def apply_filters(self):
        self.d = A.filter_df(self.df, self.spec())
        self._auto = None
        d = self.d
        warn = ""
        if d["cond"].nunique() > 1:
            warn = "<br><span style='color:#b00'>⚠ Multiple text conditions mixed – raw levels are not comparable.</span>"
        self.status.setText(f"<b>{len(d)}</b> tests · <b>{d['session'].nunique()}</b> sessions · "
                            f"<b>{d['date'].nunique()}</b> days{warn}")
        self.dirty = set(self.pages)
        self._render_current()

    def _render_current(self):
        name = self.tabs.tabText(self.tabs.currentIndex())
        if name in self.dirty:
            self.dirty.discard(name)
            if len(self.d) < 10 and name not in ("Tags & labels", "Methods", "Data"):
                return
            QtWidgets_busy(lambda: self.pages[name]())

    def state_changed(self):
        self.st.save()
        self.df = A.apply_state(self.base, self.st)
        keep = {k: w.selected() for k, w in self.fl.items()}
        self._refresh_filter_lists()
        for k in ("files", "cond", "difficulty"):
            if keep[k] is not None:
                self.fl[k].check_only(keep[k])
        self.apply_filters()

    # ---------------------------------------------------------------- labelling
    def label_ids(self, ids, suggestion=""):
        ids = list(ids)
        if not ids:
            return
        name, ok = W.QInputDialog.getItem(
            self, "Label tests", f"Label name for {len(ids)} tests\n(existing name → tests are added):",
            sorted(self.st.labels) or [suggestion], 0, True)
        if not ok or not name.strip():
            return
        name = name.strip()
        self.st.labels[name] = sorted(set(self.st.labels.get(name, [])) | set(ids))
        self.state_changed()

    def auto(self):
        """Metric-derived groupings on the current filter (cached)."""
        if self._auto is None:
            sens = getattr(self, "sens", None)
            st, info = A.change_points(self.d, self.m, sens.value() if sens else 1.0)
            kf = self.kfix.value() if hasattr(self, "kfix") else 0
            cl = A.clusters(self.d, k_fixed=kf or None)
            self._auto = dict(st=st, info=info, cl=cl, regime=A.regimes_per_test(self.d, st),
                              anom=A.anomalous_sessions(self.d, self.m))
        return self._auto

    # ---------------------------------------------------------------- tab: overview
    def _tab_overview(self):
        w = W.QSplitter(Qt.Orientation.Vertical)
        txt = W.QTextBrowser()
        plot = Plot(7)
        w.addWidget(txt)
        w.addWidget(plot)
        w.setSizes([260, 700])

        def render():
            d, m = self.d, self.m
            s = A.summary(d, m)
            lab = A.METRICS[m]
            tr = s.get("trend")
            rows = [("Tests / sessions / days", f"{s['n']} / {s['sessions']} / {s['days']}  "
                     f"({s['first']:%Y-%m-%d} → {s['last']:%Y-%m-%d})"),
                    (f"{lab}", f"median <b>{s['median']:.2f}</b>, mean {s['mean']:.2f} ± {s['sd']:.2f} SD, "
                     f"90th pct {s['p90']:.2f}, best {s['max']:.2f}, PBs flagged {s['n_pb']}"),
                    ("Last 14 days vs before", f"median {s['recent_median']:.2f} vs {s['before_median']:.2f}")]
            if not np.isnan(s["icc"]):
                rows.append(("Variance decomposition (ICC1)",
                             f"<b>{s['icc']:.0%}</b> of variance is between sessions (day/state/setup), "
                             f"{1 - s['icc']:.0%} test-to-test. Between-session SD {s['sd_between']:.2f}, "
                             f"within-session SD (noise floor) <b>{s['sd_within']:.2f}</b> → a single test "
                             f"tells you ±{1.96 * s['sd_within']:.1f}; a 10-test session mean ±"
                             f"{1.96 * s['sd_within'] / np.sqrt(10):.1f}."))
            if tr:
                rows.append(("Learning trend", f"<b>{tr['per_doubling']:+.2f}</b> per doubling of practice "
                             f"[95% CI {tr['lo']:+.2f}, {tr['hi']:+.2f}], {pstar(tr['p'])} "
                             f"(controls: session position, condition, difficulty, tags; "
                             f"SE clustered over {tr['n_sessions']} sessions)"))
            rows.append(("Effort", f"{s['restarts_per_saved']:.1f} restarts and "
                         f"{s['abandoned_s_per_saved']:.1f}s of abandoned typing per saved test"))
            if len(s["conds"]) > 1:
                rows.append(("⚠ Conditions", ", ".join(f"{k}: {v}" for k, v in s["conds"].items())))
            txt.setHtml(html_table(rows))

            fig = plot.clear()
            ax = fig.subplots(2, 2)
            # timeline
            a = ax[0, 0]
            groups = d["tag_combo"] if d["cond"].nunique() == 1 else d["cond"]
            for i, (g, sub) in enumerate(d.groupby(groups)):
                a.scatter(sub["t"], sub[m], s=5, alpha=.35, color=PALETTE[i % len(PALETTE)], label=g)
            sm = d.groupby("session").agg(t=("t", "median"), y=(m, "median"))
            a.plot(sm["t"], sm["y"], "k.-", lw=.8, ms=4, label="session median")
            roll = d[m].rolling(50, min_periods=10).quantile(.9)
            a.plot(d["t"], roll, color="crimson", lw=1.2, label="rolling p90 (50 tests)")
            pb = d[d["isPb"]]
            a.scatter(pb["t"], pb[m], marker="*", s=90, color="gold", edgecolor="k", zorder=5, label="PB")
            a.set_title(f"{lab} over time")
            a.legend(fontsize=7, markerscale=2, ncol=2)
            # learning curve
            a = ax[0, 1]
            a.scatter(d["practice"], d[m], s=4, alpha=.25, color="gray")
            x, y = A.smooth(d["practice"].values.astype(float), d[m].values.astype(float))
            a.plot(x, y, color="C0", lw=2, label="LOWESS")
            a.set_xscale("log")
            a.set_xlabel("practice (cumulative tests, log)")
            a.set_title("Learning curve")
            a.legend(fontsize=8)
            # distribution
            a = ax[1, 0]
            a.hist(d[m].dropna(), bins=50, color="C0", alpha=.7)
            for q, c in ((s["median"], "k"), (s["p90"], "crimson")):
                a.axvline(q, color=c, ls="--", lw=1)
            a.set_title(f"Distribution (median black, p90 red)")
            # volume
            a = ax[1, 1]
            daily = d.groupby("date").agg(n=(m, "size"), med=(m, "median"))
            a.bar(daily.index, daily["n"], color="lightgray", width=.9)
            a.set_ylabel("tests / day")
            a2 = a.twinx()
            a2.plot(daily.index, daily["med"], "C1.-", lw=1)
            a2.set_ylabel(f"daily median {lab}")
            a.set_title("Volume and daily median")
            for a in ax.flat:
                a.tick_params(labelsize=8)
            fig.autofmt_xdate()
            plot.draw()
        return w, render

    # ---------------------------------------------------------------- tab: groups
    def _tab_groups(self):
        w = W.QWidget()
        lay = W.QVBoxLayout(w)
        top = W.QHBoxLayout()
        top.addWidget(W.QLabel("Group by:"))
        combo = W.QComboBox()
        for k, v in A.FACTORS.items():
            combo.addItem(k, v)
        combo.addItem("Regime (auto-detected)", "_regime")
        combo.addItem("Cluster (auto-detected)", "_cluster")
        top.addWidget(combo)
        top.addStretch()
        blab = W.QPushButton("Label selected groups…")
        top.addWidget(blab)
        lay.addLayout(top)
        split = W.QSplitter(Qt.Orientation.Vertical)
        plot = Plot(5)
        txt = W.QTextBrowser()
        tab = W.QTableWidget()
        tab.setSelectionBehavior(W.QAbstractItemView.SelectionBehavior.SelectRows)
        bottom = W.QSplitter(Qt.Orientation.Horizontal)
        bottom.addWidget(tab)
        bottom.addWidget(txt)
        bottom.setSizes([900, 400])
        split.addWidget(plot)
        split.addWidget(bottom)
        split.setSizes([500, 350])
        lay.addWidget(split)
        cur = {}

        def render():
            d, m, col = self.d.copy(), self.m, combo.currentData()
            if col == "_regime":
                d["_regime"] = self.auto()["regime"].map(lambda r: f"R{r}" if pd.notna(r) else "(small session)")
            elif col == "_cluster":
                cl = self.auto()["cl"]
                d["_cluster"] = cl["labels"].reindex(d.index).map(lambda c: f"C{c}" if pd.notna(c) else "n/a") \
                    if cl else "n/a"
            r = A.compare_groups(d, m, col)
            cur.update(d=d, col=col)
            fig = plot.clear()
            if r is None:
                txt.setHtml("Only one group in current filter.")
                tab.setRowCount(0)
                plot.draw()
                return
            order = [x["group"] for x in r["rows"]]
            colors = {g: PALETTE[i % len(PALETTE)] for i, g in enumerate(order)}
            ax1, ax2 = fig.subplots(1, 2, width_ratios=[1, 1.4])
            data = [d.loc[d[col].astype(str) == g, m].dropna().values for g in order]
            ax1.boxplot(data, vert=False, tick_labels=[f"{g} (n={len(x)})" for g, x in zip(order, data)],
                        showfliers=False, widths=.6)
            rng = np.random.default_rng(0)
            for i, (g, x) in enumerate(zip(order, data)):
                ax1.scatter(x, i + 1 + rng.uniform(-.2, .2, len(x)), s=4, alpha=.35, color=colors[g])
            ax1.invert_yaxis()
            ax1.tick_params(labelsize=8)
            ax1.set_xlabel(A.METRICS[m])
            for g in order:
                sub = d[d[col].astype(str) == g]
                ax2.scatter(sub["t"], sub[m], s=6, alpha=.5, color=colors[g], label=g)
            ax2.set_title("Groups over time (confounding with learning is visible here)", fontsize=9)
            ax2.legend(fontsize=7, markerscale=2, ncol=2)
            ax2.tick_params(labelsize=8)
            fig.autofmt_xdate()
            plot.draw()
            t = pd.DataFrame(r["rows"])
            t["adj 95% CI"] = [f"[{a:+.2f}, {b:+.2f}]" if not np.isnan(a) else "" for a, b in zip(t["adj_lo"], t["adj_hi"])]
            t = t[["group", "n", "sessions", "median", "mean", "sd", "delta", "cliff", "adj", "adj 95% CI",
                   "adj_p", "overlap", "first", "last"]].rename(columns={
                       "delta": "Δmedian vs ref", "cliff": "Cliff δ", "adj": "adjusted Δ", "adj_p": "adj p",
                       "overlap": "practice overlap"})

            def color(row):
                if row["n"] < A.MIN_LEVEL_N:
                    return BAD
                if row["sessions"] < 3 or (not np.isnan(row["practice overlap"]) and row["practice overlap"] < .2):
                    return WARN
                return None
            fill_table(tab, t, row_color=color)
            ctrl = ", ".join(r["controls"]) or "none"
            txt.setHtml(
                f"<b>Reference:</b> {r['ref']} (largest group)<br>"
                f"<b>Omnibus, adjusted</b> (cluster-robust Wald): {pstar(r.get('f_p', np.nan))}, "
                f"ΔR² = {r.get('dr2', np.nan):.3f} over controls<br>"
                f"<b>Omnibus, naive</b> (Kruskal–Wallis, ignores sessions & trend): {pstar(r['kw_p'])}<br>"
                f"<b>Controls:</b> {ctrl}<br>{'<b>Model error:</b> ' + r['error'] + '<br>' if 'error' in r else ''}<br>"
                "<b>How to read:</b> <i>Δmedian</i> is the raw difference; <i>adjusted Δ</i> removes practice, "
                "session position and known setup, with SEs clustered by session. "
                "<span style='background:#fff0b4'>Yellow</span> = fewer than 3 sessions or <20% practice overlap "
                "with the reference (the effect is weakly identified). "
                "<span style='background:#ffcdbe'>Red</span> = n&lt;5, not modelled. Cliff δ: |δ|≥.15 small, "
                "≥.33 medium, ≥.47 large."
                + ("<br><br><b>Note:</b> regimes/clusters are derived from the metric itself, so p-values here "
                   "are circular and only descriptive." if col.startswith("_") else ""))

        def do_label():
            rows = sorted({i.row() for i in tab.selectedIndexes()})
            if not rows or not cur:
                return
            gs = [tab.item(r, 0).text() for r in rows]
            d = cur["d"]
            self.label_ids(d.index[d[cur["col"]].astype(str).isin(gs)], " + ".join(gs))

        combo.currentIndexChanged.connect(lambda _: render())
        blab.clicked.connect(do_label)
        return w, render

    # ---------------------------------------------------------------- tab: factor scan
    def _tab_scan(self):
        w = W.QSplitter(Qt.Orientation.Vertical)
        plot = Plot(4)
        tab = W.QTableWidget()
        info = W.QLabel(
            "Every exogenous factor tested with the same model: metric ~ factor + ln(practice) + ln(position) "
            "+ condition + difficulty + tags (a control is dropped when it is the factor). ΔR² = variance explained "
            "beyond the controls; p from a session-clustered Wald test; q = Benjamini–Hochberg FDR across factors. "
            "Yellow = its smallest level spans <3 sessions (fragile).")
        info.setWordWrap(True)
        box = W.QWidget()
        bl = W.QVBoxLayout(box)
        bl.addWidget(info)
        bl.addWidget(tab)
        w.addWidget(plot)
        w.addWidget(box)
        w.setSizes([420, 450])

        def render():
            sc = A.factor_scan(self.d, self.m, self.st.tag_name)
            fig = plot.clear()
            if sc is None or not len(sc):
                tab.setRowCount(0)
                plot.draw()
                return
            s2 = sc.sort_values("dr2")
            ax = fig.subplots()
            ax.barh(s2["factor"], s2["dr2"], color=["C3" if q < .05 else "lightgray" for q in s2["q"]])
            ax.set_xlabel("ΔR² beyond controls (red: q<0.05)")
            ax.tick_params(labelsize=8)
            plot.draw()
            t = sc.rename(columns={"dr2": "ΔR²", "p": "p (clustered)", "kw_p": "p naive (KW)", "q": "q (FDR)",
                                   "strongest": "largest adjusted contrast"})
            t = t[["factor", "levels", "ΔR²", "p (clustered)", "q (FDR)", "p naive (KW)",
                   "largest adjusted contrast", "min_sessions", "min_overlap"]]
            fill_table(tab, t, dec=4, row_color=lambda r: WARN if r["min_sessions"] < 3 else None)
        return w, render

    # ---------------------------------------------------------------- tab: auto-detect
    def _tab_auto(self):
        w = W.QWidget()
        lay = W.QVBoxLayout(w)
        top = W.QHBoxLayout()
        top.addWidget(W.QLabel("Change-point sensitivity"))
        self.sens = W.QDoubleSpinBox()
        self.sens.setRange(.2, 5)
        self.sens.setSingleStep(.2)
        self.sens.setValue(1.0)
        top.addWidget(self.sens)
        top.addWidget(W.QLabel("   Clusters k (0 = choose by BIC)"))
        self.kfix = W.QSpinBox()
        self.kfix.setRange(0, 8)
        top.addWidget(self.kfix)
        b = W.QPushButton("Recompute")
        top.addWidget(b)
        top.addStretch()
        lay.addLayout(top)
        split = W.QSplitter(Qt.Orientation.Vertical)
        plot = Plot(6)
        split.addWidget(plot)
        sub = W.QTabWidget()
        tables = {}
        for name in ("Regimes", "Clusters", "Anomalous sessions"):
            pw = W.QWidget()
            pl = W.QVBoxLayout(pw)
            t = W.QTableWidget()
            t.setSelectionBehavior(W.QAbstractItemView.SelectionBehavior.SelectRows)
            pl.addWidget(t)
            bl = W.QPushButton(f"Label tests in selected {name.lower()}…")
            pl.addWidget(bl)
            tables[name] = (t, bl)
            sub.addTab(pw, name)
        note = W.QLabel()
        note.setWordWrap(True)
        split.addWidget(sub)
        split.setSizes([520, 330])
        lay.addWidget(note)
        lay.addWidget(split)

        def render():
            d, m = self.d, self.m
            au = self.auto()
            st, info, cl, an = au["st"], au["info"], au["cl"], au["anom"]
            fig = plot.clear()
            gs = fig.add_gridspec(2, 2)
            a = fig.add_subplot(gs[0, :])
            a.plot(st["start"], st["med"], "k.", ms=5, label="session median (≥3 tests)")
            for rg, g in st.groupby("regime"):
                a.hlines(g["med"].median(), g["start"].min(), g["end"].max(), color=PALETTE[rg % 10], lw=3)
                a.text(g["start"].min(), g["med"].median(), f" R{rg}", color=PALETTE[rg % 10], va="bottom",
                       fontsize=9, fontweight="bold")
            ymin = st["med"].min() if len(st) else 0
            for i, c in enumerate(info):
                a.axvline(c["at"], color="crimson", ls="--", lw=1)
                a.text(c["at"], ymin, f"CP{i + 1}" + ("" if c["changes"] else "?"), rotation=90, fontsize=8,
                       color="crimson", va="bottom", ha="right")
            a.set_title(f"Regimes of session-median {A.METRICS[m]} (binary segmentation, BIC penalty; "
                        "CP? = no recorded change explains it)", fontsize=9)
            a.tick_params(labelsize=8)
            a.legend(fontsize=8)
            if cl:
                a2, a3 = fig.add_subplot(gs[1, 0]), fig.add_subplot(gs[1, 1])
                lab = cl["labels"]
                dd = d.loc[lab.index]
                for c in range(cl["k"]):
                    s = dd[lab == c]
                    a2.scatter(s["wpm"], s["consistency"], s=5, alpha=.5, color=PALETTE[c], label=f"C{c}")
                    a3.scatter(s["t"], s["wpm"], s=5, alpha=.5, color=PALETTE[c])
                a2.set_xlabel("wpm")
                a2.set_ylabel("consistency")
                a2.legend(fontsize=7, markerscale=2)
                a2.set_title(f"GMM clusters (k={cl['k']})", fontsize=9)
                a3.set_title("Clusters over time", fontsize=9)
                for x in (a2, a3):
                    x.tick_params(labelsize=8)
            plot.draw()

            # regimes table
            rt = []
            starts = {c["at"]: c for c in info}
            for rg, g in st.groupby("regime"):
                ids = d.index[d["session"].isin(g.index)]
                c = starts.get(g["start"].min())
                rt.append(dict(regime=f"R{rg}", start=g["start"].min(), end=g["end"].max(), sessions=len(g),
                               tests=len(ids), median=d.loc[ids, m].median(),
                               shift=(c["after"] - c["before"]) if c else np.nan,
                               **{"what changed at start": "; ".join(c["changes"]) if c else ""},
                               tags=d.loc[ids, "tag_combo"].value_counts().head(2).to_dict()))
            fill_table(tables["Regimes"][0], pd.DataFrame(rt))
            if cl:
                ct = cl["table"].copy()
                ct["cluster"] = "C" + ct["cluster"].astype(str)
                fill_table(tables["Clusters"][0], ct[["cluster", "n", "share", "wpm", "acc", "consistency",
                                                      "testDuration", "descriptors", "first", "last"]])
                note.setText(f"BIC by k: {', '.join(f'{k}:{v:.0f}' for k, v in cl['bics'].items())}. "
                             "Clusters describe typical 'kinds of runs'; descriptors = metadata over-represented "
                             "in a cluster (share, lift; Fisher test q<0.05). No descriptor = something unrecorded.")
            else:
                tables["Clusters"][0].setRowCount(0)
            if len(an):
                at = an.reset_index()[["session", "start", "end", "n", "med", "resid", "z", "tags"]]
                at = at.rename(columns={"resid": "residual (unexplained Δ)", "med": "median"})
                fill_table(tables["Anomalous sessions"][0], at)
            else:
                tables["Anomalous sessions"][0].setRowCount(0)

        def label_from(name):
            t = tables[name][0]
            rows = sorted({i.row() for i in t.selectedIndexes()})
            if not rows:
                return
            key = [t.item(r, 0).text() for r in rows]
            au, d = self.auto(), self.d
            if name == "Regimes":
                regs = {int(k[1:]) for k in key}
                ids = d.index[au["regime"].isin(regs).fillna(False)]
            elif name == "Clusters":
                cs = {int(k[1:]) for k in key}
                lab = au["cl"]["labels"]
                ids = lab.index[lab.isin(cs)]
            else:
                ids = d.index[d["session"].isin([int(k) for k in key])]
            self.label_ids(ids, ", ".join(key))

        for name, (_, bl) in tables.items():
            bl.clicked.connect(lambda _, n=name: label_from(n))

        def recompute():
            self._auto = None
            self.dirty = set(self.pages)
            self._render_current()
        b.clicked.connect(recompute)
        return w, render

    # ---------------------------------------------------------------- tab: dynamics
    def _tab_dynamics(self):
        w = W.QSplitter(Qt.Orientation.Vertical)
        txt = W.QTextBrowser()
        plot = Plot(5)
        w.addWidget(txt)
        w.addWidget(plot)
        w.setSizes([140, 700])

        def render():
            r = A.warmup(self.d, self.m)
            fig = plot.clear()
            if r is None:
                txt.setHtml("Not enough sessions with ≥5 tests.")
                plot.draw()
                return
            ax = fig.subplots(1, 3)
            p = r["profile"]
            ax[0].errorbar(p["pos"], p["mean"], yerr=[p["mean"] - p["lo"], p["hi"] - p["mean"]], fmt="o-", capsize=3)
            ax[0].axhline(0, color="k", lw=.8)
            ax[0].set_xlabel("test # in session")
            ax[0].set_ylabel(f"Δ vs session median ({A.METRICS[self.m]})")
            ax[0].set_title("Warm-up / fatigue (mean ± 95% CI)", fontsize=9)
            g = r["gaps"]
            ax[1].bar(g["gap"], g["mean"], yerr=[g["mean"] - g["lo"], g["hi"] - g["mean"]], capsize=4, color="C1")
            ax[1].axhline(0, color="k", lw=.8)
            ax[1].set_title("Pause before the test", fontsize=9)
            for i, n in enumerate(g["n"]):
                ax[1].text(i, 0, f"n={n}", ha="center", va="bottom", fontsize=7)
            x, y = r["tmin"]
            ax[2].plot(x, y, color="C2")
            ax[2].axhline(0, color="k", lw=.8)
            ax[2].set_xlabel("minutes into session")
            ax[2].set_title("Within-session drift (LOWESS)", fontsize=9)
            plot.draw()
            p1 = p.iloc[0]
            txt.setHtml(html_table([
                ("Sessions used", f"{r['n_sessions']} (≥5 tests); deviations from each session's own median remove "
                                  "day-to-day differences"),
                ("First test of session", f"{p1['mean']:+.2f} [{p1['lo']:+.2f}, {p1['hi']:+.2f}] vs session median"),
                ("Warm-up slope", f"{r['slope']:+.2f} per e-fold of position [{r['slope_lo']:+.2f}, "
                                  f"{r['slope_hi']:+.2f}], {pstar(r['p'])} (clustered SE)")]))
        return w, render

    # ---------------------------------------------------------------- tab: relationships
    def _tab_rel(self):
        w = W.QSplitter(Qt.Orientation.Vertical)
        txt = W.QTextBrowser()
        plot = Plot(7)
        w.addWidget(txt)
        w.addWidget(plot)
        w.setSizes([110, 750])

        def render():
            d = self.d
            fig = plot.clear()
            ax = fig.subplots(2, 2)
            c = A.corr_matrix(d)
            im = ax[0, 0].imshow(c.values, cmap="RdBu_r", vmin=-1, vmax=1)
            ax[0, 0].set_xticks(range(len(c)), c.columns, rotation=90, fontsize=7)
            ax[0, 0].set_yticks(range(len(c)), c.index, fontsize=7)
            for i in range(len(c)):
                for j in range(len(c)):
                    if not np.isnan(c.values[i, j]):
                        ax[0, 0].text(j, i, f"{c.values[i, j]:.1f}", ha="center", va="center", fontsize=5)
            fig.colorbar(im, ax=ax[0, 0], shrink=.8)
            ax[0, 0].set_title("Spearman correlations", fontsize=9)
            sa = A.speed_accuracy(d)
            if sa:
                ax[0, 1].scatter(sa["within_df"]["rawWpm"], sa["within_df"]["acc"], s=4, alpha=.3, label="within session")
                ax[0, 1].set_xlabel("rawWpm − session mean")
                ax[0, 1].set_ylabel("acc − session mean")
                ax[0, 1].set_title(f"Speed–accuracy within sessions: ρ={sa['within'][0]:.2f}", fontsize=9)
                ax[1, 0].scatter(sa["between_df"]["rawWpm"], sa["between_df"]["acc"], s=12, color="C1")
                ax[1, 0].set_xlabel("session mean rawWpm")
                ax[1, 0].set_ylabel("session mean acc")
                ax[1, 0].set_title(f"Between sessions: ρ={sa['between'][0]:.2f}", fontsize=9)
                txt.setHtml(
                    f"Speed–accuracy uses <b>rawWpm</b> (wpm only counts correct characters, which would "
                    f"create an artificial correlation). Overall ρ={sa['total'][0]:.2f} ({pstar(sa['total'][1])}); "
                    f"within sessions ρ={sa['within'][0]:.2f} ({pstar(sa['within'][1])}); between sessions "
                    f"ρ={sa['between'][0]:.2f} ({pstar(sa['between'][1])}). A negative within-session ρ means "
                    "pushing speed costs accuracy; a positive between-session ρ means good days are good in both "
                    "(different signs = Simpson's paradox).")
            mo = d.groupby("month")[["c_incorrect", "c_extra", "c_missed"]].sum()
            tot = d.groupby("month")["chars"].sum()
            (mo.div(tot, axis=0) * 100).plot.bar(stacked=True, ax=ax[1, 1], color=["C3", "C4", "C7"])
            ax[1, 1].set_ylabel("% of characters")
            ax[1, 1].set_title("Error composition by month", fontsize=9)
            for a in ax.flat:
                a.tick_params(labelsize=8)
            plot.draw()
        return w, render

    # ---------------------------------------------------------------- tab: tags & labels
    def _tab_tags(self):
        w = W.QSplitter(Qt.Orientation.Horizontal)
        left = W.QWidget()
        ll = W.QVBoxLayout(left)
        ll.addWidget(W.QLabel("<b>Tags</b> — double-click the name column to name a tag (e.g. keyboard, layout)."))
        ttab = W.QTableWidget()
        ll.addWidget(ttab)
        bsave = W.QPushButton("Save tag names")
        ll.addWidget(bsave)
        right = W.QWidget()
        rl = W.QVBoxLayout(right)
        rl.addWidget(W.QLabel("<b>Labels</b> — your named groups (usable as filter, group and factor)."))
        ltab = W.QTableWidget()
        ltab.setSelectionBehavior(W.QAbstractItemView.SelectionBehavior.SelectRows)
        rl.addWidget(ltab)
        row = W.QHBoxLayout()
        bdel = W.QPushButton("Delete selected")
        bren = W.QPushButton("Rename selected")
        row.addWidget(bdel)
        row.addWidget(bren)
        rl.addLayout(row)
        box = W.QGroupBox("Create label")
        bl = W.QFormLayout(box)
        df_, dt_ = W.QDateTimeEdit(), W.QDateTimeEdit()
        for x in (df_, dt_):
            x.setCalendarPopup(True)
            x.setDisplayFormat("yyyy-MM-dd HH:mm")
        bl.addRow("From", df_)
        bl.addRow("To", dt_)
        brange = W.QPushButton("Label tests in this time range (all tests, ignoring filters)")
        bl.addRow(brange)
        bfilt = W.QPushButton("Label all currently filtered tests…")
        bl.addRow(bfilt)
        rl.addWidget(box)
        w.addWidget(left)
        w.addWidget(right)
        w.setSizes([800, 600])

        def render():
            df = self.df
            rows = []
            tids = A.all_tag_ids(df)
            has = {t: df["tag_key"].str.contains(t, regex=False) for t in tids}
            for t in tids:
                sub = df[has[t]]
                co = [self.st.tag_name(o) for o in tids if o != t and (has[o][has[t]]).all()]
                rows.append({"tag id": t, "name": self.st.tag_names.get(t, ""), "tests": len(sub),
                             "sessions": sub["session"].nunique(), "first": sub["t"].min(), "last": sub["t"].max(),
                             "conditions": ", ".join(sub["cond"].value_counts().index[:3]),
                             "median wpm": sub["wpm"].median(), "always together with": ", ".join(co)})
            fill_table(ttab, pd.DataFrame(rows), editable=("name",))
            lr = [{"label": k, "tests": len(v),
                   "first": df.loc[df.index.intersection(v), "t"].min(),
                   "last": df.loc[df.index.intersection(v), "t"].max()} for k, v in sorted(self.st.labels.items())]
            fill_table(ltab, pd.DataFrame(lr, columns=["label", "tests", "first", "last"]))
            mn, mx = df["t"].min(), df["t"].max()
            df_.setDateTime(QtCore.QDateTime(mn.to_pydatetime()))
            dt_.setDateTime(QtCore.QDateTime(mx.to_pydatetime()))

        def save_names():
            for r in range(ttab.rowCount()):
                tid, name = ttab.item(r, 0).text(), ttab.item(r, 1).text().strip()
                if name:
                    self.st.tag_names[tid] = name
                else:
                    self.st.tag_names.pop(tid, None)
            self.state_changed()

        def sel_labels():
            return [ltab.item(r, 0).text() for r in sorted({i.row() for i in ltab.selectedIndexes()})]

        def delete():
            for k in sel_labels():
                self.st.labels.pop(k, None)
            self.state_changed()

        def rename():
            for k in sel_labels():
                new, ok = W.QInputDialog.getText(self, "Rename label", f"New name for '{k}':", text=k)
                if ok and new.strip() and new.strip() != k:
                    self.st.labels[new.strip()] = sorted(set(self.st.labels.get(new.strip(), []))
                                                         | set(self.st.labels.pop(k)))
            self.state_changed()

        def by_range():
            a = pd.Timestamp(df_.dateTime().toPyDateTime())
            b = pd.Timestamp(dt_.dateTime().toPyDateTime())
            self.label_ids(self.df.index[(self.df["t"] >= a) & (self.df["t"] <= b)])

        bsave.clicked.connect(save_names)
        bdel.clicked.connect(delete)
        bren.clicked.connect(rename)
        brange.clicked.connect(by_range)
        bfilt.clicked.connect(lambda: self.label_ids(self.d.index))
        return w, render

    # ---------------------------------------------------------------- tab: data
    def _tab_data(self):
        w = W.QWidget()
        lay = W.QVBoxLayout(w)
        b = W.QPushButton("Export filtered data to CSV…")
        lay.addWidget(b)
        tab = W.QTableWidget()
        lay.addWidget(tab)
        cols = ["t", "wpm", "rawWpm", "acc", "consistency", "err_rate", "correction_cost", "testDuration",
                "restartCount", "incompleteTestSeconds", "cond", "difficulty", "tag_combo", "label", "session",
                "pos", "practice", "isPb", "files"]

        def render():
            fill_table(tab, self.d[cols].reset_index())

        def export():
            p, _ = W.QFileDialog.getSaveFileName(self, "Export", os.path.join(HERE, "filtered.csv"), "CSV (*.csv)")
            if p:
                self.d.drop(columns=["files", "labels"]).to_csv(p)
        b.clicked.connect(export)
        return w, render

    def _tab_methods(self):
        t = W.QTextBrowser()
        path = os.path.join(HERE, "METHODS.md")
        t.setMarkdown(open(path).read() if os.path.exists(path) else "METHODS.md not found")
        return t, lambda: None


def QtWidgets_busy(fn):
    W.QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        fn()
    except Exception as e:  # keep the app alive, show what failed
        import traceback
        traceback.print_exc()
        W.QMessageBox.warning(None, "Analysis error", f"{type(e).__name__}: {e}")
    finally:
        W.QApplication.restoreOverrideCursor()


def main():
    data_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "data")
    app = W.QApplication(sys.argv)
    win = App(data_dir)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
