from flask import Flask, render_template, request, redirect, url_for, jsonify
import psycopg2
from datetime import date
import os
from werkzeug.utils import secure_filename
from markupsafe import Markup
import json
import fitz
import smtplib
from email.message import EmailMessage
from bs4 import BeautifulSoup
import spacy
import re
from markdown2 import markdown
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from flask import send_from_directory

app = Flask(__name__)
UPLOAD_FOLDER = 'resumes'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER


nlp = spacy.load("en_core_web_sm")


def extract_title_from_text(jd_text):
    """
    Extracts job title from a raw job description, handling newlines and spacing.
    """
    if not jd_text:
        return "Untitled"

    # Normalize line breaks (handle both \n and \\n)
    normalized_text = jd_text.replace("\\n", "\n")

    # Look for a line starting with 'Job Title':
    match = re.search(r'(?i)job\s*title\s*:\s*(.+)', normalized_text)
    return match.group(1).strip() if match else "Untitled"


def extract_location_from_text(jd_text):
    """
    Extracts job location from a raw job description.
    """
    if not jd_text:
        return "Location Unknown"

    # Normalize escaped line breaks
    normalized_text = jd_text.replace("\\n", "\n")

    # Look for a line like 'Job Location: Remote'
    match = re.search(r'(?i)job\s*location\s*:\s*(.+)', normalized_text)
    return match.group(1).strip() if match else "Location Unknown"


def db_connections():
    try:
        connection = psycopg2.connect(
            database="postgres",
            user="postgres",
            password="admin",
            host="localhost",
            port="5432"
        )
        return connection
    except Exception as e:
        print(f"Database connection failed: {e}")
        return None

def format_job_description(text):
    sections = {
        "Summary:": "p",
        "Responsibilities:": "ul",
        "Required Skills:": "ul",
        "Preferred Qualifications:": "ul",
        "Experience Range:": "p",
        "Job Location:": "p"
    }

    # Ensure a text is a string and replace '\n' with real line breaks
    lines = text.replace("\\n", "\n").splitlines()
    html_output = ""
    current_section = None

    for line in lines:
        line = line.strip()
        if not line:
            continue

        matched_section = next((s for s in sections if line.startswith(s)), None)

        if matched_section:
            if current_section and sections[current_section] == "ul":
                html_output += "</ul>\n"
            current_section = matched_section
            tag = sections[matched_section]
            content = line.replace(matched_section, "").strip()
            if tag == "p":
                html_output += f"<h5>{matched_section}</h5>\n<p>{content}</p>\n"
                current_section = None
            elif tag == "ul":
                html_output += f"<h5>{matched_section}</h5>\n<ul>\n"
        else:
            if current_section and sections[current_section] == "ul":
                html_output += f"<li>{line.lstrip('-*• ').strip()}</li>\n"
            else:
                html_output += f"<p>{line}</p>\n"

    if current_section and sections[current_section] == "ul":
        html_output += "</ul>\n"

    return Markup(html_output)
app.jinja_env.filters['format_jd'] = format_job_description


def clean_jd(text):
    if not text:
        return ""
    return Markup(text.replace('\\n', ' ').replace('\n', ' ').replace('\\', '').strip())

app.jinja_env.filters['clean_jd'] = clean_jd


@app.route('/')
def index():
    connection = db_connections()
    jobs = []
    if connection:
        cursor = connection.cursor()
        cursor.execute("SELECT job_id, job_description FROM jd_details ORDER BY job_date DESC")
        rows = cursor.fetchall()
        for row in rows:
            job_id = row[0]
            full_desc = row[1] or ""

            try:
                jd_json = json.loads(full_desc)
                raw_title = jd_json.get("Job Title", "").strip()
                title = raw_title if raw_title else extract_title_from_text(full_desc)

                raw_location = jd_json.get("Job Location", "").strip()
                location = raw_location if raw_location else extract_location_from_text(full_desc)

                formatted_desc = convert_jd_json_to_text(jd_json)
            except Exception:
                title = extract_title_from_text(full_desc)
                location = extract_location_from_text(full_desc)
                formatted_desc = full_desc

            formatted_html = format_job_description(formatted_desc)

            # Extract plain text for preview
            soup = BeautifulSoup(formatted_html, "html.parser")
            plain_text_preview = soup.get_text(separator=' ', strip=True)

            jobs.append({
                "title": title,
                "company": "Statscog Labs",
                "location": location,
                "description": plain_text_preview,
                "applyLink": f"/apply?job_id={job_id}",
                "viewLink": f"/job/{job_id}"
            })

        cursor.close()
        connection.close()

    return render_template('index.html', jobs=jobs)


