import os, re, io, datetime, json, logging, uuid
from fastapi import FastAPI, UploadFile, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.exceptions import RequestValidationError
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from openai import OpenAI

# ─────────────────────────────
# ロガー
# ─────────────────────────────
logger = logging.getLogger("app")
logging.basicConfig(level=logging.INFO)

# ─────────────────────────────
# FastAPI / Clients
# ─────────────────────────────
app = FastAPI()
openai = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
slack = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
SLACK_CHANNEL = os.environ["SLACK_CHANNEL_ID"]

# ─────────────────────────────
# 共通エラーJSON
# ─────────────────────────────
def err_json(message: str, detail: str = ""):
    cid = str(uuid.uuid4())
    payload = {"ok": False, "error": message, "detail": detail, "correlation_id": cid}
    logger.error(f"[{cid}] {message} :: {detail}")
    return payload, cid

@app.exception_handler(Exception)
async def all_exception_handler(request: Request, exc: Exception):
    payload, _ = err_json("server failed", repr(exc))
    return JSONResponse(status_code=500, content=payload)

from fastapi.exceptions import RequestValidationError
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    payload, _ = err_json("invalid request", exc.errors().__repr__())
    return JSONResponse(status_code=422, content=payload)

# ─────────────────────────────
# 小物（favicon）
# ─────────────────────────────
@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(content=b"", media_type="image/x-icon")

# ─────────────────────────────
# プロンプト（JSON固定）
# ─────────────────────────────
SYSTEM_PROMPT = """
あなたは「会議議事録とアクション抽出」の専門家です。発話ログだけを根拠に、推測せず、
1) 要約、2) 決定事項、3) ToDo（担当者・期限YYYY-MM-DD・優先度・根拠タイムスタンプ）、4) parking_lot を抽出します。
日付は必ず正規化（例: 来週金曜 → YYYY-MM-DD）。不確定表現のものはToDoではなくparking_lotへ。
返答は **次のJSONオブジェクト1個のみ** で返してください（文章やコードブロックは不要）:

{
  "machine_json": {
    "meeting": { "title": string, "datetime": string, "participants": [{"name":string,"email":string|null}] },
    "summary": {
      "context": string,
      "key_points": [string],
      "decisions": [{"text":string,"evidence_ts":string|null}],
      "risks": [string],
      "parking_lot": [string]
    },
    "actions": [{
      "task": string,
      "owner": {"name": string, "email": string|null},
      "due_date": string|null,
      "priority": "low"|"medium"|"high"|null,
      "evidence_ts": string|null,
      "confidence": number|null,
      "notes": string|null
    }],
    "next_meeting": {
      "suggested_datetime": string|null,
      "suggested_agenda": [string]
    }
  },
  "human_summary": string
}
現在日時: {NOW_ISO}。
"""
def coerce_llm_json(obj, raw_text: str = ""):
    """
    LLMの出力を {machine_json, human_summary} 形式に補正して返す。
    想定外のキー名やラッパー欠落に耐える。
    """
    # 期待通り
    if isinstance(obj, dict) and "machine_json" in obj and "human_summary" in obj:
        return obj["machine_json"], obj["human_summary"]

    # 大文字キーの揺れ
    if isinstance(obj, dict) and "MACHINE_JSON" in obj and "HUMAN_SUMMARY" in obj:
        return obj["MACHINE_JSON"], obj["HUMAN_SUMMARY"]

    # ラッパーが無く、machine_json そのものが返ってきたケース
    likely_keys = {"meeting", "summary", "actions", "next_meeting"}
    if isinstance(obj, dict) and any(k in obj for k in likely_keys):
        # 人間向け要約が無ければ、簡易要約を合成
        ks = []
        try:
            ks = obj.get("summary", {}).get("key_points", []) or []
        except Exception:
            pass
        hs = " / ".join(ks[:6]) if ks else "会議の要点を抽出しました。"
        return obj, hs

    # {"machine_json": {...}} だけ返るケース
    if isinstance(obj, dict) and "machine_json" in obj:
        return obj["machine_json"], obj.get("human_summary") or "会議サマリーを作成しました。"

    # テキスト内に埋め込まれたJSONを救出（最初の { ... } を再パース）
    try:
        m = re.search(r"\{[\s\S]*\}", raw_text)
        if m:
            inner = json.loads(m.group(0))
            return coerce_llm_json(inner)
    except Exception:
        pass

    raise ValueError("missing keys in JSON response")

