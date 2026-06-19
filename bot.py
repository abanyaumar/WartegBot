# -*- coding: utf-8 -*-
import os, json, logging, re, html, datetime
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

RESTAURANTS = [
    "Pisangan Lama","Kebagusan","Pejaten","Kranggan",
    "Cibinong","Siaga Raya","Ragunan","Buncit Raya",
    "WKB Tuban","WKB Bogor","Yogya UMY","Yogya ISI",
]

# States
SELECT_RESTAURANT, CONFIRM_DATA, EDIT_FIELD, VALIDATE_BELANJA, MAIN_GOFOOD = range(5)
PENG_SELECT, PENG_WAIT_INPUT, PENG_CONFIRM = range(4, 7)
GOFOOD_SELECT, GOFOOD_WAIT_PHOTO, GOFOOD_CONFIRM, GOFOOD_DATE = range(7, 11)
RSUM_SELECT, RSUM_DATE = 11, 12

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
    wkb_tuban_note = ""  # no special prompt; AI reads omzet including carry-over naturally
    prompt = (
        "Kamu asisten keuangan DAN auditor untuk cabang " + restaurant + ".\n"
        "Baca laporan harian TULISAN TANGAN ini.\n\n"
        "Struktur laporan:\n"
        "1. Pemasukan tunai/cash per shift (pagi/siang/malam/shift 1/2/3) -> jumlahkan ke omzet\n"
        "   PENTING: JANGAN masukkan pendapatan GoFood ke dalam omzet\n"
        "2. Pendapatan GoFood/GrabFood/online order -> gofood_order (nominal bruto/kotor)\n"
        "   dan gofood_net (nominal setelah potongan). Jika hanya ada 1 angka GoFood, isi keduanya sama.\n"
        "   Jika tidak ada GoFood di laporan, isi 0.\n"
        "3. Belanja Warung (LPG/es batu/operasional) -> belanja_warung\n"
        "4. Belanja Pasar (sembako/sayur/ayam/ikan/dll) -> belanja_pasar\n"
        + wkb_tuban_note +
        "\nATURAN: omzet = pemasukan TUNAI saja. GoFood HARUS dipisah ke gofood_order/gofood_net.\n\n"
        "OUTPUT dua bagian TANPA markdown:\n"
        "JSON_DATA:\n"
        '{"tanggal":"YYYY-MM-DD","omzet":0,"belanja_warung":0,"belanja_pasar":0,'
        '"belanja_warung_items":{"lpg":0,"es_batu":0},'
        '"belanja_pasar_items":{"sembako":0,"sayur":0,"ayam":0,"ikan":0,"lain":0},'
        '"gofood_order":0,"gofood_net":0,"catatan":""}\n'
        "AUDIT:\n"
        "Tulis maks 3 pertanyaan audit spesifik Bahasa Indonesia menggunakan angka konkret. "
        "Jika semua wajar tulis: TIDAK ADA CATATAN AUDIT"
    )
    resp = client.models.generate_content(model="models/gemini-2.5-flash-lite", contents=[prompt, img_data])
    text = resp.text.strip()
    data = {}
    m = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", text, re.DOTALL)
    if m:
        try:
            d = json.loads(m.group())
            for f in ["omzet","belanja_warung","belanja_pasar","gofood_order","gofood_net"]:
                d[f] = int(str(d.get(f,0)).replace(",","").replace(".","") or 0)
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
        resp = client.models.generate_content(model="models/gemini-2.5-flash-lite", contents=[prompt] + parts)
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
            resp = client.models.generate_content(model="models/gemini-2.5-flash-lite", contents=[prompt, img])
        else:
            resp = client.models.generate_content(model="models/gemini-2.5-flash-lite", contents=[prompt + "\n\nData:\n" + content_data])
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
        "Screenshot laporan GoFood cabang " + restaurant + ".\n"
        'OUTPUT JSON: {"periode":"","total_bruto":0,"total_netto":0,"jumlah_transaksi":0,"catatan":""}'
    )
    try:
        resp = client.models.generate_content(model="models/gemini-2.5-flash-lite", contents=[prompt, img])
        m = re.search(r"\{.*?\}", resp.text.strip(), re.DOTALL)
        if m:
            d = json.loads(m.group())
            for f in ["total_bruto","total_netto","jumlah_transaksi"]:
                d[f] = int(str(d.get(f,0)).replace(",","").replace(".","") or 0)
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

