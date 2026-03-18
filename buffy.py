import streamlit as st
import pandas as pd
import numpy as np
import os
import json
import re
from datetime import datetime, timedelta
from streamlit_echarts import st_echarts
from google import genai
from google.genai import types
from streamlit_gsheets import GSheetsConnection

# --- API SETUP ---
client = genai.Client(api_key="AIzaSyBwI5VmQ15PSKJzPfy31bplk10ZfYbWf24")

buffy_instruction = """
You are Buffy, a ruthless fitness data analyst and the master of this app. Be short, aggressive, pushy, and heavily data-driven. The baseline calculates protein at 1g per lb of bodyweight. 
CRITICAL SYSTEM COMMANDS: You have full write-access to the user's database. 
1. If the user asks to change their target macros/calories and you agree, end your response with: [UPDATE_MACROS: cals, protein, carbs, fats]. Example: [UPDATE_MACROS: 2500, 220, 200, 60]
2. If the user tells you they completed a workout, log it by ending your response with: [LOG_WORKOUT: "Brief Description", minutes]. Example: [LOG_WORKOUT: "Heavy Push Day", 60]
"""

# --- CONFIG & CSS ---
st.set_page_config(page_title="BuffBurn Command", layout="wide", initial_sidebar_state="collapsed")

st.markdown("""
    <style>
    .stApp, section[data-testid="stSidebar"] { background-color: #0e1117 !important; }
    section[data-testid="stSidebar"] { border-right: 1px solid #30363d; }
    div[data-testid="metric-container"] { background-color: #161b22; border: 1px solid #30363d; padding: 15px; border-radius: 10px; }
    
    /* FORCE ALL METRIC LABELS AND NUMBERS TO WHITE */
    div[data-testid="metric-container"] label { color: #8b949e !important; } 
    [data-testid="stMetricValue"] { color: #ffffff !important; font-weight: bold; }
    [data-testid="stMetricDelta"] * { color: #ffffff !important; font-size: 1.1rem !important; }
    
    .stTabs [data-baseweb="tab-list"] { gap: 10px; }
    .stTabs [data-baseweb="tab"] { background-color: #161b22; color: #8b949e !important; border-radius: 4px 4px 0px 0px; padding: 10px 20px; }
    .stTabs [aria-selected="true"] { background-color: #ff4b4b !important; color: #ffffff !important; font-weight: bold; }
    .stButton > button, div[data-testid="stFormSubmitButton"] > button { background-color: #ff4b4b !important; color: #ffffff !important; border: none !important; border-radius: 5px !important; font-weight: bold !important; }
    .stButton > button:hover, div[data-testid="stFormSubmitButton"] > button:hover { background-color: #d43f3f !important; }
    h1, h2, h3, h4, h5, h6, label, .stMarkdown p, .stMarkdown li { color: #ffffff !important; }
    .buffy-alert { background-color: #2a1010; border-left: 5px solid #ff4b4b; padding: 15px; border-radius: 5px; margin-bottom: 20px; }
    div[data-testid="stDataFrame"] { background-color: #161b22; }
    </style>
""", unsafe_allow_html=True)

# --- SECURE AUTHENTICATION SYSTEM ---
def check_password():
    def password_entered():
        user = st.session_state["username"]
        pwd = st.session_state["password"]
        if user in st.secrets["passwords"] and st.secrets["passwords"][user] == pwd:
            st.session_state["password_correct"] = True
            st.session_state["active_user"] = user
            del st.session_state["password"]
        else:
            st.session_state["password_correct"] = False

    if "password_correct" not in st.session_state:
        st.title("🔒 BUFFBURN: RESTRICTED ACCESS")
        st.text_input("Username", key="username")
        st.text_input("Password", type="password", key="password")
        st.button("Authenticate", on_click=password_entered)
        return False
    elif not st.session_state["password_correct"]:
        st.title("🔒 BUFFBURN: RESTRICTED ACCESS")
        st.text_input("Username", key="username")
        st.text_input("Password", type="password", key="password")
        st.button("Authenticate", on_click=password_entered)
        st.error("Access Denied. Incorrect username or password.")
        return False
    else:
        return True

if not check_password():
    st.stop()

active_user = st.session_state["active_user"]

with st.sidebar:
    st.header("⚙️ PROFILE")
    st.write(f"Logged in as: **{active_user}**")
    if st.button("Logout"):
        del st.session_state["password_correct"]
        del st.session_state["active_user"]
        st.rerun()

