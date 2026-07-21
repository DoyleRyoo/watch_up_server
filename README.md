# watch_up_server

WatchUp의 FastAPI 백엔드 서버입니다. 현재 구현 범위는 기본 애플리케이션 구조와
프로세스 상태를 확인하는 `GET /api/health`입니다.

## 로컬 실행

Python 환경을 준비한 뒤 의존성을 설치합니다.

```bash
python -m pip install -r requirements-dev.txt
cp .env.example .env
```

실제 Supabase 및 Redis 설정값은 현재 단계에서 선택 사항이며 health check에서
사용하지 않습니다. 실제 secret이 포함된 `.env`는 커밋하지 마세요.

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

## 검증

```bash
pytest
ruff check .
mypy app
python -c "from app.main import app; print(app.title)"
```

이 단계에서는 JWT 검증, Supabase/Redis 연결, 업비트 호출, 코인 검색 및 관심
코인 API를 구현하지 않습니다.
