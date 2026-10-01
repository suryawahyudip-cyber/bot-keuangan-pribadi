# bot.py (v3)
# Sekarang pakai environment variables (bukan config.py) supaya aman untuk di-deploy.
# Format pesan: <kategori> <jumlah> <akun> <deskripsi opsional>
# Contoh: "makan 15000 bca beli nasi goreng"

import os
import json
from datetime import date

import gspread
from google.oauth2.service_account import Credentials
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from dotenv import load_dotenv

load_dotenv()  # baca file .env kalau ada (untuk development di laptop)

TOKEN = os.environ["TELEGRAM_TOKEN"]
SHEET_ID = os.environ["SHEET_ID"]
GOOGLE_CREDENTIALS_JSON = os.environ["GOOGLE_CREDENTIALS_JSON"]


# --- Setup koneksi ke Google Sheets (dijalankan sekali saat bot mulai) ---

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
creds_info = json.loads(GOOGLE_CREDENTIALS_JSON)
creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
gc = gspread.authorize(creds)
sheet = gc.open_by_key(SHEET_ID)

ws_transaksi = sheet.worksheet("Transaksi")
ws_kategori = sheet.worksheet("Kategori")
ws_akun = sheet.worksheet("Akun")
ws_ringkasan = sheet.worksheet("Ringkasan Bulanan")


def ambil_daftar(worksheet, kolom):
    # Ambil semua nilai di satu kolom (mulai baris 5), buang sel kosong
    nilai_mentah = worksheet.col_values(kolom)[4:]  # index 4 = baris 5 (mulai dari 0)
    return [v for v in nilai_mentah if v.strip() != ""]


def cari_kecocokan(kata, daftar):
    # Cocokkan kata (huruf besar/kecil diabaikan) dengan salah satu isi daftar
    kata_lower = kata.lower()
    for item in daftar:
        if item.lower() == kata_lower or item.lower().startswith(kata_lower):
            return item  # kembalikan versi asli dari sheet, misal "Makan" bukan "makan"
    return None


def parse_pesan(teks):
    """Ubah teks pesan jadi data transaksi. Return (data_dict, None) kalau sukses,
    atau (None, pesan_error) kalau gagal."""
    bagian = teks.strip().split(maxsplit=3)
    if len(bagian) < 3:
        return None, (
            "Format pesan kurang lengkap.\n"
            "Contoh: makan 15000 bca beli nasi goreng"
        )

    kategori_input, jumlah_input, akun_input = bagian[0], bagian[1], bagian[2]
    deskripsi = bagian[3] if len(bagian) > 3 else ""

    jumlah_bersih = jumlah_input.replace(".", "").replace(",", "")
    if not jumlah_bersih.isdigit():
        return None, f"'{jumlah_input}' bukan angka yang valid untuk jumlah."
    jumlah = int(jumlah_bersih)

    daftar_pengeluaran = ambil_daftar(ws_kategori, 1)  # kolom A
    daftar_pemasukan = ambil_daftar(ws_kategori, 3)    # kolom C

    kategori_cocok = cari_kecocokan(kategori_input, daftar_pengeluaran)
    if kategori_cocok:
        tipe = "Out"
    else:
        kategori_cocok = cari_kecocokan(kategori_input, daftar_pemasukan)
        tipe = "In" if kategori_cocok else None

    if not kategori_cocok:
        return None, (
            f"Kategori '{kategori_input}' tidak dikenali.\n"
            f"Kategori pengeluaran: {', '.join(daftar_pengeluaran)}\n"
            f"Sumber pemasukan: {', '.join(daftar_pemasukan)}"
        )

    daftar_akun = ambil_daftar(ws_akun, 1)  # kolom A
    akun_cocok = cari_kecocokan(akun_input, daftar_akun)
    if not akun_cocok:
        return None, f"Akun '{akun_input}' tidak dikenali. Akun tersedia: {', '.join(daftar_akun)}"

    return {
        "tanggal": date.today().strftime("%Y-%m-%d"),
        "tipe": tipe,
        "kategori": kategori_cocok,
        "akun": akun_cocok,
        "jumlah": jumlah,
        "deskripsi": deskripsi,
    }, None