# --- GOOGLE SHEETS CLOUD ENGINE ---
conn = st.connection("gsheets", type=GSheetsConnection)
KG_TO_LBS = 2.20462
TODAY = datetime.now().strftime("%Y-%m-%d")

@st.cache_data(ttl=0) 
def load_sheet(sheet_name, default_cols):
    try:
        df = conn.read(worksheet=sheet_name, ttl=0).dropna(how="all")
        if df.empty or len(df.columns) == 0 or df.columns[0] not in default_cols:
            df = pd.DataFrame(columns=default_cols)
            conn.update(worksheet=sheet_name, data=df)
        return df
    except Exception:
        df = pd.DataFrame(columns=default_cols)
        conn.update(worksheet=sheet_name, data=df)
        return df

with st.spinner("Syncing Cloud Database..."):
    df_users = load_sheet("db_users", ["User", "Unit", "Goal", "TargetWeight", "Weeks", "OverrideCals", "OverrideP", "OverrideC", "OverrideF"])
    df_macros = load_sheet("db_macros", ["User", "Date", "Food", "P", "C", "F"])
    df_weight = load_sheet("db_weight", ["User", "Date", "Weight_kg", "BodyFat"])
    df_workouts = load_sheet("db_workouts", ["User", "Date", "Workout", "Duration"])

# --- ONBOARDING ---
user_profile = df_users[df_users["User"] == active_user]

if user_profile.empty:
    st.title("⚠️ INIT PROTOCOL REQUIRED")
    with st.form("onboarding_form"):
        c1, c2 = st.columns(2)
        unit_pref = c1.radio("Preferred Unit", ["kg", "lbs"], horizontal=True, index=1)
        goal_pref = c2.selectbox("Primary Objective", ["Cut", "Bulk", "Maintain"])
        c3, c4 = st.columns(2)
        start_weight = c3.number_input(f"Current Mass", value=200.0, step=0.1)
        start_bf = c4.number_input("Estimated Body Fat %", value=15.0, step=0.1)
        c5, c6 = st.columns(2)
        target_weight = c5.number_input(f"Target Mass", value=180.0, step=0.1)
        weeks_pref = c6.slider("Timeline (Weeks)", 1, 24, 12)
        
        if st.form_submit_button("Lock Protocol & Execute"):
            new_user = pd.DataFrame([{"User": active_user, "Unit": unit_pref, "Goal": goal_pref, "TargetWeight": target_weight, "Weeks": weeks_pref, "OverrideCals": 0, "OverrideP": 0, "OverrideC": 0, "OverrideF": 0}])
            conn.update(worksheet="db_users", data=pd.concat([df_users, new_user], ignore_index=True))
            
            w_kg = start_weight if unit_pref == "kg" else start_weight / KG_TO_LBS
            new_weight = pd.DataFrame([{"User": active_user, "Date": TODAY, "Weight_kg": w_kg, "BodyFat": start_bf}])
            conn.update(worksheet="db_weight", data=pd.concat([df_weight, new_weight], ignore_index=True))
            
            st.rerun()
    st.stop() 

# --- LOAD USER SETTINGS ---
unit = user_profile.iloc[0]["Unit"]
current_goal = user_profile.iloc[0]["Goal"]
target_weight_input = user_profile.iloc[0]["TargetWeight"]
weeks_to_goal = user_profile.iloc[0]["Weeks"]

ov_cals = user_profile.iloc[0].get("OverrideCals", 0)
ov_p = user_profile.iloc[0].get("OverrideP", 0)
ov_c = user_profile.iloc[0].get("OverrideC", 0)
ov_f = user_profile.iloc[0].get("OverrideF", 0)

