"""
us_earnings_screener.py — 미국 상장기업 실적 스크리너
=====================================================
10-Q/10-K + 8-K(Item 2.02 실적발표) 두 가지 소스로 실적 기업 감지.

  10-Q/10-K: fetch_quarterly(refresh=True) → SEC XBRL 강제 갱신 → MD 저장
  8-K:       yfinance로 최신 분기 즉시 수집 → 기존 SEC 캐시에 병합 → MD 저장

Usage:
    python us_earnings_screener.py                        # 최근 14일, 기본 필터
    python us_earnings_screener.py --from 20260501 --to 20260507
    python us_earnings_screener.py --min-rev-yoy 10 --min-op-yoy 15
    python us_earnings_screener.py --telegram             # 신규 기업 텔레그램 발송
    python us_earnings_screener.py --no-update            # MD 업데이트 없이 스크리닝만
"""

import os
import sys
import re
import json
import time
import argparse
import requests
from datetime import date, datetime, timedelta
from urllib.parse import quote
from urllib.request import urlopen, Request

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

from sec_quarterly import (
    fetch_quarterly, _sort_key, _prev_year, AVRateLimitError,
    _fetch_from_yfinance, _load_cache as _sq_load_cache, _save_cache as _sq_save_cache,
    CACHE_PATH as SEC_CACHE_PATH, CURRENCY_OVERRIDES,
)
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
COMPANIES_PATH = os.path.join(BASE_DIR, "us_companies.json")
OUTPUT_DIR     = os.path.join(BASE_DIR, "reports_earnings")
US_MD_DIR      = os.path.join(BASE_DIR, "reports_us_quarterly")
META_PATH      = os.path.join(BASE_DIR, "us_quarterly_meta.json")
DB_PATH        = os.path.join(BASE_DIR, "us_earnings_db.json")

SEC_HEADERS = {
    "User-Agent": "DART-analyzer-bot contact@example.com",
    "Accept":     "application/json",
}
TG_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

_us_earnings_db: dict = {}

_CURRENCY_SYMBOLS = {
    "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "KRW": "₩",
    "TWD": "NT$", "CAD": "CA$", "AUD": "A$", "CNY": "¥", "HKD": "HK$",
    "BRL": "R$", "INR": "₹", "MXN": "MX$", "SEK": "kr",
}


# ─────────────────────────────────────────────────────────────
# DB 관리
# ─────────────────────────────────────────────────────────────

def _load_db() -> None:
    global _us_earnings_db
    if _us_earnings_db:
        return
    if os.path.exists(DB_PATH):
        with open(DB_PATH, encoding="utf-8") as f:
            _us_earnings_db = json.load(f)
        print(f"[US 실적 DB] {len(_us_earnings_db)}건 로드")


def _save_db() -> None:
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(_us_earnings_db, f, ensure_ascii=False, indent=2)


def _db_key(ticker: str, quarter_label: str) -> str:
    return f"{ticker.upper()}:{quarter_label}"


# ─────────────────────────────────────────────────────────────
# SEC EFTS — 필링 검색 (공통 페이지네이션)
# ─────────────────────────────────────────────────────────────

def _efts_query(q: str, forms: str, start_date: str, end_date: str) -> list[dict]:
    """
    EFTS 공통 쿼리. 결과: [{cik, form_type, filing_date, period_of_report}, ...]
    """
    results = []
    from_idx = 0
    size = 100

    while True:
        url = (
            f"https://efts.sec.gov/LATEST/search-index"
            f"?q={q}&forms={forms}"
            f"&dateRange=custom&startdt={start_date}&enddt={end_date}"
            f"&from={from_idx}&size={size}"
        )
        try:
            req = Request(url, headers=SEC_HEADERS)
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"  [EFTS 오류] {e}")
            break

        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            break

        for hit in hits:
            acc_id = hit.get("_id", "")
            src    = hit.get("_source", {})
            cik = ""
            if "-" in acc_id:
                try:
                    cik = str(int(acc_id.split("-")[0]))
                except ValueError:
                    pass
            form_type   = src.get("form_type") or src.get("file_type") or ""
            filing_date = src.get("file_date") or ""
            period      = src.get("period_of_report") or ""
            if cik and form_type:
                results.append({
                    "cik":              cik,
                    "form_type":        form_type,
                    "filing_date":      filing_date,
                    "period_of_report": period,
                })

        total = data.get("hits", {}).get("total", {})
        total_val = total.get("value", 0) if isinstance(total, dict) else (total or 0)
        from_idx += size
        if from_idx >= total_val or not hits:
            break
        time.sleep(0.1)

    return results


