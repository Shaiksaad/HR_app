import os
import re
import io
import smtplib
import ibm_boto3
import pandas as pd
from flask import Flask, render_template, request, redirect, url_for, jsonify
from werkzeug.utils import secure_filename
from markupsafe import Markup
from ibm_botocore.client import Config
from io import StringIO
from fpdf import FPDF
from reportlab.lib.pagesizes import A4
from reportlab.platypus import  Table, TableStyle
from reportlab.lib import colors
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from datetime import datetime
from email.mime.text import MIMEText
from email.message import EmailMessage
from dotenv import load_dotenv

load_dotenv()

COS_ENDPOINT = os.getenv("COS_ENDPOINT")
COS_API_KEY_ID = os.getenv("COS_API_KEY_ID")
COS_INSTANCE_CRN = os.getenv("COS_INSTANCE_CRN")
COS_BUCKET_NAME = os.getenv("COS_BUCKET_NAME")

cos = ibm_boto3.client("s3",
    ibm_api_key_id=COS_API_KEY_ID,
    ibm_service_instance_id=COS_INSTANCE_CRN,
    config=Config(signature_version="oauth"),
    endpoint_url=COS_ENDPOINT
)

EMAIL_HOST = "webmail.4technologies.in"
EMAIL_PORT = 587  # STARTTLS port
EMAIL_USERNAME = os.getenv("EMAIL_USERNAME")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")

app = Flask(__name__)
UPLOAD_FOLDER = 'resumes'
DATA_FOLDER = 'data_source'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(DATA_FOLDER, exist_ok=True)

EXCEL_JOBS_FILE = os.path.join(DATA_FOLDER, 'jd_details.csv')
EXCEL_FORMS_FILE = os.path.join(DATA_FOLDER, 'job_form.csv')

ZOOM_LINK = os.getenv("ZOOM_LINK")


def read_csv_from_cos(key):
    try:
        response = cos.get_object(Bucket=COS_BUCKET_NAME, Key=key)
        csv_body = response["Body"].read().decode("utf-8")
        return pd.read_csv(StringIO(csv_body))
    except Exception as e:
        print(f"⚠️ Error reading {key}: {e}")
        return pd.DataFrame()

def write_csv_to_cos(filename, df):
    csv_buffer = io.StringIO()
    df.to_csv(csv_buffer, index=False)
    cos.put_object(
        Bucket=COS_BUCKET_NAME,
        Key=filename,
        Body=csv_buffer.getvalue()
    )

JD_CSV_KEY = "jd_details.csv"
FORM_CSV_KEY = "job_form.csv"
EMPLOYEE_CSV_KEY = "employee_details.csv"

def load_jobs_df():
    return read_csv_from_cos(JD_CSV_KEY)

def save_jobs_df(df):
    write_csv_to_cos(df, JD_CSV_KEY)

def load_forms_df():
    return read_csv_from_cos(FORM_CSV_KEY)

def save_forms_df(df):
    write_csv_to_cos(df, FORM_CSV_KEY)

def load_employee_df():
    return read_csv_from_cos(EMPLOYEE_CSV_KEY)


@app.template_filter('truncate_words')
def truncate_words(s, num=40):
    if not s:
        return ''
    words = s.split()
    return ' '.join(words[:num]) + ('...' if len(words) > num else '')


@app.template_filter('clean_jd')
def clean_jd(text):
    if not text:
        return ""
    return text.replace('\\n', ' ').replace('\n', ' ').replace('\\', '').strip()


@app.template_filter('format_jd')
def format_jd(text):
    if not text:
        return ""

    sections = {
        "Summary:": "p",
        "Responsibilities:": "ul",
        "Required Skills:": "ul",
        "Preferred Qualifications:": "ul",
        "Experience Range:": "p",
        "Job Location:": "p"
    }

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


