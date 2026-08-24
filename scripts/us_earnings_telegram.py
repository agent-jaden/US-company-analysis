"""
us_earnings_telegram.py — 미국 상장기업(us-gaap) 실적 상세 텔레그램 발송
=====================================================================
us_earnings_screener.py의 run_us_screener()를 재사용해 신규 실적 필링을 감지하고,
has_us_gaap=True 기업(us_companies.json)에 한해 기업별 상세 메시지를 발송한다.
(us_earnings_screener.py --telegram 의 배치 요약과는 별개 — 기업 1개당 메시지 1개)
YoY 필터 통과 여부와 무관하게 신규 필링이 감지된 기업 전체를 발송한다 (require_filter=False).

Usage:
    python us_earnings_telegram.py                        # 최근 14일
    python us_earnings_telegram.py --from 20260501 --to 20260507
    python us_earnings_telegram.py --dry-run               # 발송 없이 미리보기만 출력
    python us_earnings_telegram.py --no-update              # MD 업데이트 생략 (screener에 위임)
"""

import os
import sys
import json
import time
import argparse
import requests
from datetime import date, timedelta
from urllib.request import urlopen, Request

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

from us_earnings_screener import run_us_screener, _fmt_usd, _get_latest_quarter_yoy, SEC_HEADERS
from sec_quarterly import _load_cache as _sq_load_cache, _sort_key, fetch_quarterly
from config import TELEGRAM_BOT_TOKEN

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
COMPANIES_PATH = os.path.join(BASE_DIR, "us_companies.json")
TG_BASE        = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# 이 스크립트 전용 채널 (US 분기실적 알림채널) — main.py의 공용 채널과 분리
TELEGRAM_CHANNEL_ID = "-1004384910285"

FORM_LABELS = {"10-K": "연간보고서", "10-Q": "분기보고서", "8-K": "실적발표(8-K)"}

RECENT_N = 5  # 최근 실적 테이블 분기 수


# ─────────────────────────────────────────────────────────────
# 유니버스 (has_us_gaap 필터)
# ─────────────────────────────────────────────────────────────

def _load_us_gaap_map() -> dict:
    """ticker → company dict, has_us_gaap=True 인 기업만."""
    if not os.path.exists(COMPANIES_PATH):
        return {}
    with open(COMPANIES_PATH, encoding="utf-8") as f:
        data = json.load(f)
    return {c["ticker"]: c for c in data["companies"] if c.get("has_us_gaap")}


# ─────────────────────────────────────────────────────────────
# SEC 공시 원문 링크 (accession number 조회)
# ─────────────────────────────────────────────────────────────

def _get_accession_number(cik: str, form_type: str, filing_date: str) -> str | None:
    cik10 = str(int(cik)).zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik10}.json"
    try:
        req = Request(url, headers=SEC_HEADERS)
        data = json.loads(urlopen(req, timeout=15).read())
        recent = data.get("filings", {}).get("recent", {})
        forms = recent.get("form", [])
        dates = recent.get("filingDate", [])
        accns = recent.get("accessionNumber", [])
        for f, d, a in zip(forms, dates, accns):
            if d == filing_date and (f == form_type or f.startswith(form_type)):
                return a
    except Exception as e:
        print(f"    [accession 조회 실패] {e}")
    return None


def _generic_sec_url(cik: str, form_type: str) -> str:
    """기업별 필링 목록 페이지 (특정 공시 accession 조회 없이)."""
    ft = form_type if form_type in ("10-Q", "10-K", "8-K") else "10-Q"
    return (
        f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
        f"&CIK={int(cik)}&type={ft}&dateb=&owner=include&count=10"
    )


def _sec_filing_url(cik: str, form_type: str, filing_date: str) -> str:
    accn = _get_accession_number(cik, form_type, filing_date)
    if accn:
        cik_int = str(int(cik))
        accn_nodash = accn.replace("-", "")
        return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{accn_nodash}/{accn}-index.htm"
    # 정확한 accession 조회 실패 시 기업별 필링 목록 페이지로 대체
    return _generic_sec_url(cik, form_type)


# ─────────────────────────────────────────────────────────────
# 포맷 헬퍼
# ─────────────────────────────────────────────────────────────

def _quarter_to_dot(label: str) -> str:
    """'2026Q2' → '2026.2Q'"""
    if "Q" not in label:
        return label
    y, q = label.split("Q")
    return f"{y}.{q}Q"