@app.template_filter('truncate_words')
def truncate_words(s, num=40):
    return ' '.join(s.split()[:num]) + '...' if s else ''


@app.route('/post-job-form', methods=['POST'])
def post_job_form():
    job_id = request.form.get('job_id')
    job_description = request.form.get('job_description')

    if job_id and job_description:
        connection = db_connections()
        if connection:
            cursor = connection.cursor()
            cursor.execute("""
                INSERT INTO jd_details (job_id, job_description, job_date)
                VALUES (%s, %s, CURRENT_DATE)
            """, (job_id, job_description))
            connection.commit()
            cursor.close()
            connection.close()
    return redirect(url_for('index'))


@app.route('/post-job', methods=['POST'])
def api_post_job():
    data = request.get_json()
    print("📥 Incoming JSON:", json.dumps(data, indent=2))

    if not data:
        return {'status': 'error', 'message': 'No data received.'}, 400

    # Handle both structured JSON and plain text description
    jd_text = None
    if isinstance(data, dict):
        if "job_description" in data and isinstance(data["job_description"], str):
            # Plain text format
            jd_text = data["job_description"].strip()
        elif any(k.lower() in data.keys() for k in ["job title", "summary", "responsibilities", "required skills"]):
            # Structured JSON format
            jd_text = convert_jd_json_to_text(data)
        else:
            print("❌ Rejected: Input doesn't appear to be a job description.")
            return {'status': 'error', 'message': 'Invalid JD format.'}, 400
    else:
        return {'status': 'error', 'message': 'Invalid JSON structure.'}, 400

    if not jd_text or len(jd_text.split()) < 10:
        return {'status': 'error', 'message': 'Job description too short.'}, 400

    # Proceed with DB insert
    connection = db_connections()
    if connection:
        cursor = connection.cursor()
        cursor.execute("SELECT job_id FROM jd_details ORDER BY job_id DESC LIMIT 1")
        last_id_row = cursor.fetchone()
        new_job_id = f"JD{int(last_id_row[0][2:]) + 1:03d}" if last_id_row else "JD001"

        cursor.execute("""
            INSERT INTO jd_details (job_id, job_description, job_date)
            VALUES (%s, %s, CURRENT_DATE)
        """, (new_job_id, jd_text))
        connection.commit()
        cursor.close()
        connection.close()

        print("✅ Job successfully inserted with ID:", new_job_id)
        return {'status': 'success', 'message': 'Job posted successfully.'}, 200

    return {'status': 'error', 'message': 'Database connection failed.'}, 500




def convert_jd_json_to_text(jd_json):
    text = []
    for key, value in jd_json.items():
        text.append(f"{key}:")
        if isinstance(value, list):
            for item in value:
                text.append(f"- {item}")
        else:
            text.append(str(value))
        text.append("")  # Add a blank line between sections
    return "\n".join(text).strip()


def extract_metadata_from_text(jd_text):
    title = "Untitled"
    location = "Unknown"
    summary = ""
    lines = jd_text.splitlines()
    for i, line in enumerate(lines):
        if line.lower().startswith("job title:"):
            title = line.split(":", 1)[1].strip()
        elif line.lower().startswith("job location:"):
            location = line.split(":", 1)[1].strip()
        elif line.lower().startswith("summary:"):
            summary = lines[i + 1].strip() if i + 1 < len(lines) else ""
    return title, location, summary


