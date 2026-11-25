import io
import os
import re
from google import genai
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from PIL import Image, ImageDraw, ImageFont


ACTIVE_STATUS_TABS = [
    "OffersSheet_Offer_Status",
    "CS_Offer_Status",
    "TS_Offer_Status",
]


def sanitize_folder_name(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]+', "_", name.strip())
    return re.sub(r"\s+", " ", cleaned)[:80]


def get_clients():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        scopes=scopes,
    )
    creds.refresh(Request())

    sheets_service = build("sheets", "v4", credentials=creds)
    drive_service = build("drive", "v3", credentials=creds)
    gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return sheets_service, drive_service, gemini_client


def fetch_active_offers(sheets_service, spreadsheet_id: str) -> list[dict]:
    offers = []
    seen_keys = set()

    for tab in ACTIVE_STATUS_TABS:
        try:
            res = (
                sheets_service.spreadsheets()
                .values()
                .get(spreadsheetId=spreadsheet_id, range=f"{tab}!A:Z")
                .execute()
            )
        except Exception as exc:
            print(f"[WARN] Could not read tab {tab}: {exc}")
            continue

        values = res.get("values", [])
        if len(values) < 2:
            continue

        headers = [str(h).strip().lower() for h in values[0]]
        col_map = {h: idx for idx, h in enumerate(headers) if h}

        for row in values[1:]:
            if not any(str(c).strip() for c in row):
                continue

            def get_col(candidates):
                for c in candidates:
                    if c in col_map and col_map[c] < len(row):
                        val = str(row[col_map[c]]).strip()
                        if val:
                            return val
                return ""

            company = get_col(["company", "account", "offer", "شركة"])
            status = get_col(["status", "state", "الحالة"])
            if not company or status.lower() != "active":
                continue

            details = {
                "source_tab": tab,
                "company": company,
                "status": status,
                "location": get_col(["location", "work location", "المكان"]),
                "level": get_col(["level", "language", "english level", "المستوى"]),
                "experience": get_col(["experience", "exp", "الخبرة"]),
                "graduation": get_col(["graduation st", "education", "التعليم"]),
                "max_age": get_col(["max age", "age", "السن"]),
                "transportation": get_col(["transportation", "المواصلات"]),
                "interview": get_col(["interview", "المقابلة"]),
            }

            unique_key = f"{tab}::{company.lower()}"
            if unique_key not in seen_keys:
                seen_keys.add(unique_key)
                offers.append(details)

    return offers


def generate_caption(gemini_client, offer: dict) -> str:
    prompt = f"""
اكتب إعلان توظيف واضح ومرتب باللغة الإنجليزية والعربية المختصرة للتوظيف في مصر بناءً على البيانات التالية فقط، دون اختلاق أي معلومات إضافية:
- الشركة / الحساب: {offer['company']}
- المكان: {offer['location'] or 'حسب التفاصيل'}
- المستوى المطلوب: {offer['level'] or 'حسب التفاصيل'}
- الخبرة: {offer['experience'] or 'حسب التفاصيل'}
- المؤهل / الدراسة: {offer['graduation'] or 'حسب التفاصيل'}
- السن الأقصى: {offer['max_age'] or 'حسب التفاصيل'}
- المواصلات: {offer['transportation'] or 'حسب التفاصيل'}
- نوع المقابلة: {offer['interview'] or 'حسب التفاصيل'}

اجعل التنسيق مناسباً للنشر على فيسبوك وواتساب مع نقاط واضحة ودعوة للتقديم.
""".strip()

    response = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )
    return (response.text or "").strip()


