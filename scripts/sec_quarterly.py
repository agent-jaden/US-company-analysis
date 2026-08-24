"""
sec_quarterly.py — 미국 상장기업 분기 실적 히스토리 (SEC EDGAR XBRL)
=====================================================================
SEC EDGAR XBRL API를 통해 미국 상장기업의 분기 재무 데이터를 수집하고
마크다운 표로 출력한다.

분기 레이블은 캘린더 기준(Q1=1~3월, Q2=4~6월, Q3=7~9월, Q4=10~12월).
회계연도가 다른 기업(Apple=9월말 등)도 달력 분기로 표시.
Q4 역산: 연간(10-K) - 9개월누적(10-Q) = 4번째 분기, start_date 매칭.

사용법:
    python sec_quarterly.py <TICKER>           # 기본 10년치
    python sec_quarterly.py <TICKER> --years 5
    python sec_quarterly.py <TICKER> --save    # reports_us_quarterly/ 에 MD 저장
"""

import os
import sys
import re
import json
import argparse
import time
from datetime import date, datetime, timedelta
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports_us_quarterly")
CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "us_quarterly_cache.json")
CACHE_TTL_DAYS = 7


def _load_cache() -> dict:
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_cache(cache: dict) -> None:
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def _cache_fresh(entry: dict) -> bool:
    fetched = entry.get("fetched_at", "")
    if not fetched:
        return False
    return (date.today() - date.fromisoformat(fetched)).days < CACHE_TTL_DAYS

SEC_HEADERS = {
    "User-Agent": "DART-analyzer-bot contact@example.com",
    "Accept": "application/json",
}

REVENUE_KEYS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
    "Revenue",
]
OPINCOME_KEYS = [
    "OperatingIncomeLoss",
]
NETINCOME_KEYS = [
    "NetIncomeLoss",
    "NetIncomeLossAvailableToCommonStockholdersBasic",
    "ProfitLoss",
]

# IFRS-full 네임스페이스 키 (외국 기업)
IFRS_REVENUE_KEYS = [
    "Revenue",
    "RevenueFromContractsWithCustomers",
    "SalesRevenueGoodsGross",
]
IFRS_OPINCOME_KEYS = [
    "ProfitLossFromOperatingActivities",
    "OperatingProfit",
]
IFRS_NETINCOME_KEYS = [
    "ProfitLoss",
    "ProfitLossAttributableToOwnersOfParent",
]

CURRENCY_SYMBOLS = {
    "USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥",
    "CAD": "CA$", "CHF": "CHF", "AUD": "A$", "KRW": "₩",
    "TWD": "NT$", "CNY": "¥", "HKD": "HK$", "INR": "₹",
    "BRL": "R$", "MXN": "MX$", "SEK": "kr", "NOK": "kr",
    "DKK": "kr", "SGD": "S$",
}

# Alpha Vantage가 통화를 "None"으로 반환하는 ADR 티커 수동 보정
# 새 ADR 추가 시 여기에 티커: 통화코드 형식으로 입력
CURRENCY_OVERRIDES: dict[str, str] = {
    # 대만
    "TSM": "TWD",
    # 중국
    "BABA": "CNY", "BIDU": "CNY", "JD": "CNY", "PDD": "CNY",
    "NIO": "CNY", "XPEV": "CNY", "LI": "CNY", "BEKE": "CNY",
    # 일본
    "TM": "JPY", "HMC": "JPY", "SONY": "JPY", "NTDOY": "JPY",
    "MUFG": "JPY", "SMFG": "JPY", "MFG": "JPY",
    # 한국
    "KB": "KRW", "SHG": "KRW", "PKX": "KRW",
    # 유럽
    "ASML": "EUR", "SAP": "EUR", "NVO": "DKK",
    "SHEL": "GBP", "BP": "GBP", "AZN": "GBP", "GSK": "GBP",
    "RIO": "GBP", "HSBC": "GBP",
    # 캐나다
    "RY": "CAD", "TD": "CAD", "BMO": "CAD", "BNS": "CAD", "CM": "CAD",
    # 브라질
    "VALE": "BRL", "ITUB": "BRL",
}


# ──────────────────────────────────────────────
# HTTP
# ──────────────────────────────────────────────

