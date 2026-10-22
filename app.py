from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_from_directory
from pymongo import MongoClient
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from bson.objectid import ObjectId
import os
import json
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = "lms_super_secret_key_bro"

UPLOAD_FOLDER = 'static/uploads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

MONGO_URI = os.getenv("MONGO_URI")
client = MongoClient(MONGO_URI)
db = client['smart_lms']
users_collection = db['users']
courses_collection = db['courses']
submissions_collection = db['submissions']
quiz_scores_collection = db['quiz_scores']
chat_history_collection = db['chat_history']
attendance_collection = db['attendance']
learning_progress_collection = db['learning_progress']
messages_collection = db['teacher_messages']

API_KEY = os.getenv("GEMINI_API_KEY")
if not API_KEY:
    print("WARNING: GEMINI_API_KEY is missing!")
genai.configure(api_key=API_KEY)
ai_model = genai.GenerativeModel('gemini-3.1-flash-lite')
tutor_model = genai.GenerativeModel('gemini-3.5-flash-lite')

@app.after_request
def fix_broken_markdown_links(response):
    if response.content_type and 'text/html' in response.content_type:
        html = response.get_data(as_text=True)
        html = re.sub(r'\[https?://[^\]]+\]\((https?://[^)]+)\)', r'\1', html)
        response.set_data(html)
    return response

@app.route('/')
def home():
    return render_template('index.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        password = request.form.get('password')
        if users_collection.find_one({"email": email}):
            flash("Email already registered! Please login.", "error")
            return redirect(url_for('register'))
        users_collection.insert_one({
            "name": name,
            "email": email,
            "password": generate_password_hash(password),
            "role": "student"
        })
        flash("Registration successful! Please sign in.", "success")
        return redirect(url_for('login', role='student'))
    return render_template('register.html')

@app.route('/login/<role>', methods=['GET', 'POST'])
def login(role):
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        if role == 'admin' and email == 'admin@lms.com' and password == 'admin123':
            session['user'] = 'Admin Sir'
            session['role'] = 'admin'
            session['email'] = email
            return redirect(url_for('admin_panel', section='dashboard'))
        user = users_collection.find_one({"email": email, "role": role})
        if user and check_password_hash(user['password'], password):
            session['user'] = user['name']
            session['email'] = user['email']
            session['role'] = user['role']
            return redirect(url_for('student_panel', section='home'))
        flash("Invalid Email or Password!", "error")
        return redirect(url_for('login', role=role))
    return render_template('login.html', role=role)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))



def india_now():
    return datetime.now(ZoneInfo('Asia/Kolkata'))

def active_attendance_dates():
    """Dates on which at least one student has used the online attendance check-in."""
    return sorted(attendance_collection.distinct('date'))

def attendance_summary(email):
    active_dates = active_attendance_dates()
    present_dates = attendance_collection.distinct('date', {'student_email': email, 'status': 'Present'})
    denominator = len(active_dates)
    percent = round((len(set(present_dates) & set(active_dates)) / denominator) * 100) if denominator else 0
    return {'percent': percent, 'present_days': len(set(present_dates) & set(active_dates)), 'active_days': denominator}

def course_progress_for(email, course):
    course_id = str(course['_id'])
    completed = {int(x.get('chapter_idx', -1)) for x in learning_progress_collection.find({'student_email': email, 'course_id': course_id, 'completed': True})}
    total = len(course.get('topics', []))
    percent = round(len(completed) / total * 100) if total else 0
    course['course_id_str'] = course_id
    course['completed_chapters'] = sorted(completed)
    course['progress_percent'] = percent
    course['completed_count'] = len([x for x in completed if 0 <= x < total])
    return course

