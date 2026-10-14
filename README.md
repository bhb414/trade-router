# Trade Router

Trade Router는 국내 증권사 API 차이를 하나의 Python 인터페이스로 통합하는
라이브러리다. 사용자가 별도 데몬이나 관리 화면을 조작하지 않아도 되도록
상위 프로그램 안에서 실행된다.

## 역할

- 증권사 인증과 토큰 재사용
- 호출 제한과 요청 우선순위
- 현재가·호가·봉·계좌·주문 모델 정규화
- WebSocket 연결과 구독 공유
- 주문 멱등성, 주문 가능 수량, 위험 한도 검증
- KIS, NAMUH와 이후 추가될 증권사 어댑터 제공

## 공개 형태

```python
async with TradeRouter(settings) as router:
    quote = await router.market.quote("005930")
    await router.orders.market_buy(
        account="main",
        code="005930",
        quantity=1,
        client_order_id="my-strategy-20261015-090000-buy",
    )
```

Trade Router에는 사용자용 웹 화면, 전략 편집기, 전략 스케줄러를 넣지 않는다.
그 기능은 별도 프로그램인 Trade Router Studio가 담당한다.

## 새 구현 원칙

- 비동기 Python API를 기준 구현으로 사용한다.
- 금액·가격·수량은 `Decimal`, 시각은 시간대가 있는 값으로 표현한다.
- 지원하지 않는 기능은 명확한 기능 오류로 반환한다.
- 주문 응답이 불명확하면 재전송하지 않고 확인 상태로 남긴다.
- 증권사별 원본 응답은 어댑터 밖으로 노출하지 않는다.
- 첫 구현은 작은 공통 계약과 paper 어댑터부터 시작한다.

