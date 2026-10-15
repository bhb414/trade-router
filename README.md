# Trade Router

Trade Router는 개인 컴퓨터에서 Python 자동매매 전략을 작성하고 실행하는
가벼운 로컬 웹 프로그램입니다. 증권사별 차이는 내부 Router 라이브러리가
통일하고, 사용자는 Studio 화면 하나만 엽니다.

전체 목표 구조와 처리 흐름은 [최종 설계 문서](docs/ARCHITECTURE.md)에
도식과 함께 정리되어 있습니다. 실제 구현 범위와 다음 순서는
[구현 상태 문서](docs/IMPLEMENTATION_STATUS.md)에서 관리합니다.

```text
브라우저 Studio
    -> Python 전략 실행기
    -> Trade Router 공통 API
    -> KIS / NAMUH / 이후 추가될 증권사
```

## 폴더 구조

```text
packages/router/   증권사 통합 모델과 API
apps/studio/       로컬 웹 화면과 전략 실행기
tests/             공통 계약과 실행기 테스트
tools/             실행과 개발 도구
run.cmd            사용자 실행 파일
```

전략, 설정, 로그와 매매 기록은 기본적으로 다음 사용자 데이터 폴더에 저장합니다.
프로그램을 업데이트해도 이 폴더는 교체하지 않습니다.

```text
%LOCALAPPDATA%\TradeRouter
```

## 실행

Windows에서 `run.cmd`를 실행합니다. 최초 실행에만 프로젝트 전용 Python 환경과
필요 패키지를 준비한 뒤 `http://127.0.0.1:8890`을 자동으로 엽니다. 표시된
명령 창을 닫으면 Studio와 실행 중인 전략이 함께 종료됩니다.

## 현재 구현 범위

- 한 프로세스에서 실행되는 로컬 웹 Studio
- 새 전략 생성 시 스크립트·블록 유형 선택, 유형별 `개요 | 코드` 또는 `개요 | 블록` 화면
- `Strategy` 클래스 기반 Python 전략 작성·저장·시작·중지·로그 확인
- 자유 배치·빈 공간 이동·Ctrl+휠 확대/축소·전체 맞춤을 지원하는 노드 캔버스
- 블록 정의(JSON)와 시각적 배치 검증·컴파일, 실행용 Python 코드 자동 관리
- HTS 조건검색·시장 순위검색·지정 종목 중 하나를 고르는 재사용 가능한 종목 데이터 블록
- 관리형 블록의 시세·봉 시장(KRX·NXT·통합)과 주문 경로(KRX·NXT·SOR·AUTO)를 독립 선택
- 시가·고가·저가·종가·거래량·볼린저를 비교하는 매수 조건과 평균 매수가·수익률까지 비교하는 매도 조건 블록
- AND·OR, N봉 전 값, 상향·하향돌파, 시장가·현재가 지정가 주문을 고르는 주문 블록
- 1~120분봉과 완성 일봉, 조회 가능한 이력 범위를 저장 전에 검증하는 관리형 블록 실행
- 09:00~10:00, 10:00~15:00처럼 시간대를 나눌 수 있는 중앙 스케줄러
- 전략 프로세스마다 발급되고 계좌·데이터 연결에 결속되는 로컬 세션
- KIS·NAMUH API 키와 거래 계좌 등록
- Windows 사용자 범위 DPAPI를 사용한 API 키·계좌번호 암호화
- 연결별 토큰 단일 발급·24시간 캐시와 중앙 호출 제한·중복 조회 합치기
- API 키·접속 대상별 REST 호출 간격과 WebSocket 세션·구독 한도 자동 관리
- KIS·NAMUH 체결가·호가 WebSocket 공유 구독, 1·2·4·8·16·30초 재연결과 자동 재구독
- KIS HTS ID 및 NAMUH 계좌 통보 채널의 실시간 주문·체결 통보와 전략 `on_order`·`on_fill` 전달
- KIS 현재가, 1~120분봉, 일봉, 주봉, 잔고, 당일 주문·체결·미체결의 공통 SDK
- KIS HTS 저장 조건식 목록·검색 결과의 공통 `ctx.discovery` API
- KIS 조회상위·거래량·거래대금·상승률 순위의 공통 `ctx.discovery` API
- KIS 시장가·지정가 주문 가능 현금·계산단가·가능수량 조회
- NAMUH PLUG 실전·모의 토큰, 현재가, 잔고, 주문 가능 수량, 당일 주문·체결의 공통 SDK
- 한 전략에서 별칭으로 여러 증권사 데이터 연결을 선택하는 구조
- 런타임 이벤트와 멱등 주문 원장을 위한 SQLite WAL 저장소
- 증권사와 접속 대상을 모르는 공통 주문 엔진(권한·위험 한도·멱등성·복구 조정)
- KIS·NAMUH 실전·모의 국내주식 현금 시장가·지정가 주문 어댑터
- 증권사 미체결 조회를 먼저 거치는 KIS·NAMUH 공통 정정·취소 SDK
- 미해결 주문이 있을 때만 동작하는 중앙 주문 상태 추적과 부분·전량체결 원장 반영
- 확인된 체결 원장에서 복구되는 전략별 보유 수량과 수동 보유분 분리
- 계좌와 전략을 각각 허용해야 열리는 주문 권한 스위치
- 공통 종목·시세·잔고·주문 모델과 증권사 기능표
- 대시보드와 전략 개요의 시세 데이터·스케줄러·계좌 조회·주문 처리 결과 탭
- 전략별 라우터 체결과 계좌별 수수료율·매도 세율을 반영한 실현·평가 순손익
- GitHub 최신 Release 확인