# ================= ADMIN ROUTES =================
@app.route('/admin/<section>')
def admin_panel(section):
    if session.get('role') != 'admin':
        return redirect(url_for('home'))
    data = {}
    courses = list(courses_collection.find())

    if section == 'dashboard':
        data['total_courses'] = len(courses)
        data['total_students'] = users_collection.count_documents({"role": "student"})
        data['total_quizzes'] = sum(len(c.get('quizzes', [])) for c in courses)
        data['total_assignments'] = sum(len(c.get('assignments', [])) for c in courses)
    elif section in ['courses', 'ai_quiz', 'assignments']:
        data['courses'] = courses
    elif section == 'students':
        students = list(users_collection.find({"role": "student"}))
        for student in students:
            student.update(attendance_summary(student.get('email', '')))
        data['students'] = students
        data['attendance_active_days'] = len(active_attendance_dates())
    elif section == 'attendance':
        data['attendance_date'] = request.args.get('date') or india_now().date().isoformat()
        data['attendance_active_days'] = len(active_attendance_dates())
        students = list(users_collection.find({'role': 'student'}))
        todays = {r['student_email']: r for r in attendance_collection.find({'date': data['attendance_date']})}
        for student in students:
            student['today_status'] = todays.get(student.get('email'), {}).get('status', 'Absent')
            student.update(attendance_summary(student.get('email', '')))
        data['students'] = students
        data['present_today'] = sum(1 for s in students if s['today_status'] == 'Present')
    elif section == 'messages':
        data['messages'] = list(messages_collection.find().sort('updated_at', -1))
        data['new_messages'] = [m for m in data['messages'] if m.get('status') == 'New']
        data['replied_messages'] = [m for m in data['messages'] if m.get('status') == 'Replied']
        data['new_count'] = messages_collection.count_documents({'status': 'New'})
        data['replied_count'] = messages_collection.count_documents({'status': 'Replied'})
    elif section == 'performance':
        students = list(users_collection.find({"role": "student"}))
        leaderboard = []
        for s in students:
            email = s['email']
            q_scores = list(quiz_scores_collection.find({"student_email": email}))
            q_total = sum(int(q.get('score', 0)) for q in q_scores)
            subs = list(submissions_collection.find({"student_email": email}))
            a_total = sum(int(sub.get('marks', 0)) for sub in subs if sub.get('marks') is not None and str(sub.get('marks')).isdigit())
            overall = q_total + a_total
            leaderboard.append({
                "name": s['name'],
                "email": email,
                "quiz_marks": q_total,
                "assign_marks": a_total,
                "total_marks": overall
            })
        leaderboard = sorted(leaderboard, key=lambda x: x['total_marks'], reverse=True)[:5]
        data['leaderboard'] = leaderboard

    return render_template('admin.html', user=session.get('user'), section=section, data=data)

@app.route('/add_course', methods=['POST'])
def add_course():
    if session.get('role') == 'admin':
        title = request.form.get('title')
        courses_collection.insert_one({
            "title": title,
            "description": f"Comprehensive module and learning resources for {title}.",
            "topics": [],
            "quizzes": [],
            "assignments": []
        })
        flash("Course Created Successfully!", "success")
    return redirect(url_for('admin_panel', section='courses'))

@app.route('/manage_course/<course_id>')
def manage_course(course_id):
    if session.get('role') != 'admin':
        return redirect(url_for('home'))
    course = courses_collection.find_one({"_id": ObjectId(course_id)})
    return render_template('manage_course.html', user=session.get('user'), course=course)

@app.route('/add_topic/<course_id>', methods=['POST'])
def add_topic(course_id):
    if session.get('role') == 'admin':
        courses_collection.update_one(
            {"_id": ObjectId(course_id)},
            {"$push": {"topics": {
                "title": request.form.get('topic_title'),
                "content": request.form.get('topic_content')
            }}}
        )
        flash("New Chapter Added Successfully!", "success")
    return redirect(url_for('manage_course', course_id=course_id))

@app.route('/edit_topic/<course_id>/<int:chapter_idx>', methods=['POST'])
def edit_topic(course_id, chapter_idx):
    if session.get('role') != 'admin':
        return redirect(url_for('home'))
    courses_collection.update_one(
        {"_id": ObjectId(course_id)},
        {"$set": {f"topics.{chapter_idx}": {
            "title": request.form.get('topic_title'),
            "content": request.form.get('topic_content')
        }}}
    )
    flash("Chapter Updated Successfully!", "success")
    return redirect(url_for('manage_course', course_id=course_id))

@app.route('/admin_view_chapter/<course_id>/<int:chapter_idx>')
def admin_view_chapter(course_id, chapter_idx):
    if session.get('role') != 'admin':
        return redirect(url_for('home'))
    course = courses_collection.find_one({"_id": ObjectId(course_id)})
    chapter = course.get('topics', [])[chapter_idx]
    return render_template('admin_view_chapter.html', user=session.get('user'), course=course, chapter=chapter)

