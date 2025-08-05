import os
from openai import AsyncOpenAI
import httpx
import asyncio
import json
from fastapi import FastAPI, Request
from dotenv import load_dotenv
from typing import Dict

from google.oauth2 import service_account
from googleapiclient.discovery import build

load_dotenv()

# === ENV ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# === FASTAPI APP ===
app = FastAPI()

# === CACHE ===
last_purchase = {}

# === GOOGLE SHEETS SETUP ===
def get_sheets_service():
    creds = service_account.Credentials.from_service_account_file(
        "service_account.json",  # put your creds file here
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds)

# === HELPER ===
async def send_telegram_message(chat_id: str, text: str):
    async with httpx.AsyncClient() as client:
        await client.post(f"{TELEGRAM_API}/sendMessage", json={
            "chat_id": chat_id,
            "text": text,
        })

async def categorize_purchase(description: str) -> str:
    categories = [
        "Groceries", "Transportation", "Eating Out", "Bills", "Entertainment",
        "Health", "Personal Care", "Education", "Clothing",
        "Travel", "Gifts", "Subscriptions", "Other"
    ]

    category_list_str = ", ".join(categories)

    prompt = f"""You are a personal finance assistant. Categorize the following purchase into **one** of the predefined high-level categories:

        Categories: {category_list_str}

        Description: \"{description}\"

        Category (must match one of the above exactly):"""

    response = await openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You strictly classify purchase descriptions into high-level budget categories."},
            {"role": "user", "content": prompt}
        ],
        temperature=0,
    )

    category = response.choices[0].message.content.strip()

    if category not in categories:
        category = "Other"

    return category

async def extract_transaction_info(notification_text: str) -> Dict[str, str]:
    prompt = f"""You are a financial assistant that processes bank notifications in both English and Spanish. 

    Analyze the following bank notification and extract transaction information. Look for these indicators of a CREDIT CARD transaction:
    - English: "credit card", "purchase", "transaction", "charged"
    - Spanish: "tarjeta de crédito", "compra", "transacción", "cargo"

    Extract:
    - amount (convert comma-separated numbers like "2,349" to integer 2349)
    - currency (CLP for Chile, USD, EUR, etc. - infer from context if not explicit)
    - raw_description (merchant/store name, clean it up but keep essential info)
    - is_credit_card (true if this is a credit card transaction)

    Examples of credit card indicators:
    - "Se realizó una compra por X con su Tarjeta de Crédito"
    - "Purchase made with Credit Card"
    - "Cargo en Tarjeta de Crédito"

    Notification: "{notification_text}"

    Respond ONLY in JSON format, according to the following schema:
    {{
        "is_credit_card": boolean,
        "amount": number,
        "currency": string,
        "raw_description": string
    }}"""

    response = await openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are an expert at extracting structured financial data from bank notifications in multiple languages. Always respond with valid JSON only."},
            {"role": "user", "content": prompt}
        ],
        temperature=0,
    )

    print(response)

    try:
        content = response.choices[0].message.content.strip()
        
        # Check if the response is wrapped in markdown code blocks
        if content.startswith('```json') and content.endswith('```'):
            # Extract JSON from between the code blocks
            json_start = content.find('{')
            json_end = content.rfind('}') + 1
            json_content = content[json_start:json_end]
        elif content.startswith('```') and content.endswith('```'):
            # Handle generic code blocks
            lines = content.split('\n')
            json_content = '\n'.join(lines[1:-1])  # Remove first and last line (code block markers)
        else:
            # Assume it's raw JSON
            json_content = content
        
        parsed = json.loads(json_content)
        print(f"✅ Parsed transaction info: {parsed}")
        return parsed
    except Exception as e:
        print(f"❌ Failed to parse OpenAI response: {e}")
        print(f"Raw content: {response.choices[0].message.content}")
        return {"is_credit_card": False}

def write_to_sheets(row: list):
    sheets = get_sheets_service()
    sheet = sheets.spreadsheets()
    sheet.values().append(
        spreadsheetId=SHEETS_ID,
        range="A1",
        valueInputOption="RAW",
        body={"values": [row]}
    ).execute()

# === MACRODROID WEBHOOK ===
@app.post("/notification")
async def handle_notification(req: Request):
    global last_purchase
    data = await req.json()
    notification_text = data.get("notification", "")
    print(f"🔍 Data: {data}")
    print(f"🔍 Notification text: {notification_text}")

    txn_info = await extract_transaction_info(notification_text)

    print(txn_info)

    if txn_info.get("is_credit_card"):
        amount = txn_info["amount"]
        currency = txn_info["currency"]
        raw_description = txn_info["raw_description"]

        last_purchase = {
            "amount": amount,
            "currency": currency,
            "raw_description": raw_description
        }

        await send_telegram_message(CHAT_ID, f"You spent {amount} {currency} at '{raw_description}'. What was it?")

    return {"ok": True}

# === TELEGRAM WEBHOOK ===
@app.post("/webhook")
async def telegram_webhook(req: Request):
    body = await req.json()
    message = body.get("message", {})
    chat_id = str(message.get("chat", {}).get("id"))
    text = message.get("text", "")

    if not text:
        return {"ok": True}

    category = await categorize_purchase(text)

    print(f"🔍 Category: {category}")

    write_to_sheets([
        last_purchase["amount"],
        last_purchase["currency"],
        last_purchase["raw_description"],
        text,        # user description
        category     # AI-generated category
    ])

    await send_telegram_message(chat_id, f"Got it. Categorized as: {category}")

    return {"ok": True}