주문 엔진은 특정 증권사나 모의투자에 맞춰져 있지 않습니다. 계좌 별칭으로
KIS 또는 NAMUH 어댑터를 선택합니다. 두 어댑터 모두 실전·모의 현금 주문,
미체결 확인, 정정·취소를 같은 SDK 계약으로 제공하며 실제 주문 자동 테스트는
수행하지 않습니다. NAMUH 과거 봉은 아직 지원하지 않습니다.

관리형 블록에서 HTS 조건검색은 저장된 조건식의 종목 목록을 가져오고, 별도의
`시세·봉 시장` 설정이 각 후보의 현재가와 매매조건 봉을 KRX·NXT·통합 중 어디서
확인할지 정합니다. `주문 경로`는 실제 주문을 KRX·NXT·SOR·AUTO 중 어디로 보낼지
정합니다. 따라서 NXT 전략은 `시세·봉 시장=NXT`, `주문 경로=NXT`로 지정합니다.
KIS 모의 연결처럼 선택한 주문 경로를 지원하지 않는 계좌는 주문 가능 수량 조회
전에 `UNSUPPORTED_CAPABILITY`로 차단됩니다.

블록의 `봉 상태`는 `완성봉만`과 `진행 중 봉 포함` 중에서 선택합니다. 완성봉만
사용하면 KRX는 15:30, NXT·통합은 NXT 애프터마켓이 끝나는 20:00 전까지 당일
일봉을 판단에서 제외하고, 매도 조건은 다음 실행일의 `매도 판단 시작` 시각에 한
번 판단합니다. 진행 중 봉을 포함하면 설정한 조회 주기마다 현재 형성 중인 OHLCV와
지표를 다시 계산해 매수·매도 조건을 판단합니다. 장중 조건은 봉 마감 전에 다시
해제될 수 있으므로 판단 로그에 사용한 봉 상태가 함께 남습니다.

## 전략에서 쓰는 공통 API

