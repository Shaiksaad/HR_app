import os
import pandas as pd
from flask import Flask, render_template, request, redirect, url_for, jsonify
from werkzeug.utils import secure_filename
from markupsafe import Markup
import ibm_boto3
from ibm_botocore.client import Config
from io import StringIO
import re

COS_ENDPOINT = "https://s3.eu-gb.cloud-object-storage.appdomain.cloud"
COS_API_KEY_ID = "vGvzRIGC-XVsKLgjESPRgOuHPthk5jZax27uSnvke0Zy"
COS_INSTANCE_CRN = "crn:v1:bluemix:public:cloud-object-storage:global:a/0878c52f44f3a46eddeab2b446c934bf:a556abaa-af25-4882-8e36-d9f3412c1dd2:bucket:hr-datasource"
COS_BUCKET_NAME = "hr-datasource"

cos = ibm_boto3.client("s3",
    ibm_api_key_id=COS_API_KEY_ID,
    ibm_service_instance_id=COS_INSTANCE_CRN,
    config=Config(signature_version="oauth"),
    endpoint_url=COS_ENDPOINT
)


app = Flask(__name__)
UPLOAD_FOLDER = 'resumes'
DATA_FOLDER = 'data_source'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(DATA_FOLDER, exist_ok=True)

EXCEL_JOBS_FILE = os.path.join(DATA_FOLDER, 'jd_details.csv')
EXCEL_FORMS_FILE = os.path.join(DATA_FOLDER, 'job_form.csv')


def read_csv_from_cos(key):
    try:
        response = cos.get_object(Bucket=COS_BUCKET_NAME, Key=key)
        csv_body = response["Body"].read().decode("utf-8")
        return pd.read_csv(StringIO(csv_body))
    except Exception as e:
        print(f"⚠️ Error reading {key}: {e}")
        return pd.DataFrame()

def write_csv_to_cos(df, key):
    try:
        csv_buffer = StringIO()
        df.to_csv(csv_buffer, index=False)
        cos.put_object(Bucket=COS_BUCKET_NAME, Key=key, Body=csv_buffer.getvalue())
    except Exception as e:
        print(f"❌ Error writing {key}: {e}")


JD_CSV_KEY = "jd_details.csv"
FORM_CSV_KEY = "job_form.csv"

def load_jobs_df():
    return read_csv_from_cos(JD_CSV_KEY)

def save_jobs_df(df):
    write_csv_to_cos(df, JD_CSV_KEY)

def load_forms_df():
    return read_csv_from_cos(FORM_CSV_KEY)

def save_forms_df(df):
    write_csv_to_cos(df, FORM_CSV_KEY)


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


@app.route('/')
def index():
    # COS connection check and bucket content listing
    # try:
    #     cos.head_bucket(Bucket=COS_BUCKET_NAME)
    #     print("✅ Connected to IBM COS successfully!")
    #
    #     response = cos.list_objects_v2(Bucket=COS_BUCKET_NAME)
    #     if 'Contents' in response:
    #         print("📦 Bucket Contents:")
    #         for obj in response['Contents']:
    #             print(f" - {obj['Key']}")
    #     else:
    #         print("ℹ️ The bucket is currently empty.")
    #
    # except Exception as e:
    #     print(f"❌ Failed to connect to IBM COS: {e}")

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


if __name__ == '__main__':
    app.run(debug=True, port=5000)