# --- SIDEBAR SETTINGS ---
with st.sidebar:
    st.divider()
    st.header("🎯 BASE PROTOCOL")
    new_unit = st.radio("Unit", ["kg", "lbs"], index=0 if unit=="kg" else 1, horizontal=True)
    new_goal = st.selectbox("Objective", ["Cut", "Bulk", "Maintain"], index=["Cut", "Bulk", "Maintain"].index(current_goal))
    new_target = st.number_input(f"Target Weight ({new_unit})", value=float(target_weight_input), step=0.1)
    new_weeks = st.slider("Timeline (Weeks)", 1, 24, int(weeks_to_goal))
    
    if st.button("Update Profile"):
        df_users.loc[df_users["User"] == active_user, ["Unit", "Goal", "TargetWeight", "Weeks"]] = [new_unit, new_goal, new_target, new_weeks]
        conn.update(worksheet="db_users", data=df_users)
        st.rerun()
        
    if ov_cals > 0:
        st.warning("⚠️ AI Macro Override Active")
        if st.button("Reset AI Overrides"):
            df_users.loc[df_users["User"] == active_user, ["OverrideCals", "OverrideP", "OverrideC", "OverrideF"]] = [0, 0, 0, 0]
            conn.update(worksheet="db_users", data=df_users)
            st.rerun()

# --- DATA FETCHING ---
user_macros = df_macros[df_macros["User"] == active_user]
user_weight = df_weight[df_weight["User"] == active_user]
user_workouts = df_workouts[df_workouts["User"] == active_user]

today_macros = user_macros[user_macros["Date"] == TODAY]
today_workouts = user_workouts[user_workouts["Date"] == TODAY]

tot_p = pd.to_numeric(today_macros["P"], errors='coerce').sum()
tot_c = pd.to_numeric(today_macros["C"], errors='coerce').sum()
tot_f = pd.to_numeric(today_macros["F"], errors='coerce').sum()
tot_cals = (tot_p * 4) + (tot_c * 4) + (tot_f * 9)

current_weight_kg = user_weight.iloc[-1]["Weight_kg"]
current_bf = user_weight.iloc[-1]["BodyFat"]
target_weight_kg = target_weight_input if unit == "kg" else target_weight_input / KG_TO_LBS

# --- SCIENCE ENGINE ---
def get_timeline_targets(current_w_kg, target_w_kg, weeks, goal):
    current_w_lbs = current_w_kg * KG_TO_LBS
    tdee = current_w_lbs * 15 
    if goal == "Maintain" or weeks == 0:
        daily_cals, warning = tdee, None
    else:
        weight_diff_kg = target_w_kg - current_w_kg 
        daily_adj = (weight_diff_kg * 7700) / (weeks * 7)
        daily_cals = tdee + daily_adj
        warning = None
        max_deficit = tdee * 0.4
        if daily_adj < -max_deficit:
            daily_cals = tdee - max_deficit
            warning = f"DELUSIONAL TIMELINE: Deficit capped to prevent muscle wasting."
    protein = current_w_lbs * 1.0 
    fats = (daily_cals * 0.25) / 9
    carbs = (daily_cals - (protein * 4) - (fats * 9)) / 4
    return {"calories": int(daily_cals), "protein": int(protein), "carbs": int(carbs), "fats": int(fats), "warning": warning}

targets = get_timeline_targets(current_weight_kg, target_weight_kg, weeks_to_goal, current_goal)

if ov_cals > 0:
    targets.update({"calories": int(ov_cals), "protein": int(ov_p), "carbs": int(ov_c), "fats": int(ov_f), "warning": "AI OVERRIDE ACTIVE: Buffy has customized your targets."})

display_weight = current_weight_kg if unit == "kg" else current_weight_kg * KG_TO_LBS

# --- UI EXECUTION ---
st.title("🔥 BUFFBURN: ELITE COMMAND")

if targets["warning"]:
    st.markdown(f'<div class="buffy-alert"><strong>⚡ SYSTEM NOTICE:</strong> {targets["warning"]}</div>', unsafe_allow_html=True)

tab_dash, tab_log, tab_ai = st.tabs(["📊 DASHBOARD", "🥩 LOG, EDIT & SUGGEST", "🧠 BUFFY AI & PROTOCOLS"])

