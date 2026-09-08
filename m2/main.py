"""
ERCOT DART 3-class procurement scorer — Render web service.
(v18: RTM 백필 수렴 — 최신일 우선 + 오래된 미수집분 순차 회수)

POST /score
  body: {
    "state": [{dt_local, lf_sys, stwpf, wgrpp, stppf, temp_fcst,
               outage_total, outage_houston, outage_irr}, ...],
    "weather":      {"time":[...], "temperature_2m":[...]},   # Open-Meteo forecast(past_days 포함)
    "weather_prev": {"time":[...], "temperature_2m_previous_day1":[...]},  # 선택: 과거일 D-1 예보
    "have_pred_days":   ["2026-07-30", ...],   # 선택: predictions 탭에 이미 있는 날짜
    "have_actual_days": ["2026-07-29", ...],   # (구) 일 단위 — 사용 비권장
    "have_actual_hours": ["2026-07-29 00:00:00", ...],  # 권장: actuals에 이미 있는 정확한 시각
    "max_backfill_days": 7                     # 선택 (기본 7, MIS 보관한도)
  }
returns: {predictions:[...], model_detail:[...], new_state:[...], settlements:[...], meta:{...}}

동작:
  - 내일치를 항상 처리하고, 최근 max_backfill_days 이내에서
    state가 비었거나 predictions가 없는 날짜를 자동으로 찾아 함께 백필한다.
  - state가 이미 있는 날은 MIS를 다시 받지 않고 state 값으로 피처를 만든다(판정만 백필).
  - state가 없는 날은 그날의 D-1 아침 발행분을 MIS 아카이브에서 찾아 복원한다(7일 보관 한도).

GET /health -> ok
"""
import os, io, json, zipfile, datetime as dt
import requests
import numpy as np
import pandas as pd
import lightgbm as lgb
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")
HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json")))
M3 = [lgb.Booster(model_file=os.path.join(HERE, "models", f"m3_{s}.txt")) for s in CFG["seeds"]]
MS = [lgb.Booster(model_file=os.path.join(HERE, "models", f"ms_{s}.txt")) for s in CFG["seeds"]]

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
MIS_LIST = "https://www.ercot.com/misapp/servlets/IceDocListJsonWS?reportTypeId={rid}"
MIS_DL = "https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId={docid}"
RID = {"load_fcst": 12312, "wind": 13028, "solar": 13483, "outage": 13103,
       "dam_spp_daily": 12331, "rtm_spp_daily": 12301}

STATE_COLS = ["lf_sys", "stwpf", "wgrpp", "stppf", "temp_fcst",
              "outage_total", "outage_houston", "outage_irr"]

VERSION = "v19"
app = FastAPI()


class ScoreReq(BaseModel):
    state: list
    weather: dict | None = None
    weather_prev: dict | None = None
    have_pred_days: list | None = None
    have_actual_days: list | None = None
    have_actual_hours: list | None = None
    max_backfill_days: int = 7
    rtm_days: int = 7
    max_rtm_docs: int = 260


# ---------------- MIS helpers ----------------
def mis_doc_list(rid, cache):
    if rid in cache:
        return cache[rid]
    j = requests.get(MIS_LIST.format(rid=rid), headers=UA, timeout=60).json()
    docs = [d["Document"] for d in j["ListDocsByRptTypeRes"]["DocumentList"]]
    cache[rid] = docs
    return docs


def doc_fmt(d):
    """MIS는 같은 리포트를 csv/xml 두 문서로 발행한다. 어느 쪽인지 판별."""
    s = " ".join(str(d.get(k, "")) for k in ("FriendlyName", "FileName", "Extension")).lower()
    if "xml" in s:
        return "xml"
    if "csv" in s:
        return "csv"
    return "?"


def pick_doc(docs, pub_day, lo=6, hi=11):
    """pub_day 발행분 중 **CSV 문서**만 골라 lo~hi시 창의 최신 것."""
    same = [d for d in docs if str(d.get("PublishDate", ""))[:10] == pub_day.isoformat()]
    if not same:
        return None
    csvs = [d for d in same if doc_fmt(d) == "csv"]
    if not csvs:
        csvs = [d for d in same if doc_fmt(d) != "xml"]   # 판별 불가면 xml만 배제
    pool = csvs or same
    win = [d for d in pool if lo <= int(str(d["PublishDate"])[11:13]) <= hi]
    pool = win or pool
    return sorted(pool, key=lambda d: d["PublishDate"])[-1]


def mis_read_csv(docid):
    r = requests.get(MIS_DL.format(docid=docid), headers=UA, timeout=120)
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = [n for n in z.namelist() if n.lower().endswith(".csv")]
    if not names:
        raise ValueError("zip에CSV없음:" + ",".join(z.namelist()[:3]))
    df = pd.read_csv(io.BytesIO(z.read(names[0])))
    df.columns = [str(c).strip() for c in df.columns]
    return df


def _c(df, *names):
    """컬럼명 대소문자·언더스코어·공백 차이를 흡수해서 실제 컬럼명을 찾는다."""
    norm = {str(c).strip().lower().replace("_", "").replace(" ", ""): c for c in df.columns}
    for n in names:
        k = str(n).strip().lower().replace("_", "").replace(" ", "")
        if k in norm:
            return norm[k]
    return None