@app.route('/job/<job_id>')
def job_detail(job_id):
    connection = db_connections()
    job = None
    if connection:
        cursor = connection.cursor()
        cursor.execute("SELECT job_id, job_description FROM jd_details WHERE job_id = %s", (job_id,))
        row = cursor.fetchone()
        if row:
            job_id, jd_text = row
            title, location, _ = extract_metadata_from_text(jd_text)
            formatted_description = jd_text.strip()

            job = {
                "job_id": job_id,
                "title": title,
                "company": "Statscog Labs",
                "location": location,
                "description": formatted_description
            }
        cursor.close()
        connection.close()

    return render_template("job_details.html", job=job)


@app.route('/apply', methods=['GET', 'POST'])
def apply_job():
    if request.method == 'POST':
        job_id = request.form.get('job_id')
        name = request.form.get('name')
        email = request.form.get('email')
        phone = request.form.get('phone_number')
        file = request.files['resume']

        connection = db_connections()
        form_id = None

        if connection:
            cursor = connection.cursor()
            # Generate next Form_ID like FM001
            cursor.execute("SELECT form_id FROM job_form ORDER BY form_id DESC LIMIT 1")
            last_row = cursor.fetchone()
            if last_row and last_row[0]:
                last_num = int(last_row[0][2:])
                form_id = f"FM{last_num+1:03d}"
            else:
                form_id = "FM001"

            if file:
                filename = secure_filename(file.filename)
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(filepath)

                cursor.execute("""
                    INSERT INTO job_form 
                    (form_id, job_id, name, email, phone_number, resume, form_date)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (form_id, job_id, name, email, phone, filename, date.today()))
                connection.commit()

            cursor.close()
            connection.close()

            # Show alert and redirect to index
            return f"""
                <script>
                    alert("✅ Application submitted successfully! Your Form ID is {form_id}");
                    window.location.href = "/";
                </script>
            """

    job_id = request.args.get('job_id')  # pre-fill from query param
    return render_template('job_apply.html', job_id=job_id)


@app.route('/api/applicants/<job_id>', methods=['GET'])
def get_applicants_by_job(job_id):
    connection = db_connections()
    applicants = []

    if connection:
        cursor = connection.cursor()
        cursor.execute("""
            SELECT form_id, name, email, phone_number, resume, form_date
            FROM job_form
            WHERE job_id = %s
            ORDER BY form_date DESC
        """, (job_id,))
        rows = cursor.fetchall()

        for row in rows:
            applicants.append({
                "form_id": row[0],
                "name": row[1],
                "email": row[2],
                "phone": row[3],
                "resume_file": row[4],
                "applied_on": row[5].strftime("%Y-%m-%d")
            })

        cursor.close()
        connection.close()

    return {"job_id": job_id, "applicants": applicants}


@app.route('/jobs', methods=['GET'])
def get_all_jobs():
    connection = db_connections()
    job_list = []

    if connection:
        cursor = connection.cursor()
        cursor.execute("SELECT job_id, job_description, job_date FROM jd_details ORDER BY job_date DESC")
        rows = cursor.fetchall()

        for row in rows:
            job_id = row[0]
            jd_text = row[1]
            job_date = row[2].strftime("%Y-%m-%d")

            try:
                title, location, summary = extract_metadata_from_text(jd_text)
            except Exception as e:
                print(f"⚠️ Error extracting metadata for job_id {job_id}: {e}")
                title, location, summary = "Untitled", "Unknown", ""

            job_list.append({
                "job_id": job_id,
                "job_title": title,
                "job_location": location,
                "job_summary": summary,
                "job_date": job_date
            })

        cursor.close()
        connection.close()
        return jsonify({"status": "success", "jobs": job_list}), 200

    return jsonify({"status": "error", "message": "Database connection failed"}), 500


@app.route('/resumes/<job_id>', methods=['GET'])
def get_resumes_for_job(job_id):
    connection = db_connections()
    resumes_data = {}

    if connection:
        try:
            cursor = connection.cursor()
            cursor.execute("""
                SELECT form_id, resume 
                FROM job_form 
                WHERE job_id = %s
            """, (job_id,))
            rows = cursor.fetchall()
            cursor.close()
            connection.close()

            if not rows:
                return jsonify({"status": "error", "message": f"No applicants found for Job ID: {job_id}"}), 404

            for form_id, filename in rows:
                resume_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                if not os.path.isfile(resume_path):
                    resumes_data[form_id] = f"⚠️ File '{filename}' not found"
                    continue

                try:
                    text = ""
                    with fitz.open(resume_path) as doc:
                        for page in doc:
                            text += page.get_text()
                    resumes_data[form_id] = text.strip() or "⚠️ Empty PDF"
                except Exception as e:
                    resumes_data[form_id] = f"❌ Error reading PDF: {str(e)}"

            return jsonify({"status": "success", "job_id": job_id, "resumes": resumes_data})

        except Exception as e:
            return jsonify({"status": "error", "message": f"Database error: {str(e)}"}), 500

    return jsonify({"status": "error", "message": "Database connection failed"}), 500


@app.route('/send-email', methods=['POST'])
def send_email_to_candidate():
    try:
        # Extract from POST JSON
        data = request.get_json()
        recipient_email = data.get('to')
        subject = data.get('subject')
        body = data.get('body')

        if not recipient_email or not subject or not body:
            return jsonify({"status": "error", "message": "Missing required fields."}), 400

        # Prepare Markdown-compliant body
        markdown_ready_body = body.replace("\\n", "\n").replace("\n", "  \n")

        # Convert to HTML with optional styling
        html_body = f"""
        <html>
          <body style="font-family: Arial, sans-serif; line-height: 1.6;">
            {markdown(markdown_ready_body)}
          </body>
        </html>
        """

        # SMTP Configuration
        smtp_server = "webmail.4technologies.in"
        smtp_port = 587
        smtp_username = "mukund.kumar@4technologies.in"
        smtp_password = "Pharos@1234"

        # Create Email
        msg = EmailMessage()
        msg["From"] = smtp_username
        msg["To"] = recipient_email
        msg["Subject"] = subject

        msg.set_content(body)  # fallback plain text
        msg.add_alternative(html_body, subtype='html')

        # Send
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(smtp_username, smtp_password)
            server.send_message(msg)

        return jsonify({"status": "success", "message": "Email sent successfully."}), 200

    except Exception as e:
        return jsonify({"status": "error", "message": f"Failed to send email: {str(e)}"}), 500


@app.route('/employee/<emp_id>', methods=['GET'])
def get_employee_by_id(emp_id):
    connection = db_connections()
    if not connection:
        return jsonify({"status": "error", "message": "Database connection failed"}), 500

    try:
        cursor = connection.cursor()

        # Fetch employee basic details
        cursor.execute("""
            SELECT emp_id, employee_name, email_id, department, date_of_joining 
            FROM employee_details 
            WHERE emp_id = %s
        """, (emp_id,))
        row = cursor.fetchone()

        if not row:
            cursor.close()
            connection.close()
            return jsonify({"status": "error", "message": f"No employee found with ID {emp_id}"}), 404

        employee_data = {
            "emp_id": row[0],
            "employee_name": row[1],
            "email_id": row[2],
            "department": row[3],
            "date_of_joining": row[4].strftime("%Y-%m-%d")
        }

        # Fetch leave details
        cursor.execute("""
            SELECT leave_id, avaliable_leaves 
            FROM employee_leaves 
            WHERE emp_id = %s
        """, (emp_id,))
        leave_rows = cursor.fetchall()

        leaves = []
        for leave in leave_rows:
            leaves.append({
                "leave_id": leave[0],
                "avaliable_leaves": leave[1]
            })

        cursor.close()
        connection.close()

        return jsonify({
            "status": "success",
            "employee": employee_data,
            "leaves": leaves
        }), 200

    except Exception as e:
        return jsonify({"status": "error", "message": f"Error fetching employee: {str(e)}"}), 500


@app.route('/generate-salary-slip', methods=['POST'])
def generate_salary_slip():
    try:
        data = request.get_json()
        emp_id = data.get('employee_id')
        gross_salary = float(data.get('salary'))

        if not emp_id or not gross_salary:
            return jsonify({"status": "error", "message": "Missing employee_id or salary."}), 400

        # Deductions
        tax = round(0.05 * gross_salary, 2)
        pf = round(0.03 * gross_salary, 2)
        net_salary = round(gross_salary - tax - pf, 2)
        today = date.today()
        month_year = today.strftime("%B %Y")

        # Save to DB
        connection = db_connections()
        if connection:
            cursor = connection.cursor()
            # Generate new slip ID
            cursor.execute("SELECT slip_id FROM salary_slips ORDER BY slip_id DESC LIMIT 1")
            last_row = cursor.fetchone()
            if last_row and last_row[0]:
                last_num = int(last_row[0][2:])
                slip_id = f"SP{last_num + 1:03d}"
            else:
                slip_id = "SP001"

            cursor.execute("""
                INSERT INTO salary_slips (slip_id, emp_id, gross_salary, tax, pf, net_salary, slip_date)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (slip_id, emp_id, gross_salary, tax, pf, net_salary, today))

            connection.commit()
            cursor.close()
            connection.close()
        else:
            return jsonify({"status": "error", "message": "Database connection failed."}), 500

        # Generate PDF
        os.makedirs("salary_slips", exist_ok=True)
        pdf_path = f"salary_slips/{emp_id}_{today}.pdf"

        doc = SimpleDocTemplate(pdf_path, pagesize=letter)
        elements = []
        styles = getSampleStyleSheet()

        # Title
        elements.append(Paragraph("<b>STATSCOG LABS</b>", styles['Title']))
        elements.append(Paragraph(f"<b>Salary Slip for {month_year}</b>", styles['Heading2']))
        elements.append(Spacer(1, 12))

        # Table data
        data_table = [
            ["Slip ID", slip_id],
            ["Employee ID", emp_id],
            ["Date", str(today)],
            ["Gross Salary (₹)", f"{gross_salary:.2f}"],
            ["Tax (5%) (₹)", f"{tax:.2f}"],
            ["PF (3%) (₹)", f"{pf:.2f}"],
            ["Net Salary (₹)", f"{net_salary:.2f}"]
        ]

        table = Table(data_table, colWidths=[160, 300])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 0), (-1, -1), 11),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey)
        ]))

        elements.append(table)
        doc.build(elements)

        return jsonify({
            "status": "success",
            "message": f"Salary slip generated and saved as PDF for {emp_id}.",
            "pdf_path": pdf_path,
            "data": {
                    "slip_id": slip_id,
                    "employee_id": emp_id,
                    "gross_salary": gross_salary,
                    "tax": tax,
                    "pf": pf,
                    "net_salary": net_salary,
                    "slip_date": str(today)
                }
        }), 200

    except Exception as e:
        return jsonify({"status": "error", "message": f"Error: {str(e)}"}), 500


@app.route('/salary-slip/<emp_id>', methods=['GET'])
def get_salary_slip_link(emp_id):
    try:
        today = date.today()
        file_name = f"{emp_id}_{today}.pdf"
        file_path = os.path.join("salary_slips", file_name)

        if not os.path.exists(file_path):
            return jsonify({"status": "error", "message": "Salary slip not found."}), 404

        # 🔗 Use static base URL
        download_url = f"https://7a0f414dbad2.ngrok-free.app/download-slip/{file_name}"

        return jsonify({
            "status": "success",
            "message": "✅ Your latest salary slip is ready. Click the link below to download.",
            "download_link": download_url
        })

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500



@app.route('/download-slip/<filename>', methods=['GET'])
def download_salary_slip(filename):
    try:
        return send_from_directory('salary_slips', filename, as_attachment=True)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Error downloading file: {str(e)}"}), 500


if __name__ == '__main__':
    app.run(debug=True, port=5000)