# -*- coding: utf-8 -*-
import os, json, logging, re, html, datetime, time, asyncio, math
from google import genai
from google.genai import types
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler,
    filters, ContextTypes
)
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY  = os.getenv("GEMINI_API_KEY")
APPS_SCRIPT_URL = os.getenv("APPS_SCRIPT_URL")
client = genai.Client(api_key=GEMINI_API_KEY)
def gemini_generate(contents, retries=6, delay=3):
    for i in range(retries):
        try:
            return client.models.generate_content(model="gemini-2.5-flash", contents=contents)
        except Exception as e:
            if i < retries - 1 and ("503" in str(e) or "UNAVAILABLE" in str(e) or "429" in str(e) or "500" in str(e)):
                time.sleep(delay * (i + 1))
            else:
                raise

# ======= GEMINI QUEUE =======
# Serializes Gemini calls to prevent 503 overload when multiple users upload simultaneously
# NOTE: gemini_queue is initialized in post_init (must be inside a running event loop)
gemini_queue = None

async def gemini_queue_worker():
    """Background worker: processes one Gemini/extract job at a time."""
    while True:
        job = await gemini_queue.get()
        try:
            loop = asyncio.get_event_loop()
            if job.get("type") == "extract":
                result = await loop.run_in_executor(
                    None, lambda: extract_and_audit(job["photo_bytes"], job["restaurant"])
                )
            else:
                result = await loop.run_in_executor(None, lambda: gemini_generate(job["contents"]))
            job["future"].set_result(result)
        except Exception as e:
            job["future"].set_exception(e)
        finally:
            gemini_queue.task_done()
        await asyncio.sleep(1)  # 1s gap between Gemini calls

async def gemini_generate_queued(contents):
    """Queue a raw Gemini call and await the result."""
    loop = asyncio.get_event_loop()
    future = loop.create_future()
    await gemini_queue.put({"contents": contents, "future": future})
    return await future

RESTAURANTS = [
    "Pisangan Lama","Kebagusan","Pejaten","Kranggan",
    "Cibinong","Siaga Raya","Ragunan","Buncit Raya",
    "WKB Tuban","WKB Bogor","Yogya UMY","Yogya ISI",
]

# Monthly fixed expenses per restaurant: kb=Kontrak Bangunan, btk=Biaya Tenaga Kerja
# btk_kasbon = portion of BTK already paid as kasbon advance in P1 (only for restaurants with employee advance)
MONTHLY_EXPENSES = {
    "Pisangan Lama": {"kb": 8_333_333, "btk": 6_000_000},
    "Kebagusan":     {"kb": 5_000_000, "btk": 0},
    "Pejaten":       {"kb": 5_000_000, "btk": 3_100_000},
    "Kranggan":      {"kb": 4_100_000, "btk": 3_000_000},
    "Cibinong":      {"kb": 4_600_000, "btk": 1_500_000},
    "Siaga Raya":    {"kb": 5_000_000, "btk": 4_800_000},
    "Ragunan":       {"kb": 3_800_000, "btk": 1_600_000},
    "Buncit Raya":   {"kb": 5_000_000, "btk": 1_500_000},
    "WKB Tuban":     {"kb": 2_500_000, "btk": 4_500_000, "btk_kasbon": 2_000_000},
    "WKB Bogor":     {"kb": 1_500_000, "btk": 1_500_000},
    "Yogya UMY":     {"kb": 2_000_000, "btk": 3_000_000},
    "Yogya ISI":     {"kb": 1_750_000, "btk": 0},
}

# States
SELECT_RESTAURANT, CONFIRM_DATA, EDIT_FIELD, VALIDATE_BELANJA, MAIN_GOFOOD = range(5)
PENG_SELECT, PENG_PERIOD, PENG_WAIT_INPUT, PENG_CONFIRM = range(4, 8)
GOFOOD_SELECT, GOFOOD_WAIT_PHOTO, GOFOOD_CONFIRM, GOFOOD_DATE = range(8, 12)
RSUM_SELECT, RSUM_PERIOD, RSUM_DATE, RSUM_KASBON = 11, 13, 12, 16
LGOFOOD_SELECT, LGOFOOD_DATE = 14, 15

def esc(text):
    return html.escape(str(text))

def restaurant_keyboard():
    kb, row = [], []
    for r in RESTAURANTS:
        row.append(InlineKeyboardButton(r, callback_data="rest|" + r))
        if len(row) == 2:
            kb.append(row); row = []
    if row: kb.append(row)
    kb.append([InlineKeyboardButton("Batalkan", callback_data="cancel")])
    return InlineKeyboardMarkup(kb)

def extract_and_audit(image_bytes, restaurant):
    img_data = types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
    wkb_tuban_note = ""
    if restaurant == "WKB Tuban":
        wkb_tuban_note = (
            "\nATURAN KHUSUS WKB TUBAN:\n"
            "- omzet = TOTAL pemasukan (termasuk saldo kemarin). Baca angka TOTAL paling bawah di bagian pemasukan.\n"
            "- Saldo kemarin juga catat sebagai belanja_warung (untuk tracking)\n"
            "- Contoh: shift 2.000.000 + saldo kemarin 84.000 = total 2.084.000 -> omzet=2.084.000, belanja_warung=84.000\n"
            "- keuntungan dihitung sistem: omzet - belanja_pasar\n"
        )
    prompt = (
        "Kamu asisten keuangan DAN auditor untuk cabang " + restaurant + ".\n"
        "Baca laporan harian TULISAN TANGAN ini.\n\n"
        "Struktur laporan:\n"
        "1. Pemasukan tunai/cash per shift (pagi/siang/malam/shift 1/2/3) -> jumlahkan ke omzet\n"
        "   PENTING: JANGAN masukkan pendapatan GoFood ke dalam omzet\n"
        "2. Pendapatan GoFood/GrabFood/online order -> gofood_order (nominal bruto/kotor)\n"
        "   gofood_net = isi sama dengan gofood_order (potongan QRIS 0.7% dihitung otomatis sistem)\n"
        "   Jika tidak ada GoFood di laporan, isi 0.\n"
        "3. Belanja Warung (LPG/es batu/operasional) -> belanja_warung\n"
        "4. Belanja Pasar (sembako/sayur/ayam/ikan/dll) -> belanja_pasar\n"
        + wkb_tuban_note +
        "\nATURAN: omzet = pemasukan TUNAI saja. GoFood HARUS dipisah ke gofood_order/gofood_net.\n\n"
        "OUTPUT dua bagian TANPA markdown:\n"
        "JSON_DATA:\n"
        '{"tanggal":"YYYY-MM-DD (gunakan tahun 2026 jika tahun tidak terbaca jelas)","omzet":0,"belanja_warung":0,"belanja_pasar":0,'
        '"belanja_warung_items":{"lpg":0,"es_batu":0},'
        '"belanja_pasar_items":{"sembako":0,"sayur":0,"ayam":0,"ikan":0,"lain":0},'
        '"gofood_order":0,"gofood_net":0,"catatan":""}\n'
        "AUDIT:\n"
        "Berikan analisis singkat Bahasa Indonesia (maks 4 poin):\n"
        "1. Ada angka tidak wajar/mencurigakan?\n"
        "2. Belanja pasar wajar untuk warteg?\n"
        "3. Ada tulisan tidak terbaca/angka meragukan?\n"
        "4. VERDICT: BAGUS / PERLU PERHATIAN / RUGI + alasan 1 kalimat.\n"
        "Jika semua wajar tulis: TIDAK ADA CATATAN AUDIT"
    )
    resp = gemini_generate(contents=[prompt, img_data])
    text = resp.text.strip()
    data = {}
    m = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL)
    if m:
        try:
            d = json.loads(m.group())
            for f in ["omzet","belanja_warung","belanja_pasar","gofood_order","gofood_net"]:
                d[f] = int(str(d.get(f,0)).replace(",","").replace(".","") or 0)
            if d.get("gofood_order", 0) > 0:
                d["gofood_net"] = round(d["gofood_order"] * 0.993)
            for g in ["belanja_warung_items","belanja_pasar_items"]:
                if g in d and isinstance(d[g], dict):
                    d[g] = {k: int(str(v).replace(",","").replace(".","") or 0) for k,v in d[g].items()}
                else:
                    d[g] = {}
            data = d
        except Exception as ex:
            logger.error("JSON parse error: " + str(ex))
    audit = None
    am = re.search(r"AUDIT:\n(.*)", text, re.DOTALL)
    if am:
        t = am.group(1).strip()
        if "TIDAK ADA CATATAN" not in t:
            audit = t
    return data, audit

def analyze_belanja_detail(images_bytes_list, restaurant, main_data):
    parts = [types.Part.from_bytes(data=b, mime_type="image/jpeg") for b in images_bytes_list]
    bw = main_data.get("belanja_warung", 0)
    bp = main_data.get("belanja_pasar", 0)
    bw_items = main_data.get("belanja_warung_items", {})
    bp_items = main_data.get("belanja_pasar_items", {})
    ref = ["Di laporan utama:", "Belanja Warung Rp " + format(bw, ",")]
    for k,v in bw_items.items():
        if v > 0: ref.append("  - " + k + ": Rp " + format(v,","))
    ref.append("Belanja Pasar Rp " + format(bp, ","))
    for k,v in bp_items.items():
        if v > 0: ref.append("  - " + k + ": Rp " + format(v,","))
    prompt = (
        "Kamu auditor keuangan cabang " + restaurant + ".\n"
        + "\n".join(ref) + "\n\n"
        "Ini " + str(len(images_bytes_list)) + " foto nota belanja.\n"
        "Tugas: baca semua item, hitung total, bandingkan per kategori dengan laporan, cari anomali.\n\n"
        "Format:\nTOTAL DARI NOTA: Rp ...\nSELISIH: Rp ... (lebih/kurang)\n\n"
        "PERBANDINGAN:\n- [kategori]: Laporan Rp X | Nota Rp Y | OK/SELISIH\n\n"
        "TEMUAN AUDIT:\n- [temuan]\nJika tidak ada: TIDAK ADA TEMUAN"
    )
    try:
        resp = gemini_generate(contents=[prompt] + parts)
        return resp.text.strip()
    except Exception as e:
        logger.error("Belanja detail error: " + str(e))
        return None

def idr(raw):
    raw = re.sub(r"\.(?=\d{3}(?:[.\s]|$))", "", str(raw))
    raw = re.sub(r"[^\d]", "", raw)
    return int(raw) if raw else 0