def _get_json(url: str, retries: int = 3) -> dict:
    req = Request(url, headers=SEC_HEADERS)
    for attempt in range(retries):
        try:
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            if e.code == 429:
                wait = 2 ** attempt
                print(f"  [429] {wait}초 대기...", file=sys.stderr)
                time.sleep(wait)
            else:
                raise
        except URLError:
            if attempt < retries - 1:
                time.sleep(2)
            else:
                raise
    raise RuntimeError(f"Failed to fetch: {url}")


# ──────────────────────────────────────────────
# CIK 조회
# ──────────────────────────────────────────────

def get_cik(ticker: str) -> str:
    data = _get_json("https://www.sec.gov/files/company_tickers.json")
    upper = ticker.upper()
    for item in data.values():
        if item.get("ticker", "").upper() == upper:
            return str(item["cik_str"]).zfill(10)
    raise ValueError(f"티커 '{ticker}' 를 SEC EDGAR에서 찾을 수 없습니다.")


# ──────────────────────────────────────────────
# 회사 정보
# ──────────────────────────────────────────────

def get_company_info(cik: str) -> dict:
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    data = _get_json(url)
    name = data.get("name", "")
    fy_end_str = data.get("fiscalYearEnd", "1231")
    try:
        fy_month = int(fy_end_str[:2])
    except Exception:
        fy_month = 12
    ticker = data.get("tickers", [""])[0] if data.get("tickers") else ""
    return {"name": name, "fy_month": fy_month, "ticker": ticker}


# ──────────────────────────────────────────────
# 유틸리티
# ──────────────────────────────────────────────

def _days(start: str, end: str) -> int:
    try:
        return (datetime.strptime(end, "%Y-%m-%d") - datetime.strptime(start, "%Y-%m-%d")).days
    except Exception:
        return 0


def _cal_quarter(end_date: str) -> str:
    """캘린더 기준 분기 레이블: '2025Q3' 형태.
    종료월이 분기 첫째 달(1·4·7·10월)이면 이전 분기로 귀속.
    (예: Feb-Apr → 종료 Apr → 2/3이 Q1 활동 → Q1으로 레이블)
    """
    from datetime import timedelta
    try:
        d = datetime.strptime(end_date, "%Y-%m-%d")
    except Exception:
        return end_date
    if d.month in (1, 4, 7, 10):
        d = d - timedelta(days=1)
    q = (d.month - 1) // 3 + 1
    return f"{d.year}Q{q}"


def _detect_currency(facts_ns: dict, keys: list[str]) -> str:
    """facts 네임스페이스에서 사용 통화 감지."""
    for key in keys:
        if key not in facts_ns:
            continue
        units = facts_ns[key].get("units", {})
        if units.get("USD"):
            return "USD"
        for cur, items in units.items():
            if items:
                return cur
    return "USD"


def _pick_metric(facts_ns: dict, keys: list[str]) -> list[dict]:
    """후보 키들의 항목을 모두 병합. USD 우선, 없으면 첫 번째 통화 사용."""
    seen: set[str] = set()
    result: list[dict] = []
    for key in keys:
        if key not in facts_ns:
            continue
        units = facts_ns[key].get("units", {})
        # USD 우선, 없으면 첫 번째 통화 fallback
        items = units.get("USD") or (next(iter(units.values())) if units else [])
        for it in items:
            dedup_key = f"{it.get('start','')}_{it.get('end','')}_{it.get('form','')}"
            if dedup_key not in seen:
                seen.add(dedup_key)
                result.append(it)
    return result


# ──────────────────────────────────────────────
# 파싱
# ──────────────────────────────────────────────

def _fmt_period(start: str, end: str) -> str:
    """'2025-10-28' ~ '2026-01-26' → '11월-1월' (3개월 표시)
    시작일이 21일 이후면 다음 달을 실질적 시작월로 표시.
    """
    try:
        from datetime import timedelta
        s = datetime.strptime(start, "%Y-%m-%d")
        e = datetime.strptime(end, "%Y-%m-%d")
        if s.day > 20:
            s = (s.replace(day=1) + timedelta(days=32)).replace(day=1)
        return f"{s.month}월-{e.month}월"
    except Exception:
        return ""