def _parse_dates(s):
    """ERCOT은 MM/DD/YYYY 와 YYYY-MM-DD 를 모두 쓴다. 둘 다 받아준다."""
    for kw in ({"format": "%m/%d/%Y"}, {"format": "%Y-%m-%d"}, {}):
        try:
            d = pd.to_datetime(s, errors="coerce", **kw)
        except Exception:
            continue
        if d.notna().any():
            return d.dt.normalize()
    return pd.Series(pd.NaT, index=s.index)


def _norm_ts(x):
    """'2026-08-01 0:00:00' / ISO / 엑셀시리얼 등을 'YYYY-MM-DD HH:MM:SS'로 정규화."""
    try:
        if isinstance(x, (int, float)) and not isinstance(x, bool):
            return str((pd.Timestamp("1899-12-30") + pd.to_timedelta(float(x), unit="D")).round("s"))
        t = pd.to_datetime(str(x), errors="coerce")
        return str(t) if pd.notna(t) else str(x).strip()
    except Exception:
        return str(x).strip()


def _hours(s):
    """HourEnding: '01:00' / '1' / 1 / '24:00' 모두 → 0-base 시작시각."""
    v = s.astype(str).str.strip().str.split(":").str[0]
    v = pd.to_numeric(v, errors="coerce")
    return v - 1


def hours_of(day):
    return pd.date_range(pd.Timestamp(day), periods=24, freq="h")



def _safe(o):
    """reindex 전에 중복 라벨 제거(방어적)."""
    try:
        if o.index.has_duplicates:
            o = o[~o.index.duplicated(keep="last")]
    except Exception:
        pass
    return o

def dedup(s):
    return s[~s.index.duplicated(keep="last")].sort_index()


def _load_product(rid, target, cache, datecol, hourcol, dstcol, pub_cands, note):
    """target 딜리버리일 행을 담은 문서를 발행일 후보 순서로 찾아 반환. (df, pub) 또는 (None, 사유)."""
    tried = []
    for pub in pub_cands:
        d = pick_doc(mis_doc_list(rid, cache), pub)
        if d is None:
            tried.append(f"{pub}:없음")
            continue
        try:
            df = mis_read_csv(d["DocID"])
        except Exception as e:
            tried.append(f"{pub}:읽기실패({type(e).__name__}:{str(e)[:40]})")
            continue
        dc = _c(df, dstcol) if dstcol else None
        if dc:
            df = df[df[dc].astype(str).str.upper().str.startswith("N")]
        col = _c(df, datecol, "DeliveryDate", "DELIVERY_DATE", "Date", "OperDay", "OPR_DATE")
        if col is None:
            tried.append(f"{pub}:컬럼없음({'|'.join(map(str, df.columns[:6]))})")
            continue
        dts = _parse_dates(df[col])
        if dts.isna().all():
            samp = str(df[col].iloc[0])[:20] if len(df) else ""
            tried.append(f"{pub}:날짜파싱실패(col={col},예='{samp}')")
            continue
        sel = df[dts == pd.Timestamp(target)]
        if len(sel) >= 20:
            return sel, str(pub)
        rng = f"{dts.min().date()}~{dts.max().date()}" if dts.notna().any() else "?"
        tried.append(f"{pub}:행{len(sel)}(문서범위 {rng})")
    return None, f"{note}[{','.join(tried)}]"


def fetch_day_inputs(target, cache):
    """딜리버리일=target 의 발행 예측치를 MIS에서 복원.
    발행일 후보: D-1(정석) → D(당일 발행분) → D-2 → D+1 순으로 시도.
    성공 시 dict, 실패 시 ('사유 문자열') 반환."""
    pubs = [target - dt.timedelta(days=1), target,
            target - dt.timedelta(days=2), target + dt.timedelta(days=1)]
    out = {}
    used = {}

    sel, info = _load_product(RID["load_fcst"], target, cache, "DeliveryDate", None, "DSTFlag", pubs, "load_fcst")
    if sel is None:
        return info
    used["load_fcst"] = info
    i1 = pd.Timestamp(target) + pd.to_timedelta(_hours(sel[_c(sel, "HourEnding", "HOUR_ENDING")]).values, unit="h")
    lfc = _c(sel, "SystemTotal", "SYSTEM_TOTAL", "SystemWide")
    out["lf_sys"] = dedup(pd.Series(pd.to_numeric(sel[lfc], errors="coerce").values, index=i1))

    sel, info = _load_product(RID["wind"], target, cache, "DELIVERY_DATE", None, "DSTFlag", pubs, "wind")
    if sel is None:
        return info
    used["wind"] = info
    i2 = pd.Timestamp(target) + pd.to_timedelta(_hours(sel[_c(sel, "HOUR_ENDING", "HourEnding")]).values, unit="h")
    out["stwpf"] = dedup(pd.Series(pd.to_numeric(sel[_c(sel, "STWPF_SYSTEM_WIDE")], errors="coerce").values, index=i2))
    out["wgrpp"] = dedup(pd.Series(pd.to_numeric(sel[_c(sel, "WGRPP_SYSTEM_WIDE")], errors="coerce").values, index=i2))

    sel, info = _load_product(RID["solar"], target, cache, "DELIVERY_DATE", None, "DSTFlag", pubs, "solar")
    if sel is None:
        return info
    used["solar"] = info
    i3 = pd.Timestamp(target) + pd.to_timedelta(_hours(sel[_c(sel, "HOUR_ENDING", "HourEnding")]).values, unit="h")
    out["stppf"] = dedup(pd.Series(pd.to_numeric(sel[_c(sel, "STPPF_SYSTEM_WIDE")], errors="coerce").values, index=i3))

    sel, info = _load_product(RID["outage"], target, cache, "Date", None, None, pubs, "outage")
    if sel is None:
        return info
    used["outage"] = info
    i4 = pd.Timestamp(target) + pd.to_timedelta(_hours(sel[_c(sel, "HourEnding", "HOUR_ENDING")]).values, unit="h")
    zc = [_c(sel, f"TotalResourceMWZone{z}") for z in ("South", "North", "West", "Houston")]
    ic = [_c(sel, f"TotalIRRMWZone{z}") for z in ("South", "North", "West", "Houston")]
    zc = [c for c in zc if c]; ic = [c for c in ic if c]
    out["outage_total"] = dedup(pd.Series(sel[zc].apply(pd.to_numeric, errors="coerce").sum(axis=1).values, index=i4))
    hc = _c(sel, "TotalResourceMWZoneHouston")
    out["outage_houston"] = dedup(pd.Series(pd.to_numeric(sel[hc], errors="coerce").values, index=i4))
    out["outage_irr"] = dedup(pd.Series(sel[ic].apply(pd.to_numeric, errors="coerce").sum(axis=1).values, index=i4))
    out["_pub_used"] = used
    return out