def extract_location(description):
    if not description:
        return "Unknown"

    # Remove line breaks and extra whitespace
    clean_desc = re.sub(r'\\n|\n', ' ', description)
    clean_desc = re.sub(r'\s+', ' ', clean_desc).strip()

    # Search for a location pattern
    match = re.search(r'(?:Job\s*)?Location\s*:\s*([A-Za-z ]+)', clean_desc, re.IGNORECASE)
    if match:
        return match.group(1).strip()

    # Fallback: try 'Location XYZ' pattern
    match_inline = re.search(r'Location\s+([A-Za-z ]+)', clean_desc, re.IGNORECASE)
    if match_inline:
        location = match_inline.group(1).strip()
        location = re.split(r'\b(Job\s*Type|About\s*Us|Summary)\b', location)[0].strip()
        return location if location else "Unknown"

    return "Unknown"

def generate_payslip_pdf(emp_data, slip_data, logo_path, output_path):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", "B", 16)

    # Title
    pdf.image(logo_path, x=10, y=8, w=30)
    pdf.cell(200, 10, txt="STATSCOG Labs PVT LTD", ln=True, align='C')
    pdf.ln(10)
    pdf.set_font("Arial", "", 12)

    # Header
    pdf.cell(200, 10, txt="Payslip (Electronically Generated)", ln=True, align='C')
    pdf.ln(10)

    # Table rows
    rows = [
        ("Payslip ID", slip_data["slip_id"]),
        ("Employee ID", emp_data["emp_id"]),
        ("Employee Name", emp_data["employee_name"]),
        ("Gross Salary", f"₹ {slip_data['gross_salary']:.2f}"),
        ("Tax (%)", f"{slip_data['tax']}%"),
        ("PF (%)", f"{slip_data['pf']}%"),
        ("Net Salary", f"₹ {slip_data['net_salary']:.2f}"),
        ("Slip Date", slip_data["slip_date"].strftime("%Y-%m-%d"))
    ]

    for label, value in rows:
        pdf.cell(60, 10, f"{label}:", 1)
        pdf.cell(120, 10, str(value), 1, ln=True)

    pdf.output(output_path)



@app.route('/')
def index():

    jobs_df = load_jobs_df()
    jobs = []

    for _, row in jobs_df.sort_values(by="job_date", ascending=False).iterrows():
        job_id = row['job_id']
        job_description = row['job_description']
        location = extract_location(job_description)

        jobs.append({
            "title": f"Job {job_id}",
            "company": "Statscog Labs",
            "location": location,
            "description": job_description[:100] + "...",
            "applyLink": f"/apply?job_id={job_id}",
            "viewLink": f"/job/{job_id}"
        })

    return render_template('index.html', jobs=jobs)


@app.route('/post-job-form', methods=['POST'])
def post_job_form():
    job_id = request.form.get('job_id')
    job_description = request.form.get('job_description')

    if job_id and job_description:
        df = load_jobs_df()
        df = pd.concat([df, pd.DataFrame([{
            "job_id": job_id,
            "job_description": job_description,
            "job_date": pd.Timestamp.now().normalize()
        }])], ignore_index=True)
        save_jobs_df(df)

    return redirect(url_for('index'))


@app.route('/job/<job_id>')
def job_detail(job_id):
    df = load_jobs_df()
    row = df[df["job_id"] == job_id]
    if row.empty:
        return render_template("job_details.html", job=None)

    job = row.iloc[0]
    return render_template("job_details.html", job={
        "job_id": job["job_id"],
        "title": f"Job {job['job_id']}",
        "company": "Statscog Labs",
        "location": "Unknown",
        "description": job["job_description"]
    })


