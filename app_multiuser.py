import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import datetime, timezone
from supabase import create_client, Client

from compliance_logic import parse_device_csv, analyze_log, daily_to_records, records_to_daily

st.set_page_config(page_title="Brace Compliance Monitor", layout="wide")

# ---------------------------------------------------------------
# SUPABASE CLIENT
# ---------------------------------------------------------------
# Requires .streamlit/secrets.toml with:
#   SUPABASE_URL = "https://xxxx.supabase.co"
#   SUPABASE_KEY = "your-anon-key"
#   ADMIN_EMAILS = "you@example.com,other-admin@example.com"
try:
    SUPABASE_URL = st.secrets["SUPABASE_URL"]
    SUPABASE_KEY = st.secrets["SUPABASE_KEY"]
    ADMIN_EMAILS = [e.strip() for e in st.secrets.get("ADMIN_EMAILS", "").split(",") if e.strip()]
except Exception:
    st.error(
        "Supabase credentials not found. Create a `.streamlit/secrets.toml` file with "
        "SUPABASE_URL, SUPABASE_KEY, and ADMIN_EMAILS. See the setup guide for details."
    )
    st.stop()

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ---------------------------------------------------------------
# AUTH STATE
# ---------------------------------------------------------------
if "user" not in st.session_state:
    st.session_state.user = None
    st.session_state.access_token = None
    st.session_state.refresh_token = None

# Streamlit re-runs the whole script on every interaction, which creates a
# brand-new `supabase` client each time. That fresh client has no memory of
# a previous login, so table requests would go out unauthenticated (failing
# RLS policies that check auth.uid()) unless we explicitly re-attach the
# saved session here on every rerun.
if st.session_state.user is not None and st.session_state.access_token:
    try:
        supabase.auth.set_session(st.session_state.access_token, st.session_state.refresh_token)
    except Exception as e:
        st.warning(f"Session expired, please log in again. ({e})")
        st.session_state.user = None
        st.session_state.access_token = None
        st.session_state.refresh_token = None


def login_form():
    st.title("Brace Compliance Monitor - Doctor Login")
    tab_login, tab_signup = st.tabs(["Log in", "Sign up"])

    with tab_login:
        email = st.text_input("Email", key="login_email")
        password = st.text_input("Password", type="password", key="login_pw")
        if st.button("Log in"):
            try:
                res = supabase.auth.sign_in_with_password({"email": email, "password": password})
                st.session_state.user = res.user
                st.session_state.access_token = res.session.access_token
                st.session_state.refresh_token = res.session.refresh_token
                st.rerun()
            except Exception as e:
                st.error(f"Login failed: {e}")

    with tab_signup:
        new_email = st.text_input("Email", key="signup_email")
        new_password = st.text_input("Password", type="password", key="signup_pw")
        name = st.text_input("Your name", key="signup_name")
        if st.button("Create account"):
            try:
                res = supabase.auth.sign_up({
                    "email": new_email,
                    "password": new_password,
                    "options": {"data": {"full_name": name}},
                })
                st.success("Account created. Check your email to confirm, then log in.")
            except Exception as e:
                st.error(f"Sign up failed: {e}")


if st.session_state.user is None:
    login_form()
    st.stop()

current_user = st.session_state.user
current_email = current_user.email
is_admin = current_email in ADMIN_EMAILS

# ---------------------------------------------------------------
# SIDEBAR NAV
# ---------------------------------------------------------------
st.sidebar.title("Brace Compliance Monitor")
st.sidebar.caption(f"Logged in as **{current_email}**" + (" (admin)" if is_admin else ""))
if st.sidebar.button("Log out"):
    supabase.auth.sign_out()
    st.session_state.user = None
    st.session_state.access_token = None
    st.session_state.refresh_token = None
    st.rerun()

pages = ["My Patients", "Upload & Analyze", "Patient History", "Wearable Temperature"]
if is_admin:
    pages.append("Team Overview")
page = st.sidebar.radio("Go to", pages)


# ---------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------
def get_my_patients():
    res = supabase.table("patients").select("*").eq("doctor_id", current_user.id).execute()
    return res.data or []