def inp_from_state(hist, target):
    """state에 이미 있는 날은 MIS 재조회 없이 그 값으로 입력 구성."""
    hrs = hours_of(target)
    out = {}
    for c in STATE_COLS:
        if c not in hist.columns:
            return None
        s = hist[c].pipe(_safe).reindex(hrs)
        out[c] = s
    if out["lf_sys"].isna().all():
        return None
    return out


def fetch_eia_actuals(days_back=10):
    key = os.environ.get("EIA_API_KEY")
    if not key:
        return None
    end = dt.datetime.now(CT)
    start = end - dt.timedelta(days=days_back)
    u = ("https://api.eia.gov/v2/electricity/rto/fuel-type-data/data/"
         f"?api_key={key}&frequency=hourly&data[0]=value&facets[respondent][]=ERCO"
         f"&facets[fueltype][]=WND&facets[fueltype][]=SUN"
         f"&start={start:%Y-%m-%dT%H}&end={end:%Y-%m-%dT%H}&length=5000")
    try:
        j = requests.get(u, timeout=60).json()
    except Exception:
        return None
    rows = j.get("response", {}).get("data", [])
    df = pd.DataFrame(rows)
    if len(df) == 0:
        return None
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df["dt"] = (pd.to_datetime(df["period"], utc=True).dt.tz_convert(CT).dt.tz_localize(None)
                - pd.Timedelta(hours=1))  # hour-ending 보정
    piv = df.pivot_table(index="dt", columns="fueltype", values="value", aggfunc="first")
    piv = piv.rename(columns={"WND": "wind", "SUN": "solar"})
    return piv[~piv.index.duplicated(keep="last")]


def _recent_csv_docs(rid, cache, n):
    """CSV 문서만, 발행일 내림차순으로 최근 n개."""
    docs = [d for d in mis_doc_list(rid, cache) if doc_fmt(d) != "xml"]
    return sorted(docs, key=lambda d: str(d.get("PublishDate", "")), reverse=True)[:n]