def _fmt_date(d: str) -> str:
    """'2026-08-14' → '2026.08.14'"""
    return d.replace("-", ".") if d else ""


# ─────────────────────────────────────────────────────────────
# 메시지 빌드
# ─────────────────────────────────────────────────────────────

def build_detail_message(e: dict, company: dict, skip_accession: bool = False) -> str:
    ticker       = e["ticker"]
    company_name = e["company_name"]
    cik          = e["cik"]
    form_type    = e["form_type"]        # "10-Q" / "10-K" / "8-K"
    filing_date  = e.get("filing_date", "")
    currency     = e.get("currency", "USD")
    yoy          = e["yoy"]
    latest_q     = yoy.get("latest_q", "")
    market_cap   = company.get("market_cap")

    잠정 = "Y" if form_type == "8-K" else "N"
    report_label = FORM_LABELS.get(form_type, form_type)

    lines = [
        f"📌 <b>{company_name} ({ticker})</b> (시가총액 : {_fmt_usd(market_cap, 'USD')})",
        f"📁 {report_label} ({_quarter_to_dot(latest_q)})",
        f"{_fmt_date(filing_date)}",
        "",
        f"잠정실적 : {잠정}",
        "",
        f"매출액 : {_fmt_usd(yoy.get('rev_curr'), currency)}",
        f"영업익 : {_fmt_usd(yoy.get('op_curr'), currency)}",
        f"순이익 : {_fmt_usd(yoy.get('net_curr'), currency)}",
        "",
    ]

    # 최근 N개 분기 실적 테이블 (sec 캐시 전체 quarters 기준)
    cache = _sq_load_cache()
    quarters = cache.get(ticker, {}).get("quarters", {})
    valid_labels = sorted(
        [l for l in quarters if "Q" in l and l[:4].isdigit()],
        key=_sort_key, reverse=True,
    )
    recent_labels = valid_labels[:RECENT_N]

    if recent_labels:
        lines.append("* 최근 실적")
        lines.append("(기간/ 매출/ 영업익/ 순익)")
        for lb in recent_labels:
            q = quarters[lb]
            lines.append(
                f"{_quarter_to_dot(lb)} {_fmt_usd(q.get('rev'), currency)}/ "
                f"{_fmt_usd(q.get('opi'), currency)}/ {_fmt_usd(q.get('net'), currency)}"
            )
        lines.append("")

        # 자동 인사이트: 표시된 분기 중 최신 분기가 매출/영업익 최댓값인지
        rev_vals = [(lb, quarters[lb].get("rev")) for lb in recent_labels if quarters[lb].get("rev") is not None]
        op_vals  = [(lb, quarters[lb].get("opi")) for lb in recent_labels if quarters[lb].get("opi") is not None]
        if rev_vals and max(rev_vals, key=lambda x: x[1])[0] == latest_q:
            lines.append(f"- 최근 {len(recent_labels)}개분기 최대 매출")
        if op_vals and max(op_vals, key=lambda x: x[1])[0] == latest_q:
            lines.append(f"- 최근 {len(recent_labels)}개분기 최대 영업익")
        lines.append("")

    sec_url = _generic_sec_url(cik, form_type) if skip_accession else _sec_filing_url(cik, form_type, filing_date)
    lines.append(f"공시링크 : {sec_url}")
    lines.append(f"회사정보 : https://finance.yahoo.com/quote/{ticker}")

    return "\n".join(lines)


def build_broadcast_message(ticker: str, company: dict) -> str | None:
    """
    특정 신규 필링 감지와 무관하게, 기업의 '현재 알고 있는 최신 분기' 기준으로 메시지 생성.
    캐시가 신선하면(TTL 7일) 그대로 사용, 아니면 fetch_quarterly가 내부적으로 갱신한다.
    신규 필링 트리거가 없으므로 정확한 공시 accession은 조회하지 않고(skip_accession=True)
    기업 필링 목록 페이지로 링크한다.
    SEC XBRL(us-gaap/ifrs-full)에 분기 데이터가 없으면 Alpha Vantage/yfinance로 폴백하지
    않고 바로 스킵한다(skip_fallback=True) — AV 쿼터 소진 방지, SEC 정식 데이터만 사용.
    """
    try:
        fetch_quarterly(ticker, years=10, refresh=False, company_info=company, skip_fallback=True)
    except Exception as ex:
        print(f"  [fetch 실패] {ticker}: {ex}")
        return None

    cache = _sq_load_cache()
    entry = cache.get(ticker, {})
    yoy = _get_latest_quarter_yoy(entry, target_q=None)
    if not yoy:
        return None

    source = entry.get("source", "SEC")
    e = {
        "ticker":       ticker,
        "company_name": entry.get("company_name", company.get("name", ticker)),
        "cik":          company.get("cik", ""),
        "form_type":    "10-Q" if source == "SEC" else "8-K",
        "filing_date":  entry.get("fetched_at", ""),
        "currency":     entry.get("currency", "USD"),
        "yoy":          yoy,
    }
    return build_detail_message(e, company, skip_accession=True)