def parse_pengeluaran_direct(text):
    KEYWORDS = {
        "beras": ["beras"],
        "pln":   ["pln","listrik"],
        "pdam":  ["pdam","air"],
        "wifi":  ["wifi","internet","speedy"],
        "sampah":["sampah"],
        "kasbon":["kasbon","kasbo","hutang"],
        "gaji":  ["gaji","upah"],
    }
    result = {k: 0 for k in list(KEYWORDS.keys()) + ["lain_lain","total"]}
    result["periode"] = ""
    result["catatan"] = ""
    lines = text.splitlines()
    lain_items = []
    for line in lines:
        m = re.search(r"[=:]\s*([\d.,]+)", line)
        if not m: continue
        amount = idr(m.group(1))
        if amount == 0: continue
        label = line[:m.start()].lower()
        parts_in_line = re.split(r"[,/+&]", label)
        matched_keys = []
        for part in parts_in_line:
            part = part.strip()
            for key, keywords in KEYWORDS.items():
                if any(kw in part for kw in keywords):
                    matched_keys.append(key)
        if matched_keys:
            split_amount = amount // len(matched_keys)
            remainder = amount % len(matched_keys)
            for i, key in enumerate(matched_keys):
                result[key] += split_amount + (remainder if i == 0 else 0)
        else:
            lain_items.append(amount)
    result["lain_lain"] = sum(lain_items)
    result["total"] = sum(result[k] for k in ["beras","pln","pdam","wifi","sampah","kasbon","gaji","lain_lain"])
    return result if result["total"] > 0 else {}

def extract_pengeluaran(content_data, restaurant, is_image=False):
    prompt = (
        "Kamu asisten keuangan cabang " + restaurant + ".\n"
        "Laporan pengeluaran 10 hari. Ekstrak semua item dan jumlahnya dalam Rupiah (integer).\n"
        "Catatan: angka bisa pakai titik sebagai pemisah ribuan (1.828.500 = 1828500).\n"
        "PLN+PDAM+wifi yang digabung: pecah rata ke masing-masing field.\n"
        "Hitung total dari semua item.\n"
        'OUTPUT JSON (semua angka integer tanpa titik/koma):\n'
        '{"periode":"","beras":0,"pln":0,"pdam":0,"wifi":0,"sampah":0,"kasbon":0,"gaji":0,"lain_lain":0,"total":0,"catatan":""}'
    )
    try:
        if is_image:
            img = types.Part.from_bytes(data=content_data, mime_type="image/jpeg")
            resp = gemini_generate(contents=[prompt, img])
        else:
            resp = gemini_generate(contents=[prompt + "\n\nData:\n" + content_data])
        text = resp.text.strip()
        m = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL)
        if not m:
            m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            d = json.loads(m.group())
            nums = ["beras","pln","pdam","wifi","sampah","kasbon","gaji","lain_lain","total"]
            for f in nums:
                d[f] = idr(d.get(f, 0))
            if d.get("total", 0) == 0:
                d["total"] = sum(d.get(k, 0) for k in nums[:-1])
            return d
    except Exception as e:
        logger.error("Pengeluaran Gemini error: " + str(e))
    if not is_image and isinstance(content_data, str):
        return parse_pengeluaran_direct(content_data)
    return {}

def extract_gofood_report(image_bytes, restaurant):
    img = types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")
    prompt = (
        "Screenshot laporan pendapatan online cabang " + restaurant + ".\n"
        "Laporan ini bisa dari GoFood, GrabFood, atau ringkasan Gojek Merchant (Ringkasan/Summary).\n"
        "Pendapatan bisa berasal dari GoFood delivery, GoFood Pickup, QRIS, atau kombinasi.\n"
        "Gunakan TOTAL PENJUALAN keseluruhan (bukan hanya baris GoFood).\n"
        "Untuk 'total_bruto' dan 'total_netto': gunakan angka 'Penjualan' atau total keseluruhan.\n"
        "Jika bruto tidak tersedia, samakan dengan netto.\n"
        "Untuk 'jumlah_transaksi': total semua transaksi.\n"
        "Untuk 'periode': isi dengan rentang tanggal atau bulan yang tertera (contoh: 'Jun 2026' atau '1-30 Jun 2026').\n"
        'OUTPUT JSON ONLY: {"periode":"","total_bruto":0,"total_netto":0,"jumlah_transaksi":0,"catatan":""}'
    )
    try:
        resp = gemini_generate(contents=[prompt, img])
        m = re.search(r"\{.*?\}", resp.text.strip(), re.DOTALL)
        if m:
            d = json.loads(m.group())
            for f in ["total_bruto","total_netto","jumlah_transaksi"]:
                d[f] = int(str(d.get(f,0)).replace(",","").replace(".","") or 0)
            # Always compute netto = bruto * 0.993 (0.7% MDR fee for QRIS/GoFood platform)
            # If Gemini already deducted fees, bruto=netto so this still applies correctly
            if d["total_bruto"] > 0:
                d["total_netto"] = round(d["total_bruto"] * 0.993)
            elif d["total_netto"] == 0:
                d["total_netto"] = 0
            return d
    except Exception as e:
        logger.error("GoFood error: " + str(e))
    return {}

def save_to_sheets(restaurant, data, data_type="laporan_harian"):
    try:
        payload = {"restaurant": restaurant, "type": data_type}
        payload.update(data)
        r = requests.post(APPS_SCRIPT_URL, json=payload, timeout=20)
        return r.status_code == 200
    except Exception as e:
        logger.error("Sheets error: " + str(e))
        return False

def fetch_summary(restaurant, days=10, start_date=None, period_tag=None):
    try:
        params = {"action":"summary","restaurant":restaurant,"days":str(days)}
        if start_date:
            params["startDate"] = start_date
        if period_tag:
            params["periodTag"] = period_tag
        r = requests.get(APPS_SCRIPT_URL, params=params, timeout=20)
        if r.status_code == 200: return r.json()
    except Exception as e:
        logger.error("Summary error: " + str(e))
    return None

def generate_smart_audit(restaurant, current_data, audit_text):
    """Enrich audit with historical comparison from Sheets."""
    try:
        summary = fetch_summary(restaurant, days=7)
        if not summary or not summary.get("rows"):
            return audit_text
        rows = summary["rows"]
        if len(rows) < 2:
            return audit_text
        # Calculate averages from past data (exclude today)
        past = rows[:-1] if len(rows) > 1 else rows
        avg_omzet = sum(r.get("omzet", 0) for r in past) / len(past)
        avg_k = sum(r.get("keuntungan", 0) for r in past) / len(past)
        cur_omzet = current_data.get("omzet", 0)
        cur_bw = current_data.get("belanja_warung", 0)
        # For WKB Tuban, adjusted omzet
        if restaurant == "WKB Tuban":
            cur_omzet = cur_omzet + cur_bw
        cur_k, _, _ = None, None, None
        _, tb, cur_k = keuntungan_calc(restaurant, current_data)

        lines = []
        # Anomaly check
        if avg_omzet > 0:
            pct = (cur_omzet - avg_omzet) / avg_omzet * 100
            if pct < -30:
                lines.append(f"⚠️ Omzet hari ini Rp {cur_omzet:,} lebih rendah {abs(pct):.0f}% dari rata-rata 7 hari (Rp {avg_omzet:,.0f})")
            elif pct > 50:
                lines.append(f"📈 Omzet hari ini Rp {cur_omzet:,} lebih tinggi {pct:.0f}% dari rata-rata 7 hari (Rp {avg_omzet:,.0f})")
            else:
                lines.append(f"✅ Omzet normal (rata-rata 7 hari: Rp {avg_omzet:,.0f})")
        # Profitability verdict
        if cur_k is not None:
            if cur_k >= avg_k * 0.9:
                verdict = "✅ BAGUS"
            elif cur_k >= 0:
                verdict = "⚠️ PERLU PERHATIAN"
            else:
                verdict = "❌ RUGI"
            lines.append(f"Keuntungan: Rp {cur_k:,} | Verdict: {verdict} (rata-rata: Rp {avg_k:,.0f})")

        enriched = "\n".join(lines)
        if audit_text:
            return enriched + "\n\n" + audit_text
        return enriched
    except Exception as e:
        logger.error("Smart audit error: " + str(e))
        return audit_text

def parse_date_input(text):
    text = text.strip()
    MONTHS = {"jan":1,"feb":2,"mar":3,"apr":4,"mei":5,"may":5,"jun":6,"jul":7,
              "ags":8,"aug":8,"sep":9,"okt":10,"oct":10,"nov":11,"des":12,"dec":12}
    m = re.match(r"(\d{1,2})\s+([a-zA-Z]+)\s+(\d{4})", text)
    if m:
        day, mon, year = int(m.group(1)), MONTHS.get(m.group(2).lower()[:3]), int(m.group(3))
        if mon:
            try: return datetime.date(year, mon, day).strftime("%Y-%m-%d")
            except: pass
    m = re.match(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})", text)
    if m:
        try: return datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1))).strftime("%Y-%m-%d")
        except: pass
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return text[:10]
    return None

def keuntungan_calc(restaurant, data):
    """WKB Tuban: AI reads omzet including carry-over; keuntungan = omzet - belanja_pasar only"""
    bw = data.get("belanja_warung", 0)
    bp = data.get("belanja_pasar", 0)
    omzet = data.get("omzet", 0)
    if restaurant == "WKB Tuban":
        # AI already reads total omzet (including carry-over); only belanja_pasar is real expense
        omzet_adj = omzet
        tb = bp
        k = omzet_adj - tb
    else:
        omzet_adj = omzet
        tb = bw + bp
        k = omzet_adj - tb
    return omzet_adj, tb, k

def fmt(restaurant, data):
    omzet_adj, tb, k = keuntungan_calc(restaurant, data)
    lines = [
        "<b>Hasil Baca -- " + restaurant + "</b>",
        "--------------------",
        "Tanggal        : <b>" + str(data.get("tanggal","?")) + "</b>",
        "Pemasukan Tunai: <b>Rp " + format(omzet_adj, ",") + "</b>",
        "",
        "<b>Belanja Warung : Rp " + format(data.get("belanja_warung",0), ",") + "</b>",
    ]
    for item, val in data.get("belanja_warung_items",{}).items():
        if val > 0: lines.append("  - " + item.replace("_"," ") + ": Rp " + format(val,","))
    lines += ["", "<b>Belanja Pasar  : Rp " + format(data.get("belanja_pasar",0), ",") + "</b>"]
    for item, val in data.get("belanja_pasar_items",{}).items():
        if val > 0: lines.append("  - " + item.replace("_"," ") + ": Rp " + format(val,","))
    lines += [
        "",
        "Total Belanja  : <b>Rp " + format(tb, ",") + "</b>",
        "Omzet Bersih   : <b>Rp " + format(k, ",") + "</b>",
    ]
    if data.get("gofood_order",0) > 0:
        lines += [
            "GoFood Order   : <b>Rp " + format(data.get("gofood_order",0),",") + "</b>",
            "GoFood Net     : <b>Rp " + format(data.get("gofood_net",0),",") + "</b>",
        ]
    if data.get("catatan"):
        lines.append("Catatan: <i>" + esc(str(data["catatan"])) + "</i>")
    lines.append("--------------------")
    return "\n".join(lines)