def parse_quarterly(items: list[dict], min_year: int) -> tuple[dict, dict]:
    """독립 3개월 항목 → ({분기: val}, {분기: period_str})."""
    result: dict[str, float] = {}
    periods: dict[str, str] = {}
    for it in items:
        start = it.get("start", "")
        end = it.get("end", "")
        val = it.get("val")
        form = it.get("form", "")
        if val is None or not start or not end:
            continue
        if int(end[:4]) < min_year:
            continue
        if not (78 <= _days(start, end) <= 103):
            continue
        label = _cal_quarter(end)
        if label not in result or "A" not in form:
            result[label] = val
            periods[label] = _fmt_period(start, end)
    return result, periods


def parse_annual(items: list[dict], min_year: int) -> list[tuple[str, str, float]]:
    """연간(10-K) 항목 → [(start, end, val)] 리스트."""
    seen: set[str] = set()
    result = []
    for it in items:
        start = it.get("start", "")
        end = it.get("end", "")
        val = it.get("val")
        if val is None or not start or not end:
            continue
        if int(end[:4]) < min_year:
            continue
        if not (340 <= _days(start, end) <= 385):
            continue
        key = f"{start}_{end}"
        if key not in seen:
            seen.add(key)
            result.append((start, end, val))
    return result


def parse_9m(items: list[dict], min_year: int) -> tuple[dict, dict]:
    """9개월 누적 항목 → ({start_date: val}, {start_date: end_date})."""
    result: dict[str, float] = {}
    ends: dict[str, str] = {}
    for it in items:
        start = it.get("start", "")
        end = it.get("end", "")
        val = it.get("val")
        if val is None or not start or not end:
            continue
        if int(end[:4]) < min_year - 1:
            continue
        if not (265 <= _days(start, end) <= 290):
            continue
        if start not in result:
            result[start] = val
            ends[start] = end
    return result, ends


def derive_q4(
    quarterly: dict[str, float],
    periods: dict[str, str],
    annual: list[tuple[str, str, float]],
    nine_month: dict[str, float],
    nine_month_ends: dict[str, str],
) -> tuple[dict, dict]:
    """연간 - 9M = Q4. start_date 매칭으로 정확도 확보. 기간도 계산."""
    result = dict(quarterly)
    result_periods = dict(periods)
    for (start, end, annual_val) in annual:
        label = _cal_quarter(end)
        if label in result:
            continue
        if start in nine_month:
            result[label] = annual_val - nine_month[start]
            # Q4 기간: 9M 종료일 다음날 ~ 연간 종료일
            try:
                from datetime import timedelta
                q4_start = datetime.strptime(nine_month_ends[start], "%Y-%m-%d") + timedelta(days=1)
                result_periods[label] = _fmt_period(q4_start.strftime("%Y-%m-%d"), end)
            except Exception:
                result_periods[label] = ""
    return result, result_periods


def _extract_all(us_gaap: dict, keys: list[str], min_year: int) -> tuple[dict, dict, list]:
    items = _pick_metric(us_gaap, keys)
    q, qp = parse_quarterly(items, min_year)
    a = parse_annual(items, min_year)
    m9, m9_ends = parse_9m(items, min_year)
    vals, periods = derive_q4(q, qp, a, m9, m9_ends)
    return vals, periods, a  # annual 리스트도 반환


def _fy_month_from_annual(annual: list) -> int | None:
    """실제 10-K end 날짜들의 최빈 month → 실제 결산월 추출."""
    from collections import Counter
    months = []
    for (_, end, _) in annual:
        try:
            months.append(datetime.strptime(end, "%Y-%m-%d").month)
        except Exception:
            pass
    if not months:
        return None
    return Counter(months).most_common(1)[0][0]


# ──────────────────────────────────────────────
# 정렬
# ──────────────────────────────────────────────

def _sort_key(label: str) -> tuple:
    m = re.match(r"(\d{4})Q(\d)", label)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _prev_year(label: str) -> str:
    m = re.match(r"(\d{4})(Q\d)", label)
    return f"{int(m.group(1))-1}{m.group(2)}" if m else ""


# ──────────────────────────────────────────────
# 포맷
# ──────────────────────────────────────────────

def _choose_unit(values: list, currency: str = "USD") -> tuple[str, float]:
    sym = CURRENCY_SYMBOLS.get(currency, currency)
    max_v = max((abs(v) for v in values if v is not None), default=0)
    if max_v >= 1_000_000_000:
        return f"B{sym}", 1_000_000_000
    if max_v >= 1_000_000:
        return f"M{sym}", 1_000_000
    return sym, 1


