"""
Shared brace-wear compliance analysis logic.
Kept separate from the Streamlit UI so it can be reused by both the
standalone MVP app and the multi-user database-backed app.
"""
import pandas as pd
import numpy as np


def parse_device_csv(uploaded_file):
    """Parse a device CSV (DateTime, Temp, Touch) into a clean DataFrame.
    Returns (df, bad_row_count). Raises ValueError on structural problems."""
    df = pd.read_csv(uploaded_file)

    expected_cols = {"DateTime", "Temp", "Touch"}
    if not expected_cols.issubset(set(df.columns)):
        raise ValueError(f"CSV must contain columns: {expected_cols}. Found: {list(df.columns)}")

    df["Timestamp"] = pd.to_datetime(df["DateTime"], format="%d%m%y %H:%M:%S", errors="coerce")
    bad_rows = int(df["Timestamp"].isna().sum())
    df = df.dropna(subset=["Timestamp"]).sort_values("Timestamp").reset_index(drop=True)

    if df.empty:
        raise ValueError("No valid rows found after parsing timestamps.")

    return df, bad_rows


def analyze_log(df, temp_min=32.0, temp_max=40.0, gap_threshold_min=5, target_hours=20):
    """
    Core compliance analysis. Takes a parsed DataFrame (must have Timestamp,
    Temp, Touch columns) and returns a dict of results:

      daily        - DataFrame: Date, WornHours, CompliancePct, MetTarget
      sessions     - DataFrame: Worn, Start, End, DurationSec (continuous streaks)
      hourly_pivot - DataFrame: Date x Hour, fraction worn
      gaps         - DataFrame: device-offline gaps (Gap start, Gap end, GapDuration)
      summary      - dict of top-line stats (avg_daily_hours, overall_compliance_pct,
                      days_meeting_target, total_days, longest_worn_hours, longest_gap_hours)
    """
    df = df.copy()

    df["TempInRange"] = df["Temp"].between(temp_min, temp_max)
    df["Worn"] = (df["Touch"] == 1) | (df["TempInRange"])

    df["NextTimestamp"] = df["Timestamp"].shift(-1)
    df["DeltaSeconds"] = (df["NextTimestamp"] - df["Timestamp"]).dt.total_seconds()

    gap_threshold_sec = gap_threshold_min * 60
    df["IsDeviceGap"] = df["DeltaSeconds"] > gap_threshold_sec
    df["EffectiveDuration"] = np.where(df["IsDeviceGap"], 0, df["DeltaSeconds"].fillna(0))

    df["Date"] = df["Timestamp"].dt.date
    df["Hour"] = df["Timestamp"].dt.hour

    # ---- daily summary ----
    daily = (
        df.groupby("Date")
        .apply(lambda g: pd.Series({
            "WornSeconds": g.loc[g["Worn"], "EffectiveDuration"].sum(),
            "AvgTemp": g["Temp"].mean(),
        }), include_groups=False)
        .reset_index()
    )
    daily["WornHours"] = daily["WornSeconds"] / 3600
    daily["CompliancePct"] = (daily["WornHours"] / target_hours * 100).clip(upper=100)
    daily["MetTarget"] = daily["WornHours"] >= target_hours
    daily = daily.drop(columns=["WornSeconds"])

    # ---- session detection (continuous worn / not-worn streaks) ----
    df["WornChange"] = (df["Worn"] != df["Worn"].shift()).cumsum()
    sessions = (
        df.groupby("WornChange")
        .agg(Worn=("Worn", "first"),
             Start=("Timestamp", "first"),
             End=("Timestamp", "last"),
             DurationSec=("EffectiveDuration", "sum"))
        .reset_index(drop=True)
    )
    worn_sessions = sessions[sessions["Worn"]]
    not_worn_sessions = sessions[~sessions["Worn"]]
    longest_worn_sec = worn_sessions["DurationSec"].max() if not worn_sessions.empty else 0
    longest_gap_sec = not_worn_sessions["DurationSec"].max() if not not_worn_sessions.empty else 0

    # ---- hour-of-day pivot ----
    hourly = (
        df.groupby(["Date", "Hour"])
        .apply(lambda g: pd.Series({
            "WornFrac": g.loc[g["Worn"], "EffectiveDuration"].sum() /
                        max(g["EffectiveDuration"].sum(), 1)
        }), include_groups=False)
        .reset_index()
    )
    hourly_pivot = hourly.pivot(index="Date", columns="Hour", values="WornFrac").fillna(0)

    # ---- device-offline gaps table ----
    gap_rows = df[df["IsDeviceGap"]][["Timestamp", "NextTimestamp", "DeltaSeconds"]].copy()
    gap_rows["GapDuration"] = (gap_rows["DeltaSeconds"] / 60).round(1).astype(str) + " min"
    gaps = gap_rows.rename(columns={"Timestamp": "Gap start", "NextTimestamp": "Gap end"})[
        ["Gap start", "Gap end", "GapDuration"]
    ]

    summary = {
        "total_days": int(daily.shape[0]),
        "avg_daily_hours": float(daily["WornHours"].mean()) if not daily.empty else 0.0,
        "overall_compliance_pct": float(daily["CompliancePct"].mean()) if not daily.empty else 0.0,
        "days_meeting_target": int(daily["MetTarget"].sum()),
        "longest_worn_hours": float(longest_worn_sec / 3600),
        "longest_gap_hours": float(longest_gap_sec / 3600),
    }

    return {
        "daily": daily,
        "sessions": sessions,
        "hourly_pivot": hourly_pivot,
        "gaps": gaps,
        "summary": summary,
    }


def daily_to_records(daily_df):
    """Convert the daily DataFrame to a JSON-serializable list of dicts,
    for storing in a database jsonb column."""
    out = daily_df.copy()
    out["Date"] = out["Date"].astype(str)
    out["WornHours"] = out["WornHours"].round(3)
    out["CompliancePct"] = out["CompliancePct"].round(1)
    if "AvgTemp" in out.columns:
        out["AvgTemp"] = out["AvgTemp"].round(1)
    return out.to_dict(orient="records")


def records_to_daily(records):
    """Reverse of daily_to_records - rebuild a DataFrame from stored JSON."""
    df = pd.DataFrame(records)
    if df.empty:
        return df
    df["Date"] = pd.to_datetime(df["Date"]).dt.date
    return df
