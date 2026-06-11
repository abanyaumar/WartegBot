import streamlit as st
import requests
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime, timedelta
import os

APPS_SCRIPT_URL = os.getenv(
    "APPS_SCRIPT_URL",
    "https://script.google.com/macros/s/AKfycbz0qH_tmDUhAeKISnErSyXxWfAzYOiU8uJnF1mGyUUM7rY0uXEqhAf4I782pljh2O_r/exec"
)
RESTAURANTS = [
    "Pisangan Lama","Kebagusan","Pejaten","Kranggan","Cibinong","Siaga Raya",
    "Ragunan","Buncit Raya","WKB Tuban","WKB Bogor","Yogya UMY","Yogya ISI"
]

def idr(x):
    return "Rp " + "{:,}".format(int(x)).replace(",",".")

def kpi_card(col, label, value, sub=""):
    col.markdown(
        f'<div style="background:#1E3A5F;border-radius:10px;padding:14px;color:white;text-align:center;">'
        f'<div style="font-size:12px;opacity:0.8">{label}</div>'
        f'<div style="font-size:18px;font-weight:bold;margin:4px 0">{value}</div>'
        f'<div style="font-size:11px;opacity:0.65">{sub}</div></div>',
        unsafe_allow_html=True
    )

@st.cache_data(ttl=300)
def fetch_restaurant(restaurant):
    for attempt in range(3):
        try:
            r = requests.get(APPS_SCRIPT_URL,
                params={"action":"getData","restaurant":restaurant}, timeout=60)
            d = r.json()
            if d.get("status") == "success" and d.get("rows"):
                df = pd.DataFrame(d["rows"])
                df["date"] = pd.to_datetime(df["date"], errors="coerce")
                df = df.dropna(subset=["date"])
                df = df[df["date"].dt.year >= 2019]
                return df.sort_values("date").reset_index(drop=True)
            break
        except Exception as e:
            if attempt == 2:
                st.warning(f"Gagal memuat {restaurant}: {e}")
    return pd.DataFrame()

@st.cache_data(ttl=300)
def fetch_all():
    # Use getAllData for one single API call instead of 12 separate calls
    try:
        r = requests.get(APPS_SCRIPT_URL, params={"action":"getAllData"}, timeout=120)
        d = r.json()
        if d.get("status") == "success" and d.get("data"):
            out = {}
            for rn, rows in d["data"].items():
                if not rows:
                    continue
                df = pd.DataFrame(rows)
                df["date"] = pd.to_datetime(df["date"], errors="coerce")
                df = df.dropna(subset=["date"])
                df = df[df["date"].dt.year >= 2019]
                if not df.empty:
                    df["restaurant"] = rn
                    out[rn] = df.sort_values("date").reset_index(drop=True)
            return out
    except Exception as e:
        st.warning(f"Gagal memuat semua data: {e}")
    # Fallback: fetch individually
    out = {}
    for rn in RESTAURANTS:
        df = fetch_restaurant(rn)
        if not df.empty:
            df["restaurant"] = rn
            out[rn] = df
    return out

# ── Page config
st.set_page_config(page_title="Warteg Dashboard", page_icon="🍛", layout="wide")
st.title("🍛 Warteg Business Dashboard")
st.caption("Data live dari Google Sheets · Auto-refresh tiap 5 menit")

# ── Sidebar
with st.sidebar:
    st.header("Filter")
    view_mode = st.radio("Tampilan", ["Semua Cabang", "Per Cabang"])
    selected = st.selectbox("Pilih Cabang", RESTAURANTS) if view_mode == "Per Cabang" else None
    st.divider()
    preset = st.selectbox("Rentang Waktu", [
        "30 Hari Terakhir","90 Hari Terakhir","6 Bulan Terakhir",
        "1 Tahun Terakhir","Semua Data","Pilih Manual"])
    today = datetime.today().date()
    if preset == "30 Hari Terakhir":    date_from, date_to = today-timedelta(30), today
    elif preset == "90 Hari Terakhir":  date_from, date_to = today-timedelta(90), today
    elif preset == "6 Bulan Terakhir":  date_from, date_to = today-timedelta(180), today
    elif preset == "1 Tahun Terakhir":  date_from, date_to = today-timedelta(365), today
    elif preset == "Semua Data":        date_from, date_to = datetime(2019,1,1).date(), today
    else:
        date_from = st.date_input("Dari", today-timedelta(30))
        date_to   = st.date_input("Sampai", today)
    if st.button("Refresh Data"):
        st.cache_data.clear()
        st.rerun()

