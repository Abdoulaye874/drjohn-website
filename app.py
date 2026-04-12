from flask import Flask, render_template, request
import smtplib
import os
from email.mime.text import MIMEText

app = Flask(__name__)

@app.route("/", methods=["GET", "POST"])
def home():
    message = ""

    if request.method == "POST":
        try:
            name = request.form.get("name", "")
            email = request.form.get("email", "")
            phone = request.form.get("phone", "")
            user_message = request.form.get("message", "")

            email_user = os.environ.get("EMAIL_USER")
            email_pass = os.environ.get("EMAIL_PASS")
            to_email = os.environ.get("TO_EMAIL")

            if not email_user or not email_pass or not to_email:
                raise ValueError("Missing one or more email environment variables.")

            subject = "New Website Inquiry"
            body = f"""
You received a new message from your website.

Name: {name}
Email: {email}
Phone: {phone}

Message:
{user_message}
"""

            msg = MIMEText(body)
            msg["Subject"] = subject
            msg["From"] = email_user
            msg["To"] = to_email
            msg["Reply-To"] = email

            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(email_user, email_pass)
                server.send_message(msg)

            message = "Thanks! Your message was sent successfully."

        except Exception as e:
            print("FULL EMAIL ERROR:", repr(e))
            message = "Sorry, something went wrong. Please try again."

    return render_template("index.html", message=message)

if __name__ == "__main__":
    app.run(debug=True)