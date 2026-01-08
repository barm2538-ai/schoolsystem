import streamlit as st
import pandas as pd
from dbfread import DBF
import zipfile
import tempfile
import os
import re
import sqlite3
import time
from datetime import datetime
from streamlit_option_menu import option_menu

# ==========================================
# 0. ตั้งค่าระบบ
# ==========================================
SCHOOL_NAME = "ศูนย์ส่งเสริมการเรียนรู้อำเภอจุน"
DB_NAME = "school_data_v8_exam.db"  # อัปเดตชื่อ DB เพื่อสร้างตารางใหม่

st.set_page_config(page_title="ระบบตรวจสอบผลการเรียน & สอบออนไลน์", layout="wide", page_icon="🎓")

# ==========================================
# 1. CSS Styles
# ==========================================
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Sarabun:wght@300;400;600&display=swap');

.stApp { background-color: #F5F7FA !important; font-family: 'Sarabun', sans-serif; color: #333333 !important; }

/* Header */
.top-header { background: linear-gradient(135deg, #154360 0%, #2980B9 100%); padding: 25px; border-radius: 12px; color: white !important; margin-bottom: 20px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
.school-name { font-size: 18px; font-weight: 300; opacity: 0.95; margin-bottom: 5px; border-bottom: 1px solid rgba(255,255,255,0.3); padding-bottom: 5px; display: inline-block; }

/* Cards */
.profile-card { background-color: white; padding: 20px; border-radius: 12px; box-shadow: 0 2px 5px rgba(0,0,0,0.05); border: 1px solid #E1E5EB; }
.stat-card { background-color: white; padding: 20px; border-radius: 12px; border-left: 5px solid #2980B9; box-shadow: 0 4px 6px rgba(0,0,0,0.05); text-align: center; margin-bottom: 10px; }

/* UI Elements */
div[data-testid="stDataFrame"] { background: white; padding: 10px; border-radius: 10px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
.level-badge { display: inline-block; background-color: #E8F8F5; color: #117864; padding: 4px 12px; border-radius: 15px; font-size: 13px; font-weight: bold; border: 1px solid #A2D9CE; }
.section-title { font-size: 20px; font-weight: bold; color: #2C3E50; margin-bottom: 15px; border-left: 5px solid #2980B9; padding-left: 10px; }
.stat-number { font-size: 32px; font-weight: bold; color: #154360; }
.stat-label { font-size: 14px; color: #7F8C8D; }
</style>
""", unsafe_allow_html=True)

# ==========================================
# 2. Database & Utils
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    c = conn.cursor()
    
    # ตารางเดิม (คงไว้)
    c.execute('CREATE TABLE IF NOT EXISTS grades (std_id TEXT, sub_code TEXT, semestry TEXT, grade TEXT, grp_code TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS schedule (sub_code TEXT, semestry TEXT, exam_day TEXT, exam_start TEXT, exam_end TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS subjects (sub_code TEXT, sub_name TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS activities (std_id TEXT, semestry TEXT, act_name TEXT, act_type TEXT, hours REAL)')
    c.execute('CREATE TABLE IF NOT EXISTS students (std_id TEXT PRIMARY KEY, prefix TEXT, name TEXT, surname TEXT, grp_code TEXT, phone TEXT, card_id TEXT, level TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS groups (grp_code TEXT PRIMARY KEY, teacher_name TEXT)')
    c.execute('CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password TEXT, role TEXT, name TEXT, assigned_group TEXT)')
    
    # --- ส่วนที่แก้: ตาราง exams เพิ่มคอลัมน์ sub_code, semestry ---
    # สร้างตาราง exams (ถ้ายังไม่มี)
    c.execute('''CREATE TABLE IF NOT EXISTS exams (
                exam_id INTEGER PRIMARY KEY AUTOINCREMENT, 
                exam_name TEXT, 
                sub_code TEXT, 
                semestry TEXT, 
                is_active INTEGER DEFAULT 0)''')
    

    # *MIGRATION CHECK*: เช็คว่าถ้าเป็น DB เก่าที่ไม่มี sub_code ให้เพิ่มเข้าไป
    try:
        c.execute("SELECT sub_code FROM exams LIMIT 1")
    except sqlite3.OperationalError:
        # ถ้า Error แสดงว่ายังไม่มีคอลัมน์ sub_code (เป็น DB เวอร์ชั่นเก่า) ให้เพิ่มเข้าไป
        c.execute("ALTER TABLE exams ADD COLUMN sub_code TEXT")
        c.execute("ALTER TABLE exams ADD COLUMN semestry TEXT")
        conn.commit()
    # -----------------------------------------------------------

    c.execute('''CREATE TABLE IF NOT EXISTS exam_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exam_id INTEGER,
                question_text TEXT,
                choice_a TEXT, choice_b TEXT, choice_c TEXT, choice_d TEXT,
                correct_answer TEXT)''')
    c.execute('CREATE TABLE IF NOT EXISTS exam_results (id INTEGER PRIMARY KEY AUTOINCREMENT, exam_id INTEGER, std_id TEXT, score INTEGER, total_score INTEGER, timestamp TEXT)')
    
    c.execute("INSERT OR IGNORE INTO users VALUES ('admin', '1234', 'admin', 'ผู้ดูแลระบบ', '')")
    c.execute("""
        CREATE TABLE IF NOT EXISTS classroom_videos (
            vid_id INTEGER PRIMARY KEY AUTOINCREMENT,
            sub_code TEXT,
            topic_name TEXT,
            video_url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    return conn

def clean_id_card(val):
    if pd.isna(val): return ""
    s = str(val).strip().replace('.0', '')
    return re.sub(r'[^0-9]', '', s)

def format_thai_time(t):
    if pd.isna(t) or t == '' or str(t).lower() == 'nan': return ""
    try:
        val = float(t)
        if val >= 24: s = str(int(val)); return f"{s[:2]}.{s[2:]} น." if len(s)==4 else f"0{s[0]}.{s[1:]} น."
        else: h = int(val); m = int(round((val - h) * 100)); return f"{h:02}.{m:02} น."
    except: return str(t)

def get_level_from_id(std_id):
    sid = clean_id_card(std_id)
    if len(sid) >= 4:
        digit = sid[3]
        if digit == '1': return 'ประถมศึกษา'
        elif digit == '2': return 'มัธยมศึกษาตอนต้น'
        elif digit == '3': return 'มัธยมศึกษาตอนปลาย'
    return "ไม่ระบุ"

def read_dbf_robust(path):
    try:
        if os.path.getsize(path) < 50: return pd.DataFrame() 
        with DBF(path, encoding='cp874', char_decode_errors='ignore', ignore_missing_memofile=True, load=True) as table:
            df = pd.DataFrame(iter(table))
        df.columns = [c.upper().strip() for c in df.columns]
        for col in df.columns:
            if df[col].dtype == 'object': df[col] = df[col].astype(str).str.strip()
        return df
    except: return pd.DataFrame()

# ==========================================
# 3. Session & Login
# ==========================================
def restore_session():
    if 'logged_in' not in st.session_state:
        qp = st.query_params
        if "user" in qp:
            username = qp["user"]
            conn = init_db()
            user = pd.read_sql("SELECT * FROM users WHERE username=?", conn, params=(username,))
            if not user.empty:
                row = user.iloc[0]
                st.session_state.logged_in = True
                st.session_state.user = row['username']
                st.session_state.role = row['role']
                st.session_state.name = row['name']
                st.session_state.assigned_group = row['assigned_group']
            else:
                std = pd.read_sql("SELECT * FROM students WHERE std_id=?", conn, params=(username,))
                if not std.empty:
                    st.session_state.logged_in = True
                    st.session_state.user = username
                    st.session_state.role = 'student'
                    st.session_state.name = f"{std.iloc[0]['prefix']}{std.iloc[0]['name']} {std.iloc[0]['surname']}"
            conn.close()
        else:
            st.session_state.logged_in = False
            st.session_state.role = ''
            st.session_state.view_mode = 'dashboard'

def do_logout():
    st.session_state.clear()
    st.query_params.clear()
    st.rerun()

def login_page():
    st.markdown("<br><br>", unsafe_allow_html=True)
    c1, c2, c3 = st.columns([1, 1.5, 1])
    with c2:
        st.markdown(f"""
        <div style='background: white; padding: 40px; border-radius: 20px; box-shadow: 0 10px 25px rgba(0,0,0,0.1); text-align: center; border: 1px solid #E1E5EB;'>
            <h4 style='color: #7f8c8d; margin-bottom: 5px;'>{SCHOOL_NAME}</h4>
            <h2 style='color: #2C3E50;'>เข้าสู่ระบบ</h2>
        </div><br>""", unsafe_allow_html=True)
        
        with st.form("login_form", border=True):
            user_input = st.text_input("ชื่อผู้ใช้ (รหัสนักศึกษา / รหัสกลุ่ม)")
            pwd_input = st.text_input("รหัสผ่าน", type="password")
            
            if st.form_submit_button("เข้าสู่ระบบ", use_container_width=True):
                conn = init_db()
                cl_user = clean_id_card(user_input)
                
                user = pd.read_sql("SELECT * FROM users WHERE username=? AND password=?", conn, params=(user_input, pwd_input))
                success = False
                
                if not user.empty:
                    row = user.iloc[0]
                    st.session_state.logged_in = True
                    st.session_state.user = row['username']
                    st.session_state.role = row['role']
                    st.session_state.name = row['name']
                    st.session_state.assigned_group = row['assigned_group']
                    success = True
                else:
                    # Logic: ถ้ารหัสผ่าน == รหัสผู้ใช้ หรือ รหัสผ่าน == เลขบัตรประชาชน (ถ้ามีในอนาคต)
                    if cl_user == clean_id_card(pwd_input):
                        search_id = cl_user[-10:] if len(cl_user) > 10 else cl_user
                        std = pd.read_sql("SELECT * FROM students WHERE std_id=?", conn, params=(search_id,))
                        if not std.empty:
                            st.session_state.logged_in = True
                            st.session_state.user = search_id
                            st.session_state.role = 'student'
                            st.session_state.name = f"{std.iloc[0]['prefix']}{std.iloc[0]['name']} {std.iloc[0]['surname']}"
                            success = True
                        else: st.error("❌ ไม่พบข้อมูลในระบบ")
                    else: st.error("❌ รหัสผ่านไม่ถูกต้อง")
                
                conn.close()
                if success:
                    st.query_params["user"] = st.session_state.user
                    st.rerun()

# ==========================================
# 4. Student View (เพิ่มเมนูสอบออนไลน์)
# ==========================================
def styled_df(df):
    if df.empty: return df
    styler = df.style.set_properties(**{'background-color': '#FFFFFF', 'color': '#000000', 'border-color': '#EEEEEE'})
    styler.set_table_styles([{'selector': 'th', 'props': [('background-color', '#F0F2F6'), ('color', '#000000'), ('font-weight', 'bold')]}])
    return styler

def view_data_page(std_id, is_teacher_view=False):
    conn = init_db()
    clean_sid = clean_id_card(std_id)
    std_info = pd.read_sql("SELECT s.*, g.teacher_name FROM students s LEFT JOIN groups g ON s.grp_code = g.grp_code WHERE s.std_id=?", conn, params=(clean_sid,))
    
    if std_info.empty:
        st.error("ไม่พบข้อมูลนักศึกษา")
        if is_teacher_view and st.button("กลับ"): 
            st.session_state.view_mode = 'dashboard'
            st.rerun()
        return

    row = std_info.iloc[0]
    s_name = f"{row['prefix']}{row['name']} {row['surname']}"
    current_level = row['level'] if row['level'] else get_level_from_id(clean_sid)

    st.markdown(f"""
    <div class='top-header'>
        <div class='school-name'>{SCHOOL_NAME}</div>
        <h2 style='margin:0; font-size:26px;'>👤 {s_name}</h2>
    </div>
    """, unsafe_allow_html=True)

    col_menu, col_content = st.columns([1, 3], gap="large")
    
    with col_menu:
        st.markdown(f"""
        <div class='profile-card'>
        <div class='profile-label'>รหัสนักศึกษา:</div><div class='profile-value'>{clean_sid}</div>
        <div class='profile-label'>ระดับชั้น:</div><div style='margin-bottom:10px;'><span class='level-badge'>{current_level}</span></div>
        <div class='profile-label'>กลุ่ม:</div><div class='profile-value'>{row['grp_code']}</div>
        <div class='profile-label'>ครูที่ปรึกษา:</div><div class='profile-value'>{row['teacher_name'] or '-'}</div>
        </div><br>
        """, unsafe_allow_html=True)
        
        # เพิ่มเมนู "แบบทดสอบออนไลน์"
        selected = option_menu(None, ["รายวิชาและผลการเรียน", "ตารางสอบ", "กิจกรรม กพช.", "แบบทดสอบออนไลน์", "ห้องเรียนออนไลน์", "ติวเข้มออนไลน์"], 
            icons=["book", "calendar", "star", "pencil-square", "play-btn-fill", "cast"], default_index=0,
            styles={"container": {"padding": "0!important", "background-color": "transparent"}})
        
        st.markdown("<br>", unsafe_allow_html=True)
        if is_teacher_view:
            if st.button("⬅️ กลับหน้าหลักครู", use_container_width=True):
                st.session_state.view_mode = 'dashboard'
                st.rerun()
        else:
            if st.button("ออกจากระบบ", use_container_width=True): do_logout()

    with col_content:
        grades = pd.read_sql("SELECT * FROM grades WHERE std_id=?", conn, params=(clean_sid,))
        
        if selected == "รายวิชาและผลการเรียน":
            st.markdown(f"<div class='section-title'>📚 รายวิชาและผลการเรียน</div>", unsafe_allow_html=True)
            if not grades.empty:
                subjects = pd.read_sql("SELECT * FROM subjects", conn)
                grades['k'] = grades['sub_code'].str.replace('-','')
                subjects['k'] = subjects['sub_code'].str.replace('-','')
                m = pd.merge(grades, subjects[['k','sub_name']], on='k', how='left')
                m['sub_name'] = m['sub_name'].fillna(m['sub_code'])
                
                sems = sorted(m['semestry'].unique(), reverse=True)
                sem_sel = st.selectbox("เลือกปีการศึกษา:", sems)
                show = m[m['semestry'] == sem_sel][['sub_code', 'sub_name', 'grade']].rename(columns={'sub_code':'รหัส','sub_name':'วิชา','grade':'เกรด'})
                st.dataframe(styled_df(show), hide_index=True, use_container_width=True)
            else: st.info("ไม่มีข้อมูลผลการเรียน")

        elif selected == "ตารางสอบ":
            st.markdown("<div class='section-title'>🗓️ ตารางสอบปลายภาค</div>", unsafe_allow_html=True)
            schedule = pd.read_sql("SELECT * FROM schedule", conn)
            
            if not grades.empty:
                my_subs = grades['sub_code'].unique()
                my_sch = schedule[schedule['sub_code'].isin(my_subs)].copy()
                
                if not my_sch.empty:
                    sems = sorted(my_sch['semestry'].unique(), reverse=True)
                    sem_sel = st.selectbox("เลือกปีการศึกษา:", sems)
                    
                    graded_subs = grades[(grades['semestry'] == sem_sel) & (grades['grade'].str.strip() != '')]['sub_code'].tolist()
                    filtered_sch = my_sch[(my_sch['semestry'] == sem_sel) & (~my_sch['sub_code'].isin(graded_subs))].copy()
                    
                    if not filtered_sch.empty:
                        subjects = pd.read_sql("SELECT sub_code, sub_name FROM subjects", conn)
                        filtered_sch['k'] = filtered_sch['sub_code'].str.replace('-','')
                        subjects['k'] = subjects['sub_code'].str.replace('-','')
                        
                        full_sch = pd.merge(filtered_sch, subjects[['k','sub_name']], on='k', how='left')
                        full_sch['time'] = full_sch.apply(lambda x: f"{format_thai_time(x['exam_start'])}-{format_thai_time(x['exam_end'])}", axis=1)
                        
                        show = full_sch[['exam_day','time','sub_code','sub_name']].rename(columns={'exam_day':'วันสอบ','time':'เวลา','sub_code':'รหัส','sub_name':'วิชา'})
                        st.dataframe(styled_df(show), hide_index=True, use_container_width=True)
                    else:
                        st.success("✅ คุณสอบครบทุกวิชา หรือ ได้รับการตัดสินผลการเรียนครบแล้วในเทอมนี้")
                else: st.info("ไม่พบตารางสอบ")
            else: st.info("ไม่มีข้อมูลลงทะเบียนเรียน")

        elif selected == "กิจกรรม กพช.":
            st.markdown("<div class='section-title'>🚩 กิจกรรม กพช.</div>", unsafe_allow_html=True)
            acts = pd.read_sql("SELECT semestry, act_name, hours FROM activities WHERE std_id=?", conn, params=(clean_sid,))
            if not acts.empty:
                total_hrs = acts['hours'].sum()
                st.info(f"ชั่วโมงสะสมรวม: {total_hrs:,.1f} ชม.")
                acts['hours'] = acts['hours'].apply(lambda x: f"{float(x):.1f}")
                show_act = acts.rename(columns={'semestry':'เทอม','act_name':'กิจกรรม','hours':'ชม.'})
                st.dataframe(styled_df(show_act), hide_index=True, use_container_width=True)
            else: st.info("ไม่มีข้อมูลกิจกรรม")
        
        # --- ส่วนที่เพิ่ม: หน้าทำข้อสอบ ---
 # --- ส่วนที่แก้: หน้าทำข้อสอบ (Student View) ---
# --- ส่วนที่แก้: หน้าทำข้อสอบ (Final Fix: แยกโหมด List / Exam) ---
        elif selected == "แบบทดสอบออนไลน์":
            st.markdown("<div class='section-title'>📝 แบบทดสอบออนไลน์</div>", unsafe_allow_html=True)

            # ========================================================
            # 🅰️ MODE 1: กำลังทำข้อสอบ (แสดงหน้าข้อสอบอย่างเดียว)
            # ========================================================
            if 'doing_exam_id' in st.session_state:
                exam_id = st.session_state.doing_exam_id
                exam_name = st.session_state.get('doing_exam_name', 'แบบทดสอบ')
                
                st.markdown(f"### ✍️ กำลังทำ: {exam_name}")
                st.info("⚠️ ห้ามกด Refresh Browser ระหว่างทำข้อสอบ")

                questions = pd.read_sql("SELECT * FROM exam_questions WHERE exam_id=?", conn, params=(exam_id,))

                if questions.empty:
                    st.warning("❌ ข้อสอบนี้ยังไม่มีคำถาม")
                    if st.button("🔙 ย้อนกลับ"):
                        del st.session_state.doing_exam_id
                        st.rerun()
                else:
                    with st.form("exam_form_student"):
                        answers = {}
                        for q_idx, q in questions.iterrows():
                            st.markdown(f"**ข้อที่ {q_idx+1}:** {q['question_text']}")
                            opts = [q['choice_a'], q['choice_b'], q['choice_c'], q['choice_d']]
                            clean_opts = [o for o in opts if o and str(o).strip() != ""] # กรองช้อยส์ว่าง
                            
                            # ดึงคำตอบเดิมถ้ามี (กรณีหน้า refresh)
                            choice = st.radio(f"เลือกคำตอบข้อ {q_idx+1}", clean_opts, key=f"q_{q['id']}", index=None)
                            answers[q['id']] = choice
                            st.markdown("---")
                        
                        col_sub, col_cancel = st.columns([1, 1])
                        with col_sub:
                            if st.form_submit_button("📤 ส่งคำตอบ", type="primary"):
                                score = 0
                                answered_count = 0
                                total_q = len(questions)

                                # ตรวจคะแนน
                                for q_idx, q in questions.iterrows():
                                    user_ans = answers.get(q['id'])
                                    if user_ans: answered_count += 1
                                    
                                    correct_val = ""
                                    if q['correct_answer'] == 'A': correct_val = q['choice_a']
                                    elif q['correct_answer'] == 'B': correct_val = q['choice_b']
                                    elif q['correct_answer'] == 'C': correct_val = q['choice_c']
                                    elif q['correct_answer'] == 'D': correct_val = q['choice_d']
                                    
                                    # เทียบคำตอบ (ตัดช่องว่าง)
                                    if str(user_ans).strip() == str(correct_val).strip():
                                        score += 1

                                if answered_count < total_q:
                                    st.error(f"⚠️ คุณตอบไป {answered_count}/{total_q} ข้อ กรุณาตอบให้ครบ")
                                else:
                                    # บันทึกผล
                                    try:
                                        cur = conn.cursor()
                                        # ลบของเก่าออกก่อน (ถ้าเป็นการสอบแก้ตัว)
                                        cur.execute("DELETE FROM exam_results WHERE exam_id=? AND std_id=?", (exam_id, clean_sid))
                                        
                                        # ใส่ของใหม่
                                        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                                        cur.execute("""
                                            INSERT INTO exam_results (exam_id, std_id, score, total_score, timestamp) 
                                            VALUES (?, ?, ?, ?, ?)
                                        """, (exam_id, clean_sid, score, total_q, timestamp))
                                        conn.commit()
                                        
                                        st.balloons()
                                        st.success(f"🎉 บันทึกสำเร็จ! คุณได้ {score} / {total_q} คะแนน")
                                        time.sleep(3)
                                        
                                        # เคลียร์สถานะ เพื่อกลับหน้ารายการ
                                        del st.session_state.doing_exam_id
                                        st.rerun()
                                    except Exception as e:
                                        st.error(f"เกิดข้อผิดพลาดในการบันทึก: {e}")

                        with col_cancel:
                            if st.form_submit_button("❌ ยกเลิกการสอบ"):
                                del st.session_state.doing_exam_id
                                st.rerun()

            # ========================================================
            # 🅱️ MODE 2: หน้ารายการวิชา (จะทำงานก็ต่อเมื่อไม่ได้สอบอยู่)
            # ========================================================
            else:
                # 1. เช็คเกรด (Nuclear Filter)
                df_my_grades = pd.read_sql("SELECT sub_code, grade FROM grades WHERE std_id=?", conn, params=(clean_sid,))
                
                passed_subjects = set()
                registered_subjects = set() 
                debug_grade_info = {}

                if not df_my_grades.empty:
                    for _, row in df_my_grades.iterrows():
                        s_code = str(row['sub_code']).strip()
                        registered_subjects.add(s_code)
                        
                        g_val = row['grade']
                        if g_val is None: g_str = ""
                        else: g_str = str(g_val).strip()
                        
                        debug_grade_info[s_code] = g_str
                        
                        if g_str != "" and g_str.lower() != "nan" and g_str.lower() != "none":
                            passed_subjects.add(s_code)

                # 2. ดึงข้อสอบ
                active_exams = pd.read_sql("SELECT * FROM exams WHERE is_active=1", conn)

                # 3. แสดงผล
                count_show = 0
                if active_exams.empty:
                    st.info("ไม่พบแบบทดสอบในระบบ")
                else:
                    for idx, exam in active_exams.iterrows():
                        exam_sub_code = str(exam['sub_code']).strip()
                        
                        # กรอง: ต้องลงทะเบียน และ ยังไม่ผ่าน
                        is_registered = exam_sub_code in registered_subjects
                        is_passed = exam_sub_code in passed_subjects
                        
                        if is_registered and not is_passed:
                            count_show += 1
                            with st.expander(f"📘 {exam_sub_code} {exam['exam_name']}", expanded=True):
                                history = pd.read_sql("SELECT * FROM exam_results WHERE exam_id=? AND std_id=?", conn, params=(exam['exam_id'], clean_sid))
                                
                                col_info, col_btn = st.columns([3, 1])
                                with col_info:
                                    if not history.empty:
                                        score = history.iloc[0]['score']
                                        total = history.iloc[0]['total_score']
                                        st.warning(f"⚠️ เคยสอบแล้วเมื่อ: {history.iloc[0]['timestamp']}")
                                        st.metric("คะแนนล่าสุด", f"{score} / {total}")
                                    else:
                                        st.info("ยังไม่เคยทำข้อสอบนี้")

                                with col_btn:
                                    btn_label = "สอบแก้ตัว" if not history.empty else "เริ่มทำแบบทดสอบ"
                                    # 🔥 จุดสำคัญ: กดปุ่มแล้ว Set State และ Rerun ทันที
                                    if st.button(btn_label, key=f"start_{exam['exam_id']}", type="primary"):
                                        st.session_state.doing_exam_id = exam['exam_id']
                                        st.session_state.doing_exam_name = exam['exam_name']
                                        st.rerun()
                    
                    if count_show == 0:
                        st.success("🎉 คุณไม่มีรายวิชาที่ต้องสอบ (สอบครบ/ผ่านหมดแล้ว)")
                        with st.expander("ตรวจสอบสถานะเกรด (Debug)"):
                             st.write(debug_grade_info)
# ========================================================
        # ✅ ส่วนที่เพิ่ม: หน้าห้องเรียนออนไลน์
        # ========================================================
        elif selected == "ห้องเรียนออนไลน์":
            st.markdown("<div class='section-title'>📺 ห้องเรียนออนไลน์</div>", unsafe_allow_html=True)
            
            # 1. เช็ควิชาที่นักเรียนลงทะเบียน (จากตาราง grades)
            try:
                # ดึงวิชาที่นักเรียนคนนี้ลงทะเบียนเรียน
                my_grades_df = pd.read_sql("SELECT DISTINCT sub_code FROM grades WHERE std_id = ?", conn, params=(clean_sid,))
                my_subjects = my_grades_df['sub_code'].tolist()
                my_subjects = [str(s).strip() for s in my_subjects] # ตัดช่องว่าง
            except:
                my_subjects = []

            # 2. ดึงวิดีโอทั้งหมดจากระบบ
            try:
                all_videos = pd.read_sql("SELECT * FROM classroom_videos ORDER BY vid_id DESC", conn)
            except:
                all_videos = pd.DataFrame()
                st.warning("⚠️ ยังไม่พบตารางวิดีโอ (รอแอดมินอัปเดต)")

            if all_videos.empty:
                st.info("📭 ยังไม่มีวิดีโอการสอนในระบบ")
            else:
                # รายชื่อวิชาภาษาไทย (Mapping)
                subject_map_video = {
                    'ทช11001': 'เศรษฐกิจพอเพียง',
                    'พท11001': 'ภาษาไทย',
                    'พค11001': 'คณิตศาสตร์',
                    'พด11001': 'พลศึกษา/สุขศึกษา',
                    'อช11001': 'ช่องทางการเข้าสู่อาชีพ',
                    'สค11001': 'สังคมศึกษา',
                    'ทข11002': 'สุขศึกษา พลศึกษา'
                }
                
                count_visible = 0
                unique_subs_in_video = all_videos['sub_code'].unique()

                # วนลูปเช็คทีละวิชาที่มีวิดีโอ
                for sub_code in unique_subs_in_video:
                    clean_sub_code = str(sub_code).strip()
                    
                    # 🔥 กรอง: แสดงเฉพาะวิชาที่นักเรียนลงทะเบียนเรียนเท่านั้น
                    if clean_sub_code in my_subjects:
                        count_visible += 1
                        sub_name = subject_map_video.get(clean_sub_code, clean_sub_code)
                        
                        # สร้างกล่องรายวิชา (Expander)
                        with st.expander(f"📚 {clean_sub_code} : {sub_name}", expanded=False):
                            # ดึงวิดีโอเฉพาะวิชานี้ออกมาแสดง
                            sub_vids = all_videos[all_videos['sub_code'] == sub_code]
                            
                            for _, v_row in sub_vids.iterrows():
                                st.markdown(f"**📌 {v_row['topic_name']}**")
                                try:
                                    st.video(v_row['video_url'])
                                except:
                                    st.warning("รูปแบบวิดีโอไม่รองรับ")

                                # เพิ่มลิ้งก์เผื่อดูไม่ได้
                                st.markdown(f"👉 หากวิดีโอเล่นไม่ได้ [คลิกเพื่อดูบน YouTube]({v_row['video_url']})")
                                st.markdown("---")
                
                if count_visible == 0:
                    st.warning("⚠️ ไม่พบวิดีโอสำหรับรายวิชาที่คุณลงทะเบียนเรียน")
# ========================================================
        # ✅ ส่วนที่แก้: หน้าติวเข้มออนไลน์ (แบบมีย่อ-ขยาย)
        # ========================================================
        elif selected == "ติวเข้มออนไลน์":
            st.markdown("<div class='section-title'>🎯 ติวเข้มออนไลน์ (Tutoring)</div>", unsafe_allow_html=True)
            st.info("รวมคลิปติวเข้ม เนื้อหาพิเศษ และเตรียมสอบ N-NET")

            try:
                # ดึงข้อมูลวิดีโอติวเข้มทั้งหมด
                tutor_vids = pd.read_sql("SELECT * FROM tutoring_videos ORDER BY id DESC", conn)
            except:
                st.warning("⚠️ ยังไม่พบตารางข้อมูล (รอแอดมินอัปเดต)")
                tutor_vids = pd.DataFrame()

            if tutor_vids.empty:
                st.warning("📭 ยังไม่มีวิดีโอในขณะนี้")
            else:
                for _, row in tutor_vids.iterrows():
                    # ✨ ใช้ st.expander เพื่อเก็บวิดีโอไว้ข้างใน (กดแล้วค่อยยืดออกมา)
                    with st.expander(f"📺 {row['title']}", expanded=False):
                        
                        # แสดงคำอธิบาย (ถ้ามี)
                        if row['description']:
                            st.caption(f"📝 {row['description']}")
                        
                        # แสดงวิดีโอ
                        try:
                            st.video(row['video_url'])
                        except:
                            st.warning("รูปแบบวิดีโอไม่รองรับ")
                        
                        # ลิ้งก์สำรอง
                        st.markdown(f"👉 หากเล่นไม่ได้ [คลิกเพื่อดูบน YouTube]({row['video_url']})")
# ==========================================
# 5. Teacher Page
# ==========================================
# ==========================================
# 5. Teacher Page (แก้ไขแล้ว)
# ==========================================
# ==========================================
# 5. Teacher Page (แก้ไข: เลขเริ่มที่ 1 + ลบสีไฮไลต์)
# ==========================================
# ==========================================
# 5. Teacher Page (แก้ไข: ใช้ Dictionary แปลงชื่อวิชา แทนการดึงจาก DB)
# ==========================================
# ==========================================
# 5. Teacher Page (แก้ไข: เพิ่มค้นหาใน Tab 1 รายชื่อ)
# ==========================================
# ==========================================
# 5. Teacher Page (แก้ไข: ย้ายเมนูไป Sidebar)
# ==========================================
def teacher_page():
    # --- กำหนดค่าเริ่มต้น ---
    if 'view_mode' not in st.session_state: st.session_state.view_mode = 'list'
    if 'target_sid' not in st.session_state: st.session_state.target_sid = None
    
    # ถ้าอยู่ในโหมดดูรายละเอียด (Detail) ให้แสดงหน้านั้นเลย
    if st.session_state.view_mode == 'detail':
        view_data_page(st.session_state.target_sid, is_teacher_view=True)
        return

    conn = init_db()
    grp = st.session_state.assigned_group
    
    # --- ส่วนหัวข้อหลัก (Main Header) ---
    st.markdown(f"<div class='top-header'><h2>👨‍🏫 ครูที่ปรึกษา กลุ่ม: {grp}</h2><p>ยินดีต้อนรับคุณ {st.session_state.name}</p></div>", unsafe_allow_html=True)
    st.divider()

    # ==========================================
    # 🟢 โซน Sidebar (เมนูทางซ้าย)
    # ==========================================
    with st.sidebar:
        st.header("⚙️ เมนูจัดการ")
        
        # 1. ส่วนเลือกภาคเรียน (ย้ายมาไว้ข้างซ้าย)
        try:
            all_sems = pd.read_sql("SELECT DISTINCT semestry FROM grades ORDER BY semestry DESC", conn)
            sem_list = all_sems['semestry'].tolist()
        except:
            sem_list = []
            
        if not sem_list:
            st.warning("⚠️ ไม่พบข้อมูลภาคเรียน")
            return

        cur_sem = st.selectbox("📅 เลือกภาคเรียน", sem_list, index=0)
        
        st.markdown("---")
        
        # 2. เมนูเลือกหน้า (แทน Tabs เดิม)
        menu_option = st.radio(
            "เลือกรายการที่ต้องการดู:",
            ["👥 รายชื่อนักศึกษา", "📊 ตารางคะแนน (Matrix)"]
        )
        
        st.markdown("---")
        # (ปุ่มออกจากระบบ จะแสดงต่อท้ายจากตรงนี้โดยอัตโนมัติ ถ้าโค้ดหลักของคุณเขียนไว้ใน main)

    # ==========================================
    # 🟢 โซนแสดงผลเนื้อหา (Main Content)
    # ==========================================

    # --- กรณีเลือก: รายชื่อนักศึกษา ---
    if menu_option == "👥 รายชื่อนักศึกษา":
        st.subheader(f"👥 รายชื่อนักศึกษา (เทอม {cur_sem})")
        
        sql_active = """
            SELECT DISTINCT s.std_id, s.prefix, s.name, s.surname 
            FROM students s
            JOIN grades g ON s.std_id = g.std_id
            WHERE s.grp_code = ? AND g.semestry = ?
            ORDER BY s.std_id
        """
        std_list = pd.read_sql(sql_active, conn, params=(grp, cur_sem))
        
        if not std_list.empty:
            std_list['full_name'] = std_list['prefix'] + std_list['name'] + ' ' + std_list['surname']

            # สถิติ
            level_counts = {'ประถมศึกษา': 0, 'มัธยมศึกษาตอนต้น': 0, 'มัธยมศึกษาตอนปลาย': 0}
            for sid in std_list['std_id']:
                lvl = get_level_from_id(sid)
                if lvl in level_counts: level_counts[lvl] += 1
            
            c1, c2, c3 = st.columns(3)
            c1.info(f"ประถม: {level_counts['ประถมศึกษา']} คน")
            c2.info(f"ม.ต้น: {level_counts['มัธยมศึกษาตอนต้น']} คน")
            c3.info(f"ม.ปลาย: {level_counts['มัธยมศึกษาตอนปลาย']} คน")

            # ช่องค้นหา
            col_search, _ = st.columns([2, 2])
            with col_search:
                search_query = st.text_input("🔍 ค้นหา (ชื่อ/รหัส):", key="search_std_list")

            if search_query:
                std_list = std_list[
                    std_list['std_id'].astype(str).str.contains(search_query, case=False) |
                    std_list['full_name'].str.contains(search_query, case=False)
                ]
            
            st.write(f"แสดงผล: {len(std_list)} คน")
            st.markdown("---")
            
            # แสดงรายการ
            for _, row in std_list.iterrows():
                with st.container():
                    c1, c2, c3 = st.columns([1.5, 4, 1.5])
                    c1.write(f"**{row['std_id']}**")
                    c2.write(row['full_name'])
                    if c3.button("🔍 ดูข้อมูล", key=f"btn_{row['std_id']}"):
                        st.session_state.target_sid = row['std_id']
                        st.session_state.view_mode = 'detail'
                        st.rerun()
                    st.markdown("---")
        else:
            st.warning(f"ไม่พบนักศึกษาในกลุ่มนี้ ที่ลงทะเบียนเรียนในภาคเรียน {cur_sem}")

    # --- กรณีเลือก: ตารางคะแนน (Matrix) ---
    elif menu_option == "📊 ตารางคะแนน (Matrix)":
        st.subheader("📊 ตารางคะแนนรวม (Score Matrix)")
        
        try:
            sql_report = """
                SELECT 
                    s.std_id, 
                    s.prefix || s.name || ' ' || s.surname AS full_name,
                    e.sub_code,
                    r.score,
                    r.total_score
                FROM exam_results r
                JOIN students s ON r.std_id = s.std_id
                JOIN exams e ON r.exam_id = e.exam_id
                WHERE s.grp_code = ? 
            """
            df_scores = pd.read_sql(sql_report, conn, params=(grp,))

            if df_scores.empty:
                st.info("📭 ยังไม่มีข้อมูลการสอบของนักเรียนในกลุ่มนี้")
            else:
                # Mapping ชื่อวิชา
                subject_map = {
                    'ทช11001': 'เศรษฐกิจพอเพียง',
                    'พท11001': 'ภาษาไทย',
                    'พค11001': 'คณิตศาสตร์',
                    'พด11001': 'พลศึกษา/สุขศึกษา',
                    'สค12025': 'ลูกเสือ กศน',
                    'อช11001': 'ช่องทางการเข้าสู่อาชีพ',
                    'อช11002': 'ทักษะการประกอบอาชีพ',
                    'อช11003': 'พัฒนาอาชีพให้มีอยู่มีกิน',
                    'สค11001': 'สังคมศึกษา',
                    'สค11002': 'ศาสนาและหน้าที่พลเมือง',
                    'สค11003': 'การพัฒนาตนเอง',
                    'สค12010': 'ประชาธิปไตยในชุมชน',
                    'ทข11002': 'สุขศึกษา พลศึกษา',
                    'ทบ11002': 'สุขศึกษา พลศึกษา'
                }

                def get_sub_name(code):
                    return subject_map.get(code, code)

                df_scores['sub_name'] = df_scores['sub_code'].apply(get_sub_name)
                df_scores['subject_label'] = df_scores['sub_name'] + " (เต็ม " + df_scores['total_score'].astype(str) + ")"
                
                # Pivot & Search
                matrix_view = df_scores.pivot_table(
                    index=['std_id', 'full_name'],  
                    columns='subject_label',       
                    values='score',                
                    aggfunc='max'
                ).reset_index()

                matrix_view = matrix_view.rename(columns={'std_id': 'รหัสนักเรียน', 'full_name': 'ชื่อ-สกุล'})

                # ค้นหาในหน้าคะแนน
                col_search_score, _ = st.columns([2, 2])
                with col_search_score:
                    search_score = st.text_input("🔍 ค้นหาคะแนน (ชื่อ/รหัส):", key="search_score_matrix")

                if search_score:
                    mask = (
                        matrix_view['รหัสนักเรียน'].astype(str).str.contains(search_score, case=False) |
                        matrix_view['ชื่อ-สกุล'].astype(str).str.contains(search_score, case=False)
                    )
                    matrix_view = matrix_view[mask]

                matrix_view.index = range(1, len(matrix_view) + 1)
                
                st.write(f"แสดงข้อมูล: {len(matrix_view)} รายการ")
                
                st.dataframe(
                    matrix_view.style.format(precision=0), 
                    use_container_width=True 
                )

                csv = matrix_view.to_csv(index=False).encode('utf-8-sig')
                st.download_button("📥 ดาวน์โหลด (CSV)", csv, "scores.csv", "text/csv")

        except Exception as e:
            st.error(f"เกิดข้อผิดพลาด: {e}")

    with st.sidebar:
        st.divider()
        if st.button("🔴 ออกจากระบบ", use_container_width=True): 
            do_logout()
            
    conn.close()
    
# ==========================================
# 6. Admin Page (เพิ่ม Tab จัดการข้อสอบ)
# ==========================================
def admin_page():
    st.title("⚙️ Admin Panel")
    conn = init_db()
    
    # เพิ่ม Tab 5: จัดการข้อสอบ
    tab1, tab2, tab3, tab4, tab5, tab6, tab7, tab8 = st.tabs(["📊 ภาพรวม", "🔎 ค้นหาข้อมูล", "📤 นำเข้าข้อมูล", "🔑 รหัสผ่าน", "📝 จัดการข้อสอบ", "📈 รายงานผลสอบ","📺 จัดการห้องเรียน","🎯 ติวเข้ม"])
    
    try: cur_sem = conn.execute("SELECT MAX(semestry) FROM grades").fetchone()[0]
    except: cur_sem = "-"

    with tab1:
        st.info(f"📌 ภาคเรียนล่าสุด: {cur_sem}")
        n_std_active = 0
        n_tea_active = 0
        
        if cur_sem != "-":
            n_std_active = conn.execute("SELECT COUNT(DISTINCT std_id) FROM grades WHERE semestry=?", (cur_sem,)).fetchone()[0]
            sql_tea = "SELECT COUNT(DISTINCT s.grp_code) FROM students s JOIN grades g ON s.std_id = g.std_id WHERE g.semestry = ?"
            n_tea_active = conn.execute(sql_tea, (cur_sem,)).fetchone()[0]
        
        c1, c2 = st.columns(2)
        c1.metric("จำนวนครู (กลุ่มที่ Active)", f"{n_tea_active} คน")
        c2.metric(f"นักศึกษา (ลงทะเบียน {cur_sem})", f"{n_std_active} คน")
        
        st.divider()
        st.markdown(f"**📈 แยกตามระดับชั้น (เฉพาะที่ลงทะเบียน {cur_sem})**")
        
        if cur_sem != "-":
            sql_level = "SELECT s.level, COUNT(DISTINCT s.std_id) as cnt FROM students s JOIN grades g ON s.std_id = g.std_id WHERE g.semestry = ? GROUP BY s.level"
            level_df = pd.read_sql(sql_level, conn, params=(cur_sem,))
            
            if not level_df.empty:
                col1, col2, col3 = st.columns(3)
                v_pri = level_df[level_df['level']=='ประถมศึกษา']['cnt'].sum()
                v_mid = level_df[level_df['level']=='มัธยมศึกษาตอนต้น']['cnt'].sum()
                v_high = level_df[level_df['level']=='มัธยมศึกษาตอนปลาย']['cnt'].sum()
                
                col1.info(f"ประถม: {v_pri} คน")
                col2.info(f"ม.ต้น: {v_mid} คน")
                col3.info(f"ม.ปลาย: {v_high} คน")
            else: st.warning("ไม่มีข้อมูลการลงทะเบียนในเทอมล่าสุด")

    with tab2:
        st.markdown("#### 🔍 ค้นหาข้อมูลครูและนักศึกษา")
        search_type = st.radio("เลือกประเภทข้อมูล:", ["นักศึกษา", "ครูที่ปรึกษา"], horizontal=True)
        search_kw = st.text_input("พิมพ์ชื่อ หรือ รหัส เพื่อค้นหา...", "")
        
        if search_kw:
            if search_type == "นักศึกษา":
                q = f"%{search_kw}%"
                sql = "SELECT std_id, prefix, name, surname, grp_code, level FROM students WHERE std_id LIKE ? OR name LIKE ? OR surname LIKE ?"
                res = pd.read_sql(sql, conn, params=(q, q, q))
                if not res.empty:
                    res['level'] = res.apply(lambda x: x['level'] if x['level'] else get_level_from_id(x['std_id']), axis=1)
                    st.dataframe(res.rename(columns={'std_id':'รหัส','name':'ชื่อ','surname':'นามสกุล','grp_code':'กลุ่ม','level':'ระดับ'}), use_container_width=True, hide_index=True)
                else: st.warning("ไม่พบข้อมูล")
            else:
                q = f"%{search_kw}%"
                sql = "SELECT grp_code, teacher_name FROM groups WHERE grp_code LIKE ? OR teacher_name LIKE ?"
                res = pd.read_sql(sql, conn, params=(q, q))
                if not res.empty:
                    st.dataframe(res.rename(columns={'grp_code':'รหัสกลุ่ม','teacher_name':'ชื่อครู'}), use_container_width=True, hide_index=True)
                else: st.warning("ไม่พบข้อมูล")
        else:
            st.caption("แสดงทั้งหมด (สูงสุด 50 รายการ)")
            if search_type == "นักศึกษา":
                res = pd.read_sql("SELECT std_id, name, surname, grp_code, level FROM students LIMIT 50", conn)
                st.dataframe(res, use_container_width=True, hide_index=True)
            else:
                res = pd.read_sql("SELECT * FROM groups LIMIT 50", conn)
                st.dataframe(res, use_container_width=True, hide_index=True)

    with tab3:
        st.info("อัปโหลดไฟล์ ZIP (ข้อมูลจะถูกบันทึกทับของเดิม)")
        uploaded = st.file_uploader("Upload ZIP", type='zip')
        if uploaded and st.button("เริ่มนำเข้าข้อมูล", type="primary"):
            progress = st.progress(0); status = st.empty()
            try:
                c = conn.cursor()
                for t in ['grades', 'schedule', 'subjects', 'activities', 'students', 'groups']: c.execute(f"DELETE FROM {t}")
                c.execute("DELETE FROM users WHERE role != 'admin'")
                conn.commit()
                
                with zipfile.ZipFile(uploaded) as z:
                    files = [f for f in z.namelist() if f.lower().endswith('.dbf')]
                    d_std, d_grd, d_sch, d_sub, d_act, d_grp, users = [], [], [], [], [], [], []
                    
                    for i, fname in enumerate(files):
                        progress.progress((i+1)/len(files))
                        status.text(f"Processing: {fname}...")
                        
                        with tempfile.NamedTemporaryFile(delete=False) as tmp: 
                            tmp.write(z.read(fname)); tmp_path = tmp.name
                        df = read_dbf_robust(tmp_path)
                        try: os.remove(tmp_path)
                        except: pass
                        if df.empty: continue
                        
                        fn = fname.lower()
                        if 'student' in fn or 'reg' in fn:
                            for _, r in df.iterrows():
                                sid = clean_id_card(str(r.get('STD_CODE', r.get('ID',''))))[-10:]
                                if sid:
                                    lvl = get_level_from_id(sid) 
                                    d_std.append((sid, r.get('PRENAME',''), r.get('NAME',''), r.get('SURNAME',''), r.get('GRP_CODE',''), str(r.get('PHONE','')), clean_id_card(r.get('CARDID','')), lvl))
                        elif 'grade' in fn:
                            for _, r in df.iterrows():
                                sid = clean_id_card(str(r.get('STD_CODE','')))[-10:]
                                d_grd.append((sid, str(r.get('SUB_CODE','')).strip(), str(r.get('SEMESTRY','')), str(r.get('GRADE','')), str(r.get('GRP_CODE',''))))
                        elif 'activit' in fn:
                            for _, r in df.iterrows():
                                sid = clean_id_card(str(r.get('STD_CODE','')))[-10:]
                                aname = str(r.get('ACT_NAME', r.get('ACTIVITY', r.get('NAME', ''))))
                                d_act.append((sid, str(r.get('SEMESTRY','')), aname, 'กพช.', r.get('HOUR',0)))
                        elif 'group' in fn:
                            for _, r in df.iterrows():
                                gc, tn = str(r.get('GRP_CODE','')).strip(), str(r.get('TEACHER_NAME', r.get('GRP_ADVIS',''))).strip()
                                d_grp.append((gc, tn))
                                users.append((gc, gc, 'teacher', tn, gc))
                        elif 'schedule' in fn:
                            for _, r in df.iterrows(): d_sch.append((str(r.get('SUB_CODE','')), str(r.get('SEMESTRY','')), str(r.get('EXAM_DAY','')), str(r.get('EXAM_START','')), str(r.get('EXAM_END',''))))
                        elif 'subject' in fn:
                            for _, r in df.iterrows(): d_sub.append((str(r.get('SUB_CODE','')), str(r.get('SUB_NAME',''))))

                c.executemany("INSERT OR REPLACE INTO students VALUES (?,?,?,?,?,?,?,?)", d_std)
                c.executemany("INSERT INTO grades VALUES (?,?,?,?,?)", d_grd)
                c.executemany("INSERT INTO schedule VALUES (?,?,?,?,?)", d_sch)
                c.executemany("INSERT OR REPLACE INTO subjects VALUES (?,?)", d_sub)
                c.executemany("INSERT INTO activities VALUES (?,?,?,?,?)", d_act)
                c.executemany("INSERT OR REPLACE INTO groups VALUES (?,?)", d_grp)
                c.executemany("INSERT OR IGNORE INTO users VALUES (?,?,?,?,?)", users)
                conn.commit()
                
                status.success("✅ นำเข้าข้อมูลสำเร็จ! ระบบจะรีเฟรชใน 2 วินาที...")
                time.sleep(2) 
                st.rerun()
                
            except Exception as e: st.error(f"Error: {e}")

    with tab4:
        st.markdown("#### 🔐 รีเซ็ตรหัสผ่าน")
        with st.form("reset"):
            u = st.text_input("Username")
            p = st.text_input("New Password", type="password")
            if st.form_submit_button("Submit"):
                if conn.execute("SELECT * FROM users WHERE username=?", (u,)).fetchone():
                    conn.execute("UPDATE users SET password=? WHERE username=?", (p, u))
                    conn.commit(); st.success("Success")
                else: st.error("User not found")
    
    # --- ส่วนที่เพิ่ม: หน้าจัดการข้อสอบ ---
    with tab5:
        st.markdown("#### 📝 จัดการข้อสอบ")

        # --- ส่วนที่ 1: Master Switch (เปิด-ปิด ทั้งระบบ) ---
        st.warning("🎮 **Control Center:** ควบคุมสถานะการสอบทุกวิชาพร้อมกัน")
        c_master1, c_master2 = st.columns(2)
        with c_master1:
            if st.button("🟢 เปิดสอบทุกวิชา (Open All)", use_container_width=True):
                conn.execute("UPDATE exams SET is_active = 1")
                conn.commit()
                st.success("เปิดระบบสอบทุกวิชาแล้ว!")
                time.sleep(1)
                st.rerun()
        with c_master2:
            if st.button("🔴 ปิดสอบทุกวิชา (Close All)", type="primary", use_container_width=True):
                conn.execute("UPDATE exams SET is_active = 0")
                conn.commit()
                st.error("ปิดระบบสอบทุกวิชาแล้ว!")
                time.sleep(1)
                st.rerun()
        st.divider()
        # -----------------------------------------------

        c1, c2 = st.columns([1, 2])
        
        # --- Column 1: สร้างและเลือกข้อสอบ ---
        with c1:
            st.write("**1. สร้างชุดข้อสอบใหม่**")
            all_subs = pd.read_sql("SELECT sub_code, sub_name FROM subjects", conn)
            
            if not all_subs.empty:
                all_subs['display'] = all_subs['sub_code'] + " - " + all_subs['sub_name']
                selected_sub = st.selectbox("เลือกรายวิชา", all_subs['display'])
                sel_sub_code = selected_sub.split(" - ")[0]
            else:
                st.error("ไม่พบฐานข้อมูลรายวิชา")
                sel_sub_code = None

            exam_name = st.text_input("ชื่อชุดข้อสอบ (เช่น สอบกลางภาค)")
            exam_sem = st.text_input("ปีการศึกษา", value=cur_sem)

            if st.button("สร้างข้อสอบ", type="primary"):
                if exam_name and sel_sub_code and exam_sem:
                    conn.execute("INSERT INTO exams (exam_name, sub_code, semestry, is_active) VALUES (?, ?, ?, 0)", 
                                 (f"{sel_sub_code} {exam_name}", sel_sub_code, exam_sem))
                    conn.commit()
                    st.success(f"สร้างข้อสอบ {sel_sub_code} เรียบร้อย!")
                    time.sleep(0.5)
                    st.rerun()

            st.divider()
            st.write("**2. เลือกข้อสอบเพื่อจัดการ**")
            exams = pd.read_sql("SELECT * FROM exams ORDER BY exam_id DESC", conn)
            
            if not exams.empty:
                def fmt_exam(x):
                    row = exams[exams['exam_id'] == x].iloc[0]
                    status = "🟢 ON" if row['is_active'] else "🔴 OFF"
                    return f"{status} | {row['sub_code']} {row['exam_name']}"

                sel_exam_id = st.selectbox("เลือกข้อสอบ:", exams['exam_id'], format_func=fmt_exam)
                
                # ปุ่มลบข้อสอบทั้งชุด
                if st.button("🗑️ ลบชุดข้อสอบนี้ทิ้ง", type="secondary", use_container_width=True):
                    conn.execute("DELETE FROM exams WHERE exam_id=?", (sel_exam_id,))
                    conn.execute("DELETE FROM exam_questions WHERE exam_id=?", (sel_exam_id,))
                    conn.execute("DELETE FROM exam_results WHERE exam_id=?", (sel_exam_id,))
                    conn.commit()
                    st.rerun()
            else:
                sel_exam_id = None
                st.info("ยังไม่มีชุดข้อสอบ")

        # --- Column 2: จัดการคำถามในข้อสอบ ---
        with c2:
            if sel_exam_id:
                curr_exam = exams[exams['exam_id']==sel_exam_id].iloc[0]
                status_text = "🟢 กำลังเปิดสอบ" if curr_exam['is_active'] else "🔴 ปิดสอบอยู่"
                st.info(f"⚙️ แก้ไขข้อสอบ: **{curr_exam['sub_code']} ({curr_exam['semestry']})** | สถานะ: {status_text}")
                
                st.divider()
                st.write("📥 **Import ข้อสอบจาก Excel**")
                st.info("รูปแบบไฟล์: Column ต้องชื่อ `Question`, `A`, `B`, `C`, `D`, `Correct` (เฉลย A/B/C/D)")
                
                up_exam = st.file_uploader("เลือกไฟล์ Excel (.xlsx)", type=['xlsx'])
                
                if up_exam and st.button("ยืนยันนำเข้าข้อมูล", type="primary"):
                    try:
                        df_ex = pd.read_excel(up_exam)
                        # เช็คชื่อคอลัมน์
                        req_cols = ['Question', 'A', 'B', 'C', 'D', 'Correct']
                        if all(col in df_ex.columns for col in req_cols):
                            count = 0
                            for _, r in df_ex.iterrows():
                                # แปลงทุกอย่างเป็น String ป้องกัน Error
                                q_text = str(r['Question'])
                                ca = str(r['A'])
                                cb = str(r['B'])
                                cc = str(r['C'])
                                cd = str(r['D'])
                                corr = str(r['Correct']).upper().strip() # ทำให้เป็นตัวใหญ่ A,B,C,D
                                
                                conn.execute("""INSERT INTO exam_questions 
                                                (exam_id, question_text, choice_a, choice_b, choice_c, choice_d, correct_answer) 
                                                VALUES (?,?,?,?,?,?,?)""", 
                                             (sel_exam_id, q_text, ca, cb, cc, cd, corr))
                                count += 1
                            conn.commit()
                            st.success(f"นำเข้าเรียบร้อย {count} ข้อ")
                            time.sleep(1)
                            st.rerun()
                        else:
                            st.error(f"ชื่อคอลัมน์ไม่ถูกต้อง ต้องมี: {req_cols}")
                    except Exception as e:
                        st.error(f"เกิดข้อผิดพลาด: {e}")
                
                # --- ส่วนเพิ่มคำถาม (Manual) ---
                with st.expander("➕ เพิ่มคำถามใหม่ (Manual)", expanded=False):
                    with st.form("add_q"):
                        q_text = st.text_area("โจทย์คำถาม")
                        c_a, c_b = st.columns(2)
                        choice_a = c_a.text_input("ตัวเลือก A")
                        choice_b = c_b.text_input("ตัวเลือก B")
                        choice_c = c_a.text_input("ตัวเลือก C")
                        choice_d = c_b.text_input("ตัวเลือก D")
                        correct = st.selectbox("เฉลย", ["A", "B", "C", "D"])
                        
                        if st.form_submit_button("บันทึกคำถาม"):
                            conn.execute("INSERT INTO exam_questions (exam_id, question_text, choice_a, choice_b, choice_c, choice_d, correct_answer) VALUES (?,?,?,?,?,?,?)",
                                         (sel_exam_id, q_text, choice_a, choice_b, choice_c, choice_d, correct))
                            conn.commit()
                            st.success("เพิ่มแล้ว")
                            st.rerun()

                # --- ส่วนแสดงรายการคำถาม (แก้ไข/ลบ ทีละข้อ) ---
                qs = pd.read_sql("SELECT * FROM exam_questions WHERE exam_id=?", conn, params=(sel_exam_id,))
                
                if not qs.empty:
                    st.write(f"📝 **รายการคำถามทั้งหมด ({len(qs)} ข้อ)**")
                    
                    # วนลูปแสดงทีละข้อ เพื่อให้แก้ไขได้
                    for index, row in qs.iterrows():
                        # ใช้ Expander ให้คลิกเพื่อเปิดแก้
                        with st.expander(f"ข้อที่ {index+1}: {row['question_text'][:50]}..."):
                            with st.form(key=f"edit_q_{row['id']}"):
                                new_q = st.text_area("แก้ไขโจทย์", value=row['question_text'])
                                ec1, ec2 = st.columns(2)
                                new_a = ec1.text_input("A", value=row['choice_a'])
                                new_b = ec2.text_input("B", value=row['choice_b'])
                                new_c = ec1.text_input("C", value=row['choice_c'])
                                new_d = ec2.text_input("D", value=row['choice_d'])
                                new_correct = st.selectbox("เฉลย", ["A", "B", "C", "D"], index=["A","B","C","D"].index(row['correct_answer']))
                                
                                c_btn1, c_btn2 = st.columns(2)
                                with c_btn1:
                                    if st.form_submit_button("💾 บันทึกการแก้ไข"):
                                        conn.execute("""UPDATE exam_questions SET 
                                                        question_text=?, choice_a=?, choice_b=?, choice_c=?, choice_d=?, correct_answer=? 
                                                        WHERE id=?""", 
                                                     (new_q, new_a, new_b, new_c, new_d, new_correct, row['id']))
                                        conn.commit()
                                        st.success("แก้ไขเรียบร้อย")
                                        time.sleep(0.5)
                                        st.rerun()
                                with c_btn2:
                                    if st.form_submit_button("🗑️ ลบข้อนี้", type="primary"):
                                        conn.execute("DELETE FROM exam_questions WHERE id=?", (row['id'],))
                                        conn.commit()
                                        st.warning("ลบแล้ว")
                                        time.sleep(0.5)
                                        st.rerun()
                else:
                    st.info("ยังไม่มีคำถามในชุดนี้")
# --- ส่วนที่เพิ่ม: Tab 6 รายงานผลสอบรวม + สถิติสรุป ---
    with tab6:
        st.subheader("📊 สรุปสถิติการเข้าสอบแบบละเอียด")
        
        # 1. Auto-Detect Column Name
        target_col = None
        try:
            test_df = pd.read_sql("SELECT * FROM grades LIMIT 1", conn)
            if 'term' in test_df.columns: target_col = 'term'
            elif 'semestry' in test_df.columns: target_col = 'semestry'
        except: pass

        if not target_col:
             st.error("⚠️ ไม่พบคอลัมน์ระบุภาคเรียน (term/semestry)")
             st.stop()
        
        # 2. เลือกภาคเรียน
        try:
            all_terms = pd.read_sql(f"SELECT DISTINCT {target_col} FROM grades ORDER BY {target_col} DESC", conn)
            term_options = all_terms[target_col].dropna().tolist()
        except: term_options = []
            
        if not term_options:
            st.warning("⚠️ ไม่พบข้อมูลการลงทะเบียนเรียน")
        else:
            c_sel, _ = st.columns([1, 3])
            with c_sel:
                selected_term = st.selectbox("📅 เลือกภาคเรียน", term_options, index=0)
            
            # -------------------------------------------------------------
            # 🔥 CORE LOGIC ใหม่: ดึงรหัสวิชา (sub_code) มาช่วยแยกระดับชั้น
            # -------------------------------------------------------------
            # ดึง std_id, grp_code และ sub_code (เอามาแค่วิชาเดียวต่อคนก็พอ เพื่อเช็คระดับ)
            sql_active = f"""
                SELECT s.std_id, s.grp_code, g.sub_code
                FROM students s
                JOIN grades g ON s.std_id = g.std_id
                WHERE g.{target_col} = ?
                GROUP BY s.std_id  -- 1 คน เอามา 1 แถวพอ (ลดความซ้ำซ้อน)
            """
            df_active = pd.read_sql(sql_active, conn, params=(selected_term,))
            
            # ดึงข้อมูลคนเข้าสอบ
            submitted_ids = set(pd.read_sql("SELECT DISTINCT std_id FROM exam_results", conn)['std_id'].astype(str))
            
            # ดึงรายชื่อครู
            teachers = pd.read_sql("SELECT name, assigned_group FROM users WHERE role='teacher'", conn)
            teacher_map = dict(zip(teachers['assigned_group'], teachers['name']))

            if not df_active.empty:
                df_active['std_id'] = df_active['std_id'].astype(str).str.strip()
                
                # --- ฟังก์ชันแยกระดับชั้นจาก "รหัสวิชา" (แม่นยำกว่ารหัสนักศึกษา) ---
                def get_level_code(sub_code):
                    if not isinstance(sub_code, str): return 'Unknown'
                    # หาตัวเลขแรกที่เจอในรหัสวิชา (เช่น ทร21001 -> เจอเลข 2)
                    import re
                    match = re.search(r'\d', sub_code)
                    if match:
                        digit = match.group(0)
                        if digit == '1': return '1' # ประถม
                        if digit == '2': return '2' # ม.ต้น
                        if digit == '3': return '3' # ม.ปลาย
                    return 'Unknown'

                # สร้างคอลัมน์ Level ใหม่ใน DataFrame เลย
                df_active['level_id'] = df_active['sub_code'].apply(get_level_code)

                # --- A. Dashboard ภาพรวม ---
                total_std = len(df_active)
                total_att = df_active['std_id'].apply(lambda x: 1 if x in submitted_ids else 0).sum()
                total_abs = total_std - total_att
                percent = (total_att / total_std * 100) if total_std > 0 else 0
                
                st.markdown(f"### 📌 ภาพรวมประจำเทอม {selected_term}")
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("นศ. ลงทะเบียน", f"{total_std:,}", "คน")
                m2.metric("เข้าสอบแล้ว", f"{total_att:,}", "คน")
                m3.metric("ขาดสอบ", f"{total_abs:,}", "คน")
                m4.metric("ร้อยละการเข้าสอบ", f"{percent:.2f}%")
                
                st.divider()

                # --- B. ตารางแยกรายกลุ่ม (คำนวณใหม่) ---
                stats_data = []
                active_groups = sorted(df_active['grp_code'].dropna().unique())
                
                for grp in active_groups:
                    g_students = df_active[df_active['grp_code'] == grp]
                    if g_students.empty: continue
                        
                    t_name = teacher_map.get(grp, "(ไม่พบข้อมูลครู)")
                    row = {"กลุ่มเรียน": grp, "ครูที่ปรึกษา": t_name}
                    
                    # ฟังก์ชันนับตาม Level ID ที่เราสร้างไว้
                    def count_by_level(lvl_id):
                        subset = g_students[g_students['level_id'] == lvl_id]
                        tot = len(subset)
                        att = subset['std_id'].apply(lambda x: 1 if x in submitted_ids else 0).sum()
                        ab = tot - att
                        return tot, att, ab

                    # นับแยกชั้น (ดูจากรหัสวิชา)
                    p_tot, p_att, p_abs = count_by_level('1')  # ประถม
                    m1_tot, m1_att, m1_abs = count_by_level('2') # ม.ต้น
                    m2_tot, m2_att, m2_abs = count_by_level('3') # ม.ปลาย
                    
                    row.update({
                        'ประถม-ทั้งหมด': p_tot, 'ประถม-เข้าสอบ': p_att, 'ประถม-ขาดสอบ': p_abs,
                        'ม.ต้น-ทั้งหมด': m1_tot, 'ม.ต้น-เข้าสอบ': m1_att, 'ม.ต้น-ขาดสอบ': m1_abs,
                        'ม.ปลาย-ทั้งหมด': m2_tot, 'ม.ปลาย-เข้าสอบ': m2_att, 'ม.ปลาย-ขาดสอบ': m2_abs
                    })
                    
                    # รวมกลุ่ม (นับจาก g_students ตรงๆ เพื่อกันพลาดกรณี Unknown Level)
                    g_tot = len(g_students)
                    g_att = g_students['std_id'].apply(lambda x: 1 if x in submitted_ids else 0).sum()
                    g_abs = g_tot - g_att
                    g_per = (g_att / g_tot * 100) if g_tot > 0 else 0
                    
                    row.update({
                        'รวม-ทั้งหมด': g_tot, 
                        'รวม-เข้าสอบ': g_att, 
                        'รวม-ขาดสอบ': g_abs, 
                        'ร้อยละ(%)': f"{g_per:.2f}%"
                    })
                    
                    stats_data.append(row)
                
                if stats_data:
                    df_stats = pd.DataFrame(stats_data)
                    # เลือกแสดงคอลัมน์
                    cols = ["กลุ่มเรียน", "ครูที่ปรึกษา", 
                            "ประถม-ทั้งหมด", "ประถม-เข้าสอบ", "ประถม-ขาดสอบ",
                            "ม.ต้น-ทั้งหมด", "ม.ต้น-เข้าสอบ", "ม.ต้น-ขาดสอบ",
                            "ม.ปลาย-ทั้งหมด", "ม.ปลาย-เข้าสอบ", "ม.ปลาย-ขาดสอบ",
                            "รวม-ทั้งหมด", "รวม-เข้าสอบ", "รวม-ขาดสอบ", "ร้อยละ(%)"]
                    final_cols = [c for c in cols if c in df_stats.columns]
                    
                    st.markdown("### 📋 รายละเอียดรายกลุ่ม")
                    st.dataframe(df_stats[final_cols], use_container_width=True, hide_index=True)
                    
                    csv = df_stats[final_cols].to_csv(index=False).encode('utf-8-sig')
                    st.download_button("📥 ดาวน์โหลด (CSV)", csv, f"Report_{selected_term.replace('/','-')}.csv")
            else:
                st.warning(f"ไม่พบนักศึกษาลงทะเบียนในเทอม {selected_term}")

            # --- C. ตารางคะแนนรายบุคคล ---
            st.divider()
            st.subheader("📈 คะแนนสอบรายบุคคล (Filtered)")
            search_res = st.text_input("🔎 กรองข้อมูล:", "")
            
            sql_report = f"""
                SELECT 
                    r.timestamp, r.std_id, 
                    s.prefix || s.name || ' ' || s.surname as fullname,
                    s.grp_code, e.sub_code, e.exam_name, r.score, r.total_score
                FROM exam_results r
                JOIN students s ON r.std_id = s.std_id
                JOIN grades g ON r.std_id = g.std_id
                LEFT JOIN exams e ON r.exam_id = e.exam_id
                WHERE g.{target_col} = ?
                GROUP BY r.id
                ORDER BY r.timestamp DESC
            """
            try:
                df_report = pd.read_sql(sql_report, conn, params=(selected_term,))
                if not df_report.empty:
                    df_report.columns = ['เวลาส่ง', 'รหัสนักศึกษา', 'ชื่อ-นามสกุล', 'กลุ่ม', 'รหัสวิชา', 'ชื่อข้อสอบ', 'คะแนน', 'คะแนนเต็ม']
                    if search_res:
                        mask = df_report.astype(str).apply(lambda x: x.str.contains(search_res, case=False)).any(axis=1)
                        df_report = df_report[mask]
                    
                    df_report.insert(0, 'ลำดับ', range(1, len(df_report) + 1))
                    st.dataframe(df_report, use_container_width=True, hide_index=True)
                else:
                    st.info("ยังไม่มีข้อมูลการสอบในเทอมนี้")
            except Exception as e:
                st.error(f"Error: {e}")
    with tab7:
        st.subheader("📺 จัดการวิดีโอการสอน (Online Classroom)")
        
        # ✅ แก้ตรงนี้: เชื่อมต่อฐานข้อมูลใหม่ใน Tab นี้เลย ป้องกัน Error หาตัวแปร c ไม่เจอ
        conn = init_db()
        c = conn.cursor()
        
        # ฟอร์มเพิ่มวิดีโอ
        with st.expander("➕ เพิ่มวิดีโอใหม่", expanded=True):
            with st.form("add_video_form_tab"):
                # รายชื่อวิชา
                subject_map_video = {
                    'ทช11001': 'เศรษฐกิจพอเพียง',
                    'พท11001': 'ภาษาไทย',
                    'พค11001': 'คณิตศาสตร์',
                    'พด11001': 'พลศึกษา/สุขศึกษา',
                    'อช11001': 'ช่องทางการเข้าสู่อาชีพ',
                    'สค11001': 'สังคมศึกษา',
                    'ทข11002': 'สุขศึกษา พลศึกษา'
                }
                sub_opts = [f"{k} : {v}" for k, v in subject_map_video.items()]
                
                c_vid1, c_vid2 = st.columns(2)
                with c_vid1:
                    sel_sub_full = st.selectbox("เลือกวิชา", sub_opts)
                    # ตัดเอาแค่รหัสวิชา (ตัวหน้าก่อนเครื่องหมาย :)
                    sel_sub_code = sel_sub_full.split(":")[0].strip()
                with c_vid2:
                    topic = st.text_input("ชื่อเรื่อง / หัวข้อ")
                
                url = st.text_input("ลิงก์ YouTube (URL)")
                
                if st.form_submit_button("บันทึกวิดีโอ"):
                    if topic and url:
                        try:
                            # ตรวจสอบก่อนว่ามีตาราง classroom_videos ไหม
                            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='classroom_videos'")
                            if not c.fetchone():
                                # ถ้าไม่มี ให้สร้างตรงนี้เลย (กันเหนียว)
                                c.execute("""
                                    CREATE TABLE IF NOT EXISTS classroom_videos (
                                        vid_id INTEGER PRIMARY KEY AUTOINCREMENT,
                                        sub_code TEXT,
                                        topic_name TEXT,
                                        video_url TEXT,
                                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                                    )
                                """)
                                conn.commit()

                            # บันทึกข้อมูล
                            c.execute("INSERT INTO classroom_videos (sub_code, topic_name, video_url) VALUES (?,?,?)",
                                      (sel_sub_code, topic, url))
                            conn.commit()
                            st.success("✅ บันทึกวิดีโอเรียบร้อย")
                            time.sleep(1) # รอสักนิดแล้วรีเฟรช
                            st.rerun()
                        except Exception as e:
                            st.error(f"เกิดข้อผิดพลาด: {e}")
                    else:
                        st.warning("กรุณากรอกข้อมูลให้ครบ")

        st.divider()

        # แสดงรายการวิดีโอ
        st.write("📋 รายการวิดีโอที่มีในระบบ")
        try:
            videos = pd.read_sql("SELECT * FROM classroom_videos ORDER BY vid_id DESC", conn)
            
            if not videos.empty:
                for _, row in videos.iterrows():
                    with st.container():
                        c1, c2, c3 = st.columns([1.5, 4, 1])
                        # แสดงตัวอย่าง
                        try:
                            c1.video(row['video_url'])
                        except:
                            c1.error("ลิงก์วิดีโอไม่ถูกต้อง")
                        
                        # แสดงข้อมูล
                        # หาชื่อวิชาจาก Dictionary
                        sub_name_show = subject_map_video.get(row['sub_code'], row['sub_code'])
                        c2.write(f"**{row['sub_code']} {sub_name_show}**")
                        c2.write(f"📌 {row['topic_name']}")
                        c2.caption(f"URL: {row['video_url']}")
                        
                        # ปุ่มลบ
                        if c3.button("🗑️ ลบ", key=f"del_vid_tab_{row['vid_id']}"):
                            c.execute("DELETE FROM classroom_videos WHERE vid_id = ?", (row['vid_id'],))
                            conn.commit()
                            st.rerun()
                        st.markdown("---")
            else:
                st.info("ยังไม่มีวิดีโอ")
        except Exception as e:
             # กรณี Database ยังไม่มีตารางเลย
             st.warning("⚠️ ยังไม่พบตารางข้อมูลวิดีโอ (ระบบจะสร้างให้อัตโนมัติเมื่อคุณกดบันทึกวิดีโอแรก)")
    # ---------------------------------------------------------
    # Tab 8: จัดการติวเข้ม (อิสระ ไม่ผูกรายวิชา)
    # ---------------------------------------------------------
    with tab8:
        st.subheader("🎯 จัดการวิดีโอติวเข้ม (Intensive Tutoring)")
        conn = init_db()
        c = conn.cursor()

        # สร้างตาราง tutoring_videos อัตโนมัติถ้ายังไม่มี
        c.execute("""
            CREATE TABLE IF NOT EXISTS tutoring_videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                video_url TEXT,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

        # ฟอร์มเพิ่มวิดีโอ
        with st.expander("➕ เพิ่มวิดีโอติวเข้มใหม่", expanded=True):
            with st.form("add_tutor_video"):
                t_title = st.text_input("หัวข้อการติว (เช่น ติว N-NET, ติวเข้มก่อนสอบ)")
                t_desc = st.text_area("รายละเอียดเพิ่มเติม (ถ้ามี)")
                t_url = st.text_input("ลิงก์ YouTube")
                
                if st.form_submit_button("บันทึก"):
                    if t_title and t_url:
                        c.execute("INSERT INTO tutoring_videos (title, video_url, description) VALUES (?,?,?)",
                                  (t_title, t_url, t_desc))
                        conn.commit()
                        st.success("✅ บันทึกเรียบร้อย")
                        st.rerun()
                    else:
                        st.warning("กรุณากรอกหัวข้อและลิงก์")

        st.divider()

        # แสดงรายการ
        st.write("📋 รายการวิดีโอติวเข้มทั้งหมด")
        t_videos = pd.read_sql("SELECT * FROM tutoring_videos ORDER BY id DESC", conn)
        
        if not t_videos.empty:
            for _, row in t_videos.iterrows():
                with st.container():
                    c1, c2 = st.columns([2, 3])
                    # แสดงวิดีโอ
                    try:
                        c1.video(row['video_url'])
                    except:
                        c1.error("ลิงก์ไม่ถูกต้อง")
                    
                    # ข้อมูล + ปุ่มลบ
                    c2.markdown(f"#### {row['title']}")
                    if row['description']:
                        c2.info(row['description'])
                    
                    if c2.button("🗑️ ลบวิดีโอนี้", key=f"del_tutor_{row['id']}"):
                        c.execute("DELETE FROM tutoring_videos WHERE id=?", (row['id'],))
                        conn.commit()
                        st.rerun()
                st.markdown("---")
        else:
            st.info("ยังไม่มีวิดีโอติวเข้ม")

# ==========================================
    # --- ส่วนที่เพิ่ม: ปุ่มออกจากระบบ (Sidebar) ---
    with st.sidebar:
        st.write(f"ผู้ดูแลระบบ: {st.session_state.name}")
        st.divider()
        if st.button("🔴 ออกจากระบบ", use_container_width=True):
            do_logout()
            
    conn.close()
# ==========================================
# Main
# ==========================================
restore_session()

if not st.session_state.logged_in: login_page()
else:
    if st.session_state.role == 'admin': admin_page()
    elif st.session_state.role == 'teacher': teacher_page()
    else: view_data_page(st.session_state.user)