def _query_10q_10k(start_date: str, end_date: str) -> list[dict]:
    """10-Q/10-K 필링 목록. EFTS는 첨부물(EX-31 등)도 반환하지만 처리에 지장 없음."""
    return _efts_query("%22%22", "10-Q,10-K", start_date, end_date)


def _query_8k_earnings(start_date: str, end_date: str) -> list[dict]:
    """8-K Item 2.02 (실적발표) 필링 목록. '2.02' 텍스트 필터로 earnings 8-K 추출."""
    return _efts_query("%222.02%22", "8-K", start_date, end_date)


# ─────────────────────────────────────────────────────────────
# 유니버스 CIK 맵
# ─────────────────────────────────────────────────────────────

def _build_cik_map() -> dict:
    """us_companies.json → {cik_str(선행0 제거): company_info_with_rank}"""
    if not os.path.exists(COMPANIES_PATH):
        return {}
    with open(COMPANIES_PATH, encoding="utf-8") as f:
        data = json.load(f)
    cik_map = {}
    for i, c in enumerate(data["companies"], 1):
        cik_raw = c.get("cik", "")
        if not cik_raw:
            continue
        try:
            cik_str = str(int(cik_raw))
        except (ValueError, TypeError):
            cik_str = str(cik_raw).lstrip("0") or "0"
        cik_map[cik_str] = dict(c, rank=i)
    return cik_map


# ─────────────────────────────────────────────────────────────
# period_of_report → 분기 레이블
# ─────────────────────────────────────────────────────────────

def _period_to_quarter(period: str) -> str:
    if not period:
        return ""
    try:
        d = datetime.strptime(period[:10], "%Y-%m-%d")
        if d.month in (1, 4, 7, 10):
            d = d - timedelta(days=1)
        return f"{d.year}Q{(d.month - 1) // 3 + 1}"
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────
# 8-K 전용: yfinance → SEC 캐시 병합
# ─────────────────────────────────────────────────────────────

def _yfinance_merge(ticker: str, company_info: dict | None = None) -> dict | None:
    """
    yfinance로 최신 분기 수집 후 기존 SEC 캐시에 없는 분기만 추가.
    10-Q 제출 전 8-K 감지 시 사용. 반환: 업데이트된 캐시 엔트리 (없으면 None).
    """
    min_year = date.today().year - 10

    print(f"  [yfinance] {ticker} 최신 분기 수집...", flush=True)
    try:
        rev, opi, net, periods, currency = _fetch_from_yfinance(ticker, min_year)
    except Exception as e:
        print(f"  [yfinance 오류] {e}", flush=True)
        return None

    if not rev and not opi:
        print(f"  → yfinance 데이터 없음", flush=True)
        return None

    if ticker in CURRENCY_OVERRIDES:
        currency = CURRENCY_OVERRIDES[ticker]

    cache = _sq_load_cache()
    existing = cache.get(ticker, {})
    existing_quarters = dict(existing.get("quarters", {}))

    # 기존 캐시에 없는 분기만 추가 (SEC 데이터 보존)
    added = []
    for q in set(rev) | set(opi) | set(net):
        if q not in existing_quarters:
            existing_quarters[q] = {
                "rev":    rev.get(q),
                "opi":    opi.get(q),
                "net":    net.get(q),
                "period": periods.get(q, ""),
            }
            added.append(q)

    if not added:
        print(f"  → yfinance: 새 분기 없음", flush=True)
        return existing or None

    print(f"  → yfinance: {sorted(added, key=_sort_key)} 추가", flush=True)

    updated = {
        **existing,
        "quarters":   existing_quarters,
        "fetched_at": date.today().isoformat(),
        "currency":   currency,
    }
    # 기존 캐시가 없으면 기본값 채우기
    if not existing:
        updated.setdefault("company_name", (company_info or {}).get("name", ticker))
        updated.setdefault("fy_month", 12)
        updated.setdefault("source", "yfinance")

    cache[ticker] = updated
    _sq_save_cache(cache)
    return updated


