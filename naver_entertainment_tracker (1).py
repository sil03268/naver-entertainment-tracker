"""
네이버 증권 "방송과엔터테인먼트" 업종 트래커 (개인 연구용)

기존 3.py(제약 업종 트래커)를 그대로 재사용하되, 기본 업종만
"방송과엔터테인먼트"로 바꾼 버전입니다. 코드 구조/함수는 전부 동일하고,
argparse의 --sector 기본값과 CSV 저장 경로만 다릅니다.
(실제로는 3.py에서 --sector "방송과엔터테인먼트" 옵션만 줘도 동일하게 동작합니다.)

사용 예
    python naver_entertainment_tracker.py --list                 # 업종 목록 확인
    python naver_entertainment_tracker.py --top 20                # 상위 20개 1회 수집
    python naver_entertainment_tracker.py --daemon --email        # 매일 자동 수집 + 메일

설치
    pip install requests beautifulsoup4
"""
import argparse
import csv
from functools import lru_cache
import os
import re
import smtplib
import ssl
import sys
import time
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# 코랩/주피터처럼 __file__이 없는 환경에서도 돌아가도록 현재 작업 폴더를 기본값으로 사용
DEFAULT_DIR = Path(globals().get("__file__", Path.cwd() / "script.py")).resolve().parent

LIST_URL = "https://stock.naver.com/market/stock/kr/industry"
SECTOR_LIST_API = "https://stock.naver.com/api/stockSecurity/rankings/v2/domestic/industries?sortType=changeRate&size=100&period=daily"
SECTOR_STOCK_API = "https://stock.naver.com/api/domestic/market/upjong/{no}/stocklist"
PUBLIC_HOLIDAYS_API = "https://date.nager.at/api/v3/PublicHolidays/{year}/KR"

COLUMNS = [
    "수집일자", "수집시각", "업종", "시총순위", "종목코드", "종목명",
    "현재가", "전일비", "등락률(%)", "거래량", "시가총액(억원)", "상장주식수",
    "PER", "EPS", "PBR", "배당수익률(%)", "외국인소진율(%)", "52주최고/최저",
]

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) personal-research-crawler",
    "Accept-Language": "ko-KR,ko;q=0.9",
})


# ---------------------------------------------------------------- 유틸
def fetch_json(url: str, retries: int = 3, params: dict | None = None) -> dict | list:
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = session.get(url, params=params, timeout=10)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last_err = e
            print(f"  [경고] 요청 실패({attempt}/{retries}): {e}", file=sys.stderr)
            time.sleep(2 * attempt)
    raise RuntimeError(f"요청 실패: {url} ({last_err})")


@lru_cache(maxsize=4)
def get_public_holidays(year: int) -> frozenset[date]:
    data = fetch_json(PUBLIC_HOLIDAYS_API.format(year=year))
    if not isinstance(data, list):
        raise RuntimeError(f"공휴일 API 응답 형식이 올바르지 않습니다: {year}")
    return frozenset(date.fromisoformat(item["date"]) for item in data if item.get("date"))


def is_business_day(day: date) -> bool:
    return day.weekday() < 5 and day not in get_public_holidays(day.year)


def has_saved_date(path: Path, day: date) -> bool:
    if not path.exists():
        return False
    with path.open(newline="", encoding="utf-8-sig") as file:
        return any(row.get("수집일자") == day.isoformat() for row in csv.DictReader(file))


def collection_date(now: datetime, history_path: Path, weekdays_only: bool) -> date | None:
    today = now.date()
    if not weekdays_only or is_business_day(today):
        return today
    if today.weekday() < 5:
        return None

    missed_day = today - timedelta(days=1)
    while not is_business_day(missed_day):
        missed_day -= timedelta(days=1)
    if has_saved_date(history_path, missed_day):
        return None
    print(f"[보충 수집] 최근 미저장 영업일 {missed_day} 데이터를 저장합니다.")
    return missed_day


def clean_num(s: str) -> str:
    """'1,234' -> '1234', '하락 300' -> '-300', 'N/A' -> ''"""
    if not s or not re.search(r"\d", s):
        return ""
    neg = "하락" in s or s.strip().startswith("-")
    num = re.sub(r"[^\d.]", "", s)
    return ("-" if neg else "") + num