# ======= MAIN REPORT FLOW =======
async def start(update, ctx):
    await update.message.reply_text(
        "<b>Bot Laporan Warteg</b>\n\nKirim foto laporan harian untuk mulai.\n\n"
        "<b>Perintah:</b>\n"
        "/pengeluaran - Input pengeluaran 10 hari\n"
        "/gofood - Upload laporan GoFood\n"
        "/ringkasan10hari - Ringkasan profit 10 hari\n"
        "/help - Panduan lengkap",
        parse_mode="HTML")

async def help_cmd(update, ctx):
    await update.message.reply_text(
        "<b>Panduan Bot Warteg:</b>\n\n"
        "<b>1. Laporan Harian:</b> Kirim foto laporan\n"
        "<b>2. Validasi Belanja:</b> Muncul otomatis setelah laporan tersimpan\n"
        "<b>3. /pengeluaran:</b> PLN/PDAM/Beras/Wifi/Kasbon tiap 10 hari\n"
        "<b>4. /gofood:</b> Screenshot laporan GoFood (update kolom sheet utama)\n"
        "<b>5. /ringkasan10hari:</b> Profit bersih 10 hari\n\n"
        "<b>Koreksi data:</b> ketik <code>omzet: 1800000</code> lalu /simpan\n"
        "/cancel untuk batalkan",
        parse_mode="HTML")

async def photo_received(update, ctx):
    photo = update.message.photo[-1]
    f = await ctx.bot.get_file(photo.file_id)
    ctx.user_data["photo_bytes"] = bytes(await f.download_as_bytearray())
    await update.message.reply_text(
        "Foto diterima!\n\n<b>Foto ini untuk cabang mana?</b>",
        parse_mode="HTML", reply_markup=restaurant_keyboard())
    return SELECT_RESTAURANT

async def restaurant_selected(update, ctx):
    q = update.callback_query
    await q.answer()
    if q.data == "cancel":
        await q.edit_message_text("Dibatalkan.")
        return ConversationHandler.END
    restaurant = q.data.split("|",1)[1]
    ctx.user_data["restaurant"] = restaurant
    ctx.user_data["extracted"] = {"restaurant": restaurant}
    # Show queue position if others are waiting
    waiting = gemini_queue.qsize() if gemini_queue else 0
    if waiting > 0:
        await q.edit_message_text("📋 Antrian: <b>" + str(waiting + 1) + " foto</b> menunggu diproses. Harap tunggu...", parse_mode="HTML")
    else:
        await q.edit_message_text("Membaca dan menganalisis laporan " + restaurant + "...")
    try:
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        photo_bytes = ctx.user_data["photo_bytes"]
        if gemini_queue:
            await gemini_queue.put({"type": "extract", "photo_bytes": photo_bytes, "restaurant": restaurant, "future": future})
            data, audit = await future
        else:
            data, audit = extract_and_audit(photo_bytes, restaurant)
    except Exception as e:
        err = str(e)
        if "503" in err or "UNAVAILABLE" in err:
            await q.edit_message_text("⚠️ Gemini sedang sibuk. Coba kirim foto lagi dalam 1-2 menit.")
        else:
            await q.edit_message_text("Gagal membaca foto: " + err)
        return ConversationHandler.END
    if not data:
        await q.edit_message_text("Data tidak terbaca. Coba foto lebih jelas.")
        return ConversationHandler.END
    data["restaurant"] = restaurant
    ctx.user_data["extracted"] = data
    ctx.user_data["audit_text"] = audit
    audit = generate_smart_audit(restaurant, data, audit)
    summary = fmt(restaurant, data)
    if audit:
        summary += "\n\n<b>Catatan Audit AI:</b>\n" + esc(audit)
    # Always ask about GoFood to ensure it's not missed
    gofood_detected = data.get("gofood_order", 0) > 0
    if gofood_detected:
        gofood_info = "\n<i>AI mendeteksi GoFood: Rp " + format(data.get("gofood_order",0),",") + "</i>"
    else:
        gofood_info = "\n<i>AI tidak mendeteksi GoFood di laporan ini.</i>"
    kb = [
        [InlineKeyboardButton("Ada GoFood, input manual", callback_data="input_gofood")],
        [InlineKeyboardButton("Tidak ada GoFood", callback_data="no_gofood")],
        [InlineKeyboardButton("🔄 Analisis Ulang", callback_data="reanalyze")],
    ]
    await q.edit_message_text(
        summary + gofood_info + "\n\n<b>Apakah ada pendapatan GoFood hari ini?</b>",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
    return MAIN_GOFOOD

async def main_gofood_action(update, ctx):
    """Handle GoFood question after main report is read."""
    q = update.callback_query
    await q.answer()
    if q.data == "reanalyze":
        restaurant = ctx.user_data.get("restaurant","")
        await q.edit_message_text("🔄 Menganalisis ulang laporan " + restaurant + "...")
        try:
            loop = asyncio.get_event_loop()
            future = loop.create_future()
            if gemini_queue:
                await gemini_queue.put({"type": "extract", "photo_bytes": ctx.user_data["photo_bytes"], "restaurant": restaurant, "future": future})
                data, audit = await future
            else:
                data, audit = extract_and_audit(ctx.user_data["photo_bytes"], restaurant)
        except Exception as e:
            err = str(e)
            if "503" in err or "UNAVAILABLE" in err:
                await q.edit_message_text("⚠️ Gemini sedang sibuk. Coba kirim foto lagi dalam 1-2 menit.")
            else:
                await q.edit_message_text("Gagal membaca foto: " + err)
            return ConversationHandler.END
        if not data:
            await q.edit_message_text("Data tidak terbaca. Coba foto lebih jelas.")
            return ConversationHandler.END
        data["restaurant"] = restaurant
        ctx.user_data["extracted"] = data
        ctx.user_data["audit_text"] = audit
        audit = generate_smart_audit(restaurant, data, audit)
        summary = fmt(restaurant, data)
        if audit:
            summary += "\n\n<b>Catatan Audit AI:</b>\n" + esc(audit)
        gofood_detected = data.get("gofood_order", 0) > 0
        gofood_info = ("\n<i>AI mendeteksi GoFood: Rp " + format(data.get("gofood_order",0),",") + "</i>") if gofood_detected else "\n<i>AI tidak mendeteksi GoFood di laporan ini.</i>"
        kb = [
            [InlineKeyboardButton("Ada GoFood, input manual", callback_data="input_gofood")],
            [InlineKeyboardButton("Tidak ada GoFood", callback_data="no_gofood")],
            [InlineKeyboardButton("🔄 Analisis Ulang", callback_data="reanalyze")],
        ]
        await q.edit_message_text(
            summary + gofood_info + "\n\n<b>Apakah ada pendapatan GoFood hari ini?</b>",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
        return MAIN_GOFOOD
    if q.data == "no_gofood":
        ctx.user_data["extracted"]["gofood_order"] = 0
        ctx.user_data["extracted"]["gofood_net"] = 0
        await _show_confirm(q, ctx)
        return CONFIRM_DATA
    if q.data == "input_gofood":
        gf = ctx.user_data["extracted"].get("gofood_order", 0)
        hint = " (AI baca: Rp " + format(gf,",") + ")" if gf > 0 else ""
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Input GROSS (bruto)", callback_data="gf_gross")],
            [InlineKeyboardButton("Input NET (setelah potongan)", callback_data="gf_net")],
            [InlineKeyboardButton("Tidak ada GoFood", callback_data="no_gofood")],
        ])
        await q.edit_message_text(
            "<b>Input GoFood</b>" + hint + "\n\nPilih jenis input:",
            parse_mode="HTML", reply_markup=kb)
        return MAIN_GOFOOD
    if q.data == "gf_gross":
        ctx.user_data["gofood_step"] = "order"
        await q.edit_message_text(
            "<b>Input GoFood GROSS</b>\n\n"
            "Ketik nominal bruto (sebelum potongan):\n"
            "<code>150000</code>\n\n"
            "Net akan dihitung otomatis (QRIS 0.7%)\n"
            "Atau ketik <code>skip</code> jika tidak ada.",
            parse_mode="HTML")
        return MAIN_GOFOOD
    if q.data == "gf_net":
        ctx.user_data["gofood_step"] = "net_only"
        await q.edit_message_text(
            "<b>Input GoFood NET</b>\n\n"
            "Ketik nominal setelah potongan platform:\n"
            "<code>135000</code>\n\n"
            "Gross akan diisi sama dengan net.\n"
            "Atau ketik <code>skip</code> jika tidak ada.",
            parse_mode="HTML")
        return MAIN_GOFOOD
    return MAIN_GOFOOD