@app.route('/generate_quiz_global', methods=['POST'])
def generate_quiz_global():
    if session.get('role') != 'admin':
        return redirect(url_for('home'))
    course_id = request.form.get('course_id')
    num_questions = int(request.form.get('num_questions'))
    total_marks = num_questions * 2

    course_doc = courses_collection.find_one({"_id": ObjectId(course_id)})
    quiz_topic = course_doc['title'] if course_doc else "General"
    quiz_number = len(course_doc.get('quizzes', [])) + 1 if course_doc else 1

    prompt = f"""You are a teacher. Generate {num_questions} MCQs about "{quiz_topic}".
    Return ONLY a valid JSON array. No markdown. Format:
    [{{"question": "Q1", "options": ["A", "B", "C", "D"], "correct_answer": "A"}}]"""
    try:
        response = ai_model.generate_content(prompt)
        response_text = response.text.strip()
        if response_text.startswith("```json"):
            response_text = response_text[7:]
        elif response_text.startswith("```"):
            response_text = response_text[3:]
        if response_text.endswith("```"):
            response_text = response_text[:-3]
        match = re.search(r'\[\s*\{.*\}\s*\]', response_text.strip(), re.DOTALL)
        json_str = match.group(0) if match else response_text.strip()

        quiz_data = json.loads(json_str)
        courses_collection.update_one(
            {"_id": ObjectId(course_id)},
            {"$push": {"quizzes": {
                "quiz_title": f"{quiz_topic} Quiz {quiz_number}",
                "questions": quiz_data,
                "total_marks": total_marks
            }}}
        )
        flash("AI Quiz Generated and Assigned Successfully!", "success")
    except Exception as e:
        print(f"AI Error: {e}")
        flash("Failed to generate quiz. Please try again.", "error")
    return redirect(url_for('admin_panel', section='ai_quiz'))

@app.route('/add_assignment_global', methods=['POST'])
def add_assignment_global():
    if session.get('role') == 'admin':
        course_id = request.form.get('course_id')
        num_questions = int(request.form.get('num_questions', 5))
        total_marks = num_questions * 4
        desc = request.form.get('assign_desc')

        course_doc = courses_collection.find_one({"_id": ObjectId(course_id)})
        course_title = course_doc['title'] if course_doc else "Course"
        task_num = len(course_doc.get('assignments', [])) + 1 if course_doc else 1
        auto_title = f"{course_title} Task {task_num}"

        courses_collection.update_one(
            {"_id": ObjectId(course_id)},
            {"$push": {"assignments": {
                "title": auto_title,
                "desc": desc,
                "num_questions": num_questions,
                "total_marks": total_marks
            }}}
        )
        flash("Task Assigned Successfully!", "success")
    return redirect(url_for('admin_panel', section='assignments'))

@app.route('/admin/student/<student_email>')
def admin_student_view(student_email):
    if session.get('role') != 'admin':
        return redirect(url_for('home'))
    student = users_collection.find_one({"email": student_email})

    quizzes = list(quiz_scores_collection.find({"student_email": student_email}))
    for q in quizzes:
        c = courses_collection.find_one({"_id": ObjectId(q['course_id'])})
        if c:
            for item in c.get('quizzes', []):
                if item['quiz_title'] == q['quiz_title']:
                    q['max_marks'] = item.get('total_marks', len(item['questions']) * 2)
        if 'max_marks' not in q:
            q['max_marks'] = q.get('total', 10)

    submissions = list(submissions_collection.find({"student_email": student_email}))
    for sub in submissions:
        c = courses_collection.find_one({"_id": ObjectId(sub['course_id'])})
        sub['course_title'] = c['title'] if c else "Unknown Course"
        if c:
            assignments = c.get('assignments', [])
            if sub['assign_idx'] < len(assignments):
                sub['max_marks'] = assignments[sub['assign_idx']].get('total_marks', 20)
            else:
                sub['max_marks'] = 20
        else:
            sub['max_marks'] = 20

    return render_template('admin_student.html', user=session.get('user'), student=student, quizzes=quizzes, submissions=submissions, attendance=attendance_summary(student_email))