def get_all_patients_with_doctor():
    res = supabase.table("patients").select("*, doctors:doctor_id(email)").execute()
    return res.data or []


def get_uploads_for_patient(patient_id):
    res = (
        supabase.table("uploads")
        .select("*")
        .eq("patient_id", patient_id)
        .order("uploaded_at", desc=False)
        .execute()
    )
    return res.data or []


def get_wearable_logs(device_id):
    """Fetch live wearable temperature records for a device from Supabase."""
    res = (
        supabase.table("wearable_logs")
        .select("id,device_id,recorded_at,device_datetime,temp")
        .eq("device_id", device_id)
        .order("recorded_at", desc=False)
        .execute()
    )
    return res.data or []


def merge_upload_history(uploads):
    """Combine daily_summary JSON from multiple uploads into one DataFrame,
    keeping the most recently uploaded record for any overlapping date."""
    frames = []
    for u in uploads:
        recs = u.get("daily_summary") or []
        df = records_to_daily(pd.DataFrame(recs))
        if not df.empty:
            df["uploaded_at"] = u["uploaded_at"]
            frames.append(df)
    if not frames:
        return pd.DataFrame(columns=["Date", "WornHours", "CompliancePct", "MetTarget"])
    combined = pd.concat(frames, ignore_index=True)
    combined = combined.sort_values("uploaded_at").drop_duplicates(subset=["Date"], keep="last")
    return combined.sort_values("Date").reset_index(drop=True)


# ---------------------------------------------------------------
# PAGE: MY PATIENTS
# ---------------------------------------------------------------
if page == "My Patients":
    st.title("My Patients")

    with st.expander("Add a new patient"):
        code = st.text_input("Patient ID / Code (do not use full names)")
        target = st.number_input("Prescribed wear target (hrs/day)", min_value=1, max_value=24, value=20)
        if st.button("Add patient"):
            if code.strip() == "":
                st.warning("Enter a patient code first.")
            else:
                supabase.table("patients").insert({
                    "doctor_id": current_user.id,
                    "patient_code": code.strip(),
                    "target_hours": target,
                }).execute()
                st.success(f"Added patient {code.strip()}")
                st.rerun()

    patients = get_my_patients()
    if not patients:
        st.info("No patients added yet.")
    else:
        st.dataframe(
            pd.DataFrame(patients)[["patient_code", "target_hours", "created_at"]],
            use_container_width=True
        )

# ---------------------------------------------------------------
# PAGE: UPLOAD & ANALYZE
# ---------------------------------------------------------------
elif page == "Upload & Analyze":
    st.title("Upload & Analyze CSV")

    patients = get_my_patients()
    if not patients:
        st.warning("Add a patient first, on the 'My Patients' page.")
        st.stop()

    patient_map = {p["patient_code"]: p for p in patients}
    selected_code = st.selectbox("Select patient", list(patient_map.keys()))
    selected_patient = patient_map[selected_code]

    uploaded_file = st.file_uploader("Upload device CSV log", type=["csv"])

    if uploaded_file is not None:
        try:
            df, bad_rows = parse_device_csv(uploaded_file)
        except ValueError as e:
            st.error(str(e))
            st.stop()

        if bad_rows > 0:
            st.warning(f"{bad_rows} row(s) had unparseable timestamps and were dropped.")

        target_hours = selected_patient["target_hours"]
        result = analyze_log(df, target_hours=target_hours)

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Days of Data", result["summary"]["total_days"])
        col2.metric("Avg Daily Wear", f"{result['summary']['avg_daily_hours']:.1f} hrs")
        col3.metric("Overall Compliance", f"{result['summary']['overall_compliance_pct']:.0f}%")
        col4.metric("Days Meeting Target",
                    f"{result['summary']['days_meeting_target']} / {result['summary']['total_days']}")

        st.subheader("Daily Wear Hours")
        st.bar_chart(result["daily"].set_index("Date")["WornHours"])

        st.subheader("Temperature vs Time")
        fig_temp = go.Figure()
        fig_temp.add_scatter(
            x=df["Timestamp"], y=df["Temp"],
            mode="lines", name="Temperature",
            line=dict(color="#4C8BF5", width=1.5)
        )
        fig_temp.add_hrect(
            y0=32.0, y1=40.0,
            fillcolor="#1a7f3c", opacity=0.08, line_width=0,
            annotation_text="Worn temp range", annotation_position="top left"
        )
        fig_temp.update_layout(
            xaxis_title="Time", yaxis_title="Temperature (°C)", height=380
        )
        st.plotly_chart(fig_temp, use_container_width=True)

        st.subheader("Device-Offline Gaps")
        if result["gaps"].empty:
            st.success("No significant gaps detected.")
        else:
            st.dataframe(result["gaps"], use_container_width=True)

        if st.button("Save this upload to patient record"):
            supabase.table("uploads").insert({
                "patient_id": selected_patient["id"],
                "doctor_id": current_user.id,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
                "daily_summary": daily_to_records(result["daily"]),
                "summary_stats": result["summary"],
                "filename": uploaded_file.name,
            }).execute()
            st.success("Saved to patient record.")

