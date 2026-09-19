import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from supabase import create_client, Client

st.set_page_config(page_title="Wearable Device - Live Data", layout="wide")

# -------- SUPABASE CLIENT (same project/secrets as the main app) --------
try:
    SUPABASE_URL = st.secrets["https://edrgnogqwybqwcmjkkwr.supabase.co"]
    SUPABASE_KEY = st.secrets["sb_publishable_xO2WfZ6jDoBfImqhIbJXoQ_hKziV_Zv"]
except Exception:
    st.error("Supabase credentials not found in secrets.toml. This page needs the same "
             "SUPABASE_URL / SUPABASE_KEY as the main app.")
    st.stop()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

st.title("Wearable Device - Live Temperature Data")

# -------- DEVICE / PATIENT SELECTION --------
col1, col2 = st.columns(2)
with col1:
    device_id = st.text_input("Device ID", value="wearable-01")
with col2:
    patient_label = st.text_input("Patient label (for your reference)", value="Patient 1")

st.caption(f"Showing data for device **{device_id}**, labeled as **{patient_label}**.")

refresh = st.button("Refresh now")

# -------- FETCH DATA --------
@st.cache_data(ttl=30)
def fetch_wearable_logs(device_id):
    res = (
        supabase.table("wearable_logs")
        .select("*")
        .eq("device_id", device_id)
        .order("recorded_at", desc=False)
        .execute()
    )
    return res.data or []

if refresh:
    fetch_wearable_logs.clear()

rows = fetch_wearable_logs(device_id)

if not rows:
    st.info(f"No data found yet for device '{device_id}'. Make sure the device has logged "
            "at least one reading while connected to WiFi.")
    st.stop()

df = pd.DataFrame(rows)

# Prefer the device's own timestamp (device_datetime) when it looks synced;
# fall back to Supabase's server-side recorded_at for UNSYNCED rows so they
# still plot in roughly the right place rather than being dropped.
def resolve_time(row):
    dt = row.get("device_datetime", "")
    if dt and not str(dt).startswith("UNSYNCED"):
        return pd.to_datetime(dt)
    return pd.to_datetime(row.get("recorded_at"))

df["PlotTime"] = df.apply(resolve_time, axis=1)
df = df.sort_values("PlotTime")

# -------- SUMMARY --------
col1, col2, col3, col4 = st.columns(4)
col1.metric("Total Readings", len(df))
col2.metric("Latest Temp", f"{df['temp'].iloc[-1]:.1f} °C")
col3.metric("Min / Max Temp", f"{df['temp'].min():.1f} / {df['temp'].max():.1f} °C")
col4.metric("Latest Reading At", df["PlotTime"].iloc[-1].strftime("%Y-%m-%d %H:%M:%S"))

# -------- LIVE-STYLE TEMP VS TIME PLOT --------
st.subheader(f"Temperature vs Time - {patient_label}")

fig = go.Figure()
fig.add_scatter(
    x=df["PlotTime"], y=df["temp"],
    mode="lines+markers", name="Temperature",
    line=dict(color="#ff4757", width=2),
    marker=dict(size=4),
)
fig.update_layout(
    xaxis_title="Time",
    yaxis_title="Temperature (°C)",
    height=480,
    hovermode="x unified",
)
st.plotly_chart(fig, use_container_width=True)

# -------- RAW DATA TABLE --------
with st.expander("View raw data"):
    st.dataframe(
        df[["device_datetime", "recorded_at", "temp"]].rename(
            columns={"device_datetime": "Device Time", "recorded_at": "Server Time", "temp": "Temp (°C)"}
        ),
        use_container_width=True,
    )

st.caption("Data refreshes automatically every 30 seconds, or click 'Refresh now' for an immediate update.")
