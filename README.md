# Trade Router

Trade Router는 개인 컴퓨터에서 Python 자동매매 전략을 작성하고 실행하는
가벼운 로컬 웹 프로그램입니다. 증권사별 차이는 내부 Router 라이브러리가
통일하고, 사용자는 Studio 화면 하나만 엽니다.

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
- 사용자 데이터 폴더의 일반 `.py` 전략 작성·저장
- 전략별 시작·중지·상태·로그 확인
- KIS·NAMUH API 키와 거래 계좌 등록
- Windows 사용자 범위 DPAPI를 사용한 API 키·계좌번호 암호화
- 공통 종목, 시세, 주문 모델의 첫 계약
- GitHub 최신 Release 확인

증권사 인증과 실거래 주문은 아직 연결하지 않았습니다. 실제 주문 기능은
공통 안전장치와 증권사별 계약 테스트를 먼저 추가한 뒤 단계적으로 연결합니다.

## 개발 확인

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
```