```python
class MyStrategy(Strategy):
    config = StrategyConfig(
        id="my-strategy",
        trading_account="등록한 계좌 별칭",
        market_sources={
            "primary": "KIS 연결 이름",
            "secondary": "NAMUH 연결 이름",
        },
        required_capabilities={
            "primary": ("quotes", "bars"),
            "secondary": ("quotes",),
        },
    )

    async def on_start(self, ctx):
        # 같은 연결·채널·종목은 중앙 소켓에서 한 번만 구독됩니다.
        await ctx.market.source("primary").subscribe_quotes(
            ["005930", "000660"], venue="INTEGRATED"
        )
        await ctx.market.source("primary").subscribe_orderbooks(
            "005930", venue="KRX"
        )
        ctx.scheduler.on_bar_close(
            "3m", schedule_id="morning", between=("09:00", "10:00")
        )
        ctx.scheduler.every(
            seconds=30, schedule_id="daytime", between=("10:00", "15:00")
        )

    async def on_bar(self, ctx, event):
        kis_bars = await ctx.market.source("primary").bars(
            "005930", "3m", count=55
        )
        nh_quote = await ctx.market.source("secondary").quote("005930")

        # 계좌 조회도 같은 중앙 토큰·호출 한도 관리자를 거칩니다.
        open_orders = await ctx.account.open_orders()
        buying_power = await ctx.account.buying_power(
            "005930", route="NXT"
        )

        # client_order_id는 전략이 만드는 고유 키입니다. 같은 키를 다시 보내면
        # 증권사에 재전송하지 않고 기존 주문 결과를 반환합니다.
        order = await ctx.orders.market_buy(
            "005930",
            1,
            client_order_id=f"005930-{event.scheduled_at:%Y%m%d%H%M%S}-buy",
            route="AUTO",
        )

    async def on_quote(self, ctx, quote):
        ctx.log.info(f"{quote.instrument.code} {quote.price}")

    async def on_orderbook(self, ctx, orderbook):
        ctx.log.info(f"최우선 매도 {orderbook.asks[0].price}")

    async def on_order(self, ctx, order):
        # 제출·정정·취소·부분체결·완료 상태가 같은 Order 모델로 옵니다.
        ctx.log.info(f"주문 {order.id}: {order.status}")

    async def on_fill(self, ctx, fill):
        ctx.log.info(f"체결 {fill.quantity}주 @ {fill.price}")
```

미체결 주문은 Router 주문 ID로 정정·취소합니다. Router는 증권사의 미체결 주문
조회에서 원주문과 가능수량을 확인한 뒤 한 번만 제출합니다. 응답이 불명확하면
재전송하지 않고 `UNKNOWN_RECONCILING`으로 전환합니다.

```python
amended = await ctx.orders.amend(
    order.id,
    quantity=1,
    price=73000,
    order_type="LIMIT",
)
cancelled = await ctx.orders.cancel(amended.id)  # 남은 수량 전부 취소
```

실제 제출 전에는 설정의 계좌 탭에서 계좌 주문을 허용하고, 실행 중인 전략의
상단 `주문 차단` 버튼으로 해당 전략도 허용해야 합니다. 계좌 허용 시 잔고와
주문 내역을 자동 조정합니다. 접수 여부가 불명확한 주문이 하나라도 있으면 새
주문은 열리지 않습니다. 권한은 재시작 뒤에도 보존되지만 첫 주문 전에 계좌를
다시 조정합니다.

토큰, App Key, 계좌번호는 전략 파일에 넣지 않습니다. 전략은 등록한 별칭만
사용하고 Router가 암호화 저장소와 공통 요청 큐를 관리합니다.

## 블록 전략 공유

블록 전략의 `블록` 화면에서 `내보내기`를 누르면 하나의
`전략명.trblock.json` 파일이 생성됩니다. 받는 사람은 전략 목록의 `+`를 누른 뒤
`블록 파일 가져오기`를 선택합니다. 공유 파일에는 제공 블록의 값과 일정만 들어가며
Python 원본, App Key, App Secret, 접근 토큰과 계좌번호는 포함하지 않습니다.
가져온 뒤에는 블록 화면의 드롭다운에서 받는 사람의 계좌·API와 종목 데이터 방식
(HTS 조건검색·순위검색·지정 종목)을 선택합니다. 저장할 때 Studio가 블록 정의를 검증하고 서버 소유 실행 코드로 한 번
컴파일하므로, 사용자가 생성된 Python을 직접 수정하지 않습니다.

블록 제목줄을 끌면 노드를 자유롭게 옮길 수 있고, 빈 공간을 끌면 캔버스가
이동합니다. `Ctrl+휠`과 상단 `− / +`로 확대·축소하며 `맞춤`은 전체 흐름을
화면에 맞춥니다. 노드 위치와 캔버스 보기는 블록 파일에 저장되지만 실행 판단에는
영향을 주지 않습니다. 연결선과 설정값을 컴파일러가 검증된 전략 실행 흐름으로
변환합니다.

