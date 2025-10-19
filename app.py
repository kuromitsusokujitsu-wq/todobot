import os, re, io, datetime, json, logging, uuid
from fastapi import FastAPI, UploadFile, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.exceptions import RequestValidationError
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from openai import OpenAI

# ─────────────────────────────
# ロガー設定
# ─────────────────────────────
logger = logging.getLogger("app")
logging.basicConfig(level=logging.INFO)

# ─────────────────────────────
# FastAPI / クライアント初期化
# ─────────────────────────────
app = FastAPI()
openai = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
slack = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
SLACK_CHANNEL = os.environ["SLACK_CHANNEL_ID"]

# ─────────────────────────────
# エラーハンドラ共通関数
# ─────────────────────────────
def err_json(message: str, detail: str = ""):
    cid = str(uuid.uuid4())
    payload = {"ok": False, "error": message, "detail": detail, "correlation_id": cid}
    logger.error(f"[{cid}] {message} :: {detail}")
    return payload, cid

# ─────────────────────────────
# 例外ハンドラ
# ─────────────────────────────
@app.exception_handler(Exception)
async def all_exception_handler(request: Request, exc: Exception):
    payload, _ = err_json("server failed", repr(exc))
    return JSONResponse(status_code=500, content=payload)

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    payload, _ = err_json("invalid request", exc.errors().__repr__())
    return JSONResponse(status_code=422, content=payload)

# ─────────────────────────────
# favicon対策
# ─────────────────────────────
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(content=b"", media_type="image/x-icon")

# ─────────────────────────────
# システムプロンプト
# ─────────────────────────────
SYSTEM_PROMPT = """あなたは「会議議事録とアクション抽出」の専門家です。発話ログだけを根拠に、推測せず、
1) 要約、2) 決定事項、3) ToDo（担当・期限YYYY-MM-DD・優先度・根拠タイムスタンプ）を返す。
不確定表現（検討/かも等）はToDoに入れずparking_lotへ。出力は MACHINE_JSON と HUMAN_SUMMARY の二部構成。
現在日時: {NOW_ISO}。会議日時は入力に含まれます。
"""

def build_user_prompt(title, meeting_dt, participants, transcript_text):
    meta = f"""<MEETING_META>
会議名: {title or "未設定"}
会議日時: {meeting_dt}
参加者: {participants}
</MEETING_META>
"""
    return meta + "\n<TRANSCRIPT>\n" + transcript_text + "\n</TRANSCRIPT>"

def segments_to_transcript(segments):
    lines = []
    for seg in segments or []:
        ss = int(seg.get("start", 0))
        ts = str(datetime.timedelta(seconds=ss))
        if len(ts) < 8: ts = "0" + ts
        text = (seg.get("text") or "").strip()
        if text:
            lines.append(f"[{ts}] {text}")
    return "\n".join(lines) if lines else ""

# ─────────────────────────────
# HTMLフロント
# ─────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index():
    return """<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><title>議事録ドラフトメーカー</title></head>
<body style="font-family:sans-serif;max-width:720px;margin:40px auto;line-height:1.6">
<h1>会議録アップロード → Slackにドラフト通知</h1>
<form id="f" method="post" action="/upload" enctype="multipart/form-data">
  <label>会議タイトル</label><br>
  <input name="title" placeholder="新LPプロジェクト定例" style="width:100%;padding:8px"><br><br>
  <label>会議日時（ISO推奨 / 空なら現在時刻）</label><br>
  <input name="meeting_datetime_iso" placeholder="2025-10-19T10:00:00+09:00" style="width:100%;padding:8px"><br><br>
  <label>参加者（JSON配列。氏名やメールを含めてOK）</label><br>
  <textarea name="participants" rows="3" style="width:100%;padding:8px">["名前 <kouki@example.com>","名前 <sato@example.com>"]</textarea><br><br>
  <label>録音/録画ファイル（mp3/m4a/mp4/wav）</label><br>
  <input type="file" name="file" accept="audio/*,video/*" required><br><br>
  <button>アップロードしてSlackにドラフト</button>
</form>
<div id="out" style="margin-top:24px;color:#333;white-space:pre-wrap"></div>
<script>
const out = document.getElementById('out');
document.getElementById('f').addEventListener('submit', async (e)=>{
  e.preventDefault();
  out.style.color = "#333";
  out.textContent = "処理中…（数十秒〜数分）";
  try {
    const fd = new FormData(e.target);
    const res = await fetch('/upload', {method:'POST', body: fd});
    let j = {};
    try { j = await res.json(); } catch (_) {}
    if (!res.ok || j.ok === false) {
      const msg = (j && (j.error || j.detail)) ? `${j.error}\\n${j.detail}` : `HTTP ${res.status}`;
      const cid = j && j.correlation_id ? `\\nCID: ${j.correlation_id}` : "";
      out.style.color = "#c00";
      out.textContent = "エラーが発生しました：\\n" + msg + cid + "\\n（改善案：短いmp3で再試験／Slack設定確認）";
      return;
    }
    out.style.color = "#0a0";
    out.textContent = "Slackにドラフトを投稿しました ✅";
  } catch (err) {
    out.style.color = "#c00";
    out.textContent = "ネットワークエラー：\\n" + (err?.message || err);
  }
});
</script>
</body></html>"""