async def main_gofood_text(update, ctx):
    """Handle GoFood input - single step, gross or net based on user choice."""
    text = update.message.text.strip()
    step = ctx.user_data.get("gofood_step", "order")

    if text.lower() == "skip":
        ctx.user_data["extracted"]["gofood_order"] = 0
        ctx.user_data["extracted"]["gofood_net"] = 0
    else:
        raw = re.sub(r"[^\d]", "", re.sub(r"\.(?=\d{3})", "", text))
        if not raw:
            await update.message.reply_text("Angka tidak valid. Ketik nominal atau <code>skip</code>.", parse_mode="HTML")
            return MAIN_GOFOOD
        amount = int(raw)
        if step == "order":  # gross input - auto calc net
            ctx.user_data["extracted"]["gofood_order"] = amount
            ctx.user_data["extracted"]["gofood_net"] = round(amount * 0.993)
        else:  # net_only - set both to same value
            ctx.user_data["extracted"]["gofood_order"] = amount
            ctx.user_data["extracted"]["gofood_net"] = amount
        ctx.user_data["gofood_step"] = "order"

    d = ctx.user_data["extracted"]
    restaurant = ctx.user_data.get("restaurant","")
    audit = ctx.user_data.get("audit_text")
    summary = fmt(restaurant, d)
    if audit:
        summary += "\n\n<b>Catatan Audit AI:</b>\n" + esc(audit)
    kb = [
        [InlineKeyboardButton("Ya, Simpan!", callback_data="save")],
        [InlineKeyboardButton("Ada yang Salah", callback_data="edit")],
        [InlineKeyboardButton("Batalkan", callback_data="cancel")],
    ]
    await update.message.reply_text(
        summary + "\n\n<b>Apakah data sudah benar?</b>",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
    return CONFIRM_DATA

async def _show_confirm(q, ctx):
    d = ctx.user_data["extracted"]
    restaurant = ctx.user_data.get("restaurant","")
    audit = ctx.user_data.get("audit_text")
    summary = fmt(restaurant, d)
    if audit:
        summary += "\n\n<b>Catatan Audit AI:</b>\n" + esc(audit)
    kb = [
        [InlineKeyboardButton("Ya, Simpan!", callback_data="save")],
        [InlineKeyboardButton("Ada yang Salah", callback_data="edit")],
        [InlineKeyboardButton("Batalkan", callback_data="cancel")],
    ]
    await q.edit_message_text(
        summary + "\n\n<b>Apakah data sudah benar?</b>",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def confirm_data(update, ctx):
    q = update.callback_query
    await q.answer()
    if q.data == "cancel":
        await q.edit_message_text("Dibatalkan.")
        return ConversationHandler.END
    if q.data == "edit":
        await q.edit_message_text(
            "<b>Mode Koreksi</b>\n\nKetik: <code>field: nilai</code>\n\n"
            "Fields: tanggal, omzet, belanja_warung, belanja_pasar, gofood_order, gofood_net\n\n"
            "Selesai ketik /simpan", parse_mode="HTML")
        return EDIT_FIELD
    await q.edit_message_text("Menyimpan ke Google Sheets...")
    # send original extracted data; Apps Script handles WKB Tuban omzet adjustment server-side
    save_data = dict(ctx.user_data["extracted"])
    ok = save_to_sheets(ctx.user_data["restaurant"], save_data)
    if ok:
        d = save_data  # keuntungan_calc will adjust omzet internally for WKB Tuban
        omzet_adj, tb, k = keuntungan_calc(ctx.user_data["restaurant"], d)
        ctx.user_data["belanja_photos"] = []
        ctx.user_data["belanja_counter_msg_id"] = None
        kb = [
            [InlineKeyboardButton("Ya, lampirkan nota", callback_data="validate_belanja")],
            [InlineKeyboardButton("Tidak, selesai", callback_data="skip_belanja")],
        ]
        await q.edit_message_text(
            "<b>TERSIMPAN!</b>\n\n"
            "Cabang: " + ctx.user_data["restaurant"] + "\n"
            "Tanggal: " + str(d.get("tanggal","")) + "\n"
            "Pemasukan: Rp " + format(omzet_adj,",") + "\n"
            "Total Belanja: Rp " + format(tb,",") + "\n"
            "Omzet Bersih: Rp " + format(k,",") + "\n\n"
            "<b>Ingin validasi belanja dengan nota/struk?</b>",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
        return VALIDATE_BELANJA
    else:
        await q.edit_message_text("Gagal simpan. Cek APPS_SCRIPT_URL.")
    return ConversationHandler.END

async def edit_field(update, ctx):
    text = update.message.text.strip()
    if text.lower() in ["/simpan","simpan"]:
        d = ctx.user_data.get("extracted",{})
        r = ctx.user_data.get("restaurant","")
        await update.message.reply_text(fmt(r,d) + "\n\nMenyimpan...", parse_mode="HTML")
        ok = save_to_sheets(r, d)
        await update.message.reply_text("<b>Tersimpan!</b>" if ok else "Gagal simpan.", parse_mode="HTML")
        return ConversationHandler.END
    if ":" not in text:
        await update.message.reply_text("Format: <code>field: nilai</code>\nAtau /simpan", parse_mode="HTML")
        return EDIT_FIELD
    field, value = text.split(":",1)
    field = field.strip().lower().replace(" ","_")
    value = value.strip()
    NUMS = ["omzet","belanja_warung","belanja_pasar","gofood_order","gofood_net"]
    if field in NUMS:
        try:
            n = int(float(re.sub(r"[^\d.]","",value)))
            ctx.user_data["extracted"][field] = n
            await update.message.reply_text(
                "<code>" + field + "</code> = <b>Rp " + format(n,",") + "</b>\n\nKoreksi lain atau /simpan",
                parse_mode="HTML")
        except:
            await update.message.reply_text("Angka tidak valid.")
    elif field == "tanggal":
        ctx.user_data["extracted"]["tanggal"] = value
        await update.message.reply_text("Tanggal = <b>" + value + "</b>\n\nKoreksi lain atau /simpan", parse_mode="HTML")
    else:
        await update.message.reply_text("Field tidak dikenal: " + field)
    return EDIT_FIELD

# ======= VALIDATE BELANJA =======
async def validate_belanja_start(update, ctx):
    q = update.callback_query
    await q.answer()
    if q.data == "skip_belanja":
        await q.edit_message_text("Selesai. Laporan tersimpan!")
        return ConversationHandler.END
    restaurant = ctx.user_data.get("restaurant","")
    ctx.user_data["belanja_photos"] = []
    ctx.user_data["belanja_counter_msg_id"] = None
    d = ctx.user_data.get("extracted",{})
    bw = d.get("belanja_warung",0); bp = d.get("belanja_pasar",0)
    ref = ""
    if bw or bp:
        ref = "\n\n<i>Referensi: Belanja Warung Rp " + format(bw,",") + " | Belanja Pasar Rp " + format(bp,",") + "</i>"
    await q.edit_message_text(
        "<b>Validasi Belanja - " + restaurant + "</b>\n\n"
        "Kirim foto nota/struk belanja (boleh lebih dari satu).\n"
        "Tekan <b>Analisis</b> setelah semua foto terkirim." + ref,
        parse_mode="HTML")
    return VALIDATE_BELANJA

async def validate_belanja_photo(update, ctx):
    photo = update.message.photo[-1]
    f = await ctx.bot.get_file(photo.file_id)
    ctx.user_data.setdefault("belanja_photos",[]).append(bytes(await f.download_as_bytearray()))
    count = len(ctx.user_data["belanja_photos"])
    kb = [
        [InlineKeyboardButton("Analisis " + str(count) + " Nota", callback_data="do_analyze_belanja")],
        [InlineKeyboardButton("Selesai Tanpa Analisis", callback_data="skip_belanja")],
    ]
    mid = ctx.user_data.get("belanja_counter_msg_id")
    if mid:
        try:
            await ctx.bot.edit_message_text(
                chat_id=update.effective_chat.id, message_id=mid,
                text=str(count) + " foto diterima. Kirim lagi atau tekan Analisis.",
                reply_markup=InlineKeyboardMarkup(kb))
            return VALIDATE_BELANJA
        except Exception:
            pass
    msg = await update.message.reply_text(
        str(count) + " foto diterima. Kirim lagi atau tekan Analisis.",
        reply_markup=InlineKeyboardMarkup(kb))
    ctx.user_data["belanja_counter_msg_id"] = msg.message_id
    return VALIDATE_BELANJA

async def validate_belanja_action(update, ctx):
    q = update.callback_query
    await q.answer()
    if q.data == "skip_belanja":
        await q.edit_message_text("Selesai. Laporan tersimpan!")
        return ConversationHandler.END
    restaurant = ctx.user_data.get("restaurant","")
    main_data = ctx.user_data.get("extracted",{})
    photos = ctx.user_data.get("belanja_photos",[])
    if not photos:
        await q.edit_message_text("Tidak ada foto. Laporan sudah tersimpan.")
        return ConversationHandler.END
    await q.edit_message_text("Menganalisis " + str(len(photos)) + " nota vs laporan utama...")
    findings = analyze_belanja_detail(photos, restaurant, main_data)
    if not findings:
        await q.edit_message_text("Gagal menganalisis. Laporan utama sudah tersimpan.")
        return ConversationHandler.END
    save_to_sheets(restaurant, {"findings": findings}, "belanja_detail")
    await q.edit_message_text(
        "<b>Hasil Validasi Belanja - " + restaurant + "</b>\n\n" + esc(findings),
        parse_mode="HTML")
    return ConversationHandler.END

# ======= PENGELUARAN =======
async def pengeluaran_start(update, ctx):
    await update.message.reply_text("<b>Input Pengeluaran 10 Hari</b>\n\nPilih cabang:", parse_mode="HTML", reply_markup=restaurant_keyboard())
    return PENG_SELECT

async def peng_restaurant_selected(update, ctx):
    q = update.callback_query
    await q.answer()
    if q.data == "cancel":
        await q.edit_message_text("Dibatalkan."); return ConversationHandler.END
    restaurant = q.data.split("|",1)[1]
    ctx.user_data["peng_restaurant"] = restaurant
    await q.edit_message_text(
        "<b>" + restaurant + " - Pengeluaran 10 Hari</b>\n\n"
        "Kirim FOTO nota atau KETIK langsung:\n"
        "<code>Beras: 250000\nPLN: 150000\nPDAM: 75000\nWifi: 100000\nSampah: 50000\nKasbon: 200000</code>",
        parse_mode="HTML")
    return PENG_WAIT_INPUT


async def peng_period_selected(update, ctx):
    q = update.callback_query; await q.answer()
    if q.data == "cancel":
        await q.edit_message_text("Dibatalkan."); return ConversationHandler.END
    ptag = q.data.split("_")[1]  # "P1", "P2", or "P3"
    ctx.user_data["peng_period"] = ptag
    restaurant = ctx.user_data.get("peng_restaurant","")
    today = datetime.date.today()
    ctx.user_data["peng_periode_tag"] = today.strftime("%Y-%m") + "-" + ptag
    await q.edit_message_text(
        "<b>" + restaurant + " - Pengeluaran " + ptag + "</b>\n\n"
        "Kirim FOTO nota atau KETIK langsung:\n"
        "<code>Beras: 250000\nPLN: 150000\nPDAM: 75000\nWifi: 100000\nSampah: 50000\nKasbon: 200000</code>",
        parse_mode="HTML")
    return PENG_WAIT_INPUT

async def peng_input_received(update, ctx):
    restaurant = ctx.user_data.get("peng_restaurant","")
    await update.message.reply_text("Menganalisis pengeluaran...")
    if update.message.photo:
        photo = update.message.photo[-1]
        f = await ctx.bot.get_file(photo.file_id)
        data = extract_pengeluaran(bytes(await f.download_as_bytearray()), restaurant, is_image=True)
    else:
        data = extract_pengeluaran(update.message.text, restaurant, is_image=False)
    if not data:
        await update.message.reply_text("Gagal membaca. Coba lagi."); return PENG_WAIT_INPUT
    ctx.user_data["peng_data"] = data
    fields = [("Beras","beras"),("PLN","pln"),("PDAM","pdam"),("Wifi","wifi"),("Sampah","sampah"),("Kasbon","kasbon"),("Gaji","gaji"),("Lain-lain","lain_lain")]
    lines = ["<b>Pengeluaran 10 Hari - " + restaurant + "</b>","Periode: <b>" + esc(data.get("periode","?")) + "</b>","--------------------"]
    for label, key in fields:
        if data.get(key,0) > 0: lines.append(label + ": Rp " + format(data[key],","))
    lines += ["--------------------","TOTAL: <b>Rp " + format(data.get("total",0),",") + "</b>"]

    # Monthly expenses check: remind/warn about Kontrak Bangunan and BTK
    monthly = MONTHLY_EXPENSES.get(restaurant, {})
    if monthly:
        today_day = datetime.date.today().day
        ptag = "P1" if today_day <= 10 else ("P2" if today_day <= 20 else "P3")
        exp_btk = monthly.get("btk", 0)
        exp_kb  = monthly.get("kb", 0)
        btk_kasbon = monthly.get("btk_kasbon", 0)
        extracted_gaji = data.get("gaji", 0)
        extracted_lain = data.get("lain_lain", 0)
        lines.append("")
        lines.append("📋 <b>Cek Biaya Bulanan (" + ptag + "):</b>")
        if ptag == "P3":
            # P3 end-of-month: expect BTK remaining (after kasbon advance) and KB
            btk_remaining = exp_btk - btk_kasbon
            if exp_btk > 0:
                gaji_ok = "✅" if extracted_gaji >= int(btk_remaining * 0.9) else "⚠️"
                lines.append(gaji_ok + " Gaji/BTK: ekspektasi <b>Rp " + format(btk_remaining, ",") + "</b> | diinput Rp " + format(extracted_gaji, ","))
                if btk_kasbon > 0:
                    lines.append("   <i>(BTK total Rp " + format(exp_btk, ",") + " - kasbon P1 Rp " + format(btk_kasbon, ",") + ")</i>")
            else:
                lines.append("ℹ️ Tidak ada BTK untuk cabang ini")
            if exp_kb > 0:
                lines.append("📌 Kontrak Bangunan: <b>Rp " + format(exp_kb, ",") + "</b> (pastikan masuk ke lain_lain | diinput Rp " + format(extracted_lain, ",") + ")")
        elif ptag == "P1":
            if exp_btk > 0 and btk_kasbon > 0:
                lines.append("📌 BTK bulan ini: Rp " + format(exp_btk, ",") + " | Kasbon advance P1: Rp " + format(btk_kasbon, ","))
                lines.append("   <i>(Sisa BTK Rp " + format(exp_btk - btk_kasbon, ",") + " akan masuk di pengeluaran P3)</i>")
            elif exp_btk > 0:
                lines.append("📌 BTK bulan ini: Rp " + format(exp_btk, ",") + " (dicatat di P3 akhir bulan)")
        # P2: no monthly expense notes needed

    kb = [[InlineKeyboardButton("Ya, Simpan!", callback_data="save_peng")],[InlineKeyboardButton("Batalkan", callback_data="cancel_peng")]]
    await update.message.reply_text("\n".join(lines) + "\n\n<b>Data sudah benar?</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
    return PENG_CONFIRM

async def peng_confirm(update, ctx):
    q = update.callback_query; await q.answer()
    if q.data == "cancel_peng":
        await q.edit_message_text("Dibatalkan."); return ConversationHandler.END
    peng_data = ctx.user_data.get("peng_data", {})
    # Tag with period: YYYY-MM-P1 (day 1-10), YYYY-MM-P2 (11-20), YYYY-MM-P3 (21+)
    _today = datetime.date.today(); _d = _today.day
    _ptag = "P1" if _d <= 10 else ("P2" if _d <= 20 else "P3")
    peng_data["periode"] = _today.strftime("%Y-%m") + "-" + _ptag
    ok = save_to_sheets(ctx.user_data.get("peng_restaurant",""), peng_data, "pengeluaran")
    await q.edit_message_text("<b>Pengeluaran tersimpan!</b>" if ok else "Gagal simpan.", parse_mode="HTML")
    return ConversationHandler.END

# ======= GOFOOD =======
async def gofood_start(update, ctx):
    await update.message.reply_text("<b>Upload Laporan GoFood</b>\n\nPilih cabang:", parse_mode="HTML", reply_markup=restaurant_keyboard())
    return GOFOOD_SELECT

async def gofood_restaurant_selected(update, ctx):
    q = update.callback_query; await q.answer()
    if q.data == "cancel":
        await q.edit_message_text("Dibatalkan."); return ConversationHandler.END
    restaurant = q.data.split("|",1)[1]
    ctx.user_data["gofood_restaurant"] = restaurant
    await q.edit_message_text("<b>" + restaurant + " - Laporan GoFood</b>\n\nKirim screenshot laporan GoFood.", parse_mode="HTML")
    return GOFOOD_WAIT_PHOTO

async def gofood_photo_received(update, ctx):
    restaurant = ctx.user_data.get("gofood_restaurant","")
    photo = update.message.photo[-1]
    f = await ctx.bot.get_file(photo.file_id)
    await update.message.reply_text("Membaca laporan GoFood...")
    data = extract_gofood_report(bytes(await f.download_as_bytearray()), restaurant)
    if not data or (data.get("total_netto",0) == 0 and data.get("jumlah_transaksi",0) == 0):
        await update.message.reply_text("Gagal membaca. Pastikan screenshot menampilkan total penjualan/pendapatan, lalu coba lagi."); return GOFOOD_WAIT_PHOTO
    ctx.user_data["gofood_data"] = data
    lines = ["<b>Laporan GoFood - " + restaurant + "</b>","--------------------",
             "Periode: <b>" + esc(data.get("periode","?")) + "</b>",
             "Jumlah Order: <b>" + format(data.get("jumlah_transaksi",0),",") + "</b>",
             "Pendapatan Bruto: <b>Rp " + format(data.get("total_bruto",0),",") + "</b>",
             "Pendapatan Netto: <b>Rp " + format(data.get("total_netto",0),",") + "</b>","--------------------"]
    await update.message.reply_text(
        "\n".join(lines) + "\n\n"
        "<b>Masukkan tanggal laporan ini</b> untuk update kolom GoFood di sheet utama.\n"
        "Contoh: <code>9 Jun 2026</code> atau <code>09/06/2026</code>\n\n"
        "Atau ketik <code>skip</code> untuk simpan tanpa update sheet utama.",
        parse_mode="HTML")
    return GOFOOD_DATE

async def gofood_date_received(update, ctx):
    restaurant = ctx.user_data.get("gofood_restaurant","")
    data = ctx.user_data.get("gofood_data",{})
    text = update.message.text.strip()
    if text.lower() != "skip":
        tgl = parse_date_input(text)
        if not tgl:
            await update.message.reply_text(
                "Format tanggal tidak dikenal. Coba <code>9 Jun 2026</code> atau ketik <code>skip</code>.",
                parse_mode="HTML")
            return GOFOOD_DATE
        data["tanggal"] = tgl
        ctx.user_data["gofood_data"] = data
    kb = [[InlineKeyboardButton("Ya, Simpan!", callback_data="save_gofood")],[InlineKeyboardButton("Batalkan", callback_data="cancel_gofood")]]
    tgl_info = "Tanggal: <b>" + data.get("tanggal","-") + "</b>\n" if data.get("tanggal") else ""
    await update.message.reply_text(
        "<b>Konfirmasi GoFood - " + restaurant + "</b>\n" + tgl_info +
        "Netto: <b>Rp " + format(data.get("total_netto",0),",") + "</b>\n"
        "Order: <b>" + format(data.get("jumlah_transaksi",0),",") + "</b>\n\n"
        "<b>Simpan data ini?</b>",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
    return GOFOOD_CONFIRM

async def gofood_confirm(update, ctx):
    q = update.callback_query; await q.answer()
    if q.data == "cancel_gofood":
        await q.edit_message_text("Dibatalkan."); return ConversationHandler.END
    ok = save_to_sheets(ctx.user_data.get("gofood_restaurant",""), ctx.user_data.get("gofood_data",{}), "gofood")
    msg = "<b>Laporan GoFood tersimpan!</b>"
    if ctx.user_data.get("gofood_data",{}).get("tanggal"):
        msg += "\nKolom GoFood di sheet utama diupdate."
    await q.edit_message_text(msg if ok else "Gagal simpan.", parse_mode="HTML")
    return ConversationHandler.END

# ======= LAPORAN GOFOOD =======
async def lgofood_start(update, ctx):
    await update.message.reply_text(
        "<b>Laporan GoFood Harian</b>\n\nPilih cabang:",
        parse_mode="HTML", reply_markup=restaurant_keyboard())
    return LGOFOOD_SELECT

async def lgofood_restaurant_selected(update, ctx):
    q = update.callback_query; await q.answer()
    if q.data == "cancel":
        await q.edit_message_text("Dibatalkan."); return ConversationHandler.END
    restaurant = q.data.split("|", 1)[1]
    ctx.user_data["lgofood_restaurant"] = restaurant
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("10 Hari Terakhir", callback_data="lgofood_latest")],
        [InlineKeyboardButton("Pilih Tanggal Mulai", callback_data="lgofood_manual")],
        [InlineKeyboardButton("Batalkan", callback_data="cancel")],
    ])
    await q.edit_message_text(
        "<b>" + restaurant + "</b>\n\nPilih rentang data GoFood:",
        parse_mode="HTML", reply_markup=kb)
    return LGOFOOD_DATE

async def lgofood_date_option(update, ctx):
    q = update.callback_query; await q.answer()
    restaurant = ctx.user_data.get("lgofood_restaurant", "")
    if q.data == "cancel":
        await q.edit_message_text("Dibatalkan."); return ConversationHandler.END
    if q.data == "lgofood_latest":
        await q.edit_message_text("Mengambil data GoFood " + restaurant + "...")
        s = fetch_summary(restaurant, days=10)
        await _send_lgofood(q.message.reply_text, restaurant, s)
        return ConversationHandler.END
    if q.data == "lgofood_manual":
        await q.edit_message_text(
            "<b>" + restaurant + " - Laporan GoFood</b>\n\n"
            "Ketik tanggal mulai, contoh:\n"
            "<code>1 Jun 2026</code>\n"
            "<code>01/06/2026</code>\n"
            "<code>2026-06-01</code>",
            parse_mode="HTML")
        return LGOFOOD_DATE
    return LGOFOOD_DATE

async def lgofood_date_input(update, ctx):
    restaurant = ctx.user_data.get("lgofood_restaurant", "")
    text = update.message.text.strip()
    start_date = parse_date_input(text)
    if not start_date:
        await update.message.reply_text(
            "Format tanggal tidak dikenal. Coba:\n"
            "<code>1 Jun 2026</code> atau <code>01/06/2026</code>",
            parse_mode="HTML")
        return LGOFOOD_DATE
    await update.message.reply_text("Mengambil data GoFood " + restaurant + " dari " + text + "...")
    s = fetch_summary(restaurant, days=10, start_date=start_date)
    await _send_lgofood(update.message.reply_text, restaurant, s)
    return ConversationHandler.END

async def _send_lgofood(send_fn, restaurant, s):
    if not s or s.get("status") == "error" or not s.get("rows"):
        await send_fn("Gagal mengambil data atau tidak ada data GoFood."); return
    rows = s.get("rows", [])
    periode = s.get("periode", "-")
    total_gf = sum(r.get("gofood_net", 0) for r in rows)
    total_omzet = sum(r.get("omzet", 0) for r in rows)
    lines = [
        "<b>Laporan GoFood Harian</b>",
        "<b>" + restaurant + "</b>",
        "Periode: " + periode,
        "====================",
        "",
    ]
    for r in rows:
        gf = r.get("gofood_net", 0)
        omzet = r.get("omzet", 0)
        date = r.get("date", "?")
        pct = round(gf / omzet * 100, 1) if omzet > 0 else 0
        gf_str = "Rp " + format(gf, ",") if gf > 0 else "<i>Tidak ada</i>"
        pct_str = (" (" + str(pct) + "% dari omzet)") if gf > 0 else ""
        lines.append("<b>" + date + "</b>")
        lines.append("  GoFood: " + gf_str + pct_str)
        lines.append("  Omzet Tunai: Rp " + format(omzet, ","))
        lines.append("")
    lines += [
        "====================",
        "Total GoFood (10 hari): <b>Rp " + format(total_gf, ",") + "</b>",
        "Total Omzet Tunai     : Rp " + format(total_omzet, ","),
    ]
    if total_omzet + total_gf > 0:
        pct_total = round(total_gf / (total_omzet + total_gf) * 100, 1)
        lines.append("GoFood = <b>" + str(pct_total) + "% dari total pendapatan</b>")
    await send_fn("\n".join(lines), parse_mode="HTML")
def get_month_and_rows(restaurant, rows):
    """Find the dominant month from rows and return all rows for that month."""
    if not rows:
        return None, []
    MONTHS_S = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"Mei":5,"May":5,"Jun":6,
                "Jul":7,"Ags":8,"Aug":8,"Sep":9,"Okt":10,"Oct":10,"Nov":11,"Des":12,"Dec":12}
    def row_ym(r):
        # date format: "24 Jun 2026" -> "2026-06"
        p = r.get("date","").split()
        if len(p) >= 3:
            try:
                return "%s-%02d" % (p[2], MONTHS_S.get(p[1], 0))
            except: pass
        return ""
    def row_date_key(r):
        # Sort key: parse "d MMM yyyy" -> datetime.date for correct chronological order
        p = r.get("date","").split()
        try: return datetime.date(int(p[2]), MONTHS_S.get(p[1],1), int(p[0]))
        except: return datetime.date(2000,1,1)
    mc = {}
    for r in rows:
        ym = row_ym(r)
        if ym:
            mc[ym] = mc.get(ym, 0) + 1
    if not mc:
        return None, []
    month = max(mc, key=mc.get)
    # Fetch full month data (31 days covers any month)
    s_full = fetch_summary(restaurant, days=30)
    all_rows = s_full.get("rows", []) if s_full else []
    month_rows = sorted(
        [r for r in all_rows if row_ym(r) == month],
        key=row_date_key  # correct chronological sort, not alphabetical
    )
    return month, month_rows

def calculate_profit_sharing(restaurant, rows, period=1, pengeluaran=0, pe_p1=0, pe_p2=0, kasbon_total=0, kasbon_p2=0, gofood_monthly=0):
    def prof(lst): return sum(r.get("keuntungan", 0) for r in lst)
    def gf(lst):   return sum(r.get("gofood_net", 0) for r in lst)
    def drange(lst):
        if not lst: return "-"
        return lst[0].get("date", "-") + " s/d " + lst[-1].get("date", "-")
    MONTHS_S = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"Mei":5,"May":5,"Jun":6,"Jul":7,"Ags":8,"Aug":8,"Sep":9,"Okt":10,"Oct":10,"Nov":11,"Des":12,"Dec":12}
    def parse_row_date(r):
        p = r.get("date","").split(); 
        try: return datetime.date(int(p[2]), MONTHS_S.get(p[1],1), int(p[0]))
        except: return datetime.date(2000,1,1)
    sorted_rows = sorted(rows, key=parse_row_date)
    # Deduplicate by date (keep last entry per date) — safety net in case Apps Script
    # returns duplicate rows (re-entered/corrected data).
    seen_d = {}
    for r in sorted_rows:
        seen_d[r.get("date", "")] = r
    sorted_rows = sorted(seen_d.values(), key=parse_row_date)
    lines = ["", "====================", "<b>\U0001f4b0 BAGI HASIL:</b>"]
    if period == 1:
        gofood_total = gf(sorted_rows)
        ke_total = prof(sorted_rows)
        profit = max(0, ke_total + gofood_total - pengeluaran)
        lines += [
            "Periode ke-1 (" + str(len(sorted_rows)) + " hari operasional)",
            "\U0001f4c5 " + drange(sorted_rows),
            "  Keuntungan harian: Rp " + format(ke_total, ","),
            ("  GoFood: +Rp " + format(gofood_total, ",")) if gofood_total > 0 else "",
            ("  Pengeluaran operasional P1: -Rp " + format(pengeluaran, ",")) if pengeluaran > 0 else "",
            "Manager \u2192 Investor: <b>Rp " + format(profit, ",") + "</b>",
            "<i>(100% profit bersih periode ini)</i>",
        ]
    elif period == 2:
        gofood_total = gf(sorted_rows)
        ke_total = prof(sorted_rows)
        profit = max(0, ke_total + gofood_total - pengeluaran)
        lines += [
            "Periode ke-2 (" + str(len(sorted_rows)) + " hari operasional)",
            "\U0001f4c5 " + drange(sorted_rows),
            "  Keuntungan harian: Rp " + format(ke_total, ","),
            ("  GoFood: +Rp " + format(gofood_total, ",")) if gofood_total > 0 else "",
            ("  Pengeluaran operasional P2: -Rp " + format(pengeluaran, ",")) if pengeluaran > 0 else "",
            "Manager \u2192 Investor: <b>Rp " + format(profit, ",") + "</b>",
            "<i>(100% profit bersih periode ini)</i>",
        ]
    else:
        # P1 = first 10 operational days, P2 = next 10, P3 = remaining
        # sorted_rows already sorted chronologically — do NOT filter by month
        # (periods can span month boundaries, e.g. 25 May - 4 Jun = P1)
        p1 = sorted_rows[:10]
        p2 = sorted_rows[10:20]
        p3 = sorted_rows[20:]
        profit_p1 = prof(p1); profit_p2 = prof(p2); profit_p3 = prof(p3)
        # GoFood is settled at P3 reconciliation (manager holds GoFood until month-end)
        # Prefer monthly GoFood report total (authoritative) over per-day sum when available
        using_monthly_gofood = gofood_monthly > 0
        gofood_daily_sum = gf(sorted_rows)
        gofood_total = gofood_monthly if using_monthly_gofood else gofood_daily_sum
        gofood_deviation = (gofood_monthly - gofood_daily_sum) if using_monthly_gofood else 0
        pe_p3 = pengeluaran - pe_p1 - pe_p2

        if restaurant == "WKB Tuban" and kasbon_p2 > 0:
            # WKB Tuban settlement — verified against manual calculation:
            # - kasbon_p2 = manager's PERSONAL advance (col G). NOT a shared operational cost.
            #   It IS counted in pe_p2 col J (Gemini includes kasbon in auto-total), so we
            #   subtract it from pengeluaran to get true shared ops.
            # - KB (Kontrak Bangunan) is a fixed monthly cost in MONTHLY_EXPENSES config.
            #   It is NOT in the sheet (paid at settlement, not from daily P3 cash).
            #   We ADD it to shared_pe here; pe_p3 from sheet is left unchanged for p3_cash.
            # - paid_p2 = profit_p2 - pe_p2 (full pe_p2, since kasbon was physically kept by mgr)
            # - Balance: bal = inv - (p3_cash + kasbon_p2); positive = investor owes manager
            kb = MONTHLY_EXPENSES.get(restaurant, {}).get("kb", 0)
            shared_pe = pengeluaran - kasbon_p2 + kb  # strip manager kasbon, add fixed KB
            total   = profit_p1 + profit_p2 + profit_p3 + gofood_total - shared_pe
            paid_p1 = max(0, profit_p1 - pe_p1)
            paid_p2 = max(0, profit_p2 - pe_p2)  # pe_p2 incl. kasbon: manager kept it, so less transferred
            paid    = paid_p1 + paid_p2
            inv     = math.ceil(total / 2)         # ceiling division matches manual
            mgr     = inv - kasbon_p2              # manager's 50% minus personal kasbon
            p3_cash = max(0, profit_p3 - pe_p3)   # cash at manager (pe_p3 from sheet, no KB deducted)
            bal     = inv - (p3_cash + kasbon_p2)  # positive = investor still owes manager
            total_pendapatan = profit_p1 + profit_p2 + profit_p3 + gofood_total
            gf_label = "GoFood/QRIS (laporan bulan)" if using_monthly_gofood else "\u26a0\ufe0f GoFood/QRIS (per hari, belum upload)"
            lines += [
                "Periode ke-3 \u2014 <b>Rekap Bulanan</b>",
                "====================", "",
                "\U0001f4b0 <b>PENDAPATAN:</b>",
                "  \u2022 P1 " + drange(p1) + " (" + str(len(p1)) + " hari)",
                "    Rp " + format(profit_p1, ","),
                "  \u2022 P2 " + drange(p2) + " (" + str(len(p2)) + " hari)",
                "    Rp " + format(profit_p2, ","),
                "  \u2022 P3 " + drange(p3) + " (" + str(len(p3)) + " hari)",
                "    Rp " + format(profit_p3, ","),
                ("  \u2022 " + gf_label + ": Rp " + format(gofood_total, ",") +
                 (("  \u26a0\ufe0f selisih Rp " + format(abs(gofood_deviation), ",") + " dari input harian") if using_monthly_gofood and gofood_deviation != 0 else "")
                ) if gofood_total > 0 else "  \u2022 GoFood/QRIS: Rp 0",
                "  \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
                "  <b>Total Pendapatan: Rp " + format(total_pendapatan, ",") + "</b>", "",
                "\U0001f4b8 <b>BIAYA:</b>",
                "  \u2022 Pengeluaran Operasional: Rp " + format(pengeluaran - kasbon_p2, ","),
                ("  \u2022 Kontrak Bangunan: Rp " + format(kb, ",")) if kb > 0 else "",
                "  \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
                "  <b>Total Biaya: Rp " + format(shared_pe, ",") + "</b>", "",
                "\U0001f4ca <b>Keuntungan Total: Rp " + format(total, ",") + "</b>",
                "\U0001f91d <b>Keuntungan Masing2 Pihak: Rp " + format(inv, ",") + "</b>",
                "====================", "",
                "\U0001f4b5 <b>PERHITUNGAN UANG:</b>",
                "  Hak Pengelola    : Rp " + format(inv, ","),
                "  Uang di Pengelola: Rp " + format(p3_cash + kasbon_p2, ","),
                "   \u2514 kas P3: Rp " + format(p3_cash, ",") + " + kas bon: Rp " + format(kasbon_p2, ","),
                "====================",
            ]
            formula_line = "  (Rp " + format(inv, ",") + " \u2212 Rp " + format(p3_cash + kasbon_p2, ",") + " = Rp " + format(bal, ",") + ")"
            if bal > 0:
                lines.append("\u27a1\ufe0f <b>Investor setor ke Pengelola: Rp " + format(bal, ",") + "</b>")
                lines.append(formula_line)
            elif bal < 0:
                lines.append("\u2b05\ufe0f <b>Pengelola transfer ke Investor: Rp " + format(abs(bal), ",") + "</b>")
                lines.append(formula_line)
            else:
                lines.append("\u2705 <b>Sudah seimbang, tidak ada transfer</b>")
        else:
            # Normal restaurants: all pengeluaran are shared costs, split 50/50
            total   = profit_p1 + profit_p2 + profit_p3 + gofood_total - pengeluaran
            paid_p1 = max(0, profit_p1 - pe_p1)
            paid_p2 = max(0, profit_p2 - pe_p2)
            paid    = paid_p1 + paid_p2
            inv     = total // 2
            bal     = inv - paid
            lines += [
                "Periode ke-3 \u2014 <b>Rekap Bulanan</b>", "",
                "P1 " + drange(p1) + " (" + str(len(p1)) + " hari): Rp " + format(profit_p1, ",") + ((" (pe: -Rp " + format(pe_p1, ",") + ")") if pe_p1 > 0 else ""),
                "P2 " + drange(p2) + " (" + str(len(p2)) + " hari): Rp " + format(profit_p2, ",") + ((" (pe: -Rp " + format(pe_p2, ",") + ")") if pe_p2 > 0 else ""),
                "P3 " + drange(p3) + " (" + str(len(p3)) + " hari): Rp " + format(profit_p3, ","),
                ("  Pengeluaran P3: -Rp " + format(pe_p3, ",")) if pe_p3 > 0 else "",
                ("GoFood (laporan bulan): +Rp " + format(gofood_total, ",")) if (gofood_total > 0 and using_monthly_gofood) else "",
                ("\u26a0\ufe0f GoFood (per hari, belum upload laporan): +Rp " + format(gofood_total, ",")) if (gofood_total > 0 and not using_monthly_gofood) else "",
                "Total Bulanan (bersih): <b>Rp " + format(total, ",") + "</b>", "",
                "Bagian Investor (50%): Rp " + format(inv, ","),
                "Sudah ditransfer P1+P2: Rp " + format(paid, ","), "",
            ]
            # positive bal = manager still owes investor; negative = investor returns to manager
            if bal > 0:
                lines.append("\u27a1\ufe0f Manager transfer ke Investor: <b>Rp " + format(bal, ",") + "</b>")
            elif bal < 0:
                lines.append("\u2b05\ufe0f Investor kembalikan ke Manager: <b>Rp " + format(abs(bal), ",") + "</b>")
            else:
                lines.append("\u2705 Sudah seimbang, tidak ada transfer")
    lines = [l for l in lines if l != ""]  # remove empty strings from conditional
    return "\n".join(lines)

