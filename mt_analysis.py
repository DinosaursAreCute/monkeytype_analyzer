"""Pure analysis layer for Monkeytype result exports (no GUI). See METHODS.md."""
import glob
import json
import os
import warnings

import numpy as np
import pandas as pd
import statsmodels.formula.api as smf
from scipy import stats
from sklearn.mixture import GaussianMixture
from sklearn.preprocessing import StandardScaler
from statsmodels.nonparametric.smoothers_lowess import lowess
from statsmodels.stats.multitest import multipletests

warnings.filterwarnings("ignore")

SESSION_GAP_MIN = 30
MIN_LEVEL_N = 5
METRICS = {
    "wpm": "WPM",
    "rawWpm": "Raw WPM",
    "acc": "Accuracy %",
    "consistency": "Consistency %",
    "err_rate": "Char error rate %",
    "correction_cost": "Raw − WPM",
}
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _local_tz():
    try:
        return os.path.realpath("/etc/localtime").split("zoneinfo/")[1]
    except Exception:
        return "UTC"


# --------------------------------------------------------------------------- state
class State:
    """User-provided meaning: tag names and labels (label name -> list of test ids)."""

    def __init__(self, path):
        self.path = path
        self.tag_names, self.labels = {}, {}
        if os.path.exists(path):
            with open(path) as f:
                d = json.load(f)
            self.tag_names = d.get("tag_names", {})
            self.labels = d.get("labels", {})

    def save(self):
        with open(self.path, "w") as f:
            json.dump({"tag_names": self.tag_names, "labels": self.labels}, f, indent=1)

    def tag_name(self, tid):
        return self.tag_names.get(tid) or tid[-6:]

    def combo_name(self, key):
        if not key:
            return "(no tags)"
        return " + ".join(sorted(self.tag_name(t) for t in key.split(";")))