@app.route('/grade_submission/<sub_id>', methods=['POST'])
def grade_submission(sub_id):
    if session.get('role') == 'admin':
        marks = request.form.get('marks')
        submissions_collection.update_one({"_id": ObjectId(sub_id)}, {"$set": {"marks": marks}})
        flash("Marks Saved Successfully!", "success")
        sub = submissions_collection.find_one({"_id": ObjectId(sub_id)})
        return redirect(url_for('admin_student_view', student_email=sub['student_email']))
    return redirect(url_for('home'))

@app.route('/edit_quiz_score/<score_id>', methods=['POST'])
def edit_quiz_score(score_id):
    if session.get('role') == 'admin':
        new_score = int(request.form.get('score'))
        score_doc = quiz_scores_collection.find_one({"_id": ObjectId(score_id)})
        quiz_scores_collection.update_one({"_id": ObjectId(score_id)}, {"$set": {"score": new_score}})
        flash("Quiz Marks Updated Successfully!", "success")
        return redirect(url_for('admin_student_view', student_email=score_doc['student_email']))
    return redirect(url_for('home'))

# ================= STUDENT ROUTES =================
@app.route('/student/<section>')
def student_panel(section):
    if session.get('role') != 'student':
        return redirect(url_for('home'))

    data = {}
    courses = list(courses_collection.find())
    email = session.get('email')
    courses = [course_progress_for(email, c) for c in courses]

    q_scores = list(quiz_scores_collection.find({"student_email": email}))
    subs = list(submissions_collection.find({"student_email": email}))

    data['total_courses'] = len(courses)
    data['quiz_attempts'] = len(q_scores)
    data['submitted_count'] = len(subs)

    total_earned = sum(int(q.get('score', 0)) for q in q_scores)
    total_max = sum(int(q.get('total', 10)) for q in q_scores)
    data['accuracy'] = round((total_earned / total_max) * 100, 1) if total_max > 0 else 0.0

    if section == 'home':
        data['courses'] = courses
    elif section == 'attendance':
        today = india_now().date().isoformat()
        data['today'] = today
        data['today_record'] = attendance_collection.find_one({'student_email': email, 'date': today})
        data['attendance_records'] = list(attendance_collection.find({'student_email': email}).sort('date', -1).limit(90))
        data.update(attendance_summary(email))
    elif section == 'messages':
        data['messages'] = list(messages_collection.find({'student_email': email}).sort('updated_at', -1))
        data['new_count'] = sum(1 for m in data['messages'] if m.get('status') == 'New')
    elif section == 'assignments':
        active_assignments = []
        completed_assignments = []
        for c in courses:
            for idx, a in enumerate(c.get('assignments', [])):
                sub = submissions_collection.find_one({
                    "course_id": str(c['_id']),
                    "assign_idx": idx,
                    "student_email": email
                })
                item = {
                    "course_id": str(c['_id']),
                    "course_title": c['title'],
                    "assign_idx": idx,
                    "title": a['title'],
                    "desc": a['desc'],
                    "total_marks": a.get('total_marks', 20)
                }
                if sub:
                    completed_assignments.append(item)
                else:
                    active_assignments.append(item)
        data['active_assignments'] = active_assignments
        data['completed_assignments'] = completed_assignments

    elif section == 'quizzes':
        active_quizzes = []
        completed_quizzes = []
        for c in courses:
            for idx, q in enumerate(c.get('quizzes', [])):
                score = quiz_scores_collection.find_one({
                    "course_id": str(c['_id']),
                    "quiz_title": q['quiz_title'],
                    "student_email": email
                })
                item = {
                    "course_id": str(c['_id']),
                    "course_title": c['title'],
                    "quiz_index": idx,
                    "quiz_title": q['quiz_title'],
                    "questions_count": len(q['questions']),
                    "total_marks": q.get('total_marks', len(q['questions']) * 2)
                }
                if score:
                    completed_quizzes.append(item)
                else:
                    active_quizzes.append(item)
        data['active_quizzes'] = active_quizzes
        data['completed_quizzes'] = completed_quizzes

    elif section == 'marks':
        q_list = list(quiz_scores_collection.find({"student_email": email}))
        for q in q_list:
            c = courses_collection.find_one({"_id": ObjectId(q['course_id'])})
            if c:
                for item in c.get('quizzes', []):
                    if item['quiz_title'] == q['quiz_title']:
                        q['max_marks'] = item.get('total_marks', len(item['questions']) * 2)
            if 'max_marks' not in q:
                q['max_marks'] = q.get('total', 10)

            sc = int(q.get('score', 0))
            mx = int(q['max_marks'])
            pct = (sc / mx) * 100 if mx > 0 else 0
            if sc < 20 and pct < 40:
                q['badge_color'] = "bg-red-50 text-red-600 border border-red-200"
                q['status_label'] = "Needs Improvement"
            elif pct < 65:
                q['badge_color'] = "bg-amber-50 text-amber-600 border border-amber-200"
                q['status_label'] = "Average"
            elif pct < 85:
                q['badge_color'] = "bg-emerald-50 text-emerald-600 border border-emerald-200"
                q['status_label'] = "Good"
            else:
                q['badge_color'] = "bg-sky-50 text-sky-600 border border-sky-200"
                q['status_label'] = "Excellent"

        sub_list = list(submissions_collection.find({"student_email": email}))
        for sub in sub_list:
            c = courses_collection.find_one({"_id": ObjectId(sub['course_id'])})
            sub['course_title'] = c['title'] if c else "Unknown"
            if c:
                assignments = c.get('assignments', [])
                if sub['assign_idx'] < len(assignments):
                    sub['max_marks'] = assignments[sub['assign_idx']].get('total_marks', 20)
                else:
                    sub['max_marks'] = 20
            else:
                sub['max_marks'] = 20

            if sub.get('marks') is not None:
                m = int(sub['marks'])
                mx = int(sub['max_marks'])
                pct = (m / mx) * 100 if mx > 0 else 0
                if m < 20 and pct < 40:
                    sub['badge_color'] = "bg-red-50 text-red-600 border border-red-200"
                    sub['status_label'] = "Needs Improvement"
                elif pct < 65:
                    sub['badge_color'] = "bg-amber-50 text-amber-600 border border-amber-200"
                    sub['status_label'] = "Average"
                elif pct < 85:
                    sub['badge_color'] = "bg-emerald-50 text-emerald-600 border border-emerald-200"
                    sub['status_label'] = "Good"
                else:
                    sub['badge_color'] = "bg-sky-50 text-sky-600 border border-sky-200"
                    sub['status_label'] = "Excellent"

        data['quizzes'] = q_list
        data['submissions'] = sub_list

    elif section in ['prepare', 'study_plan']:
        data['courses'] = courses

    return render_template('student_sidebar.html', user=session.get('user'), section=section, data=data)