def build_ringkasan_msg(restaurant, s, period=1):
    """Returns a list of message parts to send (split to stay under Telegram 4096 limit)."""
    pe    = s.get("total_pengeluaran", 0)
    pe_p1 = s.get("pengeluaran_p1", 0)
    pe_p2 = s.get("pengeluaran_p2", 0)
    pe_p3 = s.get("pengeluaran_p3", 0)
    kasbon_total = s.get("kasbon_total", 0)
    kasbon_p1    = s.get("kasbon_p1", 0)
    kasbon_p2    = s.get("kasbon_p2", 0)
    logger.info("DEBUG kasbon — restaurant=%s period=%d pe_p1=%d kasbon_p1=%d kasbon_p2=%d pe_raw=%s",
                restaurant, period, pe_p1, kasbon_p1, kasbon_p2,
                str({k: s.get(k) for k in ["pengeluaran_p1","kasbon_p1","kasbon_p2","kasbon_total"]}))
    gofood_monthly = s.get("total_gofood_netto", 0)  # monthly GoFood report total (authoritative for P3)
    rows = s.get("rows", [])
    om = sum(r.get("omzet", 0) for r in rows)
    gf = sum(r.get("gofood_net", 0) for r in rows)
    bw = sum(r.get("belanja_warung", 0) for r in rows)
    bl = sum(r.get("total_belanja", 0) for r in rows)
    k  = sum(r.get("keuntungan", 0) for r in rows)
    if restaurant == "WKB Tuban":
        ti = om
        to = bl - bw
    else:
        ti = om + gf
        to = bl
    pr = k - pe

    header = [
        "<b>Ringkasan " + ("Bulanan" if period == 3 else "10 Hari") + " - " + restaurant + "</b>",
        "Periode: <b>" + s.get("periode","-") + "</b>",
        "====================",
    ]

    # Build daily detail rows — each day is a small chunk
    day_lines = []
    if rows:
        day_lines.append("<b>DETAIL PER HARI:</b>")
        for r in rows:
            d    = r.get("date","?")
            r_om = r.get("omzet", 0)
            r_gf = r.get("gofood_net", 0)
            r_bl = r.get("total_belanja", 0)
            r_k  = r.get("keuntungan", 0)
            r_in = r_om + (0 if restaurant == "WKB Tuban" else r_gf)
            day_lines.append("")
            day_lines.append("<b>" + d + "</b>")
            day_lines.append("  Pemasukan : Rp " + format(r_in, ",") + ("  (GoFood: Rp " + format(r_gf,",") + ")" if r_gf > 0 and restaurant != "WKB Tuban" else ""))
            day_lines.append("  Pengeluaran: Rp " + format(r_bl, ","))
            day_lines.append("  Keuntungan: <b>Rp " + format(r_k, ",") + "</b>")
        day_lines.append("")
        day_lines.append("====================")

    profit_section = calculate_profit_sharing(
        restaurant, rows, period,
        pengeluaran = (pe_p1 - kasbon_p1) if period == 1 else ((pe_p2 - kasbon_p2) if period == 2 else pe),
        pe_p1=pe_p1, pe_p2=pe_p2,
        kasbon_total=kasbon_total, kasbon_p2=kasbon_p2,
        gofood_monthly=gofood_monthly if period == 3 else 0
    )
    # For WKB Tuban P3, the profit_section already contains a complete breakdown —
    # skip the raw TOTAL block to avoid redundant/confusing numbers.
    is_wkb_tuban_p3 = (restaurant == "WKB Tuban" and period == 3 and profit_section)
    if is_wkb_tuban_p3:
        summary_lines = []
    else:
        pe_display = (pe_p1 - kasbon_p1) if period == 1 else ((pe_p2 - kasbon_p2) if period == 2 else pe)
        pr_display = k - pe_display if period in (1, 2) else pr
        summary_lines = [
            "<b>TOTAL:</b>",
            "Total Pemasukan : Rp " + format(ti, ","),
            "Total Belanja Harian: Rp " + format(to, ","),
        ]
        if pe_display > 0:
            summary_lines.append("Pengeluaran Operasional: Rp " + format(pe_display, ","))
        summary_lines.append("<b>PROFIT BERSIH: Rp " + format(pr_display, ",") + "</b>")
    if profit_section:
        summary_lines.append(profit_section)

    # Pack into messages ≤ 4000 chars each
    parts = []
    def flush(lines):
        if lines:
            parts.append("\n".join(lines))

    current = header[:]
    for line in day_lines:
        if len("\n".join(current)) + len(line) + 1 > 3900:
            flush(current)
            current = [line]
        else:
            current.append(line)
    # Attach summary to last chunk if it fits, else new message
    summary_text = "\n".join(summary_lines)
    if len("\n".join(current)) + len(summary_text) + 1 > 3900:
        flush(current)
        parts.append(summary_text)
    else:
        current += summary_lines
        flush(current)

    return parts