# --------------------------------------------------------------------------- loading
def load(data_dir):
    frames = []
    for f in sorted(glob.glob(os.path.join(data_dir, "*.csv"))):
        d = pd.read_csv(f, dtype=str, keep_default_na=False)
        d["file"] = os.path.basename(f)
        frames.append(d)
    if not frames:
        raise FileNotFoundError(f"no CSV files in {data_dir}")
    raw = pd.concat(frames, ignore_index=True)
    files = raw.groupby("_id")["file"].agg(lambda s: tuple(sorted(set(s))))
    df = raw.drop_duplicates("_id").set_index("_id").drop(columns="file")
    df["files"] = files

    for c in ["wpm", "acc", "rawWpm", "consistency", "restartCount", "testDuration",
              "afkDuration", "incompleteTestSeconds", "timestamp"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    for c in ["isPb", "punctuation", "numbers", "lazyMode", "blindMode", "bailedOut"]:
        df[c] = df[c].str.lower().eq("true")

    cs = df["charStats"].str.split(";", expand=True).reindex(columns=range(4)).apply(
        pd.to_numeric, errors="coerce").fillna(0)
    df[["c_correct", "c_incorrect", "c_extra", "c_missed"]] = cs.values
    tot = cs.sum(axis=1).replace(0, np.nan)
    df["chars"] = tot
    df["err_rate"] = 100 * (cs[1] + cs[2] + cs[3]) / tot
    df["correction_cost"] = df["rawWpm"] - df["wpm"]

    df["t"] = (pd.to_datetime(df["timestamp"], unit="ms", utc=True)
               .dt.tz_convert(_local_tz()).dt.tz_localize(None))
    df = df.sort_values("t").copy()
    df["date"] = df["t"].dt.normalize()
    df["hour"] = df["t"].dt.hour
    df["month"] = df["t"].dt.strftime("%Y-%m")
    df["weekday"] = pd.Categorical(df["t"].dt.dayofweek.map(dict(enumerate(WEEKDAYS))),
                                   WEEKDAYS, ordered=True)

    # practice & sessions are computed on the FULL dataset (exposure is independent of filters)
    df["practice"] = np.arange(1, len(df) + 1)
    gap = df["t"].diff().dt.total_seconds()
    new = gap.isna() | (gap > SESSION_GAP_MIN * 60)
    df["session"] = new.cumsum().astype(int)
    df["gap_before"] = gap.where(~new)
    df["pos"] = df.groupby("session").cumcount() + 1
    df["session_size"] = df.groupby("session")["pos"].transform("max")
    df["min_into_session"] = (df["t"] - df.groupby("session")["t"].transform("min")).dt.total_seconds() / 60
    df["lp"] = np.log(df["practice"])
    df["lpos"] = np.log(df["pos"])

    df["cond"] = (df["mode"] + " " + df["mode2"]
                  + np.where(df["language"] != "english", " " + df["language"], "")
                  + np.where(df["funbox"] != "", " [" + df["funbox"] + "]", "")
                  + np.where(df["punctuation"], " +punct", "")
                  + np.where(df["numbers"], " +num", ""))
    df["tag_key"] = df["tags"].map(lambda s: ";".join(sorted(x for x in s.split(";") if x)))

    # binned exogenous factors
    df["hour_bin"] = df["hour"].map(lambda h: f"{h // 3 * 3:02d}-{h // 3 * 3 + 3:02d}h")
    df["pos_bin"] = pd.cut(df["pos"], [0, 1, 3, 7, 15, 10 ** 6],
                           labels=["1st", "2-3", "4-7", "8-15", "16+"]).astype(str)
    g = df["gap_before"]
    df["gap_bin"] = np.select([g.isna(), g < 30, g < 120, g < 600],
                              ["session start", "<30s", "30s-2m", "2-10m"], "10-30m")
    df["restart_bin"] = pd.cut(df["restartCount"], [-1, 0, 2, 5, 10, 10 ** 6],
                               labels=["0", "1-2", "3-5", "6-10", "11+"]).astype(str)
    return df


def apply_state(df, st):
    """Attach user-facing tag names and labels. Cheap; call after every state change."""
    df = df.copy()
    df["tag_combo"] = df["tag_key"].map(st.combo_name)
    lab = {i: [] for i in df.index}
    for name, ids in st.labels.items():
        for i in ids:
            if i in lab:
                lab[i].append(name)
    df["labels"] = pd.Series({i: tuple(sorted(v)) for i, v in lab.items()})
    df["label"] = df["labels"].map(lambda v: " + ".join(v) if v else "(none)")
    return df


def all_tag_ids(df):
    return sorted({t for k in df["tag_key"] for t in k.split(";") if t})


def filter_df(df, spec):
    m = pd.Series(True, index=df.index)
    if spec.get("files") is not None:
        fs = set(spec["files"])
        m &= df["files"].map(lambda t: bool(fs.intersection(t)))
    for col in ["cond", "difficulty", "tag_combo", "label"]:
        if spec.get(col) is not None:
            m &= df[col].isin(spec[col])
    if spec.get("date_from") is not None:
        m &= df["date"] >= pd.Timestamp(spec["date_from"])
    if spec.get("date_to") is not None:
        m &= df["date"] <= pd.Timestamp(spec["date_to"])
    if spec.get("exclude_afk"):
        m &= df["afkDuration"] <= 0
    d = df[m]
    if spec.get("exclude_outliers") and len(d):
        metric = spec.get("metric", "wpm")
        z = d.groupby("cond")[metric].transform(robust_z)
        d = d[z.abs().fillna(0) <= 4]
    return d


# --------------------------------------------------------------------------- helpers
def robust_z(x):
    x = pd.Series(x, dtype=float)
    mad = stats.median_abs_deviation(x, nan_policy="omit", scale="normal")
    if not mad or np.isnan(mad):
        return x * 0
    return (x - x.median()) / mad


def cliffs_delta(a, b):
    a, b = np.asarray(a), np.asarray(b)
    if len(a) == 0 or len(b) == 0:
        return np.nan
    u = stats.mannwhitneyu(a, b).statistic
    return 2 * u / (len(a) * len(b)) - 1


def mean_ci(x):
    x = np.asarray(x, float)
    x = x[~np.isnan(x)]
    if len(x) < 2:
        return (np.nan, np.nan, np.nan)
    m, se = x.mean(), x.std(ddof=1) / np.sqrt(len(x))
    h = stats.t.ppf(0.975, len(x) - 1) * se
    return m, m - h, m + h


def _fit(formula, data):
    """OLS with session-clustered SEs (HC3 fallback when too few sessions)."""
    model = smf.ols(formula, data)
    ng = data["session"].nunique()
    if ng >= 5:
        return model.fit(cov_type="cluster", cov_kwds={"groups": pd.factorize(data["session"])[0]})
    return model.fit(cov_type="HC3")


def _controls(d, exclude=()):
    terms = []
    if "lp" not in exclude and d["lp"].nunique() > 1:
        terms.append("lp")
    if "lpos" not in exclude and d["lpos"].nunique() > 1:
        terms.append("lpos")
    for c in ("cond", "difficulty", "tag_combo"):  # known setup variables
        if c in d and c not in exclude and d[c].nunique() > 1:
            terms.append(f"C({c})")
    return terms


def _lump(s, min_n=MIN_LEVEL_N):
    vc = s.value_counts()
    small = vc[vc < min_n].index
    return s.where(~s.isin(small), "other") if len(small) else s


# --------------------------------------------------------------------------- 1. overview
def icc1(y, groups):
    d = pd.DataFrame({"y": y, "g": groups}).dropna()
    gs = d.groupby("g")["y"]
    a, N = gs.ngroups, len(d)
    if a < 2 or N - a < 1:
        return np.nan, np.nan, np.nan
    n_i, m_i, M = gs.size(), gs.mean(), d["y"].mean()
    msb = (n_i * (m_i - M) ** 2).sum() / (a - 1)
    msw = ((d["y"] - d["g"].map(m_i)) ** 2).sum() / (N - a)
    k0 = (N - (n_i ** 2).sum() / N) / (a - 1)
    vb = max((msb - msw) / k0, 0)
    return vb / (vb + msw), np.sqrt(vb), np.sqrt(msw)


def learning_trend(d, m):
    d = d[[m, "lp", "lpos", "cond", "difficulty", "tag_combo", "session"]].dropna()
    if len(d) < 10 or d["lp"].nunique() < 3:
        return None
    terms = ["lp"] + [t for t in _controls(d) if t != "lp"]
    f = _fit(f"{m} ~ " + " + ".join(terms), d)
    b, (lo, hi) = f.params["lp"], f.conf_int().loc["lp"]
    return dict(per_doubling=b * np.log(2), lo=lo * np.log(2), hi=hi * np.log(2), p=f.pvalues["lp"],
                a=f.params["Intercept"], b=b, n_sessions=d["session"].nunique())


def summary(d, m):
    y = d[m].dropna()
    out = dict(n=len(y), sessions=d["session"].nunique(), days=d["date"].nunique(),
               first=d["t"].min(), last=d["t"].max())
    if len(y) == 0:
        return out
    out.update(median=y.median(), mean=y.mean(), sd=y.std(), p90=y.quantile(.9), max=y.max(),
               min=y.min(), n_pb=int(d["isPb"].sum()))
    out["icc"], out["sd_between"], out["sd_within"] = icc1(d[m], d["session"])
    out["trend"] = learning_trend(d, m)
    out["conds"] = d["cond"].value_counts().to_dict()
    out["restarts_per_saved"] = d["restartCount"].mean()
    out["abandoned_s_per_saved"] = d["incompleteTestSeconds"].mean()
    recent = d[d["t"] >= d["t"].max() - pd.Timedelta(days=14)][m]
    before = d[d["t"] < d["t"].max() - pd.Timedelta(days=14)][m]
    out["recent_median"], out["before_median"] = recent.median(), before.median()
    return out


def smooth(x, y, frac=0.3):
    ok = ~(np.isnan(x) | np.isnan(y))
    if ok.sum() < 10:
        return np.array([]), np.array([])
    r = lowess(y[ok], x[ok], frac=frac, return_sorted=True)
    return r[:, 0], r[:, 1]


# --------------------------------------------------------------------------- 2. group comparison
FACTORS = {  # label -> column (exogenous factors only)
    "Tag combo": "tag_combo",
    "Label": "label",
    "Difficulty": "difficulty",
    "Condition": "cond",
    "Funbox": "funbox",
    "Hour of day": "hour_bin",
    "Weekday": "weekday",
    "Month": "month",
    "Position in session": "pos_bin",
    "Gap before test": "gap_bin",
    "Restarts before test": "restart_bin",
}
FACTOR_EXCLUDES = {"cond": ("cond",), "difficulty": ("difficulty",), "pos_bin": ("lpos",),
                   "gap_bin": ("lpos",), "month": ("lp",), "funbox": ("cond",),
                   "tag_combo": ("tag_combo",), "label": ("tag_combo",)}
TAGLIKE = ("tag_combo",)  # exclusion used for single-tag / label factors


def compare_groups(d, m, col, exclude=None):
    """Descriptive + adjusted comparison of groups defined by column `col`."""
    exclude = FACTOR_EXCLUDES.get(col, ()) if exclude is None else exclude
    cols = list(dict.fromkeys([m, col, "lp", "lpos", "cond", "difficulty", "tag_combo", "session", "practice"]))
    dd = d[cols].dropna(subset=[m]).copy()
    dd["g"] = dd[col].astype(str)
    if dd["g"].nunique() < 2:
        return None
    order = dd["g"].value_counts().index.tolist()  # reference = largest group
    ref = order[0]
    dd["g"] = pd.Categorical(dd["g"], order)
    big = [l for l in order if (dd["g"] == l).sum() >= MIN_LEVEL_N]
    md = dd[dd["g"].isin(big)].copy()
    md["g"] = pd.Categorical(md["g"].astype(str), big)
    ctrl = _controls(md, exclude)
    res = dict(ref=ref, controls=ctrl, rows=[])
    try:
        if len(big) < 2:
            raise ValueError("fewer than two groups with n >= %d" % MIN_LEVEL_N)
        full = _fit(f"{m} ~ C(g)" + "".join(" + " + t for t in ctrl), md)
        base = _fit(f"{m} ~ " + (" + ".join(ctrl) if ctrl else "1"), md)
        names = [p for p in full.params.index if p.startswith("C(g)")]
        R = np.zeros((len(names), len(full.params)))
        for i, p in enumerate(names):
            R[i, list(full.params.index).index(p)] = 1
        ft = full.f_test(R)
        res.update(f_p=float(np.squeeze(ft.pvalue)), dr2=full.rsquared - base.rsquared,
                   r2=full.rsquared, n_clusters=md["session"].nunique())
        ci = full.conf_int()
    except Exception as e:  # singular design etc.
        full, res["error"] = None, str(e)
    samples = [g[m].values for _, g in dd.groupby("g", observed=True)]
    res["kw_p"] = stats.kruskal(*samples).pvalue if len(samples) > 1 and all(len(s) > 0 for s in samples) else np.nan
    r = dd[dd["g"] == ref]
    rlo, rhi = r["practice"].min(), r["practice"].max()
    for lvl in order:
        g = dd[dd["g"] == lvl]
        row = dict(group=lvl, n=len(g), sessions=g["session"].nunique(), median=g[m].median(),
                   mean=g[m].mean(), sd=g[m].std(), first=d.loc[g.index, "t"].min(),
                   last=d.loc[g.index, "t"].max())
        if lvl == ref:
            row.update(delta=0.0, cliff=0.0, adj=0.0, adj_lo=np.nan, adj_hi=np.nan, adj_p=np.nan, overlap=1.0)
        else:
            row["delta"] = g[m].median() - r[m].median()
            row["cliff"] = cliffs_delta(g[m], r[m])
            key = f"C(g)[T.{lvl}]"
            if full is not None and key in full.params:
                row.update(adj=full.params[key], adj_lo=ci.loc[key, 0], adj_hi=ci.loc[key, 1],
                           adj_p=full.pvalues[key])
            else:
                row.update(adj=np.nan, adj_lo=np.nan, adj_hi=np.nan, adj_p=np.nan)
            glo, ghi = g["practice"].min(), g["practice"].max()
            row["overlap"] = (np.nan if "lp" in exclude else
                              max(0, min(ghi, rhi) - max(glo, rlo)) / max(ghi - glo, 1))
        res["rows"].append(row)
    return res


def factor_scan(d, m, tag_name=lambda t: t[-6:]):
    """Test every exogenous factor with the same adjusted model; BH-FDR across factors."""
    cands = dict(FACTORS)
    tag_cols = {}
    for tid in all_tag_ids(d):
        tag_cols[tid] = d["tag_key"].str.contains(tid, regex=False).map({True: "yes", False: "no"})
    lab_cols = {}
    for name in sorted({l for v in d["labels"] for l in v}):
        lab_cols[name] = d["labels"].map(lambda v: "yes" if name in v else "no")
    rows = []

    def run(label, series, excl):
        dd = d.copy()
        dd["_f"] = _lump(series.astype(str))
        if dd["_f"].nunique() < 2 or (dd["_f"].value_counts() >= MIN_LEVEL_N).sum() < 2:
            return
        r = compare_groups(dd, m, "_f", exclude=excl)
        if r is None or "f_p" not in r:
            return
        best = max(r["rows"][1:], key=lambda x: abs(x["adj"]) if not np.isnan(x["adj"]) else -1)
        modelled = [x for x in r["rows"] if x["n"] >= MIN_LEVEL_N]
        rows.append(dict(factor=label, levels=dd["_f"].nunique(), dr2=r["dr2"], p=r["f_p"], kw_p=r["kw_p"],
                         strongest=f"{best['group']} vs {r['ref']}: {best['adj']:+.2f}",
                         min_overlap=np.nanmin([x["overlap"] for x in modelled] + [np.inf]),
                         min_sessions=min(x["sessions"] for x in modelled)))

    for label, col in cands.items():
        run(label, d[col], FACTOR_EXCLUDES.get(col, ()))
    for tid, s in tag_cols.items():
        run(f"Tag: {tag_name(tid)}", s, TAGLIKE)
    for name, s in lab_cols.items():
        run(f"Label: {name}", s, TAGLIKE)
    out = pd.DataFrame(rows)
    if len(out):
        out["q"] = multipletests(out["p"].fillna(1), method="fdr_bh")[1]
        out = out.sort_values(["q", "dr2"], ascending=[True, False]).reset_index(drop=True)
    return out


# --------------------------------------------------------------------------- 3. auto-detection
def session_table(d, m, min_n=1):
    s = d.groupby("session").agg(start=("t", "min"), end=("t", "max"), n=(m, "size"),
                                 med=(m, "median"), mean=(m, "mean"))
    return s[s["n"] >= min_n]


def binseg(y, pen, minsize=3):
    y = np.asarray(y, float)
    cs, cs2 = np.concatenate([[0], np.cumsum(y)]), np.concatenate([[0], np.cumsum(y ** 2)])

    def cost(a, b):
        n = b - a
        return cs2[b] - cs2[a] - (cs[b] - cs[a]) ** 2 / n if n > 0 else 0

    cps, stack = [], [(0, len(y))]
    while stack:
        a, b = stack.pop()
        if b - a < 2 * minsize:
            continue
        c0 = cost(a, b)
        gains = [(c0 - cost(a, k) - cost(k, b), k) for k in range(a + minsize, b - minsize + 1)]
        g, k = max(gains)
        if g > pen:
            cps.append(k)
            stack += [(a, k), (k, b)]
    return sorted(cps)


def _mode(s):
    s = s.dropna()
    return s.mode().iloc[0] if len(s) else None


def change_points(d, m, sensitivity=1.0, min_tests=3, minsize=3):
    st = session_table(d, m, min_tests)
    if len(st) < 2 * minsize:
        return st.assign(regime=0), []
    y = st["med"].values
    sigma = stats.median_abs_deviation(np.diff(y), scale="normal") / np.sqrt(2)
    pen = 2 * sigma ** 2 * np.log(len(y)) / max(sensitivity, 1e-3)
    cps = binseg(y, pen, minsize)
    st = st.copy()
    st["regime"] = np.searchsorted(cps, np.arange(len(st)), side="right")
    sess = st.index.values
    info = []
    for k in cps:
        before, after = sess[max(0, k - 2):k], sess[k:k + 2]
        db, da = d[d["session"].isin(before)], d[d["session"].isin(after)]
        changes = []
        for col, nm in [("tag_combo", "tags"), ("difficulty", "difficulty"), ("cond", "condition"),
                        ("label", "label"), ("funbox", "funbox")]:
            a, b = _mode(db[col]), _mode(da[col])
            if a != b:
                changes.append(f"{nm}: {a} → {b}")
        pause = (st.loc[sess[k], "start"] - st.loc[sess[k - 1], "end"]).total_seconds() / 86400
        if pause >= 3:
            changes.append(f"break of {pause:.0f} days")
        info.append(dict(at=st.loc[sess[k], "start"], before=st["med"].iloc[max(0, k - minsize):k].median(),
                         after=st["med"].iloc[k:k + minsize].median(), changes=changes))
    return st, info


def regimes_per_test(d, st):
    return d["session"].map(st["regime"]).astype("Int64")


CLUSTER_FEATS = ["wpm", "acc", "consistency", "testDuration"]
ENRICH_COLS = {"tag_combo": "tags", "difficulty": "difficulty", "cond": "cond", "hour_bin": "hour",
               "weekday": "weekday", "pos_bin": "pos", "month": "month", "label": "label",
               "restart_bin": "restarts"}


def clusters(d, kmax=6, seed=0, k_fixed=None):
    X = d[CLUSTER_FEATS].copy()
    X["testDuration"] = np.log(X["testDuration"].clip(lower=0.5))
    X = X.dropna()
    if len(X) < 30:
        return None
    Z = StandardScaler().fit_transform(X)
    fits = []
    # reg_covar: minimum variance 0.1 SD^2 prevents components collapsing onto the acc=100 point mass
    for k in ([k_fixed] if k_fixed else range(1, kmax + 1)):
        gm = GaussianMixture(k, covariance_type="full", n_init=3, reg_covar=0.1, random_state=seed).fit(Z)
        fits.append((gm.bic(Z), k, gm))
    bics = {k: b for b, k, _ in fits}
    _, k, gm = min(fits, key=lambda x: x[0])
    lab = pd.Series(gm.predict(Z), index=X.index)
    prob = pd.Series(gm.predict_proba(Z).max(axis=1), index=X.index)
    # relabel by descending size
    order = lab.value_counts().index
    lab = lab.map({o: i for i, o in enumerate(order)})
    rows, tests = [], []
    for c in range(k):
        idx = lab.index[lab == c]
        sub = d.loc[idx]
        desc = []
        for col, nm in ENRICH_COLS.items():
            for v, a in sub[col].astype(str).value_counts().items():
                nv = (d.loc[X.index, col].astype(str) == v).sum()
                b, c2 = len(sub) - a, nv - a
                dd = len(X) - len(sub) - c2
                p = stats.fisher_exact([[a, b], [c2, dd]], alternative="greater").pvalue
                lift = (a / len(sub)) / (nv / len(X))
                tests.append((c, f"{nm}={v}", a / len(sub), lift, p))
        rows.append(dict(cluster=c, n=len(sub), share=len(sub) / len(X),
                         **{f: sub[f].median() for f in CLUSTER_FEATS},
                         first=sub["t"].min(), last=sub["t"].max()))
    te = pd.DataFrame(tests, columns=["cluster", "value", "frac", "lift", "p"])
    te["q"] = multipletests(te["p"], method="fdr_bh")[1] if len(te) else []
    tab = pd.DataFrame(rows)
    tab["descriptors"] = [
        "; ".join(f"{r.value} ({r.frac:.0%}, ×{r.lift:.1f})" for r in
                  te[(te.cluster == c) & (te.q < 0.05) & (te.lift > 1.3)].sort_values("lift", ascending=False)
                  .head(4).itertuples()) or "no metadata over-represented → unexplained"
        for c in tab["cluster"]]
    return dict(k=k, bics=bics, labels=lab, prob=prob, table=tab)


def anomalous_sessions(d, m, zthr=2.5, min_n=3):
    dd = d[[m, "lp", "lpos", "cond", "difficulty", "tag_combo", "session"]].dropna().copy()
    if len(dd) < 20:
        return pd.DataFrame()
    terms = _controls(dd)
    f = smf.ols(f"{m} ~ " + (" + ".join(terms) if terms else "1"), dd).fit()
    dd["res"] = f.resid
    _, tau, sigma = icc1(dd["res"], dd["session"])
    s = dd.groupby("session").agg(n=("res", "size"), resid=("res", "mean"))
    s = s[s["n"] >= min_n]
    if len(s) < 5:
        return pd.DataFrame()
    # a session mean residual has variance tau^2 (true session effects) + sigma^2/n (test noise)
    s["z"] = (s["resid"] - s["resid"].median()) / np.sqrt(tau ** 2 + sigma ** 2 / s["n"])
    st = session_table(d, m)
    s = s.join(st[["start", "end", "med"]])
    s["tags"] = d.groupby("session")["tag_combo"].agg(_mode)
    return s[s["z"].abs() >= zthr].sort_values("z")


# --------------------------------------------------------------------------- 4. dynamics
def warmup(d, m, max_pos=20, min_session=5):
    dd = d[d["session_size"] >= min_session].copy()
    if len(dd) < 20:
        return None
    dd["dev"] = dd[m] - dd.groupby("session")[m].transform("median")
    prof = []
    for p in range(1, max_pos + 1):
        mm, lo, hi = mean_ci(dd.loc[dd["pos"] == p, "dev"])
        prof.append((p, mm, lo, hi, int((dd["pos"] == p).sum())))
    prof = pd.DataFrame(prof, columns=["pos", "mean", "lo", "hi", "n"])
    f = _fit("dev ~ lpos", dd.dropna(subset=["dev"]))
    gaps = []
    for gb in ["<30s", "30s-2m", "2-10m", "10-30m"]:
        mm, lo, hi = mean_ci(dd.loc[dd["gap_bin"] == gb, "dev"])
        gaps.append((gb, mm, lo, hi, int((dd["gap_bin"] == gb).sum())))
    tmin = dd[["min_into_session", "dev"]].dropna()
    tmin = tmin[tmin["min_into_session"] <= tmin["min_into_session"].quantile(.95)]  # avoid sparse tail
    return dict(profile=prof, slope=f.params["lpos"], slope_lo=f.conf_int().loc["lpos", 0],
                slope_hi=f.conf_int().loc["lpos", 1], p=f.pvalues["lpos"],
                gaps=pd.DataFrame(gaps, columns=["gap", "mean", "lo", "hi", "n"]),
                tmin=smooth(tmin["min_into_session"].values, tmin["dev"].values, 0.4),
                n_sessions=dd["session"].nunique())


def speed_accuracy(d):
    """Trade-off uses rawWpm: wpm counts only correct chars, so wpm~acc is partly mechanical."""
    dd = d[["rawWpm", "acc", "session"]].dropna()
    if len(dd) < 10:
        return None
    grp = dd.groupby("session")[["rawWpm", "acc"]]
    between = grp.mean()
    within = dd[["rawWpm", "acc"]] - grp.transform("mean")
    rb = stats.spearmanr(between["rawWpm"], between["acc"]) if len(between) > 3 else (np.nan, np.nan)
    rw = stats.spearmanr(within["rawWpm"], within["acc"])
    rt = stats.spearmanr(dd["rawWpm"], dd["acc"])
    return dict(total=tuple(rt), between=tuple(rb), within=tuple(rw), within_df=within, between_df=between)


CORR_COLS = ["wpm", "rawWpm", "acc", "consistency", "err_rate", "correction_cost", "testDuration",
             "restartCount", "incompleteTestSeconds", "pos", "gap_before", "practice", "hour"]


def corr_matrix(d):
    return d[CORR_COLS].corr(method="spearman")