with tab_dash:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Calories Left", f"{targets['calories'] - tot_cals}", f"Total: {targets['calories']}", delta_color="off")
    c2.metric("Protein Left", f"{int(targets['protein']) - tot_p}g", f"Total: {int(targets['protein'])}g", delta_color="off")
    c3.metric("Carbs Left", f"{int(targets['carbs']) - tot_c}g", f"Total: {int(targets['carbs'])}g", delta_color="off")
    c4.metric("Fats Left", f"{int(targets['fats']) - tot_f}g", f"Total: {int(targets['fats'])}g", delta_color="off")
    st.divider()
    
    st.subheader(f"Goal: {current_goal} to {target_weight_input} {unit} in {weeks_to_goal} Weeks")
    initial_weight_kg = user_weight.iloc[0]["Weight_kg"]
    total_to_lose = abs(target_weight_kg - initial_weight_kg)
    if total_to_lose == 0:
        progress_val = 1.0
    else:
        lost = abs(current_weight_kg - initial_weight_kg)
        progress_val = max(0.0, min(1.0, lost / total_to_lose))
    st.progress(progress_val, text=f"Timeline Progress")
    st.divider()
    
    proj_dates = [datetime.strptime(TODAY, "%Y-%m-%d") + timedelta(days=i) for i in range(1, 15)]
    proj_dates_str = [d.strftime("%Y-%m-%d") for d in proj_dates]
    days_to_goal = weeks_to_goal * 7
    daily_trend = (target_weight_input - display_weight) / days_to_goal if days_to_goal > 0 else 0
    proj_weights = [display_weight + (daily_trend * i) for i in range(1, 15)]
    hist_w = (user_weight['Weight_kg'] * KG_TO_LBS).tolist() if unit == "lbs" else user_weight['Weight_kg'].tolist()
    
    options = {
        "tooltip": {"trigger": 'axis'},
        "legend": {"data": [f'Historical ({unit})', f'14-Day Projection ({unit})'], "textStyle": {"color": "#fff"}},
        "xAxis": {"type": 'category', "data": user_weight['Date'].tolist() + proj_dates_str},
        "yAxis": {"type": 'value'},
        "series": [
            {"name": f'Historical ({unit})', "type": 'line', "data": hist_w, "smooth": True, "lineStyle": {"color": "#1f77b4", "width": 3}},
            {"name": f'14-Day Projection ({unit})', "type": 'line', "data": [None]*(len(hist_w)-1) + [hist_w[-1]] + proj_weights, "smooth": True, "lineStyle": {"color": "#ff4b4b", "type": "dashed", "width": 3}}
        ]
    }
    st_echarts(options=options, height="400px")

with tab_log:
    st.subheader("Today's Total Intake")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Calories", f"{tot_cals} kcal")
    m2.metric("Total Protein", f"{tot_p}g")
    m3.metric("Total Carbs", f"{tot_c}g")
    m4.metric("Total Fats", f"{tot_f}g")
    st.divider()

    col_meal, col_edit = st.columns([1, 1.5])
    
    with col_meal:
        st.subheader("Smart AI Logger")
        with st.form("smart_log"):
            smart_food = st.text_area("What did you eat?", placeholder="e.g. 1 whole egg, 300g egg whites, 200g rice")
            if st.form_submit_button("Auto-Log Individual Macros"):
                if smart_food:
                    prompt = f"Break down this meal: '{smart_food}'. Return ONLY a JSON array of objects. Format: [{{\"Food\": \"Item name\", \"P\": int, \"C\": int, \"F\": int}}]."
                    try:
                        api_config = types.GenerateContentConfig(response_mime_type="application/json")
                        raw_response = client.models.generate_content(model='gemini-2.5-flash', contents=prompt, config=api_config).text
                        clean_json = raw_response[raw_response.find('['):raw_response.rfind(']')+1]
                        resp = json.loads(clean_json)
                        
                        new_rows = pd.DataFrame([{"User": active_user, "Date": TODAY, "Food": item["Food"], "P": item["P"], "C": item["C"], "F": item["F"]} for item in resp])
                        conn.update(worksheet="db_macros", data=pd.concat([df_macros, new_rows], ignore_index=True))
                        st.rerun()
                    except Exception as e:
                        st.error(f"Parse failed. Error details: {e}")

        st.divider()
        st.subheader("Morning Weigh-in")
        with st.form("weight_form", clear_on_submit=True):
            w_in = st.number_input(f"Mass ({unit})", value=float(display_weight), step=0.1)
            bf_in = st.number_input("Body Fat %", value=float(current_bf), step=0.1)
            if st.form_submit_button("Record Biometrics"):
                save_w_kg = w_in if unit == "kg" else w_in / KG_TO_LBS
                new_weight = pd.DataFrame([{"User": active_user, "Date": TODAY, "Weight_kg": save_w_kg, "BodyFat": bf_in}])
                conn.update(worksheet="db_weight", data=pd.concat([df_weight, new_weight], ignore_index=True))
                st.rerun()

    with col_edit:
        st.subheader("Today's Food Log")
        display_cols = ["Food", "P", "C", "F"]
        edited_df = st.data_editor(today_macros[display_cols], num_rows="dynamic", width="stretch", column_config={"Food": st.column_config.TextColumn("Food Item", width="large")})
        if st.button("Sync Database Updates"):
            edited_df["User"], edited_df["Date"] = active_user, TODAY
            df_macros_clean = df_macros[(df_macros["User"] != active_user) | (df_macros["Date"] != TODAY)]
            conn.update(worksheet="db_macros", data=pd.concat([df_macros_clean, edited_df], ignore_index=True))
            st.rerun()

        st.divider()
        st.subheader("Today's Workouts")
        if not today_workouts.empty:
            st.dataframe(today_workouts[["Workout", "Duration"]], width="stretch", hide_index=True)
        else:
            st.caption("No workouts logged today. Tell Buffy in the Terminal to log one.")