def build_post_image(offer: dict) -> bytes:
    width, height = 1080, 1080
    img = Image.new("RGB", (width, height), color=(18, 28, 45))
    draw = ImageDraw.Draw(img)

    draw.rectangle([0, 0, width, 160], fill=(37, 99, 235))
    draw.rounded_rectangle([60, 210, width - 60, height - 80], radius=28, fill=(28, 42, 66))

    font_title = ImageFont.load_default(size=44)
    font_body = ImageFont.load_default(size=30)

    draw.text((90, 55), "NOW HIRING", fill=(255, 255, 255), font=font_title)

    lines = [
        f"Role / Account: {offer['company']}",
        f"Location: {offer['location'] or 'N/A'}",
        f"Language Level: {offer['level'] or 'N/A'}",
        f"Experience: {offer['experience'] or 'N/A'}",
        f"Education: {offer['graduation'] or 'N/A'}",
        f"Max Age: {offer['max_age'] or 'N/A'}",
        f"Transportation: {offer['transportation'] or 'N/A'}",
        f"Interview: {offer['interview'] or 'N/A'}",
    ]

    y = 260
    for line in lines:
        draw.text((100, y), line[:68], fill=(241, 245, 249), font=font_body)
        y += 82

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def list_child_folders(drive_service, parent_id: str) -> dict[str, str]:
    folders = {}
    page_token = None
    query = (
        f"'{parent_id}' in parents and "
        "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    while True:
        resp = (
            drive_service.files()
            .list(
                q=query,
                fields="nextPageToken, files(id, name)",
                pageSize=100,
                pageToken=page_token,
            )
            .execute()
        )
        for item in resp.get("files", []):
            folders[item["name"]] = item["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return folders


def ensure_folder(drive_service, parent_id: str, folder_name: str, existing: dict[str, str]) -> str:
    if folder_name in existing:
        return existing[folder_name]
    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    created = drive_service.files().create(body=metadata, fields="id").execute()
    folder_id = created["id"]
    existing[folder_name] = folder_id
    return folder_id


def upsert_file(drive_service, folder_id: str, filename: str, content_bytes: bytes, mime_type: str):
    escaped_name = filename.replace("'", "\\'")
    query = (
        f"name = '{escaped_name}' and '{folder_id}' in parents and trashed = false"
    )
    existing = drive_service.files().list(q=query, fields="files(id)").execute().get("files", [])
    media = MediaIoBaseUpload(io.BytesIO(content_bytes), mimetype=mime_type, resumable=False)

    if existing:
        file_id = existing[0]["id"]
        drive_service.files().update(fileId=file_id, media_body=media).execute()
    else:
        metadata = {"name": filename, "parents": [folder_id]}
        drive_service.files().create(body=metadata, media_body=media, fields="id").execute()


def main():
    spreadsheet_id = os.environ["MASTER_SHEET_ID"]
    root_folder_id = os.environ["DRIVE_FOLDER_ID"]

    sheets_service, drive_service, gemini_client = get_clients()
    active_offers = fetch_active_offers(sheets_service, spreadsheet_id)
    existing_folders = list_child_folders(drive_service, root_folder_id)

    expected_names = set()

    for offer in active_offers:
        folder_name = sanitize_folder_name(f"{offer['company']} - {offer['source_tab']}")
        expected_names.add(folder_name)

        sub_folder_id = ensure_folder(drive_service, root_folder_id, folder_name, existing_folders)
        caption_text = generate_caption(gemini_client, offer)
        image_bytes = build_post_image(offer)

        upsert_file(
            drive_service,
            sub_folder_id,
            "caption.txt",
            caption_text.encode("utf-8"),
            "text/plain; charset=utf-8",
        )
        upsert_file(
            drive_service,
            sub_folder_id,
            "post.png",
            image_bytes,
            "image/png",
        )
        print(f"[OK] Synced: {folder_name}")

    for folder_name, folder_id in existing_folders.items():
        if folder_name not in expected_names:
            drive_service.files().delete(fileId=folder_id).execute()
            print(f"[DELETED] Removed inactive/hold folder: {folder_name}")


if __name__ == "__main__":
    main()import io
import json
import os
import re
from google import genai
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from PIL import Image, ImageDraw, ImageFont


ACTIVE_STATUS_TABS = [
    "OffersSheet_Offer_Status",
    "CS_Offer_Status",
    "TS_Offer_Status",
]


def sanitize_folder_name(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]+', "_", name.strip())
    return re.sub(r"\s+", " ", cleaned)[:80]


def get_clients():
    sa_raw = os.environ["GCP_SERVICE_ACCOUNT_JSON"]
    sa_info = json.loads(sa_raw)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = service_account.Credentials.from_service_account_info(sa_info, scopes=scopes)
    sheets_service = build("sheets", "v4", credentials=creds)
    drive_service = build("drive", "v3", credentials=creds)
    gemini_client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return sheets_service, drive_service, gemini_client


def fetch_active_offers(sheets_service, spreadsheet_id: str) -> list[dict]:
    offers = []
    seen_keys = set()

    for tab in ACTIVE_STATUS_TABS:
        try:
            res = (
                sheets_service.spreadsheets()
                .values()
                .get(spreadsheetId=spreadsheet_id, range=f"{tab}!A:Z")
                .execute()
            )
        except Exception as exc:
            print(f"[WARN] Could not read tab {tab}: {exc}")
            continue

        values = res.get("values", [])
        if len(values) < 2:
            continue

        headers = [str(h).strip().lower() for h in values[0]]
        col_map = {h: idx for idx, h in enumerate(headers) if h}

        for row in values[1:]:
            if not any(str(c).strip() for c in row):
                continue

            def get_col(candidates):
                for c in candidates:
                    if c in col_map and col_map[c] < len(row):
                        val = str(row[col_map[c]]).strip()
                        if val:
                            return val
                return ""

            company = get_col(["company", "account", "offer", "شركة"])
            status = get_col(["status", "state", "الحالة"])
            if not company or status.lower() != "active":
                continue

            details = {
                "source_tab": tab,
                "company": company,
                "status": status,
                "location": get_col(["location", "work location", "المكان"]),
                "level": get_col(["level", "language", "english level", "المستوى"]),
                "experience": get_col(["experience", "exp", "الخبرة"]),
                "graduation": get_col(["graduation st", "education", "التعليم"]),
                "max_age": get_col(["max age", "age", "السن"]),
                "transportation": get_col(["transportation", "المواصلات"]),
                "interview": get_col(["interview", "المقابلة"]),
            }

            unique_key = f"{tab}::{company.lower()}"
            if unique_key not in seen_keys:
                seen_keys.add(unique_key)
                offers.append(details)

    return offers


def generate_caption(gemini_client, offer: dict) -> str:
    prompt = f"""
اكتب إعلان توظيف واضح ومرتب باللغة الإنجليزية والعربية المختصرة للتوظيف في مصر بناءً على البيانات التالية فقط، دون اختلاق أي معلومات إضافية:
- الشركة / الحساب: {offer['company']}
- المكان: {offer['location'] or 'حسب التفاصيل'}
- المستوى المطلوب: {offer['level'] or 'حسب التفاصيل'}
- الخبرة: {offer['experience'] or 'حسب التفاصيل'}
- المؤهل / الدراسة: {offer['graduation'] or 'حسب التفاصيل'}
- السن الأقصى: {offer['max_age'] or 'حسب التفاصيل'}
- المواصلات: {offer['transportation'] or 'حسب التفاصيل'}
- نوع المقابلة: {offer['interview'] or 'حسب التفاصيل'}

اجعل التنسيق مناسباً للنشر على فيسبوك وواتساب مع نقاط واضحة ودعوة للتقديم.
""".strip()

    response = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )
    return (response.text or "").strip()


def build_post_image(offer: dict) -> bytes:
    width, height = 1080, 1080
    img = Image.new("RGB", (width, height), color=(18, 28, 45))
    draw = ImageDraw.Draw(img)

    # شريط علوي وزخرفة بسيطة
    draw.rectangle([0, 0, width, 160], fill=(37, 99, 235))
    draw.rounded_rectangle([60, 210, width - 60, height - 80], radius=28, fill=(28, 42, 66))

    font_title = ImageFont.load_default(size=44)
    font_body = ImageFont.load_default(size=30)

    draw.text((90, 55), "NOW HIRING", fill=(255, 255, 255), font=font_title)

    lines = [
        f"Role / Account: {offer['company']}",
        f"Location: {offer['location'] or 'N/A'}",
        f"Language Level: {offer['level'] or 'N/A'}",
        f"Experience: {offer['experience'] or 'N/A'}",
        f"Education: {offer['graduation'] or 'N/A'}",
        f"Max Age: {offer['max_age'] or 'N/A'}",
        f"Transportation: {offer['transportation'] or 'N/A'}",
        f"Interview: {offer['interview'] or 'N/A'}",
    ]

    y = 260
    for line in lines:
        draw.text((100, y), line[:68], fill=(241, 245, 249), font=font_body)
        y += 82

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def list_child_folders(drive_service, parent_id: str) -> dict[str, str]:
    folders = {}
    page_token = None
    query = (
        f"'{parent_id}' in parents and "
        "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    while True:
        resp = (
            drive_service.files()
            .list(
                q=query,
                fields="nextPageToken, files(id, name)",
                pageSize=100,
                pageToken=page_token,
            )
            .execute()
        )
        for item in resp.get("files", []):
            folders[item["name"]] = item["id"]
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return folders


def ensure_folder(drive_service, parent_id: str, folder_name: str, existing: dict[str, str]) -> str:
    if folder_name in existing:
        return existing[folder_name]
    metadata = {
        "name": folder_name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    created = drive_service.files().create(body=metadata, fields="id").execute()
    folder_id = created["id"]
    existing[folder_name] = folder_id
    return folder_id


def upsert_file(drive_service, folder_id: str, filename: str, content_bytes: bytes, mime_type: str):
    escaped_name = filename.replace("'", "\\'")
    query = (
        f"name = '{escaped_name}' and '{folder_id}' in parents and trashed = false"
    )
    existing = drive_service.files().list(q=query, fields="files(id)").execute().get("files", [])
    media = MediaIoBaseUpload(io.BytesIO(content_bytes), mimetype=mime_type, resumable=False)

    if existing:
        file_id = existing[0]["id"]
        drive_service.files().update(fileId=file_id, media_body=media).execute()
    else:
        metadata = {"name": filename, "parents": [folder_id]}
        drive_service.files().create(body=metadata, media_body=media, fields="id").execute()


def main():
    spreadsheet_id = os.environ["MASTER_SHEET_ID"]
    root_folder_id = os.environ["DRIVE_FOLDER_ID"]

    sheets_service, drive_service, gemini_client = get_clients()
    active_offers = fetch_active_offers(sheets_service, spreadsheet_id)
    existing_folders = list_child_folders(drive_service, root_folder_id)

    expected_names = set()

    for offer in active_offers:
        folder_name = sanitize_folder_name(f"{offer['company']} - {offer['source_tab']}")
        expected_names.add(folder_name)

        sub_folder_id = ensure_folder(drive_service, root_folder_id, folder_name, existing_folders)
        caption_text = generate_caption(gemini_client, offer)
        image_bytes = build_post_image(offer)

        upsert_file(
            drive_service,
            sub_folder_id,
            "caption.txt",
            caption_text.encode("utf-8"),
            "text/plain; charset=utf-8",
        )
        upsert_file(
            drive_service,
            sub_folder_id,
            "post.png",
            image_bytes,
            "image/png",
        )
        print(f"[OK] Synced: {folder_name}")

    # حذف مجلدات العروض التي أصبحت Hold أو أُزيلت من Active
    for folder_name, folder_id in existing_folders.items():
        if folder_name not in expected_names:
            drive_service.files().delete(fileId=folder_id).execute()
            print(f"[DELETED] Removed inactive/hold folder: {folder_name}")


if __name__ == "__main__":
    main()
