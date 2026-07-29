# watch_up_server

WatchUp의 FastAPI 백엔드 서버입니다. 현재 `develop_steps.md`의 2단계까지 구현되어
기본 애플리케이션 구조, 공개 health check, Supabase JWT 검증, 요청별 사용자
Supabase Client 생성을 제공합니다.

## 로컬 실행

Python 환경을 준비한 뒤 의존성을 설치합니다.

```bash
python -m pip install -r requirements-dev.txt
cp .env.example .env
```

실제 secret이 포함된 `.env`는 커밋하지 마세요. `/api/health`는 Supabase 설정이
비어 있어도 동작하며, 인증 dependency가 호출될 때 아래 설정이 필요합니다.

```text
SUPABASE_URL
SUPABASE_ANON_KEY
SUPABASE_JWKS_URL
SUPABASE_ISSUER
SUPABASE_AUDIENCE=authenticated
```

개발 서버를 실행합니다.

```bash
uvicorn app.main:app --reload
```

애플리케이션 상태는 인증 없이 확인할 수 있습니다.

```bash
curl http://127.0.0.1:8000/api/health
```

응답은 다음과 같습니다.

```json
{
  "data": {
    "status": "ok"
  },
  "meta": null
}
```

## 인증 구조

보호 API가 추가되면 `Authorization: Bearer {access_token}`을 인증 dependency로
검증합니다. 검증은 Supabase JWKS의 공개키와 `kid`를 사용하며 `ES256`과
`RS256`만 허용합니다. 서명, `exp`, `iss`, `aud`, 필수 UUID 형식 `sub`를 모두
검증하고, 검증된 `sub`만 내부 사용자 ID로 사용합니다.

JWKS 응답은 PyJWT의 공개 cache API로 5분간 보관하고 조회 timeout은 5초입니다.
캐시에 없는 `kid`는 JWKS를 한 번만 새로 받은 뒤 다시 확인합니다. 계속 존재하지
않는 `kid`는 30초 동안 최대 32개까지 bounded negative cache에 저장하여 반복
요청이 무제한 원격 갱신을 만들지 않게 합니다. JWKS 장애는 잘못된 사용자 토큰이나
만료 토큰으로 위장하지 않고 공통 500 처리로 전달합니다.

인증 성공 후에는 Supabase anon key를 `apikey`로 유지하고 검증된 사용자 JWT를
`Authorization`으로 설정한 새 Supabase SDK 객체를 요청마다 생성합니다. 사용자
Client 사이에 인증 상태를 공유하지 않으며 로그인, refresh token 저장, 세션 갱신,
service role 사용을 하지 않습니다. HTTP 연결 풀은 사용자 상태 없이 공유하고
10초 timeout을 적용하며 FastAPI lifespan 종료 시 닫습니다.

인증 실패 응답은 다음 정책을 사용합니다.

- 토큰 없음 또는 사용할 수 없는 형식/검증 실패: 401 `AUTH_REQUIRED`
- 만료 토큰: 401 `AUTH_TOKEN_EXPIRED`
- 두 응답 모두 `WWW-Authenticate: Bearer` 포함
- JWKS 또는 서버 설정 장애: 500 `INTERNAL_SERVER_ERROR`

프로덕션에는 인증 테스트용 endpoint를 추가하지 않았습니다. 현재 공개 endpoint는
기존 `GET /api/health`뿐입니다.

## 검증

```bash
python -m pytest -q
python -m ruff check app tests main.py
python -m ruff format --check app tests main.py
python -m mypy app
python -c "from app.main import app; print('application import: OK')"
```

실제 Supabase 프로젝트와 테스트 사용자의 access token이 제공된 환경에서만 RLS
통합 검증을 별도로 수행해야 합니다. 저장소 단위 테스트는 임시 RSA 키와 로컬 가짜
JWKS 서버 및 mock HTTP transport를 사용하므로 실제 secret이나 외부 서비스가
필요하지 않습니다.

코인 검색, 관심 코인 CRUD, Redis, 업비트 연동과 공통 API 계약 전체 구현은
`develop_steps.md`의 3단계 이후 범위이며 아직 구현하지 않았습니다.