def fetch_summary(restaurant, days=10, start_date=None):
    try:
        params = {"action":"summary","restaurant":restaurant,"days":str(days)}
        if start_date:
            params["startDate"] = start_date
        r = requests.get(APPS_SCRIPT_URL, params=params, timeout=20)
        if r.status_code == 200: return r.json()
    except Exception as e:
        logger.error("Summary error: " + str(e))
    return None

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
        # AI already includes carry-over in omzet; only belanja_pasar is real expense
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
    await q.edit_message_text("Membaca dan menganalisis laporan " + restaurant + "...")
    try:
        data, audit = extract_and_audit(ctx.user_data["photo_bytes"], restaurant)
    except Exception as e:
        await q.edit_message_text("Gagal membaca foto: " + str(e))
        return ConversationHandler.END
    if not data:
        await q.edit_message_text("Data tidak terbaca. Coba foto lebih jelas.")
        return ConversationHandler.END
    data["restaurant"] = restaurant
    ctx.user_data["extracted"] = data
    ctx.user_data["audit_text"] = audit
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
    ]
    await q.edit_message_text(
        summary + gofood_info + "\n\n<b>Apakah ada pendapatan GoFood hari ini?</b>",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
    return MAIN_GOFOOD

async def main_gofood_action(update, ctx):
    """Handle GoFood question after main report is read."""
    q = update.callback_query
    await q.answer()
    if q.data == "no_gofood":
        ctx.user_data["extracted"]["gofood_order"] = 0
        ctx.user_data["extracted"]["gofood_net"] = 0
        await _show_confirm(q, ctx)
        return CONFIRM_DATA
    if q.data == "input_gofood":
        gf = ctx.user_data["extracted"].get("gofood_order", 0)
        hint = " (AI baca: Rp " + format(gf,",") + ")" if gf > 0 else ""
        await q.edit_message_text(
            "<b>Input GoFood</b>" + hint + "\n\n"
            "Ketik nominal GoFood NET (setelah potongan platform):\n"
            "<code>150000</code>\n\n"
            "Atau ketik <code>skip</code> jika tidak ada.",
            parse_mode="HTML")
        return MAIN_GOFOOD
    return MAIN_GOFOOD

async def main_gofood_text(update, ctx):
    """Handle manual GoFood amount input."""
    text = update.message.text.strip()
    if text.lower() == "skip":
        ctx.user_data["extracted"]["gofood_order"] = 0
        ctx.user_data["extracted"]["gofood_net"] = 0
    else:
        raw = re.sub(r"[^\d]", "", re.sub(r"\.(?=\d{3})", "", text))
        if not raw:
            await update.message.reply_text("Angka tidak valid. Ketik nominal atau <code>skip</code>.", parse_mode="HTML")
            return MAIN_GOFOOD
        amount = int(raw)
        ctx.user_data["extracted"]["gofood_order"] = amount
        ctx.user_data["extracted"]["gofood_net"] = amount
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
    kb = [[InlineKeyboardButton("Ya, Simpan!", callback_data="save_peng")],[InlineKeyboardButton("Batalkan", callback_data="cancel_peng")]]
    await update.message.reply_text("\n".join(lines) + "\n\n<b>Data sudah benar?</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
    return PENG_CONFIRM

async def peng_confirm(update, ctx):
    q = update.callback_query; await q.answer()
    if q.data == "cancel_peng":
        await q.edit_message_text("Dibatalkan."); return ConversationHandler.END
    ok = save_to_sheets(ctx.user_data.get("peng_restaurant",""), ctx.user_data.get("peng_data",{}), "pengeluaran")
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
    if not data or data.get("total_netto",0) == 0:
        await update.message.reply_text("Gagal membaca. Coba screenshot lebih jelas."); return GOFOOD_WAIT_PHOTO
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

# ======= RINGKASAN =======
def build_ringkasan_msg(restaurant, s):
    om = s.get("total_omzet",0); gf = s.get("total_gofood_netto",0)
    bl = s.get("total_belanja",0); pe = s.get("total_pengeluaran",0)
    ti = om + gf; to = bl + pe; pr = ti - to
    return "\n".join([
        "<b>Ringkasan 10 Hari - " + restaurant + "</b>",
        "Periode: <b>" + s.get("periode","-") + "</b>",
        "====================","<b>PEMASUKAN:</b>",
        "Omzet Harian  : Rp " + format(om,","),
        "GoFood Netto  : Rp " + format(gf,","),
        "Total Masuk   : <b>Rp " + format(ti,",") + "</b>",
        "--------------------","<b>PENGELUARAN:</b>",
        "Total Belanja : Rp " + format(bl,","),
        "Pengeluaran   : Rp " + format(pe,","),
        "Total Keluar  : <b>Rp " + format(to,",") + "</b>",
        "====================",
        "<b>PROFIT BERSIH: Rp " + format(pr,",") + "</b>",
    ])

async def ringkasan_start(update, ctx):
    await update.message.reply_text("<b>Ringkasan 10 Hari</b>\n\nPilih cabang:", parse_mode="HTML", reply_markup=restaurant_keyboard())
    return RSUM_SELECT

async def ringkasan_restaurant_selected(update, ctx):
    q = update.callback_query; await q.answer()
    if q.data == "cancel":
        await q.edit_message_text("Dibatalkan."); return ConversationHandler.END
    restaurant = q.data.split("|",1)[1]
    ctx.user_data["rsum_restaurant"] = restaurant
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("10 Hari Terakhir", callback_data="rsum_latest")],
        [InlineKeyboardButton("Pilih Tanggal Mulai", callback_data="rsum_manual")],
        [InlineKeyboardButton("Batalkan", callback_data="cancel")],
    ])
    await q.edit_message_text("<b>" + restaurant + "</b>\n\nPilih periode:", parse_mode="HTML", reply_markup=kb)
    return RSUM_DATE

