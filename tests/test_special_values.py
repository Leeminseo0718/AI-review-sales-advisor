"""원천 특수값(-999999.9) 처리 검증.

카드 데이터의 비율 컬럼에는 '해당 없음'(예: 배달 미운영)을 뜻하는 특수값 -999999.9가 들어 있다.
이 값을 0%로 바꾸면 평균·비교 계산에 섞여 지표가 왜곡되므로, 앱은 이를 결측(NULL)으로 분리해야 한다.

실행 방법 (로컬 테스트용 Postgres 필요):
    TEST_DATABASE_URL=postgresql+psycopg2://postgres@localhost:5432/postgres pytest tests/test_special_values.py

안전장치: 운영 DB(Supabase 등)에서 실행되지 않도록 localhost 주소일 때만 동작한다.
테스트는 전용 가맹점 ID의 행만 넣고 끝나면 지운다.
"""
import os
import sys
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_URL = os.getenv("TEST_DATABASE_URL")


def _is_local(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or parse_qs(parsed.query).get("host", [""])[0]
    return host in ("localhost", "127.0.0.1") or host.startswith("/")


pytestmark = pytest.mark.skipif(
    not TEST_URL or not _is_local(TEST_URL),
    reason="로컬 TEST_DATABASE_URL이 필요합니다 (운영 DB에서는 실행하지 않음)",
)

MCT = "TEST_SPECIAL_VALUE"
PEER = "TEST_SPECIAL_PEER"
SPECIAL = -999999.9


@pytest.fixture(scope="module")
def seeded():
    os.environ["DATABASE_URL"] = TEST_URL  # app.deps가 이 값을 읽음
    sys.path.insert(0, str(ROOT))
    from sqlalchemy import create_engine, text

    eng = create_engine(TEST_URL, future=True)
    ddl = (ROOT / "db" / "ddl_001_create_raw_tables.sql").read_text(encoding="utf-8")

    def cleanup(conn):
        for table in ("stg_merchant_monthly_customers", "stg_merchant_monthly_usage", "stg_merchant_overview"):
            conn.execute(text(f"delete from {table} where encoded_mct in (:a, :b)"), {"a": MCT, "b": PEER})

    with eng.begin() as c:
        c.exec_driver_sql(ddl)
        cleanup(c)
        for mct, name in ((MCT, "테스트카페"), (PEER, "비교카페")):
            c.execute(text("""insert into stg_merchant_overview values
              (:m, '서울 성동구', :n, null, '성동구', '카페', '성수', '2020-01-01', '')"""), {"m": mct, "n": name})
        # 대상 매장: 배달 비중 12.5% → 0%(실제 0) → 특수값
        for ym, dlv in (("209907", 12.5), ("209908", 0), ("209909", SPECIAL)):
            c.execute(text("""insert into stg_merchant_monthly_usage values
              (:m, :ym, '3_25-50%', '2_10-25%', '2_10-25%', '3_25-50%', '3_25-50%', '1_상위1구간',
               :dlv, 150, 120, 20, 10, 5, 4)"""), {"m": MCT, "ym": ym, "dlv": dlv})
            c.execute(text("""insert into stg_merchant_monthly_customers values
              (:m, :ym, 10, 8, 5, 3, 1, 20, 15, 8, 5, 2, 30, 70, 40, 30, 30)"""), {"m": MCT, "ym": ym})
        # 비교 매장: 상권 내 백분위가 특수값
        c.execute(text("""insert into stg_merchant_monthly_usage values
          (:m, '209909', '3_25-50%', '2_10-25%', '2_10-25%', '3_25-50%', '3_25-50%', '1_상위1구간',
           5, 130, 110, 30, :sp, 5, 4)"""), {"m": PEER, "sp": SPECIAL})

    yield
    with eng.begin() as c:
        cleanup(c)


def test_view_keeps_special_value_as_null(seeded):
    from sqlalchemy import create_engine, text
    with create_engine(TEST_URL, future=True).begin() as c:
        rows = c.execute(text("""select delivery_ratio from merchant_monthly_usage
                                 where encoded_mct = :m order by month"""), {"m": MCT}).scalars().all()
    assert [None if v is None else float(v) for v in rows] == [12.5, 0.0, None]


def test_app_query_does_not_turn_special_value_into_zero(seeded):
    from app.repo.metrics_repo import fetch_timeseries, fetch_snapshot
    ts = fetch_timeseries(MCT, "2099-07-01", "2099-09-01")
    assert [r["delivery_ratio"] if r["delivery_ratio"] is None else float(r["delivery_ratio"]) for r in ts] == [12.5, 0.0, None]
    assert fetch_snapshot(MCT)["delivery_ratio"] is None


def test_card_shows_dash_instead_of_zero(seeded):
    from app.services.card_items_service import build_dashboard_cards
    cards = build_dashboard_cards(MCT, "2099-07-01", "2099-09-01")["cards"]
    delivery = next(c for c in cards if c["key"] == "delivery_ratio")
    assert delivery["value"] == "-"
    assert delivery["delta"] is None  # 비교 불가 시 증감 미표시


def test_average_excludes_special_value(seeded):
    import pandas as pd
    from app.repo.metrics_repo import fetch_timeseries
    ts = pd.DataFrame(fetch_timeseries(MCT, "2099-07-01", "2099-09-01"))
    avg = pd.to_numeric(ts["delivery_ratio"], errors="coerce").mean(skipna=True)
    assert avg == pytest.approx(6.25)  # (12.5 + 0) / 2, 특수값을 0%로 넣으면 4.17로 낮아짐


def test_competitor_table_hides_special_value(seeded):
    from app.repo.compare_repo import fetch_top_competitors
    rows = fetch_top_competitors(MCT)
    peer = next(r for r in rows if r["encoded_mct"] == PEER)
    assert peer["area_rank_pct"] is None