@app.route('/apply', methods=['GET', 'POST'])
def apply_job():
    if request.method == 'POST':
        job_id = request.form.get('job_id')
        name = request.form.get('name')
        email = request.form.get('email')
        phone = request.form.get('phone_number')
        file = request.files['resume']

        df = load_forms_df()
        last_form_id = df['form_id'].dropna().iloc[-1] if not df.empty else "FM000"
        form_id = f"FM{int(last_form_id[2:]) + 1:03d}"

        if file:
            filename = secure_filename(file.filename)
            resume_key = f"resumes/{filename}"
            cos.upload_fileobj(file, COS_BUCKET_NAME, resume_key)

            df = pd.concat([df, pd.DataFrame([{
                "form_id": form_id,
                "job_id": job_id,
                "name": name,
                "email": email,
                "phone_number": phone,
                "resume": filename,
                "form_date": pd.Timestamp.now().normalize()
            }])], ignore_index=True)

            save_forms_df(df)

        return f"""
            <script>
                alert("✅ Application submitted successfully! Your Form ID is {form_id}");
                window.location.href = "/";
            </script>
        """

    job_id = request.args.get('job_id')
    return render_template('job_apply.html', job_id=job_id)


@app.route('/api/applicants/<job_id>', methods=['GET'])
def get_applicants_by_job(job_id):
    df = load_forms_df()
    df_job = df[df["job_id"] == job_id]

    applicants = []
    for _, row in df_job.iterrows():
        applicants.append({
            "form_id": row["form_id"],
            "name": row["name"],
            "email": row["email"],
            "phone": row["phone_number"],
            "resume_file": row["resume"],
            "applied_on": row["form_date"].strftime("%Y-%m-%d")
        })

    return jsonify({"job_id": job_id, "applicants": applicants})


@app.route('/send-email', methods=['POST'])
def send_email():
    data = request.get_json()
    recipient = data.get("recipient_email")
    subject = data.get("subject")
    body = data.get("body")

    if not recipient or not subject or not body:
        return jsonify({"status": "error", "message": "Missing required fields."}), 400

    try:
        msg = MIMEText(body, "plain")
        msg["Subject"] = subject
        msg["From"] = EMAIL_USERNAME
        msg["To"] = recipient

        server = smtplib.SMTP(EMAIL_HOST, EMAIL_PORT)
        server.starttls()
        server.login(EMAIL_USERNAME, EMAIL_PASSWORD)
        server.sendmail(EMAIL_USERNAME, [recipient], msg.as_string())
        server.quit()

        return jsonify({"status": "success", "message": f"Email sent to {recipient}."})

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/schedule-zoom-meeting', methods=['POST'])
def schedule_zoom_meeting():
    data = request.get_json()
    meeting_date = data.get("date")   # Expected format: YYYY-MM-DD
    meeting_time = data.get("time")   # Expected format: HH:MM (24hr)

    if not meeting_date or not meeting_time:
        return jsonify({"status": "error", "message": "Both 'date' and 'time' are required."}), 400

    try:
        # Validate date and time
        scheduled_dt = datetime.strptime(f"{meeting_date} {meeting_time}", "%Y-%m-%d %H:%M")
        formatted_date = scheduled_dt.strftime("%B %d, %Y")
        formatted_time = scheduled_dt.strftime("%I:%M %p")

        return jsonify({
            "status": "success",
            "message": "Zoom meeting scheduled successfully.",
            "meeting_details": {
                "Date": formatted_date,
                "Time": formatted_time,
                "Zoom_Link": ZOOM_LINK
            }
        })

    except ValueError:
        return jsonify({"status": "error", "message": "Invalid date or time format. Use YYYY-MM-DD and HH:MM (24hr)."}), 400


@app.route('/delete-job', methods=['POST'])
def delete_job_post():
    job_id = request.form.get('job_id')
    if not job_id:
        return jsonify({"status": "error", "message": "Missing job_id"}), 400

    df = load_jobs_df()
    if job_id not in df['job_id'].values:
        return jsonify({"status": "error", "message": f"Job ID '{job_id}' not found."}), 404

    df = df[df['job_id'] != job_id]
    save_jobs_df(df)

    return jsonify({"status": "success", "message": f"Job ID '{job_id}' deleted successfully."})