def _fmt(val, divisor, decimals=2):
    if val is None:
        return "—"
    v = val / divisor
    return f"{v:,.{decimals}f}"


def _pct(num, den):
    if num is None or den is None or den == 0:
        return "—"
    return f"{num/den*100:.1f}%"


def _yoy(cur, prev):
    if cur is None or prev is None or prev == 0:
        return "—"
    if prev < 0 and cur >= 0:
        return "흑자전환"
    if prev >= 0 and cur < 0:
        return "적자전환"
    pct = (cur - prev) / abs(prev) * 100
    if prev < 0 and cur < 0:
        return "적자축소" if pct < 0 else "적자확대"
    return f"{pct:+.1f}%"


MONTH_KO = {1:"1월",2:"2월",3:"3월",4:"4월",5:"5월",6:"6월",
            7:"7월",8:"8월",9:"9월",10:"10월",11:"11월",12:"12월"}


# ──────────────────────────────────────────────
# Alpha Vantage / yfinance fallback
# ──────────────────────────────────────────────

_av_call_index = 0  # 라운드로빈 인덱스


class AVRateLimitError(Exception):
    """Alpha Vantage 일일 rate limit 도달."""
    pass


def _get_alphavantage_api_keys() -> list[str]:
    try:
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import main as _main
        keys = []
        for attr in ("ALPHA_VANTAGE_API_KEY", "ALPHA_VANTAGE_API_KEY_2",
                     "ALPHA_VANTAGE_API_KEY_3", "ALPHA_VANTAGE_API_KEY_4"):
            k = getattr(_main, attr, "")
            if k:
                keys.append(k)
        return keys
    except Exception:
        return []


def _get_alphavantage_api_key() -> str:
    """하위 호환용 단일 키 반환 (첫 번째 키)."""
    keys = _get_alphavantage_api_keys()
    return keys[0] if keys else ""


def _safe_float(v) -> float | None:
    if v is None or str(v).strip() in ("None", "", "N/A"):
        return None
    try:
        return float(v)
    except Exception:
        return None


def _fetch_from_alphavantage(ticker: str, min_year: int) -> tuple[dict, dict, dict, dict, str]:
    """Alpha Vantage INCOME_STATEMENT → (rev, opi, net, periods, currency). 5년+ 분기."""
    global _av_call_index
    keys = _get_alphavantage_api_keys()
    if not keys:
        return {}, {}, {}, {}, "USD"

    # 모든 키를 순환하여 rate limit인 키는 건너뜀
    tried = 0
    last_err = None
    while tried < len(keys):
        api_key = keys[_av_call_index % len(keys)]
        _av_call_index += 1
        tried += 1
        try:
            url = (f"https://www.alphavantage.co/query"
                   f"?function=INCOME_STATEMENT&symbol={ticker}&apikey={api_key}")
            data = _get_json(url)
            # rate limit 감지 (Note 또는 Information 키)
            if "Note" in data or "Information" in data:
                last_err = (data.get("Note") or data.get("Information") or "rate limit")[:60]
                continue  # 다음 키 시도
            quarterly = data.get("quarterlyReports", [])
            if not quarterly:
                return {}, {}, {}, {}, "USD"

            raw_cur = quarterly[0].get("reportedCurrency", "") if quarterly else ""
            currency = raw_cur if raw_cur and raw_cur != "None" else "USD"
            rev, opi, net, periods = {}, {}, {}, {}

            for item in quarterly:
                end_str = item.get("fiscalDateEnding", "")
                if not end_str or int(end_str[:4]) < min_year:
                    continue
                label = _cal_quarter(end_str)
                if label in rev:
                    continue
                rv = _safe_float(item.get("totalRevenue"))
                op = _safe_float(item.get("operatingIncome"))
                nt = _safe_float(item.get("netIncome"))
                if rv is not None: rev[label] = rv
                if op is not None: opi[label] = op
                if nt is not None: net[label] = nt

            return rev, opi, net, periods, currency
        except AVRateLimitError as e:
            last_err = str(e)
            continue  # 다음 키 시도
        except Exception:
            return {}, {}, {}, {}, "USD"

    # 모든 키 소진
    raise AVRateLimitError(last_err or "all keys rate limited")