# ── Load
with st.spinner("Memuat data dari Google Sheets..."):
    all_data = fetch_all()

def filt(df):
    if df.empty: return df
    return df[(df["date"].dt.date >= date_from) & (df["date"].dt.date <= date_to)].copy()


# ═══════════════════════════════════════
# SEMUA CABANG
# ═══════════════════════════════════════
if view_mode == "Semua Cabang":
    combined = [filt(df) for df in all_data.values() if not filt(df).empty]
    if not combined:
        st.warning("Tidak ada data untuk rentang ini.")
        st.stop()
    all_df = pd.concat(combined, ignore_index=True)

    omz  = all_df["omzet"].sum()
    bel  = all_df["total_belanja"].sum()
    knt  = all_df["keuntungan"].sum()
    gfn  = all_df["gofood_net"].sum()
    mgn  = knt/omz*100 if omz>0 else 0
    act  = len(combined)

    st.subheader(f"Ringkasan — {preset}")
    c1,c2,c3,c4,c5,c6 = st.columns(6)
    kpi_card(c1,"Total Omzet",      idr(omz),  f"{act} cabang aktif")
    kpi_card(c2,"Total Belanja",     idr(bel),  "Bahan pokok")
    kpi_card(c3,"Total Keuntungan",  idr(knt),  f"Margin {mgn:.1f}%")
    kpi_card(c4,"GoFood Net",        idr(gfn),  "Online income")
    kpi_card(c5,"Avg Omzet/Hari",    idr(omz/max(all_df['date'].nunique(),1)), "Semua cabang")
    kpi_card(c6,"Cabang Aktif",      str(act),  f"dari {len(RESTAURANTS)}")
    st.divider()

    col1, col2 = st.columns([3,2])
    with col1:
        st.subheader("Tren Omzet Bulanan per Cabang")
        m = all_df.copy()
        m["bulan"] = m["date"].dt.to_period("M").astype(str)
        mg = m.groupby(["bulan","restaurant"])["omzet"].sum().reset_index()
        fig = px.line(mg, x="bulan", y="omzet", color="restaurant", template="plotly_white",
                      labels={"omzet":"Omzet","bulan":"Bulan","restaurant":"Cabang"})
        fig.update_layout(legend=dict(orientation="h",yanchor="bottom",y=-0.5))
        fig.update_yaxes(tickformat=",.0f")
        st.plotly_chart(fig, use_container_width=True)
    with col2:
        st.subheader("Total Omzet per Cabang")
        pr = all_df.groupby("restaurant").agg(omzet=("omzet","sum"),keuntungan=("keuntungan","sum")).reset_index()
        pr = pr.sort_values("omzet", ascending=True)
        fig2 = px.bar(pr, x="omzet", y="restaurant", orientation="h",
                      color="keuntungan", color_continuous_scale="Blues", template="plotly_white",
                      labels={"omzet":"Total Omzet","restaurant":"Cabang","keuntungan":"Keuntungan"})
        fig2.update_xaxes(tickformat=",.0f")
        st.plotly_chart(fig2, use_container_width=True)

    col3, col4 = st.columns(2)
    with col3:
        st.subheader("Margin Keuntungan per Cabang")
        mdf = all_df.groupby("restaurant").agg(omzet=("omzet","sum"),keuntungan=("keuntungan","sum")).reset_index()
        mdf["pct"] = (mdf["keuntungan"]/mdf["omzet"].replace(0,1)*100).round(1)
        mdf = mdf.sort_values("pct", ascending=True)
        fig3 = px.bar(mdf, x="pct", y="restaurant", orientation="h",
                      color="pct", color_continuous_scale="RdYlGn", range_color=[0,50],
                      template="plotly_white", labels={"pct":"Margin (%)","restaurant":"Cabang"})
        fig3.update_xaxes(ticksuffix="%")
        st.plotly_chart(fig3, use_container_width=True)
    with col4:
        st.subheader("Kontribusi GoFood per Cabang")
        gfd = all_df.groupby("restaurant")["gofood_net"].sum().reset_index()
        gfd = gfd[gfd["gofood_net"]>0]
        if not gfd.empty:
            fig4 = px.pie(gfd, values="gofood_net", names="restaurant", hole=0.4, template="plotly_white")
            fig4.update_traces(textposition="inside", textinfo="percent+label")
            st.plotly_chart(fig4, use_container_width=True)
        else:
            st.info("Belum ada data GoFood pada rentang ini.")

    st.subheader("Tabel Ringkasan per Cabang")
    rows = []
    for rn in RESTAURANTS:
        if rn not in all_data: continue
        fd = filt(all_data[rn])
        if fd.empty: continue
        o,b,k,g = fd["omzet"].sum(),fd["total_belanja"].sum(),fd["keuntungan"].sum(),fd["gofood_net"].sum()
        rows.append({"Cabang":rn,"Hari":len(fd),"Omzet":idr(o),"Belanja":idr(b),
                     "Keuntungan":idr(k),"GoFood Net":idr(g),
                     "Margin":f"{k/max(o,1)*100:.1f}%","Avg/Hari":idr(o/max(len(fd),1))})
    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# ═══════════════════════════════════════