def fetch_settlements(cache, skip_hours, skip_days=(), max_docs=25,
                      min_intervals=4, rtm_days=7, max_rtm_docs=260):
    """LZ_HOUSTON DA/RT 정산값.

    DAM(12331)은 하루 1문서 × 24행이지만, RTM(12301)은 **15분마다 1문서 × 1행**이다.
    따라서 RTM은 문서를 구간(interval) 단위로 모아 시간별로 4구간을 채워야 한다.
    (v16까지는 문서별로 시간평균을 낸 뒤 시간 기준 중복제거를 해서 3구간을 버렸고,
     그 결과 n>=4 조건을 영원히 만족하지 못해 actuals가 항상 비었다.)
    """
    def parse_dam(df):
        pc = _c(df, "SettlementPoint", "Settlement Point", "SettlementPointName")
        vc = _c(df, "SettlementPointPrice", "Settlement Point Price")
        dc = _c(df, "DeliveryDate", "Delivery Date")
        hc = _c(df, "HourEnding", "Hour Ending")
        df = df[df[pc].astype(str).str.strip() == "LZ_HOUSTON"]
        idx = _parse_dates(df[dc]) + pd.to_timedelta(_hours(df[hc]).values, unit="h")
        return pd.Series(pd.to_numeric(df[vc], errors="coerce").values, index=idx).dropna()

    def parse_rtm_intervals(df):
        """구간 단위 원본을 (시각, 구간번호, 가격) 프레임으로 반환."""
        pc = _c(df, "SettlementPointName", "SettlementPoint", "Settlement Point Name")
        if pc:
            df = df[df[pc].astype(str).str.strip() == "LZ_HOUSTON"]
        dc = _c(df, "DeliveryDate", "Delivery Date")
        hc = _c(df, "DeliveryHour", "Delivery Hour")
        ic = _c(df, "DeliveryInterval", "Delivery Interval")
        vc = _c(df, "SettlementPointPrice", "Settlement Point Price")
        if vc is None:
            vc = [c for c in df.columns if "Price" in c][0]
        idx = _parse_dates(df[dc]) + pd.to_timedelta(_hours(df[hc]).values, unit="h")
        iv = pd.to_numeric(df[ic], errors="coerce") if ic else pd.Series(1, index=df.index)
        return pd.DataFrame({"ts": idx, "iv": iv.values,
                             "rt": pd.to_numeric(df[vc], errors="coerce").values}).dropna()

    diag = {"dam": [], "rtm": [], "notes": []}

    # ---- DAM (일 단위 문서) ----
    das = []
    try:
        docs = _recent_csv_docs(RID["dam_spp_daily"], cache, max_docs)
        diag["notes"].append(f"dam:CSV문서 {len(docs)}개")
        for d in docs:
            pub = str(d.get("PublishDate", ""))[:16]
            try:
                r = parse_dam(mis_read_csv(d["DocID"]))
                das.append(r); diag["dam"].append(f"{pub}:행{len(r)}")
            except Exception as e:
                diag["dam"].append(f"{pub}:실패({type(e).__name__}:{str(e)[:50]})")
    except Exception as e:
        diag["notes"].append(f"dam:목록실패 {type(e).__name__}:{e}")
    if not das:
        diag["notes"].append("DAM 없음 → settlements 비움")
        return [], diag
    da = pd.concat(das)
    da = da[~da.index.duplicated(keep="last")].sort_index()

    # ---- RTM (15분 단위 문서) : 필요한 날짜만 골라 받는다 ----
    try:
        alldocs = [d for d in mis_doc_list(RID["rtm_spp_daily"], cache) if doc_fmt(d) != "xml"]
    except Exception as e:
        diag["notes"].append(f"rtm:목록실패 {type(e).__name__}:{e}")
        return [], diag
    alldocs = sorted(alldocs, key=lambda d: str(d.get("PublishDate", "")), reverse=True)
    pubdays = sorted({str(d.get("PublishDate", ""))[:10] for d in alldocs} - {""}, reverse=True)
    want = set(pubdays[:max(1, rtm_days) + 1])       # 자정 넘어 발행되는 마지막 구간까지 포함
    cand = [d for d in alldocs if str(d.get("PublishDate", ""))[:10] in want]

    def _covered(d):
        """발행시각으로 딜리버리 시간을 추정해, 이미 시트에 있는 시간대면 받지 않는다."""
        if not skip_hours:
            return False
        try:
            p = pd.to_datetime(str(d.get("PublishDate", "")).replace("T", " "))
        except Exception:
            return False
        # MIS 의 PublishDate 는 '2026-09-08 17:02:02-05:00' 처럼 시간대가 붙어 온다.
        # skip_hours 는 시트에서 온 시간대 없는 문자열이라, 떼지 않으면 str() 비교가
        # 영원히 어긋나 '이미 받은 문서'를 하나도 걸러내지 못한다(v18 까지의 증상:
        # 매 실행 '미수집 706개' → 예산을 이미 채워진 날짜 재수신에 소진).
        # tz_localize(None) 은 현지 벽시계 시각을 그대로 두고 시간대만 뗀다.
        if p.tzinfo is not None:
            p = p.tz_localize(None)
        h0 = p.floor("h")
        return str(h0) in skip_hours and str(h0 - pd.Timedelta(hours=1)) in skip_hours

    fresh = [d for d in cand if not _covered(d)]
    # 최신일 먼저(운영 최신성) → 나머지는 오래된 순(백필 수렴). 상한을 넘겨도 매 실행 진도가 나간다.
    newest = pubdays[0] if pubdays else ""
    head = [d for d in fresh if str(d.get("PublishDate", ""))[:10] == newest]
    tail = sorted([d for d in fresh if str(d.get("PublishDate", ""))[:10] != newest],
                  key=lambda d: str(d.get("PublishDate", "")))
    picked = (head[:110] + tail)[:max_rtm_docs]
    diag["notes"].append(f"rtm:전체문서 {len(alldocs)}개 / 발행일 {len(pubdays)}일 → 대상 {len(want)}일 "
                         f"({min(want)}~{max(want)}) 후보 {len(cand)}개, 미수집 {len(fresh)}개 중 {len(picked)}개 수신"
                         + (f" — 잔여 {len(fresh)-len(picked)}개는 다음 실행에서" if len(fresh) > len(picked) else ""))

    parts, fails = [], 0
    for d in picked:
        try:
            parts.append(parse_rtm_intervals(mis_read_csv(d["DocID"])))
        except Exception as e:
            fails += 1
            if fails <= 3:
                diag["rtm"].append(f"{str(d.get('PublishDate',''))[:16]}:실패({type(e).__name__}:{str(e)[:40]})")
    diag["notes"].append(f"rtm:수신 {len(picked)}개 중 파싱실패 {fails}개")
    if not parts:
        diag["notes"].append("RTM 파싱 결과 없음 → settlements 비움")
        return [], diag

    iv = pd.concat(parts, ignore_index=True)
    iv = iv.drop_duplicates(subset=["ts", "iv"], keep="last")      # 구간 단위 중복 제거
    g = iv.groupby("ts")["rt"]
    rt_all = pd.DataFrame({"rt": g.mean(), "n": g.size()}).sort_index()
    n_hist = rt_all["n"].value_counts().sort_index().to_dict()
    rt = rt_all[rt_all["n"] >= min_intervals]["rt"]
    diag["notes"].append(f"RT 구간행 {len(iv)} → 시간 {len(rt_all)} "
                         f"(구간수분포 {n_hist}) → 완결(n>={min_intervals}) {len(rt)}")
    diag["notes"].append(f"DAM시간 {len(da)} ({da.index.min()}~{da.index.max()})")

    both = pd.concat([da.rename("da"), rt.rename("rt")], axis=1).dropna()
    diag["notes"].append(f"DA∩RT 교집합 {len(both)}시간"
                         + (f" ({both.index.min()}~{both.index.max()})" if len(both) else ""))

    out, skipped_n = [], 0
    for t, row in both.iterrows():
        ts = str(t)
        if ts in skip_hours or (skip_days and ts[:10] in skip_days):
            skipped_n += 1
            continue
        out.append({"dt_local": ts, "da_spp": round(float(row["da"]), 2),
                    "rt_spp": round(float(row["rt"]), 2),
                    "dart": round(float(row["da"] - row["rt"]), 2)})
    diag["notes"].append(f"이미 시트에 있어 스킵 {skipped_n} → 신규 {len(out)}행")
    return out, diag