def _fetch_from_yfinance(ticker: str, min_year: int) -> tuple[dict, dict, dict, dict, str]:
    """yfinance quarterly_income_stmt → (rev, opi, net, periods, currency)"""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        df = t.quarterly_income_stmt
        if df is None or df.empty:
            return {}, {}, {}, {}, "USD"

        currency = getattr(t.fast_info, "currency", "USD") or "USD"

        REV_ROWS = ["Total Revenue", "Revenue"]
        OPI_ROWS = ["Operating Income", "EBIT", "Operating Revenue"]
        NET_ROWS = ["Net Income", "Net Income Common Stockholders",
                    "Net Income From Continuing Operations"]

        rev, opi, net, periods = {}, {}, {}, {}
        for col in df.columns:
            end_str = col.strftime("%Y-%m-%d")
            if int(end_str[:4]) < min_year:
                continue
            label = _cal_quarter(end_str)

            for row in REV_ROWS:
                if row in df.index:
                    v = df.loc[row, col]
                    if v is not None and str(v) not in ("nan", "None"):
                        rev[label] = float(v)
                        break

            for row in OPI_ROWS:
                if row in df.index:
                    v = df.loc[row, col]
                    if v is not None and str(v) not in ("nan", "None"):
                        opi[label] = float(v)
                        break

            for row in NET_ROWS:
                if row in df.index:
                    v = df.loc[row, col]
                    if v is not None and str(v) not in ("nan", "None"):
                        net[label] = float(v)
                        break

        return rev, opi, net, periods, currency
    except Exception:
        return {}, {}, {}, {}, "USD"


# ──────────────────────────────────────────────
# 밸류에이션
# ──────────────────────────────────────────────