async def ringkasan_period_selected(update, ctx):
    q = update.callback_query; await q.answer()
    restaurant = ctx.user_data.get("rsum_restaurant","")
    if q.data == "cancel":
        await q.edit_message_text("Dibatalkan."); return ConversationHandler.END
    if q.data == "rsum_latest":
        await q.edit_message_text("Mengambil data " + restaurant + "...")
        s = fetch_summary(restaurant, days=10)
        if not s or s.get("status") == "error":
            await q.edit_message_text("Gagal mengambil data."); return ConversationHandler.END
        await q.edit_message_text(build_ringkasan_msg(restaurant, s), parse_mode="HTML")
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
    s = fetch_summary(restaurant, days=10, start_date=start_date)
    if not s or s.get("status") == "error":
        await update.message.reply_text("Gagal mengambil data. Pastikan data sudah diinput.")
        return ConversationHandler.END
    await update.message.reply_text(build_ringkasan_msg(restaurant, s), parse_mode="HTML")
    return ConversationHandler.END

async def cancel(update, ctx):
    await update.message.reply_text("Dibatalkan.")
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
        await application.bot.set_my_commands([
            BotCommand("start",           "Mulai / info bot"),
            BotCommand("help",            "Panduan lengkap"),
            BotCommand("pengeluaran",     "Input pengeluaran 10 hari"),
            BotCommand("gofood",          "Upload laporan GoFood"),
            BotCommand("ringkasan10hari", "Ringkasan dan profit bersih 10 hari"),
            BotCommand("cancel",          "Batalkan operasi saat ini"),
        ])

    app = Application.builder().token(TELEGRAM_TOKEN).request(request).post_init(post_init).build()

    conv_main = ConversationHandler(
        entry_points=[MessageHandler(filters.PHOTO, photo_received)],
        states={
            SELECT_RESTAURANT: [CallbackQueryHandler(restaurant_selected)],
            MAIN_GOFOOD: [
                CallbackQueryHandler(main_gofood_action, pattern="^(input_gofood|no_gofood)$"),
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
    conv_ringkasan = ConversationHandler(
        entry_points=[CommandHandler("ringkasan10hari", ringkasan_start)],
        states={
            RSUM_SELECT: [CallbackQueryHandler(ringkasan_restaurant_selected)],
            RSUM_DATE:   [
                CallbackQueryHandler(ringkasan_period_selected),
                MessageHandler(filters.TEXT & ~filters.COMMAND, ringkasan_date_input),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(conv_main)
    app.add_handler(conv_peng)
    app.add_handler(conv_gofood)
    app.add_handler(conv_ringkasan)
    app.add_error_handler(error_handler)
    print("Bot Warteg aktif!")
    app.run_polling(bootstrap_retries=5)

if __name__ == "__main__":
    main()