# ---------------- feature builder ----------------
def build_features(target, inp, hist, eia, temps_now, temp_fcst_series):
    hrs = hours_of(target)
    f = pd.DataFrame(index=hrs)
    f["hour"] = f.index.hour
    f["dow"] = f.index.dayofweek
    f["month"] = f.index.month
    f["is_weekend"] = (f["dow"] >= 5).astype(int)
    f["rtcb"] = 1

    lf = inp["lf_sys"].pipe(_safe).reindex(hrs)
    f["op_df_level"] = lf.values
    lf_all = dedup(pd.concat([hist["lf_sys"].dropna(), lf.dropna()]))
    f["op_df_ramp1"] = lf_all.diff(1).pipe(_safe).reindex(hrs).values
    f["op_df_dpeak"] = float(np.nanmax(lf.values)) if lf.notna().any() else np.nan

    win_end = pd.Timestamp(target) - pd.Timedelta(days=2)

    def same_hour_mean(series, h, days=7):
        s = series.dropna()
        s = s[(s.index >= win_end - pd.Timedelta(days=days - 1)) &
              (s.index < win_end + pd.Timedelta(days=1))]
        s = s[s.index.hour == h]
        return float(s.mean()) if len(s) else np.nan

    f["op_df_anom"] = [f["op_df_level"].iloc[i] - same_hour_mean(hist["lf_sys"], h)
                       for i, h in enumerate(f["hour"])]

    tf = temp_fcst_series.pipe(_safe).reindex(hrs)
    f["temp_fcst"] = tf.values
    f["wx_cdd"] = (f["temp_fcst"] - 21).clip(lower=0)
    f["wx_hdd"] = (10 - f["temp_fcst"]).clip(lower=0)
    f["wx_dmax"] = float(np.nanmax(tf.values)) if tf.notna().any() else np.nan
    past_temps = temps_now[temps_now.index < pd.Timestamp(target)]
    f["wx_anom"] = [f["temp_fcst"].iloc[i] - same_hour_mean(past_temps, h)
                    for i, h in enumerate(f["hour"])]

    d2 = hours_of((pd.Timestamp(target) - pd.Timedelta(days=2)).date())
    f["wx_err_lag48"] = (temps_now.pipe(_safe).reindex(d2).values - hist["temp_fcst"].pipe(_safe).reindex(d2).values)
    if eia is not None:
        f["wind_err_lag48"] = eia["wind"].pipe(_safe).reindex(d2).values - hist["stwpf"].pipe(_safe).reindex(d2).values
        f["solar_err_lag48"] = eia["solar"].pipe(_safe).reindex(d2).values - hist["stppf"].pipe(_safe).reindex(d2).values
    else:
        f["wind_err_lag48"] = np.nan
        f["solar_err_lag48"] = np.nan

    stw = inp["stwpf"].pipe(_safe).reindex(hrs)
    stp = inp["stppf"].pipe(_safe).reindex(hrs)
    f["wind_unc"] = (stw - inp["wgrpp"].pipe(_safe).reindex(hrs)).values
    f["outage_total"] = inp["outage_total"].pipe(_safe).reindex(hrs).values
    f["outage_houston"] = inp["outage_houston"].pipe(_safe).reindex(hrs).values
    f["outage_irr"] = inp["outage_irr"].pipe(_safe).reindex(hrs).values
    f["op_scarcity"] = f["op_df_level"] - stw.values - stp.values + f["outage_total"]

    o_hist = hist["outage_total"].dropna()
    o_hist = o_hist[o_hist.index < pd.Timestamp(target) - pd.Timedelta(days=1)].tail(14 * 24)
    f["outage_anom"] = f["outage_total"] - (float(o_hist.mean()) if len(o_hist) else np.nan)
    return f


def _decide(ED, ps):
    band = CFG["band"]
    dec = np.where(ED < -band, "BUY_DA", np.where(ED > band, "BUY_RT", "NEUTRAL_5050"))
    if CFG.get("spike_threshold"):
        dec = np.where(np.asarray(ps) > CFG["spike_threshold"], "BUY_DA", dec)
    return dec


def predict_day(f):
    """returns p3, ps, ED, dec, detail
    detail: 앙상블 평균 전 개별 모델(시드별) 출력 원본"""
    F3 = f[CFG["features_3class"]].astype(float)
    FS = f[CFG["features_spike"]].astype(float)
    F3 = F3.fillna(F3.median())
    FS = FS.fillna(FS.median())
    seeds = CFG["seeds"]
    p3_seed = {s: np.asarray(b.predict(F3.values)) for s, b in zip(seeds, M3)}
    ps_seed = {s: np.asarray(b.predict(FS.values)).ravel() for s, b in zip(seeds, MS)}
    p3 = np.mean([p3_seed[s] for s in seeds], axis=0)
    ps = np.mean([ps_seed[s] for s in seeds], axis=0)
    mu = CFG["mu"]
    ED = p3[:, 0] * mu[0] + p3[:, 1] * mu[1] + p3[:, 2] * mu[2]
    dec = _decide(ED, ps)
    return p3, ps, ED, dec, {"p3_seed": p3_seed, "ps_seed": ps_seed}