# ─────────────────────────────
# メイン処理 /upload
# ─────────────────────────────
@app.post("/upload")
async def upload(file: UploadFile, title: str = Form(None),
                 meeting_datetime_iso: str = Form(None),
                 participants: str = Form("[]")):
    # 入力チェック
    if not file or not file.filename:
        payload, _ = err_json("no file", "ファイルが添付されていません")
        return JSONResponse(status_code=400, content=payload)
    if file.content_type and not (file.content_type.startswith("audio/") or file.content_type.startswith("video/")):
        payload, _ = err_json("unsupported type", f"content_type={file.content_type}")
        return JSONResponse(status_code=415, content=payload)

    # 1) 文字起こし
    audio_bytes = await file.read()
    audio_file = io.BytesIO(audio_bytes)
    audio_file.name = file.filename or "upload"

    try:
        tr = openai.audio.transcriptions.create(
            model="gpt-4o-transcribe",
            file=audio_file
        )
    except Exception as e:
        payload, _ = err_json("transcribe failed", str(e))
        return JSONResponse(status_code=500, content=payload)

    transcript_text = getattr(tr, "text", "") or ""

    # 2) LLMで要約/ToDo抽出
    try:
        now_iso = datetime.datetime.now().astimezone().isoformat()
        sys = SYSTEM_PROMPT.format(NOW_ISO=now_iso)
        user_prompt = build_user_prompt(title, meeting_datetime_iso or now_iso, participants, transcript_text)

        resp = openai.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.2,
            messages=[
                {"role": "system", "content": sys},
                {"role": "user", "content": user_prompt}
            ]
        )
        content = resp.choices[0].message.content
        m = re.search(r"MACHINE_JSON:\\s*([\\s\\S]*?)\\n\\nHUMAN_SUMMARY:\\s*([\\s\\S]*)$", content)
        if not m:
            payload, _ = err_json("parse failed", "LLM出力の解析に失敗しました")
            return JSONResponse(status_code=500, content=payload)
        machine_json = json.loads(m.group(1))
        human_summary = m.group(2).strip()
    except Exception as e:
        payload, _ = err_json("LLM failed", str(e))
        return JSONResponse(status_code=500, content=payload)

    # 3) Slackにドラフト投稿
    try:
        header = f"【議事録ドラフト】{title or '会議'}"
        blocks = [
            {"type": "header", "text": {"type": "plain_text", "text": header}},
            {"type": "section", "text": {"type": "mrkdwn", "text": human_summary}},
            {"type": "actions", "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "✅ 確定して共有"},
                 "style": "primary", "value": "confirm", "action_id": "confirm_minutes"}
            ]}
        ]
        result = slack.chat_postMessage(channel=SLACK_CHANNEL, text="議事録ドラフト", blocks=blocks)
        ts = result["ts"]
    except SlackApiError as e:
        payload, _ = err_json("slack post failed", str(e))
        return JSONResponse(status_code=500, content=payload)

    return {"ok": True, "slack_ts": ts, "machine_json": machine_json, "human_summary": human_summary}

# ─────────────────────────────
# Slackボタン操作
# ─────────────────────────────
@app.post("/slack/interact")
async def slack_interact(req: Request):
    form = await req.form()
    payload = json.loads(form.get("payload", "{}"))
    action_id = (payload.get("actions") or [{}])[0].get("action_id")
    channel = payload.get("channel", {}).get("id")
    message_ts = payload.get("message", {}).get("ts")

    if action_id == "confirm_minutes" and channel and message_ts:
        slack.chat_update(
            channel=channel, ts=message_ts,
            text="議事録を確定しました。",
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "*議事録を確定しました。* チーム共有を開始します。"}}]
        )
    return {"ok": True}