with tab_ai:
    col_chat, col_work = st.columns([1, 1])
    
    with col_chat:
        st.subheader("💬 Direct Terminal")
        if "messages" not in st.session_state: st.session_state.messages = []
        for msg in st.session_state.messages: st.chat_message(msg["role"]).write(msg["content"])

        if prompt := st.chat_input("Ask Buffy directly..."):
            st.session_state.messages.append({"role": "user", "content": prompt})
            st.chat_message("user").write(prompt)
            macro_context = f"[OMNI-DATA: Weight={display_weight:.1f}{unit}, Target={target_weight_input}{unit}, Timeline={weeks_to_goal}w, Goal={current_goal}. Today's Macros: {tot_p}g P / {int(targets['protein'])}g.]"
            try:
                response = client.models.generate_content(model='gemini-2.5-flash', contents=f"{macro_context} {prompt}", config=types.GenerateContentConfig(system_instruction=buffy_instruction))
                response_text = response.text
                
                match_macros = re.search(r'\[UPDATE_MACROS:\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)\]', response_text)
                if match_macros:
                    c, p, cb, f = match_macros.groups()
                    df_users.loc[df_users["User"] == active_user, ["OverrideCals", "OverrideP", "OverrideC", "OverrideF"]] = [int(c), int(p), int(cb), int(f)]
                    conn.update(worksheet="db_users", data=df_users)
                    response_text = re.sub(r'\[UPDATE_MACROS:.*?\]', '', response_text).strip()
                    st.session_state.messages.append({"role": "assistant", "content": response_text})
                    st.rerun()
                
                match_workout = re.search(r'\[LOG_WORKOUT:\s*"(.*?)",\s*(\d+)\]', response_text)
                if match_workout:
                    desc, mins = match_workout.groups()
                    new_workout = pd.DataFrame([{"User": active_user, "Date": TODAY, "Workout": desc, "Duration": int(mins)}])
                    conn.update(worksheet="db_workouts", data=pd.concat([df_workouts, new_workout], ignore_index=True))
                    response_text = re.sub(r'\[LOG_WORKOUT:.*?\]', '', response_text).strip()
                    st.session_state.messages.append({"role": "assistant", "content": response_text})
                    st.rerun()
                
                if not match_macros and not match_workout:
                    st.session_state.messages.append({"role": "assistant", "content": response_text})
                    st.chat_message("assistant").write(response_text)
            except Exception as e:
                st.error(f"API Error: {e}")

    with col_work:
        st.subheader("🛠️ Protocol Generation")
        target_muscle = st.selectbox("Lifting Focus", ["Push", "Pull", "Legs", "Upper", "Lower", "Full Body"])
        lift_time = st.slider("Lifting Time (min)", 30, 120, 60, step=5)
        cardio_type = st.selectbox("Cardio Protocol", ["LISS (Low Intensity)", "HIIT (Intervals)", "Zone 2", "None"])
        cardio_time = st.slider("Cardio Time (min)", 0, 60, 20, step=5)
        
        if st.button("Generate Complete Routine", width="stretch"):
            prompt = f"Goal: {current_goal}. Generate a strict workout. PART 1: LIFTING ({lift_time} mins, {target_muscle}). PART 2: CARDIO ({cardio_time} mins, {cardio_type})."
            try: st.markdown(client.models.generate_content(model='gemini-2.5-flash', contents=prompt).text) 
            except Exception as e: st.error(f"API Error: {e}")