# ---------------------------------------------------------------- 수집
def get_sectors() -> list[dict]:
    data = fetch_json(SECTOR_LIST_API)
    if not isinstance(data, dict):
        return []
    return [
        {
            "no": item["code"],
            "name": item["name"],
            "updated_at": item.get("updatedAt", ""),
        }
        for item in data.get("items", [])
        if item.get("code") and item.get("name")
    ]


def diagnose_sector_page() -> None:
    try:
        response = session.get(LIST_URL, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        title = soup.title.get_text(" ", strip=True) if soup.title else "(제목 없음)"
        print(f"HTML 상태: HTTP {response.status_code}")
        print(f"최종 URL: {response.url}")
        print(f"Content-Type: {response.headers.get('content-type', '(없음)')}")
        print(f"페이지 제목: {title}")
        print(f"기존 업종 링크: {len(soup.select('a[href*=sise_group_detail]'))}개")
        html_path = Path("naver_sector_list_debug.html")
        html_path.write_bytes(response.content)
        print(f"HTML 저장: {html_path.resolve()}")
    except requests.RequestException as e:
        print(f"HTML 요청 실패: {e}", file=sys.stderr)

    try:
        print(f"업종 API: {len(get_sectors())}개 확인")
    except RuntimeError as e:
        print(f"업종 API 요청 실패: {e}", file=sys.stderr)


def get_sector_stocks(sector_no: str, delay: float = 0) -> list[dict]:
    """업종 종목의 시세와 주요 지표를 API 페이지 전체에서 수집한다."""
    stocks = []
    page = 0
    while page < 100:
        items = fetch_json(
            SECTOR_STOCK_API.format(no=sector_no),
            params={"marketType": "ALL", "orderType": "priceTop", "startIdx": page, "pageSize": 100},
        )
        if not isinstance(items, list):
            raise RuntimeError(f"업종 종목 API 응답 형식이 올바르지 않습니다: {sector_no}")
        if not items:
            break
        for item in items:
            code = str(item.get("itemcode") or "")
            market_sum = int(item.get("marketSum") or 0)
            if not code:
                continue
            high = str(item.get("week52HighPrice") or "")
            low = str(item.get("week52LowPrice") or "")
            stocks.append({
                "code": code,
                "name": str(item.get("itemname") or ""),
                "market_sum": market_sum,
                "list": {
                    "현재가": str(item.get("nowPrice") or ""),
                    "전일비": str(item.get("prevChangePrice") or ""),
                    "등락률": str(item.get("prevChangeRate") or ""),
                    "거래량": str(item.get("tradeVolume") or ""),
                },
                "detail": {
                    "현재가": str(item.get("nowPrice") or ""),
                    "시가총액": market_sum // 100_000_000,
                    "상장주식수": str(item.get("listedStockCnt") or ""),
                    "PER": str(item.get("per") or ""),
                    "EPS": str(item.get("eps") or ""),
                    "PBR": str(item.get("pbr") or ""),
                    "배당수익률": str(item.get("dividendRate") or ""),
                    "외국인소진율": str(item.get("frgnHoldRate") or ""),
                    "52주": f"{high}/{low}" if high or low else "",
                },
            })
        if len(items) < 100:
            break
        page += 1
        if delay:
            time.sleep(delay)
    return stocks


# ---------------------------------------------------------------- 저장
def save_history(rows: list[dict], path: Path) -> None:
    """누적 저장: 오늘 날짜 데이터는 교체, 이전 날짜 데이터는 보존."""
    path.parent.mkdir(parents=True, exist_ok=True)
    today = rows[0]["수집일자"]
    old = []
    if path.exists():
        with open(path, newline="", encoding="utf-8-sig") as f:
            old = [r for r in csv.DictReader(f) if r.get("수집일자") != today]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:  # 엑셀 한글 호환
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(old + rows)


def send_result_email(rows: list[dict], history_path: Path) -> None:
    host = os.getenv("NAVER_TRACKER_SMTP_HOST", "").strip()
    port = int(os.getenv("NAVER_TRACKER_SMTP_PORT", "465"))
    username = os.getenv("NAVER_TRACKER_SMTP_USER", "").strip()
    password = os.getenv("NAVER_TRACKER_SMTP_PASSWORD", "")
    sender = os.getenv("NAVER_TRACKER_EMAIL_FROM", username).strip()
    recipient = os.getenv("NAVER_TRACKER_EMAIL_TO", "").strip()
    missing = [
        name for name, value in (
            ("NAVER_TRACKER_SMTP_HOST", host),
            ("NAVER_TRACKER_SMTP_USER", username),
            ("NAVER_TRACKER_SMTP_PASSWORD", password),
            ("NAVER_TRACKER_EMAIL_TO", recipient),
        ) if not value
    ]
    if missing:
        raise ValueError("이메일 설정이 필요합니다: " + ", ".join(missing))

    sectors = ", ".join(sorted({row["업종"] for row in rows}))
    message = EmailMessage()
    message["Subject"] = f"네이버 업종 트래커 - {rows[0]['수집일자']} ({sectors})"
    message["From"] = sender
    message["To"] = recipient
    message.set_content("\n".join(
        [f"수집일자: {rows[0]['수집일자']}", f"업종: {sectors}", "", "시총 상위 종목:"]
        + [
            f"{row['시총순위']}. {row['종목명']} ({row['종목코드']}) - "
            f"시총 {row['시가총액(억원)']}억원, PER {row['PER'] or '-'}, PBR {row['PBR'] or '-'}"
            for row in rows
        ]
        + ["", f"전체 누적 CSV를 첨부했습니다: {history_path.name}"]
    ))
    message.add_attachment(
        history_path.read_bytes(),
        maintype="text",
        subtype="csv",
        filename=history_path.name,
    )

    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as smtp:
            smtp.login(username, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(username, password)
            smtp.send_message(message)


# ---------------------------------------------------------------- 실행
def run_once(args) -> None:
    now = datetime.now()
    record_date = collection_date(
        now,
        Path(args.history),
        getattr(args, "weekdays_only", False),
    )
    if record_date is None:
        print("[건너뜀] 주말 또는 공휴일이며 보충할 미저장 영업일이 없습니다.")
        return

    sectors = get_sectors()
    if not sectors:
        print("업종 목록을 가져오지 못했습니다. --diagnose로 응답을 확인하세요.", file=sys.stderr)
        return

    targets = []
    for kw in args.sector:
        hit = [s for s in sectors if (kw in s["name"] if args.contains else s["name"] == kw)]
        if not hit:
            near = [s["name"] for s in sectors if kw in s["name"]]
            print(f"업종 '{kw}'을(를) 찾지 못했습니다. 비슷한 업종: {near or '없음'}", file=sys.stderr)
        targets += hit
    if not targets:
        return

    pool = {}  # code -> record (여러 업종에 중복되면 한 번만)
    for s in targets:
        time.sleep(args.delay)
        stocks = get_sector_stocks(s["no"], args.delay)
        print(f"[{s['name']}] 종목 {len(stocks)}개 발견")
        for st in stocks:
            pool.setdefault(st["code"], {**st, "sector": s["name"], "updated_at": s["updated_at"]})

    ranked = [r for r in pool.values() if r["market_sum"]]
    ranked.sort(key=lambda r: r["market_sum"], reverse=True)
    ranked = ranked[: args.top]
    if not ranked:
        print("시가총액을 수집한 종목이 없습니다. 선택자(#_market_sum)를 확인하세요.", file=sys.stderr)
        return

    rows = []
    for rank, r in enumerate(ranked, 1):
        d, lst = r["detail"], r["list"]
        rows.append({
            "수집일자": record_date.isoformat(),
            "수집시각": datetime.fromisoformat(r["updated_at"]).strftime("%H:%M") if r["updated_at"] else "",
            "업종": r["sector"],
            "시총순위": rank,
            "종목코드": r["code"],
            "종목명": r["name"],
            "현재가": d.get("현재가") or clean_num(lst.get("현재가", "")),
            "전일비": clean_num(lst.get("전일비", "")),
            "등락률(%)": clean_num(lst.get("등락률", "")),
            "거래량": clean_num(lst.get("거래량", "")),
            "시가총액(억원)": d["시가총액"],
            "상장주식수": d.get("상장주식수", ""),
            "PER": d.get("PER", ""),
            "EPS": d.get("EPS", ""),
            "PBR": d.get("PBR", ""),
            "배당수익률(%)": d.get("배당수익률", ""),
            "외국인소진율(%)": d.get("외국인소진율", ""),
            "52주최고/최저": d.get("52주", ""),
        })
    save_history(rows, Path(args.history))

    print(f"\n[저장 완료] {args.history}  ({rows[0]['수집일자']}, 상위 {len(rows)}개)")
    for r in rows:
        print(f"  {r['시총순위']:>2}. {r['종목명']:<14} 시총 {r['시가총액(억원)']:>9,}억  PER {r['PER'] or '-'}")
    if args.email:
        try:
            send_result_email(rows, Path(args.history))
            print("[메일 전송 완료]")
        except (OSError, smtplib.SMTPException, ValueError) as e:
            print(f"[메일 전송 실패] CSV는 저장되었습니다: {e}", file=sys.stderr)


def next_run_time(now: datetime, hhmm: str, weekdays_only: bool = False) -> datetime:
    h, m = map(int, hhmm.split(":"))
    t = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if t <= now:
        t += timedelta(days=1)
    if weekdays_only:
        while not is_business_day(t.date()):
            t += timedelta(days=1)
    return t


def daemon(args) -> None:
    schedule = "평일" if args.weekdays_only else "매일"
    print(f"자동 실행 모드: {schedule} {args.at}에 실행합니다. 종료는 Ctrl+C")
    while True:
        t = next_run_time(datetime.now(), args.at, args.weekdays_only)
        print(f"다음 실행: {t:%Y-%m-%d %H:%M}")
        while (remain := (t - datetime.now()).total_seconds()) > 0:
            time.sleep(min(remain, 60))
        try:
            run_once(args)
        except Exception as e:  # 하루 실패가 전체 종료로 이어지지 않게
            print(f"[오류] 이번 실행 실패: {e}", file=sys.stderr)


def main() -> None:
    p = argparse.ArgumentParser(description="네이버 증권 업종 트래커 (방송과엔터테인먼트 기본값)")
    p.add_argument("--list", action="store_true", help="업종 목록 출력 후 종료")
    p.add_argument("--diagnose", action="store_true", help="업종 페이지 HTML/API 진단 및 HTML 저장")
    # ↓ 제약 업종 트래커(3.py)와 유일하게 다른 부분: 기본 업종을 "방송과엔터테인먼트"로 지정
    p.add_argument("--sector", nargs="+", default=["방송과엔터테인먼트"], help="업종명 (기본: 방송과엔터테인먼트)")
    p.add_argument("--contains", action="store_true", help="업종명을 부분 일치로 검색")
    p.add_argument("--top", type=int, default=20, help="시가총액 상위 N개 (기본 20)")
    p.add_argument(
        "--history",
        default=str(DEFAULT_DIR / "entertainment_sector_history.csv"),
        help="누적 CSV 경로 (기본: 현재 작업 폴더/entertainment_sector_history.csv, 코랩이면 /content)",
    )
    p.add_argument("--delay", type=float, default=1.0, help="요청 간 대기(초)")
    p.add_argument("--daemon", action="store_true", help="매일 자동 실행 모드")
    p.add_argument("--at", default="16:00", help="자동 실행 시각 HH:MM (기본 16:00)")
    p.add_argument("--weekdays-only", action="store_true", help="공휴일 제외, 주말에는 미저장 영업일 1회 보충")
    p.add_argument("--email", action="store_true", help="저장 후 SMTP로 결과 이메일 발송")
    args, _unknown = p.parse_known_args()  # 코랩 등 노트북 환경이 넘기는 -f 커널 인자 무시

    if args.diagnose:
        diagnose_sector_page()
    elif args.list:
        sectors = get_sectors()
        if not sectors:
            print("업종 목록을 가져오지 못했습니다. 접속은 됐지만 페이지에서 업종 링크를 찾지 못했습니다.\n"
                  "--diagnose 명령으로 응답 HTML과 API 상태를 확인해 주세요.",
                  file=sys.stderr)
            sys.exit(1)
        for s in sectors:
            print(f"{s['no']:>4}  {s['name']}")
    elif args.daemon:
        daemon(args)
    else:
        run_once(args)


if __name__ == "__main__":
    main()
