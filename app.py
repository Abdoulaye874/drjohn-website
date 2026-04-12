from flask import Flask, render_template, request

app = Flask(__name__)

@app.route("/", methods=["GET", "POST"])
def home():
    message = ""

    if request.method == "POST":
        name = request.form.get("name")
        email = request.form.get("email")
        phone = request.form.get("phone")
        user_message = request.form.get("message")

        print("New contact form submission")
        print(f"Name: {name}")
        print(f"Email: {email}")
        print(f"Message: {user_message}")
        print(f"Phone: {phone}")

        message = "Thank you. Your message has been received."

    return render_template("index.html", message=message)

if __name__ == "__main__":
    app.run(debug=True)