# ─────────────────────────────────────────────────────────────
# YoY 계산
# ─────────────────────────────────────────────────────────────

def _calc_yoy(curr, prev):
    if curr is None or prev is None or prev == 0:
        return None
    try:
        return (curr - prev) / abs(prev) * 100
    except Exception:
        return None


def _get_latest_quarter_yoy(cache_entry: dict, target_q: str = None) -> dict | None:
    quarters = cache_entry.get("quarters", {})
    if not quarters:
        return None
    valid = [l for l in quarters if re.match(r"\d{4}Q\d", l)]
    if not valid:
        return None
    label = target_q if (target_q and target_q in quarters) else max(valid, key=_sort_key)
    prev  = _prev_year(label)

    curr_q = quarters.get(label, {})
    prev_q = quarters.get(prev, {})

    rev_curr = curr_q.get("rev");  rev_prev = prev_q.get("rev")
    op_curr  = curr_q.get("opi");  op_prev  = prev_q.get("opi")
    net_curr = curr_q.get("net");  net_prev = prev_q.get("net")

    return {
        "latest_q": label,
        "rev_curr": rev_curr, "rev_prev": rev_prev, "rev_yoy": _calc_yoy(rev_curr, rev_prev),
        "op_curr":  op_curr,  "op_prev":  op_prev,  "op_yoy":  _calc_yoy(op_curr,  op_prev),
        "net_curr": net_curr, "net_prev": net_prev,  "net_yoy": _calc_yoy(net_curr, net_prev),
    }


# ─────────────────────────────────────────────────────────────
# 필터
# ─────────────────────────────────────────────────────────────

def _passes_filter(yoy: dict, min_rev_yoy: float, min_op_yoy: float, min_op_abs: float) -> bool:
    rev_yoy = yoy.get("rev_yoy");  op_yoy  = yoy.get("op_yoy")
    op_curr = yoy.get("op_curr");  op_prev = yoy.get("op_prev")
    is_흑전  = bool(op_curr and op_curr > 0 and op_prev and op_prev < 0)
    op_abs_ok = min_op_abs <= 0 or (op_curr is not None and op_curr >= min_op_abs)
    rev_ok    = rev_yoy is not None and rev_yoy >= min_rev_yoy
    op_ok     = (op_yoy is not None and op_yoy >= min_op_yoy) or is_흑전
    return rev_ok and op_ok and op_abs_ok


# ─────────────────────────────────────────────────────────────
# 포맷 헬퍼
# ─────────────────────────────────────────────────────────────

def _fmt_usd(v, currency: str = "USD") -> str:
    if v is None:
        return "N/A"
    sym   = _CURRENCY_SYMBOLS.get(currency, currency)
    sign  = "-" if v < 0 else ""
    abs_v = abs(v)
    if abs_v >= 1e12: return f"{sign}{sym}{abs_v/1e12:.2f}T"
    if abs_v >= 1e9:  return f"{sign}{sym}{abs_v/1e9:.2f}B"
    if abs_v >= 1e6:  return f"{sign}{sym}{abs_v/1e6:.1f}M"
    return f"{sign}{sym}{abs_v/1e3:.0f}K"


def _fmt_yoy(v) -> str:
    return "N/A" if v is None else f"{v:+.1f}%"


def _fmt_흑전(op_curr, op_prev) -> str:
    if op_curr and op_curr > 0 and op_prev and op_prev < 0: return " *(흑전)*"
    if op_curr and op_curr < 0 and op_prev and op_prev > 0: return " *(적전)*"
    return ""


# ─────────────────────────────────────────────────────────────
# 누적 분기 MD 생성
# ─────────────────────────────────────────────────────────────