# PER CABANG
# ═══════════════════════════════════════
else:
    if selected not in all_data or all_data[selected].empty:
        st.warning(f"Tidak ada data untuk {selected}. Pastikan sheet sudah diimport ke Google Sheets.")
        st.stop()
    df = filt(all_data[selected])
    if df.empty:
        st.warning(f"Tidak ada data {selected} untuk rentang {preset}.")
        st.stop()

    st.subheader(f"{selected} — {preset}")
    omzet  = df["omzet"].sum()
    belanj = df["total_belanja"].sum()
    keunt  = df["keuntungan"].sum()
    gfnet  = df["gofood_net"].sum()
    days   = len(df)
    mgn    = keunt/omzet*100 if omzet>0 else 0

    c1,c2,c3,c4,c5 = st.columns(5)
    kpi_card(c1,"Total Omzet",    idr(omzet),  f"{days} hari")
    kpi_card(c2,"Total Belanja",  idr(belanj), "")
    kpi_card(c3,"Keuntungan",     idr(keunt),  f"Margin {mgn:.1f}%")
    kpi_card(c4,"GoFood Net",     idr(gfnet),  "")
    kpi_card(c5,"Avg Omzet/Hari", idr(omzet/days), "")
    st.divider()

    col1, col2 = st.columns([3,2])
    with col1:
        st.subheader("Tren Harian")
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=df["date"], y=df["omzet"], name="Omzet",
                                 line=dict(color="#1E3A5F",width=2)))
        fig.add_trace(go.Scatter(x=df["date"], y=df["total_belanja"], name="Belanja",
                                 line=dict(color="#E74C3C",width=2)))
        fig.add_trace(go.Scatter(x=df["date"], y=df["keuntungan"], name="Keuntungan",
                                 line=dict(color="#27AE60",width=2),
                                 fill="tozeroy", fillcolor="rgba(39,174,96,0.1)"))
        fig.update_layout(template="plotly_white", legend=dict(orientation="h"))
        fig.update_yaxes(tickformat=",.0f")
        st.plotly_chart(fig, use_container_width=True)
    with col2:
        st.subheader("Distribusi Omzet Harian")
        fig2 = px.box(df, y="omzet", template="plotly_white",
                      labels={"omzet":"Omzet Harian"},
                      color_discrete_sequence=["#1E3A5F"])
        fig2.update_yaxes(tickformat=",.0f")
        st.plotly_chart(fig2, use_container_width=True)

    st.subheader("Rekapitulasi Bulanan")
    df2 = df.copy()
    df2["bulan"] = df2["date"].dt.to_period("M").astype(str)
    mon = df2.groupby("bulan").agg(
        omzet=("omzet","sum"), belanja=("total_belanja","sum"),
        keuntungan=("keuntungan","sum"), gofood=("gofood_net","sum"),
        hari=("omzet","count")
    ).reset_index()
    mon["margin"] = (mon["keuntungan"]/mon["omzet"].replace(0,1)*100).round(1)
    fig3 = px.bar(mon, x="bulan", y=["omzet","belanja","keuntungan"], barmode="group",
                  template="plotly_white",
                  labels={"value":"Rupiah","bulan":"Bulan","variable":"Kategori"},
                  color_discrete_map={"omzet":"#1E3A5F","belanja":"#E74C3C","keuntungan":"#27AE60"})
    fig3.update_yaxes(tickformat=",.0f")
    st.plotly_chart(fig3, use_container_width=True)

    with st.expander("Lihat Data Harian Lengkap"):
        show = df[["date","omzet","total_belanja","keuntungan","gofood_net","notes"]].copy()
        show.columns = ["Tanggal","Omzet","Total Belanja","Keuntungan","GoFood Net","Notes"]
        show["Tanggal"] = show["Tanggal"].dt.strftime("%d-%b-%Y")
        for c in ["Omzet","Total Belanja","Keuntungan","GoFood Net"]:
            show[c] = show[c].apply(idr)
        st.dataframe(show.sort_values("Tanggal",ascending=False),
                     use_container_width=True, hide_index=True)