@app.route('/student/attendance/mark', methods=['POST'])
def student_mark_attendance():
    if session.get('role') != 'student':
        return redirect(url_for('home'))
    today = india_now().date().isoformat()
    email = session.get('email')
    existing = attendance_collection.find_one({'student_email': email, 'date': today})
    if existing:
        flash('Your attendance is already marked for today.', 'info')
    else:
        attendance_collection.insert_one({'student_email': email, 'student_name': session.get('user'), 'date': today, 'status': 'Present', 'marked_at': india_now().isoformat(timespec='seconds'), 'method': 'student_self_checkin'})
        flash('Attendance marked successfully for today!', 'success')
    return redirect(url_for('student_panel', section='attendance'))

@app.route('/student/messages/send', methods=['POST'])
def student_send_message():
    if session.get('role') != 'student':
        return redirect(url_for('home'))
    subject = (request.form.get('subject') or '').strip()[:160]
    body = (request.form.get('body') or '').strip()[:5000]
    if not subject or not body:
        flash('Please enter both a subject and a message.', 'error')
    else:
        now = india_now().isoformat(timespec='seconds')
        messages_collection.insert_one({'student_email': session.get('email'), 'student_name': session.get('user'), 'subject': subject, 'body': body, 'reply': '', 'status': 'New', 'created_at': now, 'updated_at': now})
        flash('Your message has been sent to the admin/teacher.', 'success')
    return redirect(url_for('student_panel', section='messages'))

@app.route('/admin/message_reply/<message_id>', methods=['POST'])
def admin_message_reply(message_id):
    if session.get('role') != 'admin':
        return redirect(url_for('home'))
    reply = (request.form.get('reply') or '').strip()[:5000]
    if reply:
        messages_collection.update_one({'_id': ObjectId(message_id)}, {'$set': {'reply': reply, 'status': 'Replied', 'updated_at': india_now().isoformat(timespec='seconds')}})
        flash('Reply sent successfully.', 'success')
    else:
        flash('Please enter a reply first.', 'error')
    return redirect(url_for('admin_panel', section='messages'))

