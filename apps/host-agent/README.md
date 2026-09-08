# Windows host agent — M0 synthetic video

M0는 Python 3.12와 `aiortc`로 합성 1600×900/15fps 화면을 전송한다. 실제 Desktop Duplication과 SendInput은 포함하지 않는다.

환경 변수:

- `MIRROR_WS_URL`: 기본값 `ws://127.0.0.1:8787/ws`
- `MIRROR_DEV_TICKET`: 10분 이하 수명의 agent ticket
- `MIRROR_DEVICE_ID`: ticket과 동일한 device ID
- `MIRROR_SESSION_ID`: ticket과 동일한 session ID
- `MIRROR_VIDEO_PROFILE`: `balanced`(기본, 1600×900/15fps); `low`(1280×720/10fps) 또는 `high`(1920×1080/20fps)
- `MIRROR_H264_BACKEND`: `libx264`(기본), `auto`, 또는 `h264_qsv`
- `MIRROR_H264_PRESET`: `fast`(기본) 등 인코더 속도/화질 프리셋

실행:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -e .\apps\host-agent
$env:MIRROR_DEV_TICKET='short-lived-ticket'
$env:MIRROR_DEVICE_ID='device_0123456789abcdef'
$env:MIRROR_SESSION_ID='session_0123456789abcdef'
.\.venv\Scripts\python -m mirror_host_agent
```

ticket이나 SDP 원문은 로그에 출력하지 않는다.