def build_quarter_md(quarter_key: str) -> str:
    entries = [v for v in _us_earnings_db.values() if v.get("quarter_label") == quarter_key]
    if not entries:
        return ""
    entries.sort(key=lambda e: e["yoy"].get("op_yoy") or -9999, reverse=True)
    today_str = date.today().strftime("%Y.%m.%d")

    lines = [
        f"# {quarter_key} US 실적 스크리닝 누적 리포트",
        "",
        f"- **기준 분기**: {quarter_key}",
        f"- **마지막 갱신**: {today_str}",
        f"- **누적 기업 수**: {len(entries)}개",
        f"- **필터**: 매출YoY ≥ +15%  &  영업이익YoY ≥ +20%",
        "",
        "## 기업 목록",
        "",
        "| # | 기업명 | 섹터 | 소스 | 매출YoY | 영업이익YoY | 순이익YoY | 공시일 |",
        "|---|--------|------|------|---------|------------|---------|--------|",
    ]
    for i, e in enumerate(entries, 1):
        y = e["yoy"]; ticker = e["ticker"]; nm = e["company_name"]; cik = e.get("cik","")
        ft = e.get("form_type", "")
        display_ft = ft if ft in ("10-Q", "10-K", "8-K") else "10-Q"
        흑전 = " 흑전" if (y.get("op_curr") or 0) > 0 and (y.get("op_prev") or 0) < 0 else ""
        sec_url = (f"https://www.sec.gov/cgi-bin/browse-edgar"
                   f"?action=getcompany&CIK={cik}&type={display_ft}"
                   f"&dateb=&owner=include&count=10")
        lines.append(
            f"| {i} | [{nm} ({ticker})]({sec_url}) | {e.get('sector','')} | {display_ft}"
            f" | {_fmt_yoy(y.get('rev_yoy'))}"
            f" | {_fmt_yoy(y.get('op_yoy'))}{흑전}"
            f" | {_fmt_yoy(y.get('net_yoy'))}"
            f" | {e.get('filing_date','')} |"
        )

    lines += ["", "## 기업별 상세", ""]
    for e in entries:
        y = e["yoy"]; ticker = e["ticker"]; nm = e["company_name"]
        cur = e.get("currency","USD"); cik = e.get("cik","")
        ft = e.get("form_type", "")
        display_ft = ft if ft in ("10-Q", "10-K", "8-K") else "10-Q"
        흑전 = _fmt_흑전(y.get("op_curr"), y.get("op_prev"))
        sec_url = (f"https://www.sec.gov/cgi-bin/browse-edgar"
                   f"?action=getcompany&CIK={cik}&type={display_ft}"
                   f"&dateb=&owner=include&count=10")
        lines += [
            f"### {nm} ({ticker})",
            "",
            f"- **소스**: {display_ft}  /  **섹터**: {e.get('sector','')}",
            f"- **분기**: {e.get('quarter_label','')}  /  **공시일**: {e.get('filing_date','')}",
            f"- **SEC**: {sec_url}",
            "",
            "| 항목 | 당기 | 전년동기 | YoY |",
            "|------|------|----------|-----|",
            f"| 매출 | {_fmt_usd(y.get('rev_curr'),cur)} | {_fmt_usd(y.get('rev_prev'),cur)} | {_fmt_yoy(y.get('rev_yoy'))} |",
            f"| 영업이익 | {_fmt_usd(y.get('op_curr'),cur)}{흑전} | {_fmt_usd(y.get('op_prev'),cur)} | {_fmt_yoy(y.get('op_yoy'))} |",
            f"| 순이익 | {_fmt_usd(y.get('net_curr'),cur)} | {_fmt_usd(y.get('net_prev'),cur)} | {_fmt_yoy(y.get('net_yoy'))} |",
            "",
        ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# 텔레그램
# ─────────────────────────────────────────────────────────────

def _send_telegram(text: str) -> bool:
    try:
        resp = requests.post(
            f"{TG_BASE}/sendMessage",
            json={"chat_id": TELEGRAM_CHANNEL_ID, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"  [텔레그램 오류] {e}")
        return False


def send_us_screening_result(results, start_date, end_date, min_rev_yoy, min_op_yoy):
    header = (
        f"📊 <b>US 실적 스크리닝 결과</b>\n"
        f"📅 {start_date} ~ {end_date}\n"
        f"🔍 필터: 매출YoY≥{min_rev_yoy:+.0f}%  영업이익YoY≥{min_op_yoy:+.0f}%\n"
        f"✅ 신규 통과: <b>{len(results)}개 기업</b>\n"
    )
    if not results:
        return _send_telegram(header + "\n해당 기업 없음.")

    QUARTZ_BASE = "https://agent-jaden.github.io/US-company-analysis/quarterly"

    lines = []
    for r in results:
        y = r["yoy"]; ticker = r["ticker"]; nm = r["company_name"]
        ft = r.get("form_type", "")
        display_ft = ft if ft in ("10-Q", "10-K", "8-K") else "10-Q"
        흑전 = " 흑전" if (y.get("op_curr") or 0) > 0 and (y.get("op_prev") or 0) < 0 else ""
        page_url = f"{QUARTZ_BASE}/{quote(f'{ticker}_분기실적')}"
        lines.append(
            f"\n<a href='{page_url}'>{nm} ({ticker})</a>"
            f"  [{r['quarter_label']} | {display_ft}]\n"
            f"  매출 ({_fmt_yoy(y.get('rev_yoy'))})  영업 ({_fmt_yoy(y.get('op_yoy'))}){흑전}"
        )

    MAX_LEN = 3800
    chunks, current = [], header
    for line in lines:
        if len(current) + len(line) > MAX_LEN:
            chunks.append(current); current = line
        else:
            current += line
    chunks.append(current)

    ok = True
    for i, chunk in enumerate(chunks):
        if i > 0: time.sleep(0.5)
        ok = ok and _send_telegram(chunk)
    return ok


# ─────────────────────────────────────────────────────────────
# 메인 스크리너
# ─────────────────────────────────────────────────────────────

def run_us_screener(
    start_date:  str,
    end_date:    str,
    min_rev_yoy: float = 15.0,
    min_op_yoy:  float = 20.0,
    min_op_abs:  float = 0,
    update_md:   bool  = True,
    require_filter: bool = True,
) -> tuple[list[dict], list[dict]]:
    _load_db()

    cik_map = _build_cik_map()
    if not cik_map:
        print("[오류] us_companies.json 없음.")
        return [], []

    # 1. EFTS 쿼리: 10-Q/10-K + 8-K(Item 2.02) 동시 조회
    print(f"[EFTS 조회] {start_date} ~ {end_date}")
    filings_10q  = _query_10q_10k(start_date, end_date)
    filings_8k   = _query_8k_earnings(start_date, end_date)
    print(f"  → 10-Q/10-K: {len(filings_10q)}건 / 8-K(2.02): {len(filings_8k)}건")

    # 2. 유니버스 교차 검증 — 10-Q/10-K 우선, 8-K는 보완
    #    같은 기업이 둘 다 있으면 10-Q/10-K 우선(정식 보고서)
    matched: dict[str, dict] = {}  # ticker → {filing_info, source}

    for f in filings_8k:
        cik = f["cik"]
        if cik not in cik_map:
            continue
        ticker = cik_map[cik]["ticker"]
        if ticker not in matched or f.get("filing_date","") > matched[ticker].get("filing_date",""):
            matched[ticker] = {**f, "company_info": cik_map[cik], "source": "8-K"}

    for f in filings_10q:
        cik = f["cik"]
        if cik not in cik_map:
            continue
        ticker = cik_map[cik]["ticker"]
        # 10-Q/10-K가 있으면 항상 덮어씀
        if ticker not in matched or matched[ticker].get("source") == "8-K" \
                or f.get("filing_date","") > matched[ticker].get("filing_date",""):
            matched[ticker] = {**f, "company_info": cik_map[cik], "source": f["form_type"]}

    n_10q = sum(1 for v in matched.values() if v["source"] != "8-K")
    n_8k  = sum(1 for v in matched.values() if v["source"] == "8-K")
    print(f"  → 유니버스 내 기업: {len(matched)}개 (10-Q/10-K: {n_10q}개 / 8-K전용: {n_8k}개)")

    if not matched:
        print("스크리닝 대상 없음.")
        return [], []

    meta = {}
    if os.path.exists(META_PATH):
        with open(META_PATH, encoding="utf-8") as f:
            meta = json.load(f)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(US_MD_DIR, exist_ok=True)

    all_passed  = []
    new_results = []
    total       = len(matched)

    print(f"\n{'='*60}")
    print(f"  US 실적 스크리닝 | 대상: {total}개 기업")
    print(f"  필터: 매출YoY≥{min_rev_yoy:+.0f}%  영업이익YoY≥{min_op_yoy:+.0f}%")
    print(f"{'='*60}\n")

    for idx, (ticker, fi) in enumerate(sorted(matched.items()), 1):
        company_info = fi["company_info"]
        source       = fi["source"]       # "8-K" / "10-Q" / "10-K"
        form_type    = fi["form_type"]
        filing_date  = fi.get("filing_date", "")
        period       = fi.get("period_of_report", "")
        cik_str      = fi["cik"]

        print(f"[{idx}/{total}] {ticker} | {source} | {filing_date}", flush=True)

        # 이미 DB에 등록된 (ticker, 분기)면 SEC/yfinance 재수집 없이 기존 결과 재사용.
        # 신고된 분기 실적은 사후 정정(restatement) 아니면 바뀌지 않으므로 안전한 스킵.
        early_q = _period_to_quarter(period)
        early_key = _db_key(ticker, early_q) if early_q else None
        if early_key and early_key in _us_earnings_db:
            e = _us_earnings_db[early_key]
            all_passed.append(e)
            print(f"  ↷ 스킵 [이미 처리됨: {early_q}]", flush=True)
            continue

        try:
            if source == "8-K":
                # ── 8-K 경로: yfinance로 최신 분기 병합 ──
                entry = _yfinance_merge(ticker, company_info)
                if not entry:
                    continue

                if update_md:
                    # 병합된 캐시(fetched_at=오늘)로 fetch_quarterly(refresh=False) → MD 생성
                    md = fetch_quarterly(ticker, years=10, refresh=False,
                                         company_info=company_info)
                    fpath = os.path.join(US_MD_DIR, f"{ticker}_분기실적.md")
                    with open(fpath, "w", encoding="utf-8") as f:
                        f.write(md)
                    meta[ticker] = {
                        "processed_at": date.today().isoformat(),
                        "name":         entry.get("company_name", ticker),
                        "file":         fpath,
                    }
                    with open(META_PATH, "w", encoding="utf-8") as f:
                        json.dump(meta, f, ensure_ascii=False, indent=2)
                    print(f"  → MD 저장: {ticker}_분기실적.md", flush=True)

            else:
                # ── 10-Q/10-K 경로: SEC XBRL 강제 갱신 ──
                md = fetch_quarterly(ticker, years=10, refresh=True,
                                     company_info=company_info)
                with open(SEC_CACHE_PATH, encoding="utf-8") as f:
                    entry = json.load(f).get(ticker, {})

                if update_md:
                    fpath = os.path.join(US_MD_DIR, f"{ticker}_분기실적.md")
                    with open(fpath, "w", encoding="utf-8") as f:
                        f.write(md)
                    meta[ticker] = {
                        "processed_at": date.today().isoformat(),
                        "name":         entry.get("company_name", ticker),
                        "file":         fpath,
                    }
                    with open(META_PATH, "w", encoding="utf-8") as f:
                        json.dump(meta, f, ensure_ascii=False, indent=2)
                    print(f"  → MD 저장: {ticker}_분기실적.md", flush=True)

            currency = entry.get("currency", "USD")
            target_q = _period_to_quarter(period)
            yoy = _get_latest_quarter_yoy(entry, target_q)
            if not yoy:
                print(f"  → YoY 계산 불가, 스킵", flush=True)
                continue

            passes_numeric = _passes_filter(yoy, min_rev_yoy, min_op_yoy, min_op_abs)
            ok  = passes_numeric or not require_filter
            qk  = yoy.get("latest_q", "")
            key = _db_key(ticker, qk)
            already = key in _us_earnings_db

            if ok:
                # 첨부물(EX-31 등)이 form_type에 나오는 경우 10-Q로 정규화
                display_form = form_type if form_type in ("10-Q", "10-K", "8-K") else "10-Q"
                e = {
                    "ticker":        ticker,
                    "company_name":  entry.get("company_name", company_info.get("name", ticker)),
                    "cik":           cik_str,
                    "quarter_label": qk,
                    "form_type":     display_form,
                    "filing_date":   filing_date,
                    "added_at":      date.today().isoformat(),
                    "currency":      currency,
                    "sector":        company_info.get("sector", ""),
                    "yoy":           yoy,
                }
                all_passed.append(e)
                tag = "통과" if passes_numeric else "필터무시"
                if not already:
                    _us_earnings_db[key] = e
                    new_results.append(e)
                    print(f"  ✓ {tag} [신규] | 매출YoY={_fmt_yoy(yoy.get('rev_yoy'))} 영업이익YoY={_fmt_yoy(yoy.get('op_yoy'))}", flush=True)
                else:
                    print(f"  ✓ {tag} [기존] | 매출YoY={_fmt_yoy(yoy.get('rev_yoy'))} 영업이익YoY={_fmt_yoy(yoy.get('op_yoy'))}", flush=True)
            else:
                print(f"  ✗ 미달 | 매출YoY={_fmt_yoy(yoy.get('rev_yoy'))} 영업이익YoY={_fmt_yoy(yoy.get('op_yoy'))}", flush=True)

        except AVRateLimitError:
            print(f"  [AV Rate Limit] 오늘 AV 한도 소진, 스킵.", flush=True)
        except Exception as e:
            print(f"  [오류] {ticker}: {e}", flush=True)

        time.sleep(0.5)

    if new_results or all_passed:
        _save_db()

    print(f"\n[US 스크리닝 완료] 업데이트 {total}개 | 통과 {len(all_passed)}개 (신규 {len(new_results)}개)")
    return new_results, all_passed


# ─────────────────────────────────────────────────────────────
# 엔트리포인트
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="미국 상장기업 실적 스크리너")
    parser.add_argument("--from",        dest="start_date", default=None,
                        help="조회 시작일 YYYYMMDD (기본: 오늘 -14일)")
    parser.add_argument("--to",          dest="end_date",   default=None,
                        help="조회 종료일 YYYYMMDD (기본: 오늘)")
    parser.add_argument("--min-rev-yoy", type=float, default=15.0)
    parser.add_argument("--min-op-yoy",  type=float, default=20.0)
    parser.add_argument("--min-op-abs",  type=float, default=0,
                        help="최소 영업이익 USD raw (기본 0, 제한 없음)")
    parser.add_argument("--telegram",    action="store_true",
                        help="신규 통과 기업 텔레그램 발송")
    parser.add_argument("--no-update",   action="store_true",
                        help="MD 파일 업데이트 없이 스크리닝만")
    args = parser.parse_args()

    today     = date.today()
    raw_end   = args.end_date   or today.strftime("%Y%m%d")
    raw_start = args.start_date or (today - timedelta(days=14)).strftime("%Y%m%d")

    start_date = f"{raw_start[:4]}-{raw_start[4:6]}-{raw_start[6:]}"
    end_date   = f"{raw_end[:4]}-{raw_end[4:6]}-{raw_end[6:]}"

    print(f"[US 실적 스크리너] 기간: {start_date} ~ {end_date}")
    print(f"  필터: 매출YoY ≥ {args.min_rev_yoy:+.0f}%  영업이익YoY ≥ {args.min_op_yoy:+.0f}%")
    print()

    new_results, all_passed = run_us_screener(
        start_date  = start_date,
        end_date    = end_date,
        min_rev_yoy = args.min_rev_yoy,
        min_op_yoy  = args.min_op_yoy,
        min_op_abs  = args.min_op_abs,
        update_md   = not args.no_update,
    )

    # 분기별 누적 MD 재생성
    affected_quarters = {e["quarter_label"] for e in all_passed if e.get("quarter_label")}
    for qk in sorted(affected_quarters):
        md_content = build_quarter_md(qk)
        if md_content:
            fname = f"US_{qk}_실적스크리닝.md"
            fpath = os.path.join(OUTPUT_DIR, fname)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(md_content)
            print(f"  → 누적 MD: {fname}")

    if args.telegram and new_results:
        print(f"\n[텔레그램 발송] {len(new_results)}개 기업...")
        send_us_screening_result(
            new_results, start_date, end_date,
            args.min_rev_yoy, args.min_op_yoy,
        )

    print("\n완료.")


if __name__ == "__main__":
    main()
