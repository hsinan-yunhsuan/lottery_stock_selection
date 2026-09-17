"""
LINE Bot：公開申購／股票抽籤提醒
----------------------------------
邏輯：
  1. 抓 HiStock 的「公開申購/股票抽籤日程表」網頁
  2. 網頁裡的表格本身就有「報酬率(%)」欄位（承銷價 vs 市價的價差），不用自己再抓即時股價算
  3. 篩選出「還沒截止申購」且「報酬率 >= THRESHOLD_PCT」的股票
  4. 透過 LINE Messaging API 推播；同一檔股票（用代碼+抽籤日當 key）只會提醒一次

需要的環境變數：
  LINE_CHANNEL_ACCESS_TOKEN
  LINE_USER_ID

安裝套件：
  pip install requests pandas lxml
"""

import os
import json
import requests
import pandas as pd
from io import StringIO

# ------------------------
# 設定區
# ------------------------
THRESHOLD_PCT = 20  # 報酬率超過這個百分比才提醒
STATE_FILE = "ipo_alert_state.json"  # 記錄「哪些（股票代碼+抽籤日）已經提醒過」，避免重複通知

LINE_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_TO = os.environ["LINE_USER_ID"]

SOURCE_URL = "https://histock.tw/stock/public.aspx"


def fetch_offering_table():
    """抓 HiStock 申購日程表，回傳 pandas DataFrame"""
    resp = requests.get(
        SOURCE_URL,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15,
    )
    resp.raise_for_status()
    resp.encoding = "utf-8"

    tables = pd.read_html(StringIO(resp.text))
    for df in tables:
        # 用欄位名稱找出「抽籤日程表」那張表，避免網頁上其他表格干擾
        if any("抽籤日期" in str(c) for c in df.columns):
            return df
    raise RuntimeError("找不到抽籤日程表，網站排版可能已經變動，需要更新爬蟲邏輯")


def find_column(df, keyword):
    """依關鍵字模糊比對欄位名稱，避免網站欄位名稱有些微變化就整個爆掉"""
    for c in df.columns:
        if keyword in str(c):
            return c
    return None


def parse_candidates(df):
    """篩選出還在申購期間內、報酬率超過門檻的股票"""
    col_name = find_column(df, "代號")
    col_market = find_column(df, "發行市場")
    col_period = find_column(df, "申購期間")
    col_draw_date = find_column(df, "抽籤日期")
    col_return = find_column(df, "報酬率")
    col_note = find_column(df, "備註")
    col_offer_price = find_column(df, "承銷價")
    col_market_price = find_column(df, "市價")

    candidates = []
    for _, row in df.iterrows():
        note = str(row.get(col_note, "")).strip()
        if note in ("已截止", "nan"):
            continue  # 已經來不及申購了，跳過

        raw_name = str(row.get(col_name, "")).strip()
        parts = raw_name.split(None, 1)
        if len(parts) < 2:
            continue
        code, name = parts[0], parts[1]

        try:
            return_pct = float(row.get(col_return))
        except (TypeError, ValueError):
            continue

        if return_pct < THRESHOLD_PCT:
            continue

        try:
            offer_price = float(row.get(col_offer_price))
        except (TypeError, ValueError):
            offer_price = None

        try:
            market_price = float(row.get(col_market_price))
        except (TypeError, ValueError):
            market_price = None

        candidates.append(
            {
                "code": code,
                "name": name,
                "market": str(row.get(col_market, "")).strip(),
                "period": str(row.get(col_period, "")).strip(),
                "draw_date": str(row.get(col_draw_date, "")).strip(),
                "return_pct": return_pct,
                "offer_price": offer_price,
                "market_price": market_price,
                "note": note,
            }
        )
    return candidates


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"notified": []}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def push_line_message(text):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_TOKEN}",
    }
    body = {"to": LINE_TO, "messages": [{"type": "text", "text": text}]}
    resp = requests.post(url, headers=headers, json=body, timeout=10)
    resp.raise_for_status()


def main():
    state = load_state()
    notified = set(state.get("notified", []))

    df = fetch_offering_table()
    candidates = parse_candidates(df)

    for item in candidates:
        key = f"{item['code']}_{item['draw_date']}"
        if key in notified:
            continue  # 這檔已經通知過了

        status = "🟢 申購中" if item["note"] == "申購中" else "🔜 即將開放申購"
        offer_price_str = f"{item['offer_price']:.2f}" if item["offer_price"] is not None else "—"
        market_price_str = f"{item['market_price']:.2f}" if item["market_price"] is not None else "—"
        text = (
            f"💰 {item['name']}({item['code']})　{item['market']}\n"
            f"{status}\n"
            f"申購期間：{item['period']}\n"
            f"抽籤日：{item['draw_date']}\n"
            f"承銷價：{offer_price_str}　市價：{market_price_str}\n"
            f"預估報酬率：{item['return_pct']:.1f}%\n"
            f"詳情：https://histock.tw/stock/{item['code']}"
        )
        push_line_message(text)
        notified.add(key)
        print(f"已推播：{item['code']} {item['name']}")

    state["notified"] = list(notified)
    save_state(state)


if __name__ == "__main__":
    main()