def model_detail_rows(hrs, p3, ps, ED, dec, detail):
    """시간당 1행: 시드별(개별 모델) 출력 + 앙상블 + 모델 간 합의도."""
    mu, seeds = np.array(CFG["mu"], dtype=float), CFG["seeds"]
    ed_seed = {s: (detail["p3_seed"][s] * mu).sum(axis=1) for s in seeds}
    dec_seed = {s: _decide(ed_seed[s], detail["ps_seed"][s]) for s in seeds}
    out = []
    for i in range(len(hrs)):
        row = {"dt_local": str(hrs[i]),
               "ens_e_dart": round(float(ED[i]), 3),
               "ens_decision": str(dec[i]),
               "ens_p_spike": round(float(ps[i]), 4)}
        for s in seeds:
            row[f"m3_{s}_p_da"] = round(float(detail["p3_seed"][s][i, 0]), 4)
            row[f"m3_{s}_p_rt"] = round(float(detail["p3_seed"][s][i, 2]), 4)
            row[f"m3_{s}_e_dart"] = round(float(ed_seed[s][i]), 3)
            row[f"m3_{s}_decision"] = str(dec_seed[s][i])
            row[f"ms_{s}_p_spike"] = round(float(detail["ps_seed"][s][i]), 4)
        eds = np.array([ed_seed[s][i] for s in seeds], dtype=float)
        pss = np.array([detail["ps_seed"][s][i] for s in seeds], dtype=float)
        row["agree_n"] = int(sum(1 for s in seeds if dec_seed[s][i] == dec[i]))
        row["e_dart_std"] = round(float(eds.std(ddof=0)), 3)
        row["e_dart_min"] = round(float(eds.min()), 3)
        row["e_dart_max"] = round(float(eds.max()), 3)
        row["p_spike_std"] = round(float(pss.std(ddof=0)), 4)
        out.append(row)
    return out


def _r(s, t):
    try:
        v = s.pipe(_safe).reindex([t]).iloc[0]
        return None if pd.isna(v) else round(float(v), 2)
    except Exception:
        return None


# ---------------- endpoints ----------------
@app.get("/health")
def health():
    return {"ok": True, "version": VERSION, "trained_through": CFG["trained_through"],
            "eia_key_set": bool(os.environ.get("EIA_API_KEY")),
            "n_models": len(M3) + len(MS),
            "endpoints": ["/health", "/probe?days=7", "/peek?product=&pub=",
                          "/settle", "POST /score"]}


@app.get("/probe")
def probe(day: str = "", days: int = 0):
    """브라우저에서 바로 열어보는 진단용. state 없이 MIS만 확인한다.
    예) /probe                    → 내일치
        /probe?day=2026-08-02     → 특정일
        /probe?days=7             → 백필 대상 전체(어제부터 과거 N일 + 내일) 한 번에
    """
    now = dt.datetime.now(CT)
    if days:
        tomorrow = (now + dt.timedelta(days=1)).date()
        targets = [tomorrow] + [tomorrow - dt.timedelta(days=k) for k in range(1, days + 1)]
        cache = {}
        res = {}
        for t in sorted(targets):
            r = _probe_one(t, cache)
            res[str(t)] = {"all_ok": r["all_ok"],
                           "detail": {k: (v.get("pub_used") or v.get("reason") or v.get("error"))
                                      for k, v in r["products"].items()}}
        ok = [d for d, v in res.items() if v["all_ok"]]
        return {"now_ct": str(now), "window_days": days, "recoverable_days": ok,
                "n_recoverable": len(ok), "by_day": res,
                "hint": "recoverable_days 에 있는 날짜만 백필 가능. 나머지는 MIS 7일 보관 한도 밖이거나 미발행"}
    target = (dt.date.fromisoformat(day) if day else (now + dt.timedelta(days=1)).date())
    return {"now_ct": str(now), **_probe_one(target, {})}


def _probe_one(target, cache):
    out = {"target_day": str(target), "products": {}}
    pubs = [target - dt.timedelta(days=1), target,
            target - dt.timedelta(days=2), target + dt.timedelta(days=1)]
    spec = {
        "load_fcst": (RID["load_fcst"], "DeliveryDate", "DSTFlag"),
        "wind":      (RID["wind"], "DELIVERY_DATE", "DSTFlag"),
        "solar":     (RID["solar"], "DELIVERY_DATE", "DSTFlag"),
        "outage":    (RID["outage"], "Date", None),
    }
    for name, (rid, datecol, dstcol) in spec.items():
        try:
            docs = mis_doc_list(rid, cache)
            avail = sorted({str(d.get("PublishDate", ""))[:10] for d in docs} - {""})
        except Exception as e:
            out["products"][name] = {"error": f"{type(e).__name__}: {e}"}
            continue
        sel, info = _load_product(rid, target, cache, datecol, None, dstcol, pubs, name)
        out["products"][name] = {
            "ok": sel is not None,
            "rows": (0 if sel is None else int(len(sel))),
            "pub_used": (info if sel is not None else None),
            "reason": (None if sel is not None else info),
            "mis_publish_dates_available": avail[-10:],
        }
    out["all_ok"] = all(v.get("ok") for v in out["products"].values())
    out["hint"] = ("정상 — /score 실패 원인은 MIS가 아님(state 부족·날씨·시트 쪽 확인)"
                   if out["all_ok"] else
                   "위 reason이 그날 예측이 비는 직접 원인. MIS 미발행이면 시간을 늦춰 재실행")
    return out


