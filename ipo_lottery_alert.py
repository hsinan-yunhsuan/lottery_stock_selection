"""
LINE Bot：公開申購／股票抽籤提醒
----------------------------------
邏輯：
1. 抓 HiStock 的「公開申購/股票抽籤日程表」網頁（連線失敗會自動重試）
2. 網頁裡的表格本身就有「報酬率(%)」欄位（承銷價 vs 市價的價差），不用自己再抓即時股價算
3. 篩選出「還沒截止申購」且「報酬率 >= THRESHOLD_PCT」的股票
4. 透過 LINE Messaging API 用 Broadcast 推播給所有加這個 Bot 好友的人；
   同一檔股票（用代碼+抽籤日當 key）只會提醒一次
5. 如果整支程式執行失敗，會推播一則失敗通知（同一天最多一則），並以錯誤碼 1 結束，
   讓 GitHub Actions 顯示紅叉

需要的環境變數：
    LINE_CHANNEL_ACCESS_TOKEN

注意：
    Broadcast 會送給「所有」目前加這個 LINE Bot 為好友、且沒有封鎖的人，
    無法指定名單。LINE 免費方案每月訊息額度是「發送次數 × 好友數」計算，
    好友數變多、或這支程式跑的頻率變高，都會更快用完額度，超過後當月
    無法再送出，要留意 LINE Official Account Manager 後台的用量。

安裝套件：
    pip install requests pandas lxml
"""

import os
import sys
import json
import time
import traceback
from datetime import datetime, timedelta, timezone
from io import StringIO

import pandas as pd
import requests

# ------------------------
# 設定區
# ------------------------
THRESHOLD_PCT = 20  # 報酬率超過這個百分比才提醒
STATE_FILE = "ipo_alert_state.json"  # 記錄已提醒過的股票，以及最近一次失敗通知的日期
LINE_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]

SOURCE_URL = "https://histock.tw/stock/public.aspx"

MAX_RETRIES = 4  # 抓網頁最多嘗試幾次
RETRY_BASE_DELAY = 5  # 重試間隔秒數（第 n 次失敗後等待 5*n 秒）

TZ_TAIPEI = timezone(timedelta(hours=8))

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Referer": "https://histock.tw/",
}


# ------------------------
# 抓取與解析
# ------------------------
def fetch_html():
    """抓 HiStock 申購日程表的 HTML，遇到連線問題會自動重試"""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(SOURCE_URL, headers=REQUEST_HEADERS, timeout=(10, 20))
            resp.raise_for_status()
            resp.encoding = "utf-8"
            return resp.text
        except requests.exceptions.RequestException as e:
            last_err = e
            print(f"第 {attempt}/{MAX_RETRIES} 次抓取失敗：{e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BASE_DELAY * attempt)
    raise last_err


def fetch_offering_table():
    """抓 HiStock 申購日程表，回傳 pandas DataFrame"""
    html = fetch_html()
    tables = pd.read_html(StringIO(html))
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


# ------------------------
# 狀態檔
# ------------------------
def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            if isinstance(state, dict):
                state.setdefault("notified", [])
                return state
        except (json.JSONDecodeError, OSError) as e:
            print(f"讀取狀態檔失敗，將使用空白狀態：{e}")
    return {"notified": []}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


# ------------------------
# LINE 推播（全用戶）
# ------------------------
def push_line_message(text):
    """用 Broadcast API 推播給所有加這個 Bot 為好友的人。"""
    url = "https://api.line.me/v2/bot/message/broadcast"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_TOKEN}",
    }
    body = {"messages": [{"type": "text", "text": text}]}
    resp = requests.post(url, headers=headers, json=body, timeout=10)
    if not resp.ok:
        # 印出 LINE 回傳的錯誤內容，方便判斷是 token 還是額度問題
        print(f"LINE API 回應 {resp.status_code}：{resp.text}")
    resp.raise_for_status()


def notify_failure(err):
    """整支程式失敗時推播提醒；同一天最多一則，避免一天跑多次時洗版"""
    today = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d")
    state = load_state()
    if state.get("last_error_alert") == today:
        print("今天已經發過失敗通知，這次不再重複發送")
        return

    err_text = f"{type(err).__name__}: {err}"
    if len(err_text) > 300:
        err_text = err_text[:300] + "…"
    text = (
        "⚠️ 申購抽籤提醒程式執行失敗\n"
        f"錯誤：{err_text}\n"
        "請到 GitHub Actions 查看詳細 log。"
    )
    try:
        push_line_message(text)
        state["last_error_alert"] = today
        save_state(state)
        print("已推播失敗通知")
    except Exception as e:  # 通知本身失敗就只記錄，不要蓋掉原本的錯誤
        print(f"失敗通知也推播失敗：{e}")


# ------------------------
# 主流程
# ------------------------
def main():
    state = load_state()
    notified = set(state.get("notified", []))

    df = fetch_offering_table()
    candidates = parse_candidates(df)
    print(f"符合條件的股票共 {len(candidates)} 檔")

    for item in candidates:
        key = f"{item['code']}_{item['draw_date']}"
        if key in notified:
            continue  # 這檔已經通知過了

        status = f"🟢 {item['note']}"
        offer_price_str = (
            f"{item['offer_price']:.2f}" if item["offer_price"] is not None else "—"
        )
        market_price_str = (
            f"{item['market_price']:.2f}" if item["market_price"] is not None else "—"
        )

        text = (
            f"💰 {item['name']}({item['code']}) {item['market']}\n"
            f"{status}\n"
            f"申購期間：{item['period']}\n"
            f"抽籤日：{item['draw_date']}\n"
            f"承銷價：{offer_price_str} 市價：{market_price_str}\n"
            f"預估報酬率：{item['return_pct']:.1f}%\n"
            f"詳情：https://histock.tw/stock/{item['code']}"
        )

        push_line_message(text)
        notified.add(key)

        # 每推播成功一檔就立刻存檔：後面某一檔失敗時，前面已通知的不會被重複推播
        state["notified"] = sorted(notified)
        save_state(state)
        print(f"已推播：{item['code']} {item['name']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        notify_failure(e)
        sys.exit(1)  # 讓 GitHub Actions 顯示紅叉