async def ringkasan_start(update, ctx):
    logger.info("ringkasan_start called user=%s", update.effective_user.id if update.effective_user else "?")
    try:
        await update.message.reply_text(
            "<b>Ringkasan 10 Hari</b>\n\nPilih cabang:",
            parse_mode="HTML", reply_markup=restaurant_keyboard())
    except Exception as e:
        logger.error("ringkasan_start error: %s", e)
        raise
    return RSUM_SELECT

async def ringkasan_restaurant_selected(update, ctx):
    q = update.callback_query; await q.answer()
    if q.data == "cancel":
        await q.edit_message_text("Dibatalkan."); return ConversationHandler.END
    restaurant = q.data.split("|",1)[1]
    ctx.user_data["rsum_restaurant"] = restaurant
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Periode 1 (Hari ke 1-10)",    callback_data="rsum_p1")],
        [InlineKeyboardButton("Periode 2 (Hari ke 11-20)",   callback_data="rsum_p2")],
        [InlineKeyboardButton("Periode 3 (Rekap Bulanan)",   callback_data="rsum_p3")],
        [InlineKeyboardButton("Batalkan", callback_data="cancel")],
    ])
    await q.edit_message_text("<b>" + restaurant + "</b>\n\nPilih periode bagi hasil:", parse_mode="HTML", reply_markup=kb)
    return RSUM_PERIOD