@app.get("/peek")
def peek(product: str = "load_fcst", pub: str = ""):
    """원본 CSV의 컬럼명과 샘플 2행을 그대로 보여준다 (파싱이 계속 실패할 때).
    예) /peek?product=wind&pub=2026-08-01
    """
    if product not in RID:
        return {"error": f"product must be one of {list(RID)}"}
    cache = {}
    docs = mis_doc_list(RID[product], cache)
    pubday = (dt.date.fromisoformat(pub) if pub else dt.datetime.now(CT).date())
    same = [x for x in docs if str(x.get("PublishDate", ""))[:10] == pubday.isoformat()]
    cands = [{"fmt": doc_fmt(x), "PublishDate": str(x.get("PublishDate")),
              "FriendlyName": str(x.get("FriendlyName", ""))[:70], "DocID": x.get("DocID")}
             for x in sorted(same, key=lambda x: str(x.get("PublishDate")))][-8:]
    d = pick_doc(docs, pubday)
    if d is None:
        return {"product": product, "pub": str(pubday), "error": "그 발행일 문서 없음",
                "candidates_that_day": cands,
                "available": sorted({str(x.get("PublishDate", ""))[:10] for x in docs} - {""})[-10:]}
    try:
        df = mis_read_csv(d["DocID"])
    except Exception as e:
        return {"product": product, "pub": str(pubday), "picked": doc_fmt(d),
                "error": f"{type(e).__name__}: {e}", "candidates_that_day": cands}
    date_col = _c(df, "DeliveryDate", "DELIVERY_DATE", "Date", "OperDay", "OPR_DATE")
    dts = _parse_dates(df[date_col]) if date_col else None
    return {
        "product": product, "pub_used": str(d.get("PublishDate")),
        "picked_fmt": doc_fmt(d), "picked_name": str(d.get("FriendlyName", ""))[:80],
        "candidates_that_day": cands, "n_rows": int(len(df)),
        "columns": [str(c) for c in df.columns],
        "date_col_found": date_col,
        "date_samples": [str(v) for v in df[date_col].head(3)] if date_col else None,
        "delivery_days_in_doc": (sorted({str(x.date()) for x in dts.dropna().unique()})
                                 if dts is not None and dts.notna().any() else None),
        "sample_rows": json.loads(df.head(2).to_json(orient="records")),
    }


@app.get("/settle")
def settle(max_docs: int = 25, min_intervals: int = 4, rtm_days: int = 7, max_rtm_docs: int = 260):
    """actuals가 안 채워질 때 원인을 보여준다. 시트 상태와 무관하게 MIS만 조회."""
    rows, diag = fetch_settlements({}, set(), (), max_docs=max_docs, min_intervals=min_intervals,
                                   rtm_days=rtm_days, max_rtm_docs=max_rtm_docs)
    return {"n_rows": len(rows), "diag": diag,
            "first": rows[:3], "last": rows[-3:],
            "hint": ("정상 — 워크플로가 이 행들을 actuals에 append해야 함" if rows else
                     "diag.notes 를 위에서부터 읽으면 어느 단계에서 끊겼는지 보임")}


@app.post("/score")
def score(req: ScoreReq):
    import traceback
    try:
        return _score(req)
    except HTTPException:
        raise
    except Exception as e:
        tb = traceback.format_exc().splitlines()
        mine = [l.strip() for l in tb if "main.py" in l]
        raise HTTPException(500, f"{type(e).__name__}: {e} | at {mine[-2:] if mine else tb[-3:]}")