# ---------------------------------------------------------------
# PAGE: PATIENT HISTORY
# ---------------------------------------------------------------
elif page == "Patient History":
    st.title("Patient History")

    patients = get_my_patients()
    if not patients:
        st.info("No patients yet.")
        st.stop()

    patient_map = {p["patient_code"]: p for p in patients}
    selected_code = st.selectbox("Select patient", list(patient_map.keys()))
    selected_patient = patient_map[selected_code]

    uploads = get_uploads_for_patient(selected_patient["id"])
    if not uploads:
        st.info("No uploads recorded for this patient yet.")
        st.stop()

    combined = merge_upload_history(uploads)
    target_hours = selected_patient["target_hours"]

    col1, col2, col3 = st.columns(3)
    col1.metric("Total Days Recorded", combined.shape[0])
    col2.metric("Avg Daily Wear", f"{combined['WornHours'].mean():.1f} hrs")
    col3.metric("Overall Compliance", f"{combined['CompliancePct'].mean():.0f}%")

    st.subheader("Wear Hours Over Time (all uploads combined)")
    st.bar_chart(combined.set_index("Date")["WornHours"])

    st.subheader("Compliance % Over Time")
    st.line_chart(combined.set_index("Date")["CompliancePct"])

    if "AvgTemp" in combined.columns:
        st.subheader("Average Daily Temperature Over Time")
        st.line_chart(combined.set_index("Date")["AvgTemp"])

    st.subheader("Upload History")
    st.dataframe(
        pd.DataFrame(uploads)[["filename", "uploaded_at"]].sort_values("uploaded_at", ascending=False),
        use_container_width=True
    )