KIS 연결 등록에서 HTS ID를 함께 입력하면 주문·체결 통보도 WebSocket으로 받습니다.
NAMUH는 계좌 통보 채널 `d2`·`d3`를 자동 구독합니다. HTS ID를 비워두거나 소켓이
잠시 끊긴 동안에는 5초 주기의 중앙 주문 조정이 같은
`on_order`·`on_fill` 이벤트를 보완합니다. 계좌 등록의 매수·매도 수수료율과 매도
세율은 소수 비율로 입력합니다(예: `0.00015` = 0.015%). 값을 비우면 임의 요율을
가정하지 않고 화면에 비용 미확정으로 표시합니다.

## 기본 예제에 입력할 값

새 설치에는 주문을 제출하지 않는 `example_strategy.py`가 들어 있습니다. 파일
상단의 다음 세 값만 자신의 설정에 맞게 바꿉니다.

| 코드의 이름 | 입력할 값 | 찾는 위치 |
| --- | --- | --- |
| `TRADING_ACCOUNT` | 계좌번호가 아닌 **계좌 별칭** | 설정 → 계좌 → 등록된 계좌 |
| `MARKET_DATA_API` | App Key가 아닌 **등록된 API 이름** | 설정 → API 키 → 등록된 API |
| `SYMBOL` | 국내주식 6자리 종목코드 | 예: 삼성전자 `005930` |

`market_sources`의 `primary`는 해당 전략 내부에서 연결을 부르는 이름입니다. 한
전략에서 여러 증권사 시세를 사용하면 `secondary`처럼 항목을 추가할 수 있습니다.
App Key, App Secret, 접근 토큰과 전체 계좌번호는 전략 파일에 입력하지 않습니다.
개인 조건식 번호·예산·매매 규칙이 담긴 전략은 기본 예제로 배포하지 않습니다.

## 공통 순위 조회

순위도 조건검색과 같은 `ctx.discovery` 서비스에서 조회합니다. 지원 유형은
`HTS_VIEW`(HTS 조회상위 20종목), `VOLUME`(거래량),
`TRADING_VALUE`(거래대금), `RISE`(상승률)입니다. 결과는 모두 순위, 종목코드,
종목명, 현재가, 등락률, 거래량, 거래대금이 있는 `RankingEntry`로 정규화되며,
증권사 응답에 없는 값은 `None`입니다.

```python
from decimal import Decimal
from trade_router import RankingKind, Venue

viewed = await ctx.discovery.source("primary").ranking(
    RankingKind.HTS_VIEW,
    limit=20,
)

traded = await ctx.discovery.source("primary").ranking(
    RankingKind.TRADING_VALUE,
    venue=Venue.INTEGRATED,
    market="KOSDAQ",
    limit=20,
    min_price=Decimal("10000"),
    max_price=Decimal("50000"),
)

risen = await ctx.discovery.source("primary").ranking(
    RankingKind.RISE,
    venue=Venue.KRX,
    market="ALL",
    limit=20,
)
```

KIS 순위 API는 실전 API 연결에서 조회합니다. 순위 조회도 토큰 재사용, 중앙 호출
한도, 중복 요청 합치기와 15초 캐시를 거치며, 처리 결과는 전략의 `시세 데이터`
탭에 `순위 조회`로 기록됩니다. 현재 NAMUH 어댑터는 공통 순위 기능을 선언하지
않으므로 외부 요청 전에 `UNSUPPORTED_CAPABILITY`로 거절합니다.

전략 잔고의 각 `Position.strategy_quantity`는 Trade Router 주문 원장의 확인된
체결량으로 복원됩니다. 같은 계좌에 수동 보유분이나 다른 전략의 보유분이 섞여도
전략은 자신의 체결 수량만 매도 대상으로 사용할 수 있습니다.

## 증권사 호출 한도

설정의 API 탭은 증권사와 접속 대상에 맞는 고정 운영값을 표시합니다. 사용자가
임의로 높일 수 없으며, 전략·계좌 조회·Studio 화면 요청은 모두 실제 App Key와
접속 대상 조합별 중앙 큐를 통과합니다. 같은 키를 여러 이름으로 등록해도 하나의
공용 한도를 사용합니다.