def build_user_prompt(title, meeting_dt, participants, transcript_text):
    meta = f"""<MEETING_META>
会議名: {title or "未設定"}
会議日時: {meeting_dt}
参加者: {participants}
</MEETING_META>
"""
    return meta + "\n<TRANSCRIPT>\n" + transcript_text + "\n</TRANSCRIPT>"

# ─────────────────────────────
# HTML（フロント）
# ─────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index():
    return """<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><title>会議録アップロード → Slackにドラフト通知</title></head>
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
# メイン：/upload
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

    # 1) 音声→テキスト
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

    # 2) LLM抽出（まずは tool call、無ければJSON本文の3段パース）
    try:
        now_iso = datetime.datetime.now().astimezone().isoformat()
        sys = SYSTEM_PROMPT.format(NOW_ISO=now_iso)
        user_prompt = build_user_prompt(title, meeting_datetime_iso or now_iso, participants, transcript_text)

        tools = [{
            "type": "function",
            "function": {
                "name": "produce_minutes",
                "description": "会議議事録のJSONと、人間向け要約を返す",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "machine_json": {"type": "object"},
                        "human_summary": {"type": "string"}
                    },
                    "required": ["machine_json","human_summary"]
                }
            }
        }]

        resp = openai.chat.completions.create(
            model="gpt-4o",          # 安定重視（miniに戻してもOK）
            temperature=0.0,
            messages=[
                {"role": "system", "content": sys},
                {"role": "user", "content": user_prompt}
            ],
            tools=tools,
            tool_choice={"type": "function", "function": {"name": "produce_minutes"}}
        )

        machine_json = None
        human_summary = None

        # --- ① tool_calls から取得（最優先） ---
        try:
            tcalls = resp.choices[0].message.tool_calls
            if tcalls and tcalls[0].function and tcalls[0].function.arguments:
                args_text = tcalls[0].function.arguments
                # デバッグログ（先頭だけ）
                logger.info("tool args head: %s", args_text[:180].replace("\n"," "))
                parsed = json.loads(args_text)
                machine_json, human_summary = coerce_llm_json(parsed, args_text)
        except Exception as _e:
            logger.warning("tool call parse fallback: %s", repr(_e))

        # --- ② tool_calls が無い/失敗 → 本文JSONを3段ガードで取得 ---
        if machine_json is None or human_summary is None:
            content = resp.choices[0].message.content or ""
            logger.info("content head: %s", content[:180].replace("\n"," "))
            parsed = None
            # 2-1 素直に
            try:
                if content.strip():
                    parsed = json.loads(content)
            except Exception:
                parsed = None
            # 2-2 コードフェンス/前後の文字を剥がす
            if parsed is None:
                m = re.search(r"\{[\s\S]*\}", content)
                if m:
                    try:
                        parsed = json.loads(m.group(0))
                    except Exception:
                        parsed = None
            if parsed is None:
                raise ValueError("JSON parse failed (raw head): " + content[:180])
            machine_json, human_summary = coerce_llm_json(parsed, content)

    except Exception as e:
        # 生ログをもう少し出す
        payload, _ = err_json("parse failed", f"LLM出力の解析に失敗しました: {e}")
        return JSONResponse(status_code=500, content=payload)

    # 3) Slackへドラフト投稿
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
# Slackボタン
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