# ---------------------------------------------------------------
# PAGE: WEARABLE TEMPERATURE
# ---------------------------------------------------------------
elif page == "Wearable Temperature":
    st.title("Wearable Temperature - Live Monitoring")

    st.caption("Live temperature data from the Supabase wearable_logs table.")

    # Patient 1 / wearable-01 as requested.
    # The label is for display; device_id is what actually selects the data.
    patient_label = "Patient 1"
    device_id = "wearable-01"

    st.info(f"Patient: **{patient_label}**  |  Device: **{device_id}**")

    # Manual refresh
    if st.button("🔄 Refresh Now", key="wearable_refresh"):
        st.rerun()

    try:
        rows = get_wearable_logs(device_id)
    except Exception as e:
        st.error("Unable to read wearable data from Supabase.")
        st.exception(e)
        st.stop()

    if not rows:
        st.warning(
            f"No wearable data found for device `{device_id}`. "
            "Make sure the ESP32 has uploaded at least one reading."
        )
        st.stop()

    wearable_df = pd.DataFrame(rows)

    # Numeric temperature
    wearable_df["temp"] = pd.to_numeric(
        wearable_df["temp"], errors="coerce"
    )

    # Supabase server time
    wearable_df["recorded_at"] = pd.to_datetime(
        wearable_df["recorded_at"], errors="coerce"
    )

    # ESP32/NTP time. If unavailable/UNSYNCED, use Supabase server time.
    wearable_df["device_datetime"] = wearable_df["device_datetime"].fillna("")
    wearable_df["PlotTime"] = pd.to_datetime(
        wearable_df["device_datetime"].where(
            ~wearable_df["device_datetime"].astype(str).str.startswith("UNSYNCED"),
            None
        ),
        errors="coerce"
    )

    wearable_df["PlotTime"] = wearable_df["PlotTime"].fillna(
        wearable_df["recorded_at"]
    )

    wearable_df = (
        wearable_df
        .dropna(subset=["temp", "PlotTime"])
        .sort_values("PlotTime")
        .reset_index(drop=True)
    )

    if wearable_df.empty:
        st.warning("No valid temperature/time records are available yet.")
        st.stop()

    # Summary metrics
    latest_temp = wearable_df["temp"].iloc[-1]
    min_temp = wearable_df["temp"].min()
    max_temp = wearable_df["temp"].max()
    total_readings = len(wearable_df)

    c1, c2, c3, c4 = st.columns(4)

    c1.metric("Latest Temperature", f"{latest_temp:.1f} °C")
    c2.metric("Minimum", f"{min_temp:.1f} °C")
    c3.metric("Maximum", f"{max_temp:.1f} °C")
    c4.metric("Total Readings", f"{total_readings:,}")

    st.caption(
        "Last reading: "
        + wearable_df["PlotTime"].iloc[-1].strftime("%Y-%m-%d %H:%M:%S")
    )

    # Live temperature vs time plot
    st.subheader(f"Temperature vs Time - {patient_label}")

    fig_wearable = go.Figure()

    fig_wearable.add_scatter(
        x=wearable_df["PlotTime"],
        y=wearable_df["temp"],
        mode="lines+markers",
        name="Temperature",
        line=dict(color="#ff4757", width=2),
        marker=dict(size=4),
        hovertemplate=(
            "<b>%{x}</b><br>"
            "Temperature: %{y:.2f} °C"
            "<extra></extra>"
        ),
    )

    fig_wearable.update_layout(
        xaxis_title="Time",
        yaxis_title="Temperature (°C)",
        height=500,
        hovermode="x unified",
        margin=dict(l=60, r=30, t=50, b=60),
    )

    st.plotly_chart(fig_wearable, use_container_width=True)

    # Raw data
    with st.expander("📋 View Wearable Data"):
        display_df = wearable_df[
            ["device_datetime", "recorded_at", "temp"]
        ].copy()

        display_df.columns = [
            "Device Time",
            "Supabase Server Time",
            "Temperature (°C)",
        ]

        display_df = display_df.sort_values(
            "Device Time", ascending=False
        )

        st.dataframe(
            display_df,
            use_container_width=True,
            hide_index=True,
        )

    st.caption(
        "Data is read directly from Supabase. "
        "Use 'Refresh Now' to fetch the latest readings."
    )


# ---------------------------------------------------------------
# PAGE: TEAM OVERVIEW (admin only)
# ---------------------------------------------------------------
elif page == "Team Overview":
    st.title("Team Overview (Admin)")

    all_patients = get_all_patients_with_doctor()
    if not all_patients:
        st.info("No patients in the system yet.")
        st.stop()

    rows = []
    for p in all_patients:
        uploads = get_uploads_for_patient(p["id"])
        if uploads:
            latest = max(uploads, key=lambda u: u["uploaded_at"])
            stats = latest.get("summary_stats") or {}
        else:
            stats = {}
        doctor_email = (p.get("doctors") or {}).get("email", "unknown")
        rows.append({
            "Patient": p["patient_code"],
            "Doctor": doctor_email,
            "Target hrs/day": p["target_hours"],
            "Avg Daily Wear": round(stats.get("avg_daily_hours", 0), 1),
            "Overall Compliance %": round(stats.get("overall_compliance_pct", 0), 1),
            "Days Meeting Target": stats.get("days_meeting_target", 0),
            "Total Days": stats.get("total_days", 0),
        })

    team_df = pd.DataFrame(rows).sort_values("Overall Compliance %")
    st.dataframe(team_df, use_container_width=True)
