import os, re, io, datetime, json
from fastapi import FastAPI, UploadFile, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from openai import OpenAI

app = FastAPI()
openai = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
slack = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
SLACK_CHANNEL = os.environ["SLACK_CHANNEL_ID"]

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
        if len(ts) < 8: ts = "0"+ts  # 0:00:05 → 00:00:05
        text = (seg.get("text") or "").strip()
        if text:
            lines.append(f"[{ts}] {text}")
    return "\n".join(lines) if lines else ""

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
  <textarea name="participants" rows="3" style="width:100%;padding:8px">["中村浩規 <kouki@example.com>","佐藤 <sato@example.com>"]</textarea><br><br>
  <label>録音/録画ファイル（mp3/m4a/mp4/wav）</label><br>
  <input type="file" name="file" accept="audio/*,video/*" required><br><br>
  <button>アップロードしてSlackにドラフト</button>
</form>
<div id="out" style="margin-top:24px;color:#333"></div>
<script>
document.getElementById('f').addEventListener('submit', async (e)=>{
  e.preventDefault();
  const out = document.getElementById('out');
  out.textContent = "処理中…（数十秒〜数分）";
  const fd = new FormData(e.target);
  const res = await fetch('/upload',{method:'POST', body: fd});
  const j = await res.json();
  out.textContent = res.ok ? "Slackにドラフトを投稿しました ✅" : "エラー: " + (j.error || "unknown");
});
</script>
</body></html>"""

@app.post("/upload")
async def upload(file: UploadFile, title: str = Form(None), meeting_datetime_iso: str = Form(None), participants: str = Form("[]")):
    # 1) Whisperで文字起こし
    audio_bytes = await file.read()
    audio_file = io.BytesIO(audio_bytes)
    ext = file.filename.split(".")[-1].lower() if "." in file.filename else "mp3"
    audio_file.name = f"upload.{ext}"

    tr = openai.audio.transcriptions.create(
        model="whisper-1",
        file=(audio_file.name, audio_file, file.content_type or "audio/mpeg"),
        response_format="verbose_json"
    )
    text = tr.text
    segments = getattr(tr, "segments", None)
    transcript_text = segments_to_transcript(segments) if segments else (text or "")

    # 2) LLMで要約/ToDo抽出
    now_iso = datetime.datetime.now().astimezone().isoformat()
    sys = SYSTEM_PROMPT.format(NOW_ISO=now_iso)
    user_prompt = build_user_prompt(title, meeting_datetime_iso or now_iso, participants, transcript_text)

    resp = openai.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.2,
        messages=[{"role":"system","content":sys},
                  {"role":"user","content":user_prompt}]
    )
    content = resp.choices[0].message.content

    m = re.search(r"MACHINE_JSON:\s*([\s\S]*?)\n\nHUMAN_SUMMARY:\s*([\s\S]*)$", content)
    if not m:
        return JSONResponse({"error":"LLM出力の解析に失敗しました","raw":content}, status_code=500)
    machine_json = json.loads(m.group(1))
    human_summary = m.group(2).strip()

    # 3) Slackにドラフト投稿
    header = f"【議事録ドラフト】{title or '会議'}"
    blocks = [
        {"type":"header","text":{"type":"plain_text","text":header}},
        {"type":"section","text":{"type":"mrkdwn","text":human_summary}},
        {"type":"actions","elements":[
            {"type":"button","text":{"type":"plain_text","text":"✅ 確定して共有"},
             "style":"primary","value":"confirm","action_id":"confirm_minutes"}
        ]}
    ]
    try:
        result = slack.chat_postMessage(channel=SLACK_CHANNEL, text="議事録ドラフト", blocks=blocks)
        ts = result["ts"]
    except SlackApiError as e:
        return JSONResponse({"error":"Slack投稿に失敗", "detail":str(e)}, status_code=500)

    return {"ok": True, "slack_ts": ts, "machine_json": machine_json, "human_summary": human_summary}

@app.post("/slack/interact")
async def slack_interact(req: Request):
    form = await req.form()
    payload = json.loads(form.get("payload","{}"))
    action_id = (payload.get("actions") or [{}])[0].get("action_id")
    channel = payload.get("channel",{}).get("id")
    message_ts = payload.get("message",{}).get("ts")
    if action_id == "confirm_minutes" and channel and message_ts:
        slack.chat_update(
            channel=channel, ts=message_ts,
            text="議事録を確定しました。",
            blocks=[{"type":"section","text":{"type":"mrkdwn","text":"*議事録を確定しました。* チーム共有を開始します。"}}]
        )
    return {"ok": True}