async def ringkasan_period_selected(update, ctx):
    q = update.callback_query; await q.answer()
    if q.data == "cancel":
        await q.edit_message_text("Dibatalkan."); return ConversationHandler.END
    period_map = {"rsum_p1": 1, "rsum_p2": 2, "rsum_p3": 3}
    if q.data not in period_map:
        await q.edit_message_text("Pilihan tidak valid."); return ConversationHandler.END
    ctx.user_data["rsum_period"] = period_map[q.data]
    ptag_map = {1: "P1", 2: "P2", 3: "P3"}
    ctx.user_data["rsum_ptag"] = ptag_map[period_map[q.data]]
    restaurant = ctx.user_data.get("rsum_restaurant","")
    label = ["","Periode 1 (Hari 1-10)","Periode 2 (Hari 11-20)","Periode 3 (Rekap Bulanan)"][period_map[q.data]]
    period_num = period_map[q.data]
    if period_num == 3:
        # P3 = monthly recap: always require a start date so the P1/P2/P3 row split
        # aligns correctly (fetching "last N rows" breaks when there are closed days).
        await q.edit_message_text(
            "<b>" + restaurant + "</b> - Rekap Bulanan (P3)\n\n"
            "Ketik <b>tanggal mulai P1</b> (hari pertama bulan itu), contoh:\n"
            "<code>25 Mei 2026</code>\n"
            "<code>25/05/2026</code>\n"
            "<code>2026-05-25</code>\n\n"
            "Bot akan otomatis hitung P1 (hari 1-10), P2 (11-20), P3 (21-30).",
            parse_mode="HTML")
        return RSUM_DATE
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("10 Hari Terakhir", callback_data="rsum_latest")],
            [InlineKeyboardButton("Pilih Tanggal Mulai", callback_data="rsum_manual")],
            [InlineKeyboardButton("Batalkan", callback_data="cancel")],
        ])
    await q.edit_message_text(
        "<b>" + restaurant + "</b> - " + label + "\n\nPilih rentang data:",
        parse_mode="HTML", reply_markup=kb)
    return RSUM_DATE

