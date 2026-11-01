from __future__ import annotations

import os
import uuid
from decimal import Decimal
from urllib.parse import urlsplit

import httpx
import streamlit as st

from trading_team.replay import demo_case
from trading_team.settings import Settings

PAGES = [
    "Trading Overview",
    "Agent Status",
    "World Monitor Events",
    "Meme Coin Scanner",
    "Prediction Opportunities",
    "Open Positions",
    "Trade History",
    "Performance Analytics",
    "Global Model Configuration",
    "Model Comparison",
    "API Cost",
    "Risk Monitor",
    "Learning Loop",
]


def main():
    st.set_page_config(page_title="Universal AI Trading Team", page_icon="📊", layout="wide")
    st.title("Universal AI Trading Team")
    st.info("PAPER ONLY · 가상 자금으로만 실행합니다. 실제 주문·지갑 서명 기능이 없습니다.")
    base = os.environ.get("DASHBOARD_API_URL", "http://127.0.0.1:8000").rstrip("/")
    parsed = urlsplit(base)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1", "api"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        st.error("로컬 API 주소를 설정해 주세요.")
        st.stop()
    admin = Settings().api_admin_token.get_secret_value()

    def request(path, *, body=None):
        try:
            with httpx.Client(
                base_url=base, timeout=25, follow_redirects=False, trust_env=False
            ) as client:
                response = (
                    client.get(path)
                    if body is None
                    else client.post(path, json=body, headers={"Authorization": "Bearer " + admin})
                )
            if response.status_code != 200:
                st.error(f"요청 실패: HTTP {response.status_code}")
                return None
            return response.json()
        except (httpx.HTTPError, ValueError):
            st.error("API에 연결할 수 없습니다. API 서비스 실행 상태를 확인해 주세요.")
            return None

    accounts = request("/accounts") or []
    ids = [x["id"] for x in accounts] or ["main"]
    account = st.sidebar.selectbox("가상 계정", ids)
    selected = next((x for x in accounts if x["id"] == account), {})
    st.sidebar.caption("자료 유형: " + selected.get("provenance", "unknown"))
    if selected.get("provenance") == "fixture":
        st.warning(
            "합성 자료를 사용하는 테스트 계정입니다. 실제 시장·실제 LLM 검증 결과가 아닙니다."
        )
    page = st.sidebar.radio("화면", PAGES)
    if not admin:
        st.sidebar.caption("조회 모드 · 운영 버튼은 API_ADMIN_TOKEN 설정 후 사용할 수 있습니다.")
    if st.sidebar.button("독립 테스트 계정으로 데모 실행", disabled=not admin):
        result = request(
            "/replay",
            body={
                "account_id": "demo-" + uuid.uuid4().hex[:10],
                "case": demo_case().model_dump(mode="json"),
            },
        )
        if result:
            st.sidebar.success("데모 완료: " + result["account_id"])
            st.json(result)
    suffix = "?account_id=" + account

    if page == "Trading Overview":
        automation = request("/automation") or {}
        if account == "main" and automation.get("enabled"):
            st.subheader("자동 모의운영")
            state_labels = {
                "observing": "자료 수집·진입 대기",
                "evaluating": "조건 평가",
                "awaiting_model": "모델 점검 대기",
                "entries_halted": "신규 진입 중지",
                "worker_stopped": "자동 작업 중지",
                "disabled": "자동 진입 비활성",
            }
            st.write(
                {
                    "상태": state_labels.get(automation.get("state"), "확인 필요"),
                    "작업자 실행": automation.get("worker_running"),
                    "자금": "페이퍼머니",
                    "개별 거래 승인": "자동",
                }
            )
            coverage = automation.get("market_data", {})
            if not coverage.get("complete_observations"):
                st.info(
                    "필수 시장자료를 기다리고 있습니다. 누락된 자료가 있는 후보의 신규 진입은 건너뜁니다."
                )
            st.caption("SCAN 15분 · RISK 5분 · 시장자료 자동 수집 · 서울 23시 마감 보고")
            with st.expander("자동 작업과 자료 상태"):
                st.json(coverage)
                st.dataframe(automation.get("jobs", []), width="stretch")
        result = request("/metrics" + suffix)
        if result:
            columns = st.columns(4)
            for column, label, value in zip(
                columns,
                ["가용 현금 USD", "평가 자산 USD", "실현 손익 USD", "열린 포지션"],
                [
                    result["cash"],
                    result["equity"],
                    result["realized_pnl"],
                    result["open_positions"],
                ],
                strict=True,
            ):
                column.metric(label, "N/A" if value is None else str(value))
            if result["killed"]:
                st.error("신규 진입 중지: " + result["kill_reason"])
            st.caption("평가 기준: " + result["valuation_reason"])
        st.subheader("운영 점검")
        col1, col2, col3 = st.columns(3)
        if col1.button("데이터 수집", disabled=not admin):
            st.json(request("/data/collect", body={}) or {})
        if col2.button("모의 분석 사이클 실행", disabled=not admin or account != "main"):
            st.json(
                request("/cycles/run", body={"account_id": account, "request_id": uuid.uuid4().hex})
                or {}
            )
        if col3.button("연결·자료 준비 상태 점검"):
            st.json(request("/readiness") or {})
        st.dataframe(request("/cycles" + suffix) or [], width="stretch")
    elif page == "Agent Status":
        result = request("/agents") or {}
        st.caption("AI: CHIEF · SCAN · VET · BOOK / 결정론적 Python: SIZE · FILLS · RISK")
        st.dataframe(result.get("health", []), width="stretch")
        st.dataframe(result.get("messages", []), width="stretch")
    elif page == "World Monitor Events":
        st.caption("로컬 World Monitor REST 사용 · degraded 상태에서는 BOOK 중지")
        if st.button("World Monitor 점검"):
            st.json((request("/readiness") or {}).get("world_monitor", {}))
        st.dataframe(request("/events") or [], width="stretch")
    elif page == "Meme Coin Scanner":
        st.caption(
            "결측 필드는 null로 유지됩니다. 보유자·온체인·실행 호가가 검증되어야 진입할 수 있습니다."
        )
        snapshots = request("/snapshots?provenance=" + selected.get("provenance", "live")) or []
        st.subheader("관측 요약")
        st.caption("Jupiter 깊이·Solana 보유자·GeckoTerminal 거래로 채운 pool만 표시합니다.")
        st.dataframe(
            [
                {
                    "asset": x["asset_id"],
                    "pool": x["pair_id"],
                    "observed_at": x["observed_at"],
                    "price": x["price"],
                    "holders": x["holder_count"],
                    "previous_holders": x["previous_holder_count"],
                    "largest_wallet_share": x["largest_token_account_share"],
                    "contract_safe": x["contract_safe"],
                    "windows_buys_sells": [[w["buys"], w["sells"]] for w in x["windows"]],
                    "large_holder_selling": x["large_holder_selling"],
                    "depth_levels": len((x.get("buy_quote") or {}).get("levels") or []),
                }
                for x in snapshots
                if x.get("observed_at")
            ],
            width="stretch",
        )
        st.subheader("전체 스냅샷")
        st.dataframe(snapshots, width="stretch")
        st.subheader("필터 결과")
        st.dataframe(request("/decisions" + suffix) or [], width="stretch")
    elif page == "Prediction Opportunities":
        st.caption("확률을 먼저 기록한 뒤 가격 조회 · 해석 규칙·마감이 정확히 일치하는 시장만 허용")
        if st.button("가격을 제외한 발견 목록 조회"):
            st.json(request("/book/discovery") or {})
        st.caption(
            "근거와 resolution rules가 준비된 BOOK 요청은 API /book/evaluate로 실행할 수 있습니다."
        )
        st.dataframe(
            [x for x in (request("/positions" + suffix) or []) if x["market_kind"] == "prediction"],
            width="stretch",
        )
    elif page == "Open Positions":
        st.dataframe(
            [x for x in (request("/positions" + suffix) or []) if Decimal(x["quantity"]) > 0],
            width="stretch",
        )
    elif page == "Trade History":
        orders = request("/orders" + suffix) or []
        st.dataframe(orders, width="stretch")
        if orders:
            order_id = st.selectbox("체결 상세", [x["id"] for x in orders])
            st.dataframe(request("/fills?order_id=" + order_id) or [], width="stretch")
    elif page == "Performance Analytics":
        st.json(request("/metrics" + suffix) or {})
        points = request("/equity" + suffix) or []
        valid = {
            x["observed_at"]: float(x["equity"])
            for x in reversed(points)
            if x["equity"] is not None
        }
        if valid:
            st.line_chart(
                {"time": list(valid), "equity": list(valid.values())}, x="time", y="equity"
            )
        st.caption(
            "승률·Profit Factor는 전체 청산된 거래 단위로 계산합니다. 가격 없는 구간의 자산과 수익률은 N/A입니다."
        )
    elif page == "Global Model Configuration":
        st.json(request("/config") or {})
        catalog = request("/providers") or {}
        st.subheader("사용 가능한 CLI")
        st.dataframe(
            [
                {
                    key: item.get(key)
                    for key in ("provider", "name", "installed", "inference_verified")
                }
                for item in catalog.get("cli_profiles", [])
            ],
            width="stretch",
        )
        st.caption(
            "전역 LLM_PROVIDER · LLM_MODEL · LLM_API_KEY 환경변수를 사용합니다. 변경 후 API와 worker를 재시작하고 연결을 점검하세요. CLI는 기존 로그인 또는 환경 인증을 사용하며, 설치와 실제 모델 인증은 각각 점검합니다."
        )
        if st.button("전역 모델 연결·출력 스키마 점검", disabled=not admin):
            st.json(request("/model/probe", body={}) or {})
    elif page == "Model Comparison":
        st.caption(
            "같은 자료 해시·정책·초기 자금 그룹만 비교하세요. 모델은 실행 사이에 전역 설정으로 교체합니다."
        )
        st.json(request("/comparison") or [])
    elif page == "API Cost":
        st.json(request("/costs") or {})
        st.caption("확인되지 않은 토큰·구독 비용은 0으로 계산하지 않습니다.")
    elif page == "Risk Monitor":
        st.caption("RISK는 5분마다 LLM과 독립 실행됩니다. 가격이 없으면 청산 요청을 보존합니다.")
        st.dataframe(request("/positions" + suffix) or [], width="stretch")
        reason = st.text_input("운영 사유", value="")
        left, right, check = st.columns(3)
        if left.button("신규 진입 중지", disabled=not admin or not reason.strip()):
            st.json(
                request("/kill", body={"account_id": account, "active": True, "reason": reason})
                or {}
            )
        if right.button("신규 진입 중지 해제", disabled=not admin or not reason.strip()):
            st.json(
                request("/kill", body={"account_id": account, "active": False, "reason": reason})
                or {}
            )
        if check.button("RISK 즉시 점검", disabled=not admin or account != "main"):
            st.json(
                request("/risk/run", body={"account_id": account, "request_id": uuid.uuid4().hex})
                or {}
            )
        st.json(request("/audit" + suffix) or {})
    elif page == "Learning Loop":
        st.caption(
            "결정과 결과를 측정해 개선안을 제안합니다. 전략 규칙은 자동으로 바꾸지 않으며 "
            "사람이 검토해 적용합니다. 표본이 작으면 결론을 내리지 않습니다."
        )
        if st.button("지금 분석 실행", disabled=not admin or account != "main"):
            request("/learning/run", body={"account_id": account})
        learned = request("/learning" + suffix) or {}
        report = learned.get("report")
        if not report:
            st.info("아직 분석 결과가 없습니다. worker가 매시간 실행하거나 위 버튼으로 실행합니다.")
        else:
            st.caption(
                f"생성 {report['generated_at']} · 최근 {report['window_days']}일 · "
                f"자료 유형 {report['provenance']} · 정책 {report['policy_version']}"
            )
            if report["provenance"] != "live":
                st.warning("합성 자료 계정입니다. 모델이나 시장에 대한 근거가 아닙니다.")
            st.subheader("개선 제안")
            items = learned.get("proposals", [])
            st.dataframe(
                [
                    {k: x[k] for k in ("status", "severity", "kind", "title", "action")}
                    for x in items
                ],
                width="stretch",
            )
            open_items = [x for x in items if x["status"] == "proposed"]
            if open_items and admin:
                chosen = st.selectbox("검토할 제안", [x["id"] for x in open_items])
                note = st.text_input("검토 메모", value="")
                for column, label, status in zip(
                    st.columns(3),
                    ["확인함", "기각", "적용함"],
                    ["acknowledged", "dismissed", "applied"],
                    strict=True,
                ):
                    if column.button(label):
                        st.json(
                            request(
                                f"/learning/proposals/{chosen}",
                                body={"account_id": account, "status": status, "note": note},
                            )
                            or {}
                        )
            st.subheader("모델 출력 신뢰도")
            st.dataframe(
                [{"role": role} | item for role, item in report["reliability"].items()],
                width="stretch",
            )
            st.subheader("게이트 결과 분포")
            st.dataframe(report["funnel"]["groups"], width="stretch")
            st.subheader("게이트 이후 24시간 가격 변화")
            st.caption(
                f"수수료·슬리피지 왕복 기준선 {report['round_trip_hurdle']} · 가격만 본 값이며 "
                "체결 깊이는 반영하지 않습니다. 같은 자산이 반복되어 표본이 겹칩니다."
            )
            st.dataframe(
                [
                    {
                        "allowed": x["allowed"],
                        "reason": x["reason"],
                        "decisions": x["decisions"],
                    }
                    | x["horizons"]["86400"]
                    for x in report["gate_regret"]
                ],
                width="stretch",
            )
            st.subheader("확률 보정")
            st.json(report["calibration"])
            with st.expander("해석상 주의"):
                for note in report["notes"]:
                    st.write("- " + note)


if __name__ == "__main__":
    main()