# ─────────────────────────────────────────────────────────────
# 텔레그램 발송
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
    except Exception as ex:
        print(f"  [텔레그램 오류] {ex}")
        return False


# ─────────────────────────────────────────────────────────────
# 엔트리포인트
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="미국 us-gaap 기업 실적 상세 텔레그램 발송")
    parser.add_argument("--from", dest="from_date", default=None, help="시작일 YYYYMMDD")
    parser.add_argument("--to",   dest="to_date",   default=None, help="종료일 YYYYMMDD")
    parser.add_argument("--no-update",   action="store_true", help="MD 업데이트 생략")
    parser.add_argument("--dry-run",     action="store_true", help="발송 없이 메시지만 출력")
    parser.add_argument("--broadcast-all", action="store_true",
                         help="신규 필링 감지 대신, has_us_gaap 전체 기업의 '현재 알고 있는 최신 분기'로 발송 (캐시 우선, 없으면 갱신)")
    parser.add_argument("--offset", type=int, default=0,
                         help="broadcast-all 전용: 정렬된 티커 목록에서 앞의 N개를 건너뛰고 재개 (중단 후 재시작용)")
    args = parser.parse_args()

    gaap_map = _load_us_gaap_map()
    print(f"[대상] has_us_gaap=True 기업: {len(gaap_map)}개")

    if args.broadcast_all:
        tickers = sorted(gaap_map.keys())
        if args.offset:
            tickers = tickers[args.offset:]
            print(f"[재개] {args.offset}개 스킵, {len(tickers)}개부터 시작")
        ok_count, fail = 0, []
        for i, ticker in enumerate(tickers, 1):
            company = gaap_map[ticker]
            print(f"[{i}/{len(tickers)}] {ticker}", flush=True)
            msg = build_broadcast_message(ticker, company)
            if not msg:
                print(f"  → 데이터 없음, 스킵", flush=True)
                fail.append(ticker)
                continue
            if args.dry_run:
                print(msg)
            else:
                ok = _send_telegram(msg)
                if ok:
                    ok_count += 1
                else:
                    fail.append(ticker)
                time.sleep(1)
        print(f"\n완료: {ok_count}/{len(tickers)} 발송 성공")
        if fail:
            print(f"실패/스킵({len(fail)}개): {fail}")
        return

    today = date.today()
    to_date   = args.to_date   or today.strftime("%Y%m%d")
    from_date = args.from_date or (today - timedelta(days=14)).strftime("%Y%m%d")
    start_fmt = f"{from_date[:4]}-{from_date[4:6]}-{from_date[6:]}"
    end_fmt   = f"{to_date[:4]}-{to_date[4:6]}-{to_date[6:]}"

    # 필터(YoY 기준) 통과 여부와 무관하게 신규 필링 전체 발송
    new_results, _ = run_us_screener(
        start_fmt, end_fmt,
        update_md=not args.no_update,
        require_filter=False,
    )

    targets = [e for e in new_results if e["ticker"] in gaap_map]
    print(f"\n[발송 대상] 신규 필링 {len(new_results)}개 중 us-gaap 기업 {len(targets)}개")

    if not targets:
        return

    for i, e in enumerate(targets, 1):
        company = gaap_map[e["ticker"]]
        msg = build_detail_message(e, company)
        print(f"\n{'='*60}\n[{i}/{len(targets)}] {e['ticker']}\n{'='*60}")
        print(msg)
        if not args.dry_run:
            ok = _send_telegram(msg)
            print(f"  → 발송 {'성공' if ok else '실패'}")
            time.sleep(1)


if __name__ == "__main__":
    main()