| 증권사·대상 | REST 운영값 | WebSocket 운영값 |
|---|---:|---|
| KIS 모의 | 초당 1회, 최소 1,000ms | 키당 1세션, 합산 41구독, 등록·해제 운영 초당 1회 |
| KIS 실전 | 초당 18회, 최소 56ms | 키당 1세션, 합산 41구독, 등록·해제 운영 초당 1회 |
| NAMUH | 초당 4회, 최소 250ms | 키당 2세션, 세션당 30구독, 등록·해제 초당 10회 |

KIS REST·세션·구독 수는 [2026년 4월 20일 기준 공식 유량 공지](https://apiportal.koreainvestment.com/community/10000000-0000-0011-0000-000000000001/post/d0d1a83f-6f8d-4437-9700-6d26702fd989)를 사용합니다.
공지에 별도 구독 제어 속도가 없어 등록·해제는 초당 1회로 보수적으로 운영합니다.
NAMUH REST는 [공식 SDK](https://github.com/PLUG-OpenAPI/nhplug-sdk) 기본값 4회(서버 상한 5회), WebSocket은 공식 SDK의 서버
한도를 사용합니다. 들어오는 실시간 데이터는 REST 횟수를 소비하지 않고, 동일
채널·종목 구독은 중앙에서 하나로 합쳐 여러 전략에 배포합니다. 같은 키를 Trade
Router 밖의 다른 프로그램에서도 사용하면 그 호출은 중앙 큐가 알 수 없으므로
증권사 한도를 함께 소비할 수 있습니다.

독립된 짧은 Python 파일이나 노트북에서는 같은 모델을 반환하는 동기 래퍼
`TradeRouterClient`를 사용할 수 있습니다. 관리형 전략은 `Strategy`와 비동기
`ctx` 서비스를 기준으로 작성합니다. 런타임 상태는 `/api/system/doctor`, 요청 큐와
호출 한도는 `/api/runtime/requests`에서 확인할 수 있습니다.

## 배포와 업데이트

GitHub에서 `v0.1.0`처럼 현재 소스 버전과 같은 태그를 푸시하면 Windows Release
워크플로가 전체 테스트를 실행하고 다음 두 파일을 Release 자산으로 만듭니다.

```text
trade-router-v0.1.0-windows.zip
trade-router-v0.1.0-windows.zip.sha256
```

처음 받는 사용자는 ZIP을 원하는 폴더에 풀고 `run.cmd`를 실행합니다. Python 3.11
이상이 필요하며 첫 실행에서 해당 폴더의 `.venv`만 자동으로 준비합니다. 실행기는
`python`, `python3`, `py -3`뿐 아니라 Windows에 등록된 Python과 일반 설치 폴더도
찾으므로 PATH 등록은 필수가 아닙니다. Python Install Manager가 Windows 정책에 막히면
[Python 3.12.10 공식 페이지](https://www.python.org/downloads/release/python-31210/)의
**Windows installer (64-bit)** `.exe`를 사용할 수 있습니다. API 키,
계좌, 전략, 주문 원장과 로그는 `%LOCALAPPDATA%\TradeRouter`에 저장되므로 프로그램
폴더를 업데이트해도 유지됩니다.

이후 버전은 설정의 **일반 → 업데이트 확인**에서 찾습니다. 새 버전이 있으면 버튼이
**업데이트 시작**으로 바뀌며, 누르면 Release ZIP과 SHA-256 파일을 함께 내려받아
압축 전체와 내부 파일 목록을 검증합니다. 검증이 끝난 뒤 전략과 연결을 정리하고
명령 창의 실행기가 프로그램 파일만 교체한 다음 같은 창에서 다시 시작합니다.
업데이트 파일은 설치 폴더의 `.trade-router-updates`에 준비하므로 Microsoft Store판
Python의 LocalCache 경로를 사용해도 실행기와 같은 파일을 확인합니다.

로컬에서 배포 ZIP만 만들려면 다음을 실행합니다.

```powershell
uv run python tools/build_release.py --version 0.1.0 --output dist
```

## 개발 확인

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
```