@app.route('/admin/attendance/mark_absent/<student_email>', methods=['POST'])
def admin_mark_absent(student_email):
    if session.get('role') != 'admin':
        return redirect(url_for('home'))
    selected_date = request.form.get('date') or india_now().date().isoformat()
    attendance_collection.update_one({'student_email': student_email, 'date': selected_date}, {'$set': {'student_email': student_email, 'student_name': (users_collection.find_one({'email': student_email}) or {}).get('name', student_email), 'date': selected_date, 'status': 'Absent', 'marked_at': india_now().isoformat(timespec='seconds'), 'method': 'admin_override'}}, upsert=True)
    flash('Attendance status updated.', 'success')
    return redirect(url_for('admin_panel', section='attendance', date=selected_date))

@app.route('/student/progress/<course_id>/<int:chapter_idx>', methods=['POST'])
def toggle_chapter_progress(course_id, chapter_idx):
    if session.get('role') != 'student':
        return redirect(url_for('home'))
    course = courses_collection.find_one({'_id': ObjectId(course_id)})
    if not course or chapter_idx < 0 or chapter_idx >= len(course.get('topics', [])):
        flash('Chapter not found.', 'error')
        return redirect(url_for('student_panel', section='home'))
    email = session.get('email')
    key = {'student_email': email, 'course_id': course_id, 'chapter_idx': chapter_idx}
    current = learning_progress_collection.find_one(key)
    if current and current.get('completed'):
        learning_progress_collection.delete_one(key)
        flash('Chapter marked as not completed.', 'info')
    else:
        learning_progress_collection.update_one(key, {'$set': {**key, 'completed': True, 'completed_at': india_now().isoformat(timespec='seconds')}}, upsert=True)
        flash('Chapter marked complete!', 'success')
    return redirect(url_for('student_view_course', course_id=course_id))

@app.route('/student/view_course/<course_id>')
def student_view_course(course_id):
    if session.get('role') != 'student':
        return redirect(url_for('home'))
    course = courses_collection.find_one({"_id": ObjectId(course_id)})
    if course:
        course = course_progress_for(session.get('email'), course)
    return render_template('student_view_course.html', user=session.get('user'), course=course)

@app.route('/student_view_chapter/<course_id>/<int:chapter_idx>')
def student_view_chapter(course_id, chapter_idx):
    if session.get('role') != 'student':
        return redirect(url_for('home'))
    course = courses_collection.find_one({"_id": ObjectId(course_id)})
    chapter = course.get('topics', [])[chapter_idx]
    return render_template('student_view_chapter.html', user=session.get('user'), course=course, chapter=chapter)

@app.route('/student/submit_assignment/<course_id>/<int:assign_idx>', methods=['POST'])
def student_submit_assignment(course_id, assign_idx):
    if session.get('role') != 'student':
        return redirect(url_for('home'))
    course = courses_collection.find_one({"_id": ObjectId(course_id)})
    assignment = course.get('assignments', [])[assign_idx]

    file = request.files.get('file')
    if not file or file.filename == '':
        flash("No file selected!", "error")
        return redirect(url_for('student_panel', section='assignments'))

    filename = secure_filename(file.filename)
    unique_filename = f"{session.get('user', 'student').replace(' ', '_')}_{assign_idx}_{filename}"
    file.save(os.path.join(app.config['UPLOAD_FOLDER'], unique_filename))

    submissions_collection.insert_one({
        "course_id": course_id,
        "assign_idx": assign_idx,
        "assign_title": assignment['title'],
        "student_name": session.get('user'),
        "student_email": session.get('email'),
        "filename": unique_filename,
        "marks": None
    })
    flash("Assignment Submitted Successfully!", "success")
    return redirect(url_for('student_panel', section='assignments'))