@app.route('/api/employee', methods=['POST'])
def get_employee_by_id_post():
    data = request.get_json()
    emp_id = data.get('emp_id')

    if not emp_id:
        return jsonify({"status": "error", "message": "Missing emp_id"}), 400

    df = load_employee_df()
    row = df[df['emp_id'] == emp_id]

    if row.empty:
        return jsonify({"status": "error", "message": f"Employee ID '{emp_id}' not found"}), 404

    employee = row.iloc[0]
    return jsonify({
        "status": "success",
        "employee": {
            "emp_id": employee['emp_id'],
            "employee_name": employee['employee_name'],
            "email_id": employee['email_id'],
            "department": employee['department'],
            "date_of_joining": employee['date_of_joining']
        }
    })


@app.route('/generate-payslip', methods=['POST'])
def generate_payslip():
    data = request.get_json()
    emp_id = data.get("emp_id")
    gross_salary = data.get("gross_salary")

    if not emp_id or gross_salary is None:
        return jsonify({"status": "error", "message": "emp_id and gross_salary are required."}), 400

    try:
        # Load employee details
        df = read_csv_from_cos("employee_details.csv")
        emp_row = df[df["emp_id"] == emp_id]

        if emp_row.empty:
            return jsonify({"status": "error", "message": f"Employee ID '{emp_id}' not found."}), 404

        employee_name = emp_row.iloc[0]["employee_name"]

        # Generate slip ID in SP001 format
        slip_csv_df = read_csv_from_cos("salary_slips.csv")
        next_slip_num = len(slip_csv_df) + 1
        slip_id = f"SP{str(next_slip_num).zfill(3)}"

        slip_date = datetime.now().date()
        month_str = slip_date.strftime("%B")

        # Salary calculations
        tax_percent = 10
        pf_percent = 5
        tax = round(gross_salary * tax_percent / 100, 2)
        pf = round(gross_salary * pf_percent / 100, 2)
        net_salary = round(gross_salary - tax - pf, 2)

        # PDF Generation
        filename = f"{emp_id}_{month_str}_Payslip.pdf"
        filepath = os.path.join("salary_slips", filename)
        os.makedirs("salary_slips", exist_ok=True)

        c = canvas.Canvas(filepath, pagesize=A4)
        width, height = A4

        # Company logo and name side-by-side
        logo_path = os.path.join("static", "statslogo.png")
        if os.path.exists(logo_path):
            logo = ImageReader(logo_path)
            c.drawImage(logo, 40, height - 90, width=40, height=30, mask='auto')

        c.setFont("Helvetica-Bold", 16)
        c.drawString(100, height - 75, "STATSCOG Labs PVT LTD")  # Positioned next to logo

        # Bolded slip month and year
        c.setFont("Helvetica-Bold", 11)
        c.drawString(40, height - 120, f"Payslip for {month_str}, {slip_date.year}")
        c.line(40, height - 125, width - 40, height - 125)

        # Build table data
        table_data = [
            ["Payslip ID", slip_id],
            ["Employee ID", emp_id],
            ["Employee Name", employee_name],
            ["Gross Salary", f"{gross_salary:.2f}"],
            ["Tax (10%)", f"{tax:.2f}"],
            ["PF (5%)", f"{pf:.2f}"],
            ["Net Salary", f"{net_salary:.2f}"],
            ["Slip Date", slip_date.strftime("%Y-%m-%d")]
        ]

        table = Table(table_data, colWidths=[2.5 * inch, 3.5 * inch])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (0, -1), colors.lightgrey),
            ('TEXTCOLOR', (0, 0), (-1, -1), colors.black),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
            ('FONTNAME', (1, 0), (1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 0), (-1, -1), 11),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ]))

        table.wrapOn(c, width, height)
        table.drawOn(c, 40, height - 400)

        c.setFont("Helvetica-Oblique", 9)
        c.drawString(40, 60, "Note: This is an electronically generated payslip and does not require signature.")
        c.showPage()
        c.save()

        # Upload the PDF to IBM COS
        with open(filepath, "rb") as file_data:
            cos.put_object(
                Bucket=COS_BUCKET_NAME,
                Key=f"salary_slips/{filename}",
                Body=file_data
            )
        os.remove(filepath)

        # Append metadata to salary_slips.csv in COS
        try:
            # Try to read existing salary_slips.csv, else start fresh
            try:
                slip_csv_df = read_csv_from_cos("salary_slips.csv")
            except:
                slip_csv_df = pd.DataFrame(
                    columns=["slip_id", "emp_id", "gross_salary", "tax", "pf", "net_salary", "slip_date"])

            new_row = {
                "slip_id": slip_id,
                "emp_id": emp_id,
                "gross_salary": gross_salary,
                "tax": tax,
                "pf": pf,
                "net_salary": net_salary,
                "slip_date": slip_date.strftime("%Y-%m-%d")
            }

            updated_df = pd.concat([slip_csv_df, pd.DataFrame([new_row])], ignore_index=True)
            write_csv_to_cos("salary_slips.csv", updated_df)

        except Exception as metadata_error:
            print(f"[WARN] Salary slip metadata not saved: {metadata_error}")

        return jsonify({
            "status": "success",
            "message": "Payslip generated and uploaded successfully.",
            "slip_id": slip_id,
            "net_salary": net_salary,
            "month": month_str,
            "file_name": filename
        })

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/get-latest-payslip', methods=['POST'])
def get_latest_payslip():
    try:
        data = request.get_json()
        emp_id = data.get("emp_id")

        if not emp_id:
            return jsonify({"status": "error", "message": "emp_id is required"}), 400

        # List objects in salary_slips folder
        response = cos.list_objects_v2(Bucket=COS_BUCKET_NAME, Prefix="salary_slips/")
        files = response.get("Contents", [])

        # Filter files matching emp_id in the filename
        matching_files = [f for f in files if f['Key'].startswith(f'salary_slips/{emp_id}_') and f['Key'].endswith('.pdf')]

        if not matching_files:
            return jsonify({"status": "error", "message": f"No payslip found for {emp_id}"}), 404

        # Pick the latest one
        latest_file = max(matching_files, key=lambda f: f['LastModified'])
        latest_key = latest_file['Key']
        filename = latest_key.split("/")[-1]

        # Construct public URL (as per your example)
        public_url = f"https://{COS_BUCKET_NAME}.s3.eu-gb.cloud-object-storage.appdomain.cloud/{latest_key}"

        return jsonify({
            "status": "success",
            "message": f"Latest payslip for {emp_id} retrieved successfully.",
            "filename": filename,
            "download_url": public_url
        })

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/send-email-to-hr', methods=['POST'])
def send_email_to_hr():
    try:
        data = request.get_json()
        emp_id = data.get("emp_id")
        subject = data.get("subject")
        message_body = data.get("message")

        if not emp_id or not subject or not message_body:
            return jsonify({"status": "error", "message": "emp_id, subject, and message are required"}), 400

        # Load employee email
        df = read_csv_from_cos("employee_details.csv")
        emp_row = df[df["emp_id"] == emp_id]

        if emp_row.empty:
            return jsonify({"status": "error", "message": f"Employee ID '{emp_id}' not found."}), 404

        sender_email = emp_row.iloc[0]["email_id"]
        sender_password = os.getenv("EMAIL_PASSWORD")  # Best practice: set this in your env or secrets vault
        smtp_server = "webmail.4technologies.in"
        smtp_port = 587
        hr_email = "saad.shaik@4technologies.in"

        # Create the email
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = sender_email
        msg["To"] = hr_email
        msg.set_content(message_body)

        # Send the email
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(sender_email, sender_password)
            server.send_message(msg)

        return jsonify({"status": "success", "message": f"Email sent to HR from {sender_email}"}), 200

    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True)