async def ringkasan_date_option_selected(update, ctx):
    q = update.callback_query; await q.answer()
    restaurant = ctx.user_data.get("rsum_restaurant","")
    period     = ctx.user_data.get("rsum_period", 1)
    if q.data == "cancel":
        await q.edit_message_text("Dibatalkan."); return ConversationHandler.END
    if q.data == "rsum_latest":
        days = 30 if period == 3 else 10
        await q.edit_message_text("Mengambil data " + restaurant + "...")
        s = fetch_summary(restaurant, days=days)
        if not s or s.get("status") == "error":
            await q.edit_message_text("Gagal mengambil data."); return ConversationHandler.END
        if _needs_kasbon_prompt(s, period):
            ctx.user_data["rsum_s"] = s
            return await _prompt_kasbon(q.message.reply_text, ctx, restaurant, s, period)
        await send_ringkasan(q.message.reply_text, restaurant, s, period)
        return ConversationHandler.END
    if q.data == "rsum_manual":
        await q.edit_message_text(
            "<b>" + restaurant + " - Pilih Tanggal</b>\n\n"
            "Ketik tanggal mulai, contoh:\n"
            "<code>1 Jun 2026</code>\n"
            "<code>01/06/2026</code>\n"
            "<code>2026-06-01</code>\n\n"
            "Bot akan tampilkan ringkasan 10 hari dari tanggal tersebut.",
            parse_mode="HTML")
        return RSUM_DATE
    return RSUM_DATE

async def ringkasan_date_input(update, ctx):
    restaurant = ctx.user_data.get("rsum_restaurant","")
    text = update.message.text.strip()
    start_date = parse_date_input(text)
    if not start_date:
        await update.message.reply_text(
            "Format tanggal tidak dikenal. Coba:\n"
            "<code>1 Jun 2026</code> atau <code>01/06/2026</code> atau <code>2026-06-01</code>",
            parse_mode="HTML")
        return RSUM_DATE
    await update.message.reply_text("Mengambil data " + restaurant + " dari " + text + "...")
    ptag   = ctx.user_data.get("rsum_ptag", "")
    period = ctx.user_data.get("rsum_period", 1)
    days   = 30 if period == 3 else 10
    s = fetch_summary(restaurant, days=days, start_date=start_date, period_tag=ptag if ptag else None)
    if not s or s.get("status") == "error":
        await update.message.reply_text("Gagal mengambil data. Pastikan data sudah diinput.")
        return ConversationHandler.END
    if _needs_kasbon_prompt(s, period):
        ctx.user_data["rsum_s"] = s
        return await _prompt_kasbon(update.message.reply_text, ctx, restaurant, s, period)
    await send_ringkasan(update.message.reply_text, restaurant, s, period)
    return ConversationHandler.END

def _needs_kasbon_prompt(s, period):
    """Return True when kasbon is missing from Apps Script response and period is P1 or P2."""
    if period == 1:
        return s.get("pengeluaran_p1", 0) > 0 and s.get("kasbon_p1", 0) == 0
    if period == 2:
        return s.get("pengeluaran_p2", 0) > 0 and s.get("kasbon_p2", 0) == 0
    return False

async def _prompt_kasbon(send_fn, ctx, restaurant, s, period):
    """Ask user for kasbon amount and return RSUM_KASBON state."""
    pe = s.get("pengeluaran_p1", 0) if period == 1 else s.get("pengeluaran_p2", 0)
    label = "P1 (kasbon karyawan)" if period == 1 else "P2 (kasbon manajer)"
    await send_fn(
        "💬 <b>Konfirmasi Kasbon " + label + "</b>\n\n"
        "Pengeluaran " + ("P1" if period == 1 else "P2") + " terdeteksi: <b>Rp " + format(pe, ",") + "</b>\n"
        "Kasbon belum terdeteksi otomatis.\n\n"
        "Ketik jumlah kasbon " + ("P1" if period == 1 else "P2") + " (atau <code>0</code> jika tidak ada):\n"
        "<i>Contoh: 460769</i>",
        parse_mode="HTML"
    )
    return RSUM_KASBON

async def ringkasan_kasbon_input(update, ctx):
    """Handle manual kasbon entry when Apps Script returns 0."""
    text = update.message.text.strip().replace(".", "").replace(",", "")
    try:
        kasbon_amt = int(text)
    except ValueError:
        await update.message.reply_text(
            "Format tidak valid. Masukkan angka (contoh: <code>460769</code>).",
            parse_mode="HTML")
        return RSUM_KASBON

    restaurant = ctx.user_data.get("rsum_restaurant", "")
    period     = ctx.user_data.get("rsum_period", 1)
    s          = ctx.user_data.get("rsum_s", {})
    if not s:
        await update.message.reply_text("Data tidak ditemukan. Mulai ulang dengan /ringkasan10hari.")
        return ConversationHandler.END

    # Inject the manually entered kasbon into the summary dict
    if period == 1:
        s["kasbon_p1"] = kasbon_amt
    elif period == 2:
        s["kasbon_p2"] = kasbon_amt

    await send_ringkasan(update.message.reply_text, restaurant, s, period)
    return ConversationHandler.END

async def send_ringkasan(send_fn, restaurant, s, period):
    """Send ringkasan as one or more messages (each ≤ 4000 chars)."""
    parts = build_ringkasan_msg(restaurant, s, period)
    for part in parts:
        await send_fn(part, parse_mode="HTML")


async def cancel(update, ctx):
    await update.message.reply_text("Operasi dibatalkan.")
    return ConversationHandler.END

async def error_handler(update, context):
    import traceback
    logger.error("Exception: %s", context.error)
    logger.error(traceback.format_exc())
    if update and update.effective_message:
        try: await update.effective_message.reply_text("Error: " + str(context.error))
        except: pass

def main():
    from telegram.request import HTTPXRequest
    request = HTTPXRequest(connect_timeout=60, read_timeout=60, write_timeout=60, pool_timeout=60)

    async def post_init(application):
        global gemini_queue
        gemini_queue = asyncio.Queue()  # must be created inside running event loop
        await application.bot.set_my_commands([
            BotCommand("start",           "Mulai / info bot"),
            BotCommand("help",            "Panduan lengkap"),
            BotCommand("pengeluaran",     "Input pengeluaran 10 hari"),
            BotCommand("laporangofood",    "Laporan GoFood harian (10 hari)"),
            BotCommand("gofood",          "Upload laporan GoFood"),
            BotCommand("ringkasan10hari", "Ringkasan dan profit bersih 10 hari"),
            BotCommand("cancel",          "Batalkan operasi saat ini"),
        ])
        # Start background queue worker for Gemini calls
        asyncio.create_task(gemini_queue_worker())

    app = Application.builder().token(TELEGRAM_TOKEN).request(request).post_init(post_init).build()

    conv_main = ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, photo_received)],
        states={
            SELECT_RESTAURANT: [CallbackQueryHandler(restaurant_selected)],
            MAIN_GOFOOD: [
                CallbackQueryHandler(main_gofood_action, pattern="^(input_gofood|no_gofood|gf_gross|gf_net|reanalyze)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, main_gofood_text),
            ],
            CONFIRM_DATA:      [CallbackQueryHandler(confirm_data)],
            EDIT_FIELD:        [
                MessageHandler(filters.TEXT & ~filters.COMMAND, edit_field),
                CommandHandler("simpan", edit_field),
            ],
            VALIDATE_BELANJA: [
                MessageHandler(filters.PHOTO, validate_belanja_photo),
                CallbackQueryHandler(validate_belanja_start, pattern="^(validate_belanja|skip_belanja)$"),
                CallbackQueryHandler(validate_belanja_action, pattern="^(do_analyze_belanja|skip_belanja)$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )
    conv_peng = ConversationHandler(
        entry_points=[CommandHandler("pengeluaran", pengeluaran_start)],
        states={
            PENG_SELECT:     [CallbackQueryHandler(peng_restaurant_selected)],
            PENG_WAIT_INPUT: [
                MessageHandler(filters.PHOTO, peng_input_received),
                MessageHandler(filters.TEXT & ~filters.COMMAND, peng_input_received),
            ],
            PENG_CONFIRM: [CallbackQueryHandler(peng_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )
    conv_gofood = ConversationHandler(
        entry_points=[CommandHandler("gofood", gofood_start)],
        states={
            GOFOOD_SELECT:     [CallbackQueryHandler(gofood_restaurant_selected)],
            GOFOOD_WAIT_PHOTO: [MessageHandler(filters.PHOTO, gofood_photo_received)],
            GOFOOD_DATE:       [MessageHandler(filters.TEXT & ~filters.COMMAND, gofood_date_received)],
            GOFOOD_CONFIRM:    [CallbackQueryHandler(gofood_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )
    conv_lgofood = ConversationHandler(
        entry_points=[CommandHandler("laporangofood", lgofood_start)],
        states={
            LGOFOOD_SELECT: [CallbackQueryHandler(lgofood_restaurant_selected)],
            LGOFOOD_DATE: [
                CallbackQueryHandler(lgofood_date_option, pattern="^(lgofood_latest|lgofood_manual|cancel)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, lgofood_date_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )
    conv_ringkasan = ConversationHandler(
        entry_points=[CommandHandler("ringkasan10hari", ringkasan_start)],
        states={
            RSUM_SELECT: [CallbackQueryHandler(ringkasan_restaurant_selected)],
            RSUM_PERIOD: [CallbackQueryHandler(ringkasan_period_selected)],
            RSUM_DATE:   [
                CallbackQueryHandler(ringkasan_date_option_selected, pattern="^rsum_"),
                CallbackQueryHandler(ringkasan_period_selected),
                MessageHandler(filters.TEXT & ~filters.COMMAND, ringkasan_date_input),
            ],
            RSUM_KASBON: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ringkasan_kasbon_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    # conv_peng, conv_gofood, conv_ringkasan MUST be added before conv_main.
    # conv_main's entry point is a PHOTO handler (catches ALL photos), so it would
    # intercept photos intended for other conversations if added first.
    app.add_handler(conv_peng)
    app.add_handler(conv_gofood)
    app.add_handler(conv_lgofood)
    app.add_handler(conv_ringkasan)
    app.add_handler(conv_main)
    app.add_error_handler(error_handler)
    print("Bot Warteg aktif!")
    app.run_polling(bootstrap_retries=5)

if __name__ == "__main__":
    main()