@app.route('/student/take_quiz/<course_id>/<int:quiz_index>', methods=['GET', 'POST'])
def student_take_quiz(course_id, quiz_index):
    if session.get('role') != 'student':
        return redirect(url_for('home'))
    course = courses_collection.find_one({"_id": ObjectId(course_id)})
    quiz = course.get('quizzes', [])[quiz_index]

    if request.method == 'POST':
        correct_count = sum(1 for i, q in enumerate(quiz['questions']) if request.form.get(f"q_{i}") == q['correct_answer'])
        total_q = len(quiz['questions'])
        earned_score = correct_count * 2
        total_possible = quiz.get('total_marks', total_q * 2)

        quiz_scores_collection.update_one(
            {"student_email": session.get('email'), "course_id": course_id, "quiz_title": quiz['quiz_title']},
            {"$set": {"score": earned_score, "total": total_possible, "course_title": course['title']}},
            upsert=True
        )
        flash("Quiz Completed Successfully!", "success")
        return redirect(url_for('student_panel', section='quizzes'))
    return render_template('student_take_quiz.html', user=session.get('user'), course=course, quiz=quiz, course_id=course_id, quiz_index=quiz_index)

@app.route('/student/ai_chat', methods=['POST'])
def student_ai_chat():
    if session.get('role') != 'student':
        return jsonify({"response": "Unauthorized"})

    course_id = request.form.get('course_id')
    question = request.form.get('question')
    email = session.get('email')

    course = courses_collection.find_one({"_id": ObjectId(course_id)})
    if not course:
        return jsonify({"response": "Course not found."})

    chat_history_collection.insert_one({
        "student_email": email,
        "course_id": course_id,
        "sender": "user",
        "text": question
    })

    past_chats = list(chat_history_collection.find({"student_email": email, "course_id": course_id}).sort("_id", 1).limit(10))
    history_text = ""
    for chat in past_chats:
        role_label = "Student" if chat['sender'] == "user" else "Tutor"
        history_text += f"{role_label}: {chat['text']}\n"

    prompt = f"""You are a helpful and knowledgeable AI Tutor assisting a student with "{course['title']}".
    Answer the student's question clearly, accurately, and directly without any restrictions.

    Conversation History:
    {history_text}

    Student Question: {question}
    """
    try:
        response = tutor_model.generate_content(prompt)
        ai_reply = response.text.strip()
    except Exception as e:
        print(f"AI Tutor Error: {e}")
        ai_reply = "Unable to reach AI service right now. Please try again in a moment."

    chat_history_collection.insert_one({
        "student_email": email,
        "course_id": course_id,
        "sender": "ai",
        "text": ai_reply
    })

    return jsonify({"response": ai_reply})

@app.route('/student/get_chat_history/<course_id>')
def get_chat_history(course_id):
    if session.get('role') != 'student':
        return jsonify([])
    chats = list(chat_history_collection.find({"student_email": session.get('email'), "course_id": course_id}).sort("_id", 1))
    history = [{"sender": c['sender'], "text": c['text']} for c in chats]
    return jsonify(history)

@app.route('/student/generate_study_plan', methods=['POST'])
def generate_study_plan():
    if session.get('role') != 'student':
        return redirect(url_for('home'))

    goal = request.form.get('goal')
    course_ids = request.form.getlist('courses')
    duration = request.form.get('duration')

    selected_courses = list(courses_collection.find({"_id": {"$in": [ObjectId(cid) for cid in course_ids]}}))
    course_names = ", ".join([c['title'] for c in selected_courses]) if selected_courses else "Selected Subjects"

    if duration == "1 Day":
        time_rule = "Provide a strict, structured HOUR-WISE study schedule."
    elif duration in ["1 Week", "2 Weeks"]:
        time_rule = "Provide a structured DAY-WISE study schedule."
    else:
        time_rule = "Provide a structured WEEK-WISE study schedule (Week 1, Week 2, etc.)."

    prompt = f"""Create a clean, well-structured study plan for a student preparing for "{goal}" covering: {course_names}.
    Duration available: {duration}.
    {time_rule}
    Keep it practical, clear, and organized.
    """
    try:
        response = ai_model.generate_content(prompt)
        plan_text = response.text.strip()
    except Exception as e:
        plan_text = "Failed to generate study plan. Please try again."

    return render_template('student_study_plan_result.html', user=session.get('user'), goal=goal, duration=duration, course_names=course_names, plan_text=plan_text)

@app.route('/uploads/<filename>')
def download_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

if __name__ == '__main__':
    app.run(debug=True)