"""
YouTube auto-upload: produce.py'in urettigi <video>.json meta dosyasini alir,
videoyu + thumbnail'i YouTube'a yukler. Coklu kanal destegi (token_<kanal>.json).

Kurulum (bir kez): client_secret.json'u bu klasore koy (Google Cloud OAuth).
Ilk calistirmada tarayici acilir, kanalinla izin verirsin -> token kaydedilir.

Kullanim:
  python upload.py <meta.json> [privacy] [kanal]
  ornek: python upload.py output\\stuxnet.json unlisted kanal1
  privacy: private | unlisted | public   (varsayilan: unlisted = guvenli)
"""
import os, json, sys
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube"]
HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT_SECRET = os.path.join(HERE, "client_secret.json")
CATEGORY_ID = "27"   # 27=Education, 28=Science & Technology


def get_service(channel="default"):
    token_file = os.path.join(HERE, f"token_{channel}.json")
    creds = None
    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CLIENT_SECRET):
                raise SystemExit(f"client_secret.json yok: {CLIENT_SECRET}\n"
                                 "Google Cloud'dan OAuth istemci indir, buraya koy.")
            flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_file, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def upload(meta_path, privacy="unlisted", channel="default"):
    meta = json.load(open(meta_path, encoding="utf-8"))
    video = meta["video"]
    if not os.path.exists(video):
        raise SystemExit(f"video yok: {video}")

    yt = get_service(channel)
    desc = meta.get("description", "")
    # SHORTS: YouTube dikey + kisa videoyu zaten otomatik Shorts sayiyor, ama
    # #Shorts etiketi bedava sigorta (eski istemcilerde ve siralamada ise yariyor).
    if "#shorts" not in desc.lower():
        desc = (desc + " #Shorts").strip()
    tags = [w[1:] for w in desc.split() if w.startswith("#")][:15]

    body = {
        "snippet": {
            "title": meta.get("title", "video")[:100],
            "description": desc,
            "tags": tags,
            "categoryId": CATEGORY_ID,
        },
        "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
    }
    media = MediaFileUpload(video, resumable=True, chunksize=-1)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    print(f"  yukleniyor ({privacy}): {os.path.basename(video)}")
    resp = None
    while resp is None:
        status, resp = req.next_chunk()
        if status:
            print(f"    %{int(status.progress()*100)}")
    vid = resp["id"]
    print(f"  YUKLENDI: https://youtu.be/{vid}")

    thumb = meta.get("thumbnail")
    if thumb and os.path.exists(thumb):
        try:
            yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(thumb)).execute()
            print("  thumbnail set edildi")
        except Exception as e:
            print("  thumbnail atlandi (kanal telefonla dogrulanmali):", str(e)[:100])
    return vid


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Kullanim: python upload.py <meta.json> [private|unlisted|public] [kanal]")
        sys.exit(1)
    meta_path = sys.argv[1]
    privacy = sys.argv[2] if len(sys.argv) > 2 else "unlisted"
    channel = sys.argv[3] if len(sys.argv) > 3 else "default"
    upload(meta_path, privacy, channel)