def simpan_ke_sheet(data):
    kolom_a = ws_transaksi.col_values(1)
    baris_baru = len(kolom_a) + 1

    ws_transaksi.update_cell(baris_baru, 1, data["tanggal"])
    ws_transaksi.update_cell(baris_baru, 3, data["tipe"])
    ws_transaksi.update_cell(baris_baru, 4, data["kategori"])
    ws_transaksi.update_cell(baris_baru, 5, data["akun"])
    ws_transaksi.update_cell(baris_baru, 6, data["jumlah"])
    ws_transaksi.update_cell(baris_baru, 7, data["deskripsi"])


def format_rp(angka):
    try:
        angka = float(angka)
    except (TypeError, ValueError):
        angka = 0
    tanda = "-" if angka < 0 else ""
    return f"{tanda}Rp{abs(angka):,.0f}"


# --- Handler bot Telegram ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Halo! Kirim transaksi dengan format:\n"
        "<kategori> <jumlah> <akun> <deskripsi opsional>\n\n"
        "Contoh: makan 15000 bca beli nasi goreng\n\n"
        "Command lain:\n"
        "/saldo - cek saldo semua akun\n"
        "/ringkasan - rekap bulan ini"
    )


async def cmd_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        nama_list = ws_akun.col_values(1)[4:]  # mulai baris 5
        saldo_list = ws_akun.col_values(3, value_render_option="UNFORMATTED_VALUE")[4:]
    except Exception as e:
        await update.message.reply_text(f"❌ Gagal ambil data saldo: {e}")
        return

    baris = []
    total = 0
    for nama, saldo in zip(nama_list, saldo_list):
        if not nama.strip():
            continue
        nilai = saldo if isinstance(saldo, (int, float)) else 0
        total += nilai
        baris.append(f"• {nama}: {format_rp(nilai)}")

    if not baris:
        await update.message.reply_text("Belum ada akun yang terdaftar di sheet Akun.")
        return

    pesan = "💰 Saldo Akun:\n" + "\n".join(baris) + f"\n\nTotal: {format_rp(total)}"
    await update.message.reply_text(pesan)


async def cmd_ringkasan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    baris_bulan_ini = 5  # baris pertama di Ringkasan Bulanan selalu bulan berjalan (formula pakai TODAY())
    try:
        bulan_label = ws_ringkasan.cell(baris_bulan_ini, 1).value
        nilai = ws_ringkasan.row_values(baris_bulan_ini, value_render_option="UNFORMATTED_VALUE")
        headers = ws_ringkasan.row_values(4)
    except Exception as e:
        await update.message.reply_text(f"❌ Gagal ambil ringkasan: {e}")
        return

    pemasukan = nilai[1] if len(nilai) > 1 else 0
    pengeluaran = nilai[2] if len(nilai) > 2 else 0
    saldo_bersih = nilai[3] if len(nilai) > 3 else 0

    baris_kategori = []
    for i in range(4, len(headers)):
        nama_kategori = headers[i]
        nilai_kategori = nilai[i] if i < len(nilai) else 0
        if isinstance(nilai_kategori, (int, float)) and nilai_kategori:
            baris_kategori.append(f"• {nama_kategori}: {format_rp(nilai_kategori)}")

    pesan = (
        f"📊 Ringkasan {bulan_label}\n\n"
        f"Pemasukan: {format_rp(pemasukan)}\n"
        f"Pengeluaran: {format_rp(pengeluaran)}\n"
        f"Saldo Bersih: {format_rp(saldo_bersih)}"
    )
    if baris_kategori:
        pesan += "\n\nPer Kategori:\n" + "\n".join(baris_kategori)

    await update.message.reply_text(pesan)


async def catat_transaksi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    teks = update.message.text
    data, error = parse_pesan(teks)

    if error:
        await update.message.reply_text(f"⚠️ {error}")
        return

    try:
        simpan_ke_sheet(data)
    except Exception as e:
        await update.message.reply_text(f"❌ Gagal simpan ke sheet: {e}")
        return

    tanda = "-" if data["tipe"] == "Out" else "+"
    await update.message.reply_text(
        f"✅ Tercatat: {data['kategori']} {tanda}Rp{data['jumlah']:,} ({data['akun']})"
    )


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("saldo", cmd_saldo))
    app.add_handler(CommandHandler("ringkasan", cmd_ringkasan))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, catat_transaksi))

    print("Bot berjalan... tekan Ctrl+C untuk berhenti.")
    app.run_polling()


if __name__ == "__main__":
    main()