def _fetch_valuation(ticker: str) -> dict:
    """yfinance에서 밸류에이션 지표 조회. 실패 시 빈 dict 반환."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        dy = info.get("trailingAnnualDividendYield") or info.get("dividendYield")
        return {
            "trailing_pe":    info.get("trailingPE"),
            "forward_pe":     info.get("forwardPE"),
            "price_to_book":  info.get("priceToBook"),
            "ev_ebitda":      info.get("enterpriseToEbitda"),
            "dividend_yield": dy,
        }
    except Exception:
        return {}


# ──────────────────────────────────────────────
# 마크다운
# ──────────────────────────────────────────────

def build_markdown(
    ticker: str,
    company_name: str,
    labels: list[str],
    revenue: dict,
    op_income: dict,
    net_income: dict,
    fy_month: int,
    years: int,
    periods: dict = None,
    company_info: dict = None,
    currency: str = "USD",
    source: str = "SEC",
) -> str:
    today_str = date.today().strftime("%Y-%m-%d")
    fy_note = f"결산월: {MONTH_KO.get(fy_month, str(fy_month)+'월')}"
    source_note = {"yfinance": "yfinance", "AlphaVantage": "Alpha Vantage"}.get(source, "SEC EDGAR XBRL")

    rev_vals = [revenue[l] for l in labels if l in revenue]
    unit_str, divisor = _choose_unit(rev_vals, currency)
    decs = 2 if unit_str == "B$" else 0

    lines = [
        f"# {company_name} ({ticker}) 분기 실적 히스토리",
        "",
        f"> 생성일: {today_str} | 출처: {source_note} | {fy_note} | 분기 기준: 캘린더 | 통화: {currency}",
        "",
    ]

    # ── 기업 개요 (A+B) ──
    if company_info:
        sector   = company_info.get("sector", "")
        industry = company_info.get("industry", "")
        mktcap   = company_info.get("market_cap")
        summary    = company_info.get("summary", "")
        summary_ko = company_info.get("summary_ko", "")

        # B: 구조화 한 줄
        meta_parts = []
        if sector:   meta_parts.append(sector)
        if industry: meta_parts.append(industry)
        rank = company_info.get("rank")
        if mktcap:
            rank_str = f" (#{rank}위)" if rank else ""
            meta_parts.append(f"시총 {mktcap/1e9:.0f}B${rank_str}")
        meta_parts.append(fy_note)
        lines.append(f"**{' | '.join(meta_parts)}**")
        lines.append("")

        # ── 밸류에이션 ──
        val = _fetch_valuation(ticker)
        val_rows = []
        if val.get("trailing_pe")    is not None: val_rows.append(f"| P/E (Trailing) | {val['trailing_pe']:.1f}x |")
        if val.get("forward_pe")     is not None: val_rows.append(f"| P/E (Forward)  | {val['forward_pe']:.1f}x |")
        if val.get("price_to_book")  is not None: val_rows.append(f"| P/B            | {val['price_to_book']:.1f}x |")
        if val.get("ev_ebitda")      is not None: val_rows.append(f"| EV/EBITDA      | {val['ev_ebitda']:.1f}x |")
        if val.get("dividend_yield") is not None: val_rows.append(f"| 배당수익률     | {val['dividend_yield']*100:.2f}% |")
        if val_rows:
            lines.append("| 지표 | 값 |")
            lines.append("|:-----|----:|")
            lines.extend(val_rows)
            lines.append("")

        # A: 사업 설명 (한국어 먼저, 있으면 영문도 병기)
        if summary_ko:
            lines.append(summary_ko)
            if summary:
                lines.append("")
                lines.append(f"*{summary}*")
            lines.append("")
        elif summary:
            lines.append(summary)
            lines.append("")

        lines.append("---")
        lines.append("")

    # ── 분기별 실적 추이 (최근 3년) ──
    recent_year = int(labels[-1][:4]) if labels else date.today().year
    cutoff_year = recent_year - 2
    recent_labels = [l for l in labels if int(l[:4]) >= cutoff_year]
    recent_labels_desc = list(reversed(recent_labels))

    lines += [
        "### 분기별 실적 추이",
        "",
        f"| 분기 | 매출({unit_str}) | 영업이익({unit_str}) | 순이익({unit_str}) | 매출 YoY | 영업이익 YoY | 순이익 YoY |",
        "|:----:|-----------:|-------------:|-----------:|:--------:|:------------:|:----------:|",
    ]

    for label in recent_labels_desc:
        rev = revenue.get(label)
        opi = op_income.get(label)
        net = net_income.get(label)
        prev = _prev_year(label)
        period = (periods or {}).get(label, "")
        label_str = f"{label} ({period})" if period else label
        lines.append(
            f"| {label_str} "
            f"| {_fmt(rev, divisor, decs)} "
            f"| {_fmt(opi, divisor, decs)} "
            f"| {_fmt(net, divisor, decs)} "
            f"| {_yoy(rev, revenue.get(prev))} "
            f"| {_yoy(opi, op_income.get(prev))} "
            f"| {_yoy(net, net_income.get(prev))} |"
        )

    # ── 요약 ──
    recent_rev = [(l, revenue[l]) for l in recent_labels if l in revenue]
    recent_opi = [(l, op_income[l]) for l in recent_labels if l in op_income]
    total_rev = sum(v for _, v in recent_rev)
    total_opi = sum(v for _, v in recent_opi)
    avg_margin = total_opi / total_rev * 100 if total_rev else 0

    lines += ["", "#### 요약", "", "| 항목 | 값 |", "|------|-----|"]
    lines.append(f"| 수록 분기 | {len(recent_labels)}개 |")
    if recent_opi:
        best_o = max(recent_opi, key=lambda x: x[1])
        worst_o = min(recent_opi, key=lambda x: x[1])
        lines.append(f"| 최고 영업이익 | {best_o[0]} ({_fmt(best_o[1], divisor, decs)} {unit_str}) |")
        lines.append(f"| 최저 영업이익 | {worst_o[0]} ({_fmt(worst_o[1], divisor, decs)} {unit_str}) |")
    lines.append(f"| 기간 합산 매출 | {_fmt(total_rev, divisor, decs)} {unit_str} |")
    lines.append(f"| 기간 합산 영업이익 | {_fmt(total_opi, divisor, decs)} {unit_str} |")
    lines.append(f"| 기간 평균 영업이익률 | {avg_margin:.1f}% |")

    # ── 연간 실적 추이 (10년) ──
    lines += [
        "",
        "### 연간 실적 추이 (10년)",
        "",
        f"| 연도 | 매출({unit_str}) | 영업이익({unit_str}) | 순이익({unit_str}) | 매출 YoY | 영업이익 YoY | 순이익 YoY |",
        "|:----:|-----------:|-------------:|-----------:|:--------:|:------------:|:----------:|",
    ]

    annual_rev, annual_opi, annual_net = {}, {}, {}
    for yr in sorted({int(l[:4]) for l in labels if re.match(r"\d{4}Q\d", l)}):
        yr_labels = [f"{yr}Q{q}" for q in range(1, 5)]
        if all(l in revenue for l in yr_labels):
            annual_rev[yr] = sum(revenue.get(l, 0) or 0 for l in yr_labels)
            annual_opi[yr] = sum(op_income.get(l, 0) or 0 for l in yr_labels)
            annual_net[yr] = sum(net_income.get(l, 0) or 0 for l in yr_labels)

    for yr in sorted(annual_rev.keys(), reverse=True)[:10]:
        prev_yr = yr - 1
        lines.append(
            f"| {yr} "
            f"| {_fmt(annual_rev[yr], divisor, decs)} "
            f"| {_fmt(annual_opi.get(yr), divisor, decs)} "
            f"| {_fmt(annual_net.get(yr), divisor, decs)} "
            f"| {_yoy(annual_rev.get(yr), annual_rev.get(prev_yr))} "
            f"| {_yoy(annual_opi.get(yr), annual_opi.get(prev_yr))} "
            f"| {_yoy(annual_net.get(yr), annual_net.get(prev_yr))} |"
        )

    return "\n".join(lines) + "\n"


# ──────────────────────────────────────────────
# 메인
# ──────────────────────────────────────────────

def fetch_quarterly(ticker: str, years: int = 10, refresh: bool = False,
                    company_info: dict = None, skip_fallback: bool = False) -> str:
    ticker = ticker.upper()
    print(f"\n{'='*60}", flush=True)
    print(f"  SEC 분기 실적: {ticker}", flush=True)
    print(f"{'='*60}\n", flush=True)

    cache = _load_cache()
    entry = cache.get(ticker, {})
    min_year = date.today().year - years

    # period 정보가 없는 구버전 캐시는 재수집 (단, yfinance/AV는 원래 period 없음 → 캐시 그대로 사용)
    src = entry.get("source", "SEC")
    has_periods = any(v.get("period") for v in entry.get("quarters", {}).values())
    cache_ok = has_periods or src in ("yfinance", "AlphaVantage")
    if not refresh and _cache_fresh(entry) and cache_ok:
        print(f"[캐시 HIT] {ticker} (fetched: {entry['fetched_at']})", flush=True)
        company_name = entry["company_name"]
        fy_month = entry["fy_month"]
        quarters = entry["quarters"]
        currency = entry.get("currency", "USD")
        source   = entry.get("source", "SEC")
        rev = {k: v["rev"] for k, v in quarters.items() if v.get("rev") is not None}
        opi = {k: v["opi"] for k, v in quarters.items() if v.get("opi") is not None}
        net = {k: v["net"] for k, v in quarters.items() if v.get("net") is not None}
        periods = {k: v.get("period", "") for k, v in quarters.items()}
        # CURRENCY_OVERRIDES 보정 (캐시에 잘못된 통화가 저장된 경우 포함)
        if ticker in CURRENCY_OVERRIDES:
            currency = CURRENCY_OVERRIDES[ticker]
    else:
        print("[1/4] CIK 조회...", flush=True)
        cik = get_cik(ticker)
        print(f"  → CIK: {cik}", flush=True)

        print("[2/4] 회사 정보 조회...", flush=True)
        info = get_company_info(cik)
        company_name = info["name"]
        fy_month = info["fy_month"]
        print(f"  → {company_name} | 결산월: {fy_month}월", flush=True)

        print("[3/4] XBRL 재무 데이터 다운로드...", flush=True)
        facts_data = _get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
        us_gaap = facts_data.get("facts", {}).get("us-gaap", {})
        ifrs    = facts_data.get("facts", {}).get("ifrs-full", {})

        print("[4/4] 분기 데이터 파싱...", flush=True)
        currency = "USD"
        source   = "SEC"

        if us_gaap:
            currency = _detect_currency(us_gaap, REVENUE_KEYS)
            rev, rev_periods, annual_data = _extract_all(us_gaap, REVENUE_KEYS, min_year)
            opi, _, _                     = _extract_all(us_gaap, OPINCOME_KEYS, min_year)
            net, _, _                     = _extract_all(us_gaap, NETINCOME_KEYS, min_year)
        elif ifrs:
            print("  → IFRS 네임스페이스 사용", flush=True)
            currency = _detect_currency(ifrs, IFRS_REVENUE_KEYS)
            rev, rev_periods, annual_data = _extract_all(ifrs, IFRS_REVENUE_KEYS, min_year)
            opi, _, _                     = _extract_all(ifrs, IFRS_OPINCOME_KEYS, min_year)
            net, _, _                     = _extract_all(ifrs, IFRS_NETINCOME_KEYS, min_year)
        else:
            raise ValueError("XBRL 데이터가 없습니다.")

        periods = rev_periods

        # 실제 10-K end 날짜로 fy_month 보정 (SEC 등록 날짜보다 정확)
        actual_fy = _fy_month_from_annual(annual_data)
        if actual_fy and actual_fy != fy_month:
            print(f"  → 결산월 보정: {fy_month}월 → {actual_fy}월 (실제 10-K 기준)", flush=True)
            fy_month = actual_fy

        # 분기 데이터 없으면 Alpha Vantage → yfinance 순으로 fallback
        test_labels = {l for l in (set(rev) | set(opi) | set(net)) if int(l[:4]) >= min_year}
        av_checked = False  # AV 호출 결과 확인 여부
        if not test_labels and skip_fallback:
            print("  → SEC 분기 데이터 없음. (skip_fallback=True, AV/yfinance 생략)", flush=True)

        if not test_labels and not skip_fallback:
            print("  → SEC 분기 데이터 없음. Alpha Vantage fallback 시도...", flush=True)
            # AVRateLimitError는 잡지 않고 상위로 전파
            rev, opi, net, periods, currency = _fetch_from_alphavantage(ticker, min_year)
            source = "AlphaVantage"
            test_labels = {l for l in (set(rev) | set(opi) | set(net)) if int(l[:4]) >= min_year}
            av_checked = True  # rate limit 없이 AV 응답 받음 (빈 결과 포함)

        if not test_labels and not skip_fallback:
            print("  → Alpha Vantage 데이터 없음. yfinance fallback 시도...", flush=True)
            rev, opi, net, periods, currency = _fetch_from_yfinance(ticker, min_year)
            source = "yfinance"

        # CURRENCY_OVERRIDES 보정 (Alpha Vantage가 "None" 반환하는 ADR)
        if ticker in CURRENCY_OVERRIDES:
            currency = CURRENCY_OVERRIDES[ticker]

        # 캐시 저장 (period, currency, source 포함)
        all_q_keys = set(rev) | set(opi) | set(net)
        quarters = {
            q: {"rev": rev.get(q), "opi": opi.get(q), "net": net.get(q),
                "period": periods.get(q, "")}
            for q in all_q_keys
        }
        cache[ticker] = {
            "cik": cik,
            "company_name": company_name,
            "fy_month": fy_month,
            "fetched_at": date.today().isoformat(),
            "currency": currency,
            "source": source,
            "av_checked": av_checked,
            "quarters": quarters,
        }
        _save_cache(cache)
        print(f"  → 캐시 저장 완료 ({CACHE_PATH})", flush=True)

    all_labels = sorted(
        {l for l in (set(rev) | set(opi) | set(net)) if int(l[:4]) >= min_year},
        key=_sort_key,
    )
    print(f"  → 분기 수: {len(all_labels)} "
          f"({all_labels[0] if all_labels else '?'} ~ {all_labels[-1] if all_labels else '?'})",
          flush=True)
    if not all_labels:
        raise ValueError("파싱된 분기 데이터가 없습니다.")

    return build_markdown(ticker, company_name, all_labels, rev, opi, net, fy_month, years,
                          periods, company_info, currency, source)


def main():
    parser = argparse.ArgumentParser(description="미국 상장기업 분기 실적 히스토리")
    parser.add_argument("ticker", help="티커 심볼 (예: AAPL, MSFT, TSLA)")
    parser.add_argument("--years", type=int, default=10, help="조회 연수 (기본 10)")
    parser.add_argument("--save", action="store_true", help="reports_us_quarterly/ 에 MD 파일 저장")
    parser.add_argument("--refresh", action="store_true", help="캐시 무시하고 SEC에서 재수집")
    args = parser.parse_args()

    ticker = args.ticker.upper()
    try:
        md = fetch_quarterly(ticker, args.years, refresh=args.refresh)
    except ValueError as e:
        print(f"\n오류: {e}", file=sys.stderr)
        sys.exit(1)

    if args.save:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        fpath = os.path.join(OUTPUT_DIR, f"{ticker}_분기실적.md")
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"\n  → 저장: {fpath}", flush=True)
    else:
        print("\n" + md)


if __name__ == "__main__":
    main()