def _score(req: ScoreReq):
    now = dt.datetime.now(CT)
    tomorrow = (now + dt.timedelta(days=1)).date()

    # ---- state 로드 ----
    st = pd.DataFrame(req.state)
    if len(st) < 24 * 8:
        raise HTTPException(400, f"state too short: {len(st)} rows (need >= 192)")
    st["dt_local"] = pd.to_datetime(st["dt_local"])
    st = st.set_index("dt_local").sort_index()
    st = st[~st.index.duplicated(keep="last")]
    for c in st.columns:
        st[c] = pd.to_numeric(st[c], errors="coerce")
    for c in STATE_COLS:
        if c not in st.columns:
            st[c] = np.nan
    hist = st.copy()

    # 날짜별 state 보유 여부 (24시간 중 20시간 이상이면 보유로 간주)
    day_counts = hist["lf_sys"].dropna().groupby(hist["lf_sys"].dropna().index.date).size()
    state_days = set(d for d, n in day_counts.items() if n >= 20)

    have_pred = set(_norm_ts(x)[:10] for x in (req.have_pred_days or []))
    have_act_h = set(_norm_ts(x) for x in (req.have_actual_hours or []))
    # 시간 목록이 오면 그것만 사용(일 단위 스킵은 구멍을 영구화하므로 미사용)
    have_act_d = set() if req.have_actual_hours is not None else \
                 set(str(x)[:10] for x in (req.have_actual_days or []))

    # ---- 날씨 시리즈 ----
    if not (req.weather and req.weather.get("time")):
        raise HTTPException(400, "weather 필드가 필요합니다 (n8n Fetch Open-Meteo 노드)")
    temps_now = dedup(pd.Series(req.weather["temperature_2m"],
                                index=pd.to_datetime(req.weather["time"])))
    temps_prev = None
    if req.weather_prev and req.weather_prev.get("time"):
        vk = [k for k in req.weather_prev if k.startswith("temperature_2m")]
        if vk:
            temps_prev = dedup(pd.Series(req.weather_prev[vk[0]],
                                         index=pd.to_datetime(req.weather_prev["time"])))

    # ---- 처리 대상 날짜 결정 ----
    nback = max(1, min(int(req.max_backfill_days or 7), 10))
    candidates = [tomorrow - dt.timedelta(days=k) for k in range(nback, -1, -1)]
    min_state_day = min(state_days) if state_days else None
    todo = []
    for d in candidates:
        if min_state_day and d < min_state_day:
            continue
        need_state = d not in state_days
        need_pred = (d.isoformat() not in have_pred) if req.have_pred_days is not None else (d == tomorrow)
        if need_state or need_pred:
            todo.append(d)
    if tomorrow not in todo:
        todo.append(tomorrow)
    todo = sorted(set(todo))

    eia = fetch_eia_actuals()
    eia_stat = {"ok": eia is not None,
                "key_set": bool(os.environ.get("EIA_API_KEY")),
                "rows": (0 if eia is None else int(len(eia))),
                "last": (None if eia is None or not len(eia) else str(eia.index.max()))}
    cache = {}
    feat_health = {}
    preds_out, state_out, detail_out, processed, skipped = [], [], [], [], []

    for day in todo:
        try:
            from_state = day in state_days
            inp = inp_from_state(hist, day) if from_state else fetch_day_inputs(day, cache)
            if inp is None or isinstance(inp, str):
                skipped.append({"day": day.isoformat(),
                                "reason": inp if isinstance(inp, str) else "state 값 부족"})
                continue
            pub_used = inp.pop("_pub_used", None)

            hrs = hours_of(day)
            tfs = None
            if day < tomorrow and temps_prev is not None:
                cand = temps_prev.pipe(_safe).reindex(hrs)
                if cand.notna().sum() >= 12:
                    tfs = cand
            if tfs is None:
                tfs = (inp["temp_fcst"].pipe(_safe).reindex(hrs) if from_state and "temp_fcst" in inp
                       else temps_now.pipe(_safe).reindex(hrs))
                if tfs.isna().all():
                    tfs = temps_now.pipe(_safe).reindex(hrs)
            inp["temp_fcst"] = tfs

            f = build_features(day, inp, hist, eia, temps_now, tfs)
            if day == tomorrow:
                need = sorted(set(CFG["features_3class"]) | set(CFG["features_spike"]))
                bad = {c: round(float(f[c].isna().mean()), 2) for c in need
                       if c in f.columns and f[c].isna().mean() > 0.5}
                miss = [c for c in need if c not in f.columns]
                feat_health = {"n_features": len(need), "high_nan": bad, "missing": miss}
            p3, ps, ED, dec, detail = predict_day(f)

            need_pred = (day.isoformat() not in have_pred) if req.have_pred_days is not None else True
            if need_pred or day == tomorrow:
                detail_out.extend(model_detail_rows(hrs, p3, ps, ED, dec, detail))
                for i in range(24):
                    preds_out.append({
                        "dt_local": str(hrs[i]),
                        "p_da_cheap": round(float(p3[i, 0]), 4),
                        "p_neutral": round(float(p3[i, 1]), 4),
                        "p_rt_cheap": round(float(p3[i, 2]), 4),
                        "p_spike": round(float(ps[i]), 4),
                        "e_dart": round(float(ED[i]), 3),
                        "decision": str(dec[i]),
                    })

            if not from_state:
                for i in range(24):
                    state_out.append({"dt_local": str(hrs[i]),
                                      **{c: _r(inp[c], hrs[i]) for c in STATE_COLS}})
                add = pd.DataFrame({c: inp[c].pipe(_safe).reindex(hrs) for c in STATE_COLS}, index=hrs)
                hist = pd.concat([hist, add])
                hist = hist[~hist.index.duplicated(keep="last")].sort_index()
                state_days.add(day)

            processed.append({"day": day.isoformat(),
                              "source": "state" if from_state else "mis",
                              "state_written": not from_state,
                              "pub_used": (pub_used if not from_state else None)})
        except Exception as e:
            skipped.append({"day": day.isoformat(), "reason": f"{type(e).__name__}: {e}"})

    settlements, settle_diag = fetch_settlements(cache, have_act_h, have_act_d,
                                                 rtm_days=req.rtm_days,
                                                 max_rtm_docs=req.max_rtm_docs)

    return {
        "predictions": preds_out,
        "model_detail": detail_out,
        "new_state": state_out,
        "settlements": settlements,
        "meta": {
            "target_day": str(tomorrow),
            "generated_at": str(now),
            "processed_days": processed,
            "skipped_days": skipped,
            "backfilled_days": [p["day"] for p in processed if p["day"] != str(tomorrow)],
            "state_days_in_sheet": sorted(str(d) for d in state_days),
            "todo_days": [str(d) for d in todo],
            "n_state_rows_returned": len(state_out),
            "n_model_detail_rows": len(detail_out),
            "n_settlements": len(settlements),
            "eia": eia_stat,
            "feature_health": feat_health,
            "settlement_diag": settle_diag,
            "config": {k: CFG[k] for k in ("band", "spike_threshold", "trained_through")},
        },
    }
