import os
import json
import sqlite3
import smtplib
from email.message import EmailMessage

try:
    import winreg
except ImportError:
    winreg = None

from flask import Flask, flash, render_template, redirect, request, url_for, session
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

try:
    import stripe
except ImportError:
    stripe = None

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "your_secret_key_here")
SHIPPING_COST = 12.50
DATABASE_PATH = os.path.join(app.root_path, "users.db")


def get_db_connection():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_users_table():
    with get_db_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                email TEXT,
                passwords TEXT NOT NULL
            )
            """
        )
        columns = {
            column["name"] for column in connection.execute("PRAGMA table_info(users)").fetchall()
        }
        if "email" not in columns:
            connection.execute("ALTER TABLE users ADD COLUMN email TEXT")
        connection.commit()


def init_orders_table():
    with get_db_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stripe_session_id TEXT UNIQUE,
                stripe_payment_intent_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                customer_json TEXT NOT NULL,
                items_json TEXT NOT NULL,
                subtotal REAL NOT NULL,
                shipping REAL NOT NULL,
                total REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.commit()


def get_user_by_username(username):
    with get_db_connection() as connection:
        return connection.execute(
            "SELECT id, username, email, passwords FROM users WHERE username = ?",
            (username,),
        ).fetchone()


def get_user_by_email(email):
    with get_db_connection() as connection:
        return connection.execute(
            "SELECT id, username, email, passwords FROM users WHERE email = ?",
            (email,),
        ).fetchone()


def create_user(username, email, password):
    with get_db_connection() as connection:
        connection.execute(
            "INSERT INTO users (username, email, passwords) VALUES (?, ?, ?)",
            (username, email, generate_password_hash(password)),
        )
        connection.commit()


def update_user_password(user_id, password):
    with get_db_connection() as connection:
        connection.execute(
            "UPDATE users SET passwords = ? WHERE id = ?",
            (generate_password_hash(password), user_id),
        )
        connection.commit()


def password_matches(user, password):
    stored_password = user["passwords"]

    if stored_password == password:
        update_user_password(user["id"], password)
        return True

    return check_password_hash(stored_password, password)


def get_current_user():
    username = session.get("user")

    if not username:
        return None

    return get_user_by_username(username)


def get_reset_serializer():
    return URLSafeTimedSerializer(app.secret_key)


init_users_table()
init_orders_table()


def clear_user_session_data():
    session.pop("cart", None)
    session.pop("last_order", None)


def start_user_session(username):
    previous_user = session.get("user")

    if previous_user != username:
        clear_user_session_data()

    session["user"] = username


def get_setting(name, default=None):
    value = os.environ.get(name)

    if value:
        return value

    if winreg is None:
        return default

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            registry_value, _ = winreg.QueryValueEx(key, name)
            return registry_value or default
    except OSError:
        return default


def price_to_float(price):
    return float(price.replace("$", ""))


def dollars_to_cents(amount):
    return int(round(amount * 100))


def get_cart_summary():
    cart = session.get("cart", {})
    cart_items = []
    subtotal = 0

    for product_slug, quantity in cart.items():
        product = get_store_item(product_slug)

        if product:
            item_total = price_to_float(product["price"]) * quantity
            subtotal += item_total
            cart_items.append({
                "slug": product_slug,
                "product": product,
                "quantity": quantity,
                "item_total": item_total
            })

    shipping = SHIPPING_COST if cart_items else 0
    total = subtotal + shipping

    return {
        "cart_items": cart_items,
        "subtotal": subtotal,
        "shipping": shipping,
        "total": total
    }


def get_stripe_secret_key():
    return get_setting("STRIPE_SECRET_KEY", "")


def get_stripe_webhook_secret():
    return get_setting("STRIPE_WEBHOOK_SECRET", "")


def get_payment_settings():
    stripe_secret_key = get_stripe_secret_key()
    return {
        "method": "stripe",
        "is_configured": bool(stripe and stripe_secret_key),
        "publishable_key": get_setting("STRIPE_PUBLISHABLE_KEY", "")
    }


def get_base_url():
    configured_url = get_setting("SITE_URL", "").rstrip("/")

    if configured_url:
        return configured_url

    return request.url_root.rstrip("/")


def serialize_order_items(cart_items):
    return [
        {
            "slug": item["slug"],
            "product": {
                "name": item["product"]["name"],
                "price": item["product"]["price"],
                "image": item["product"].get("image"),
                "image_pending": item["product"].get("image_pending", False),
                "photo_blend": item["product"].get("photo_blend", False),
                "wide_photo": item["product"].get("wide_photo", False),
            },
            "quantity": item["quantity"],
            "item_total": item["item_total"],
        }
        for item in cart_items
    ]


def create_order(summary, customer):
    items = serialize_order_items(summary["cart_items"])

    with get_db_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO orders (
                status, customer_json, items_json, subtotal, shipping, total
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "pending",
                json.dumps(customer),
                json.dumps(items),
                summary["subtotal"],
                summary["shipping"],
                summary["total"],
            ),
        )
        connection.commit()
        return cursor.lastrowid


def update_order_payment(order_id, status, stripe_session_id=None, stripe_payment_intent_id=None):
    with get_db_connection() as connection:
        connection.execute(
            """
            UPDATE orders
            SET status = ?,
                stripe_session_id = COALESCE(?, stripe_session_id),
                stripe_payment_intent_id = COALESCE(?, stripe_payment_intent_id)
            WHERE id = ?
            """,
            (status, stripe_session_id, stripe_payment_intent_id, order_id),
        )
        connection.commit()


def get_order_by_id(order_id):
    with get_db_connection() as connection:
        row = connection.execute(
            """
            SELECT id, stripe_session_id, stripe_payment_intent_id, status,
                   customer_json, items_json, subtotal, shipping, total
            FROM orders
            WHERE id = ?
            """,
            (order_id,),
        ).fetchone()

    return format_order(row) if row else None


def get_order_by_stripe_session(stripe_session_id):
    with get_db_connection() as connection:
        row = connection.execute(
            """
            SELECT id, stripe_session_id, stripe_payment_intent_id, status,
                   customer_json, items_json, subtotal, shipping, total
            FROM orders
            WHERE stripe_session_id = ?
            """,
            (stripe_session_id,),
        ).fetchone()

    return format_order(row) if row else None


def format_order(row):
    return {
        "id": row["id"],
        "stripe_session_id": row["stripe_session_id"],
        "stripe_payment_intent_id": row["stripe_payment_intent_id"],
        "status": row["status"],
        "customer": json.loads(row["customer_json"]),
        "items": json.loads(row["items_json"]),
        "subtotal": row["subtotal"],
        "shipping": row["shipping"],
        "total": row["total"],
        "payment": get_payment_settings(),
    }


def create_stripe_checkout_session(order_id, summary, customer):
    if stripe is None:
        raise RuntimeError("The Stripe package is not installed.")

    stripe.api_key = get_stripe_secret_key()

    if not stripe.api_key:
        raise RuntimeError("Stripe is not configured.")

    line_items = []

    for item in summary["cart_items"]:
        line_items.append(
            {
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": item["product"]["name"],
                    },
                    "unit_amount": dollars_to_cents(price_to_float(item["product"]["price"])),
                },
                "quantity": item["quantity"],
            }
        )

    if summary["shipping"]:
        line_items.append(
            {
                "price_data": {
                    "currency": "usd",
                    "product_data": {
                        "name": "Shipping",
                    },
                    "unit_amount": dollars_to_cents(summary["shipping"]),
                },
                "quantity": 1,
            }
        )

    base_url = get_base_url()

    return stripe.checkout.Session.create(
        mode="payment",
        line_items=line_items,
        customer_email=customer["email"],
        client_reference_id=str(order_id),
        metadata={"order_id": str(order_id)},
        success_url=f"{base_url}{url_for('order_confirmation')}?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base_url}{url_for('checkout')}?payment=cancelled",
    )


def send_consultation_email(name, email, phone, message):
    smtp_host = get_setting("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(get_setting("SMTP_PORT", "587"))
    smtp_username = get_setting("SMTP_USERNAME", "resultshealthsupplements@gmail.com")
    smtp_password = get_setting("SMTP_PASSWORD")
    consultation_recipient = get_setting("CONSULTATION_TO", "resultshealthsupplements@gmail.com")

    if not smtp_username or not smtp_password or not consultation_recipient:
        raise RuntimeError("Consultation email settings are not configured.")

    email_message = EmailMessage()
    email_message["Subject"] = f"New consultation request from {name}"
    email_message["From"] = smtp_username
    email_message["To"] = consultation_recipient
    email_message["Reply-To"] = email
    email_message.set_content(
        "\n".join(
            [
                "A new consultation request was submitted.",
                "",
                f"Name: {name}",
                f"Email: {email}",
                f"Phone: {phone or 'Not provided'}",
                "",
                "Message:",
                message,
            ]
        )
    )

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_username, smtp_password)
        server.send_message(email_message)


def send_password_reset_email(user):
    smtp_host = get_setting("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(get_setting("SMTP_PORT", "587"))
    smtp_username = get_setting("SMTP_USERNAME", "resultshealthsupplements@gmail.com")
    smtp_password = get_setting("SMTP_PASSWORD")

    if not smtp_username or not smtp_password or not user["email"]:
        raise RuntimeError("Password reset email settings are not configured.")

    token = get_reset_serializer().dumps(user["username"], salt="password-reset")
    reset_link = url_for("reset_password", token=token, _external=True)

    email_message = EmailMessage()
    email_message["Subject"] = "Reset your DRJOHN RESULTS password"
    email_message["From"] = smtp_username
    email_message["To"] = user["email"]
    email_message.set_content(
        "\n".join(
            [
                f"Hello {user['username']},",
                "",
                "We received a request to reset your password.",
                "Use the link below to choose a new password:",
                reset_link,
                "",
                "This link expires in 1 hour.",
                "If you did not request this, you can ignore this email.",
            ]
        )
    )

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_username, smtp_password)
        server.send_message(email_message)


@app.context_processor
def inject_cart_count():
    cart = session.get("cart", {})
    return {
        "cart_count": sum(cart.values()),
        "current_user": session.get("user")
    }

# ------------------ PRODUCTS ------------------
products = {
    "pain-relief": {
        "name": "Pain Relief",
        "description": "Herbal wellness support for everyday comfort and body balance.",
        "benefits": [
            "Supports everyday comfort",
            "Promotes body balance",
            "Crafted with organic herbal ingredients"
        ],
        "category": "Comfort",
        "price": "$35.00",
        "image": "product images/IMG_0615.webp",
        "bottle_image": "product images/IMG_0615.webp",
        "featured": True
    },
    "super-human-immune-system": {
        "name": "Super Human Immune System",
        "description": "Immune-focused herbal support for year-round wellness.",
        "benefits": [
            "Supports immune wellness",
            "Helps maintain natural resilience",
            "Crafted with organic herbal ingredients"
        ],
        "category": "Immune Support",
        "price": "$89.99",
        "image": "images/#7shis super human immune system.png",
        "bottle_image": "images/#7shis super human immune system.png",
        "featured": True
    },
    "blood-pressure-health": {
        "name": "Blood Pressure Health",
        "description": "Herbal support for circulation, balance, and cardiovascular wellness.",
        "benefits": [
            "Supports cardiovascular wellness",
            "Promotes body balance",
            "Supports healthy circulation"
        ],
        "category": "Circulation",
        "price": "$35.00",
        "image": "product images/c3378245-61a1-418a-ba0b-55aaba66e6f3 copy.webp",
        "bottle_image": "product images/c3378245-61a1-418a-ba0b-55aaba66e6f3 copy.webp",
        "featured": True
    },
    "infinity": {
        "name": "Infinity",
        "description": "Supports focus, clarity, vitality, stamina, and energy through a blend of organic herbs.",
        "benefits": [
            "Improves focus and concentration",
            "Boosts energy and stamina",
            "Supports overall vitality"
        ],
        "category": "Energy & Focus",
        "price": "$89.99",
        "image": "images/infinity 2.png",
        "bottle_image": "images/infinity 2.png",
        "photo_blend": True,
        "featured": True
    },
    "circulation-health": {
        "name": "Circulation Health",
        "description": "Supports healthy blood circulation and body function.",
        "benefits": [
            "Supports circulation",
            "Promotes body balance",
            "Supports overall wellness"
        ],
        "category": "Circulation",
        "price": "$35.00",
        "image": "images/circulation health.png",
        "bottle_image": "images/circulation health.png"
    },
    "infection-relief": {
        "name": "Infection Relief",
        "description": "Immune-focused herbal support for natural wellness routines.",
        "benefits": [
            "Supports immune wellness",
            "Helps the body maintain balance",
            "Crafted with organic herbal ingredients"
        ],
        "category": "Immune Support",
        "price": "$35.00",
        "image": "images/Infr.png",
        "bottle_image": "images/Infr.png"
    },
    "sugar-regulator-health": {
        "name": "Sugar Regulator Health",
        "description": "Supports balanced insulin production and sugar regulation.",
        "benefits": [
            "Supports sugar balance",
            "Supports pancreas wellness",
            "Promotes body balance"
        ],
        "category": "Sugar Balance",
        "price": "$35.00",
        "image": "product images/9f50fe20-1255-4922-98ee-12643efcdcc6 copy.webp",
        "bottle_image": "product images/9f50fe20-1255-4922-98ee-12643efcdcc6 copy.webp"
    },
    "beyond-cancer-body-mind-health": {
        "name": "Frances Irene Beyond Cancer",
        "description": "Herbal wellness support for body-mind balance and daily vitality.",
        "benefits": [
            "Supports whole-body wellness",
            "Promotes natural balance",
            "Crafted with organic herbal ingredients"
        ],
        "category": "Whole Body",
        "price": "$89.99",
        "image": "images/Irene.png",
        "bottle_image": "images/Irene.png",
        "featured": True
    },
    "digestion-elimination-health": {
        "name": "Digestion Elimination Health",
        "description": "Supports digestion, absorption, and healthy elimination.",
        "benefits": [
            "Supports digestion",
            "Helps with absorption",
            "Promotes healthy elimination"
        ],
        "category": "Digestion",
        "price": "$35.00",
        "image": "product images/d485b4f7-e327-4811-9874-e2192ec3e26d.png",
        "bottle_image": "product images/d485b4f7-e327-4811-9874-e2192ec3e26d.png",
        "featured": True
    },
    "probiotic-health": {
        "name": "Probiotic Health",
        "description": "Probiotic support for digestive balance and daily wellness.",
        "benefits": [
            "Supports digestive wellness",
            "Promotes healthy balance",
            "Supports everyday wellness"
        ],
        "category": "Digestion",
        "price": "$35.00",
        "image": "images/Probiotic.png",
        "bottle_image": "images/Probiotic.png"
    },
    "prostate-health": {
        "name": "Prostate Health",
        "description": "Herbal support for prostate and urinary wellness.",
        "benefits": [
            "Supports prostate wellness",
            "Promotes body balance",
            "Crafted with organic herbal ingredients"
        ],
        "category": "Men's Health",
        "price": "$35.00",
        "image": "images/ph.png",
        "bottle_image": "images/ph.png"
    },
    "diabetes-relief-health": {
        "name": "Diabetes-Relief Health",
        "description": "Herbal support for sugar balance and liver wellness.",
        "benefits": [
            "Supports sugar regulation",
            "Supports liver cleansing",
            "Promotes balance"
        ],
        "category": "Sugar Balance",
        "price": "$35.00",
        "image": "images/Diabetes relief healh.png",
        "bottle_image": "images/Diabetes relief healh.png"
    },
    "kidney-health": {
        "name": "Kidney Health",
        "description": "Supports healthy kidney function.",
        "benefits": [
            "Supports kidney wellness",
            "Promotes body balance",
            "Crafted with herbal ingredients"
        ],
        "category": "Whole Body",
        "price": "$35.00",
        "image": "product images/b68727ed-e463-4369-83ec-f37c0c5b879d.png",
        "bottle_image": "product images/b68727ed-e463-4369-83ec-f37c0c5b879d.png"
    },
    "big-mack-mind-memory": {
        "name": "Big-Mack Mind Memory",
        "description": "Herbal support for memory, focus, and mental clarity.",
        "benefits": [
            "Supports memory and focus",
            "Promotes mental clarity",
            "Crafted with organic herbal ingredients"
        ],
        "category": "Energy & Focus",
        "price": "$49.99",
        "image": "images/BMMM.png",
        "bottle_image": "images/BMMM.png"
    },
    "liver-cleanse": {
        "name": "Liver Cleanse",
        "description": "Herbal support for liver wellness, natural filtration, and daily vitality.",
        "benefits": [
            "Supports the body's natural filtration system",
            "Promotes digestive and metabolic balance",
            "Helps support energy, clarity, and overall wellness"
        ],
        "category": "Whole Body",
        "price": "$35.00",
        "image": "images/Liver cleanse.JPG",
        "bottle_image": "images/Liver cleanse.JPG",
        "video": "images/Liver cleanse video .mp4",
        "wide_photo": True,
        "detail_intro": "How Do You Know When Your Liver Needs Cleansing?",
        "detail_sections": [
            {
                "title": "Your Body's Primary Filter",
                "paragraphs": [
                    "Every day, your three-pound liver works tirelessly to help keep you alive and healthy. As the second-largest organ in the body after the skin, the liver performs hundreds of vital functions essential to your well-being.",
                    "This remarkable organ acts as the body's primary filtration system, processing everything you eat and drink. It helps neutralize and eliminate toxins, supports healthy blood sugar levels, stores excess glucose as glycogen for future energy needs, and converts excess carbohydrates and proteins into forms the body can store and use later.",
                    "Because the liver filters and processes so many substances, it can become overburdened by poor diet, environmental toxins, medications, alcohol, and chronic stress. When overwhelmed, its ability to function efficiently may decline, potentially affecting your overall health and vitality."
                ]
            },
            {
                "title": "Think About It",
                "paragraphs": [
                    "If you owned an automobile for 40 years and never changed the oil, never replaced the filter, and never performed routine maintenance, how well do you think that vehicle would run?",
                    "Eventually, the engine would become clogged, performance would decline, and breakdowns would become inevitable.",
                    "Your body works much the same way. The liver is the body's primary filter, processing everything you eat, drink, breathe, and absorb. Day after day, year after year, it works tirelessly to remove waste and toxins while helping maintain balance throughout the body.",
                    "Just as a vehicle requires regular maintenance to perform at its best, your liver deserves support to function efficiently and help keep you healthy, energized, and thriving."
                ]
            }
        ],
        "support_signs": [
            "Persistent fatigue or low energy levels",
            "Dark circles under the eyes",
            "A yellowish tint to the skin or eyes",
            "Liver spots or changes in skin pigmentation",
            "Discomfort, fullness, or tenderness on the right side of the abdomen",
            "Regular or moderate alcohol consumption",
            "Difficulty digesting fatty foods",
            "Bloating or digestive discomfort",
            "Brain fog or difficulty concentrating",
            "Unexplained weight gain or difficulty losing weight"
        ],
        "detail_note": "These symptoms do not necessarily indicate liver disease, but they may suggest your liver is working harder than normal and could benefit from lifestyle changes and nutritional support."
    },
}

book_products = {
    "afro-truism-book": {
        "name": "DRJOHN RESULTS Afro-truism",
        "description": "Adult luxury comic book edition.",
        "benefits": [
            "Graphic novel-style DRJOHN RESULTS feature",
            "Collector's edition presentation",
            "Books/Audios catalog item"
        ],
        "category": "Books/Audios",
        "price": "$35.00",
        "image": "images/Screenshot 2026-06-01 213813.png",
        "book_file": "images/FRONT COVER 2026.pdf"
    },
    "spiritual-coma-book": {
        "name": "The Spiritual-Coma",
        "description": "Adult luxury comic book edition.",
        "benefits": [
            "Graphic novel-style DRJOHN RESULTS feature",
            "Collector's edition presentation",
            "Books/Audios catalog item"
        ],
        "category": "Books/Audios",
        "price": "$35.00",
        "image": "images/Screenshot 2026-06-04 143407.png"
    },
    "cancer-is-dead-book": {
        "name": "Cancer is Dead",
        "description": "Adult luxury comic book edition.",
        "benefits": [
            "Graphic novel-style DRJOHN RESULTS feature",
            "Collector's edition presentation",
            "Books/Audios catalog item"
        ],
        "category": "Books/Audios",
        "price": "$35.00",
        "image": "images/Screenshot 2026-06-04 143358.png"
    },
    "drjohn-results-book": {
        "name": "DRJOHN RESULTS",
        "description": "Adult luxury comic book edition.",
        "benefits": [
            "Graphic novel-style DRJOHN RESULTS feature",
            "Collector's edition presentation",
            "Books/Audios catalog item"
        ],
        "category": "Books/Audios",
        "price": "$35.00",
        "image_pending": True
    },
    "megalomania-diary-of-whiteness-book": {
        "name": "The Megalomania Diary of Whiteness",
        "description": "Adult luxury comic book edition.",
        "benefits": [
            "Graphic novel-style DRJOHN RESULTS feature",
            "Collector's edition presentation",
            "Books/Audios catalog item"
        ],
        "category": "Books/Audios",
        "price": "$35.00",
        "image_pending": True
    },
    "living-demonstration-book": {
        "name": "Living Demonstration",
        "description": "Adult luxury comic book edition.",
        "benefits": [
            "Graphic novel-style DRJOHN RESULTS feature",
            "Collector's edition presentation",
            "Books/Audios catalog item"
        ],
        "category": "Books/Audios",
        "price": "$35.00",
        "image": "images/Screenshot 2026-06-04 143326.png"
    },
    "live-like-an-orgasm-life-book": {
        "name": "How To Live Like an Orgasm Life",
        "description": "Adult luxury comic book edition.",
        "benefits": [
            "Graphic novel-style DRJOHN RESULTS feature",
            "Collector's edition presentation",
            "Books/Audios catalog item"
        ],
        "category": "Books/Audios",
        "price": "$35.00",
        "image": "images/Screenshot 2026-06-04 140956.png"
    },
    "cosmic-drjohn-results-book": {
        "name": "DRJOHN RESULTS Cosmic Edition",
        "description": "Adult luxury comic book edition.",
        "benefits": [
            "Graphic novel-style DRJOHN RESULTS feature",
            "Collector's edition presentation",
            "Books/Audios catalog item"
        ],
        "category": "Books/Audios",
        "price": "$35.00",
        "image": "images/Screenshot 2026-06-04 140913.png"
    }
}


def get_store_item(item_slug):
    return products.get(item_slug) or book_products.get(item_slug)

# ------------------ ROUTES ------------------

@app.route("/")
def home():
    featured_products = {
        slug: product for slug, product in products.items()
        if product.get("featured") and not product.get("image_pending")
    }
    featured_products = dict(list(featured_products.items())[:4])
    return render_template("index.html", featured_products=featured_products)

@app.route("/shop")
def shop():
    categories = sorted({product["category"] for product in products.values()})
    selected_category = request.args.get("category", "all")

    if selected_category in categories:
        visible_products = {
            slug: product for slug, product in products.items()
            if product["category"] == selected_category
        }
    else:
        selected_category = "all"
        visible_products = products

    return render_template(
        "shop.html",
        products=visible_products,
        categories=categories,
        selected_category=selected_category
    )

@app.route("/consultation", methods=["GET", "POST"])
def consultation():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip()
        phone = request.form.get("phone", "").strip()
        message = request.form.get("message", "").strip()

        if not name or not email or not message:
            flash("Please complete the required fields before submitting.", "error")
            return render_template("consultation.html")

        try:
            send_consultation_email(name, email, phone, message)
        except Exception:
            flash("The consultation form is not ready to send email yet. Please finish the email setup.", "error")
            return render_template("consultation.html")

        flash("Your consultation request was sent successfully.", "success")
        return redirect(url_for("consultation"))

    return render_template("consultation.html")

@app.route("/testimonials")
def testimonials():
    return render_template("testimonials.html")


@app.route("/book")
def book():
    return redirect(url_for("book_drjohn"))


@app.route("/books-audios/drjohn")
def book_drjohn():
    return render_template("book_drjohn.html")


@app.route("/books-audios/frances-irene")
def book_frances_irene():
    return render_template("book_frances_irene.html")

@app.route("/product/<product_name>")
def product(product_name):
    selected_product = products.get(product_name)

    if not selected_product:
        return "Product not found", 404

    return render_template("product.html", product=selected_product, product_slug=product_name)

@app.route("/cart")
def cart():
    return render_template("cart.html", **get_cart_summary())


@app.route("/add-to-cart/<product_name>")
def add_to_cart(product_name):
    if not get_store_item(product_name):
        return "Product not found", 404

    cart = session.get("cart", {})
    cart[product_name] = cart.get(product_name, 0) + 1
    session["cart"] = cart
    session.modified = True
    return redirect(url_for("cart"))

@app.route("/increase-cart/<product_name>", methods=["POST"])
def increase_cart(product_name):
    if not get_store_item(product_name):
        return "Product not found", 404

    cart = session.get("cart", {})
    cart[product_name] = cart.get(product_name, 0) + 1
    session["cart"] = cart
    session.modified = True
    return redirect(url_for("cart"))


@app.route("/decrease-cart/<product_name>", methods=["POST"])
def decrease_cart(product_name):
    cart = session.get("cart", {})

    if product_name in cart:
        cart[product_name] -= 1

        if cart[product_name] <= 0:
            cart.pop(product_name)

    session["cart"] = cart
    session.modified = True
    return redirect(url_for("cart"))


@app.route("/remove-from-cart/<product_name>", methods=["POST"])
def remove_from_cart(product_name):
    cart = session.get("cart", {})
    cart.pop(product_name, None)
    session["cart"] = cart
    session.modified = True
    return redirect(url_for("cart"))


@app.route("/clear-cart", methods=["POST"])
def clear_cart():
    session.pop("cart", None)
    return redirect(url_for("cart"))


@app.route("/checkout", methods=["GET", "POST"])
def checkout():
    summary = get_cart_summary()

    if request.args.get("payment") == "cancelled":
        flash("Your payment was cancelled. Your cart is still ready when you are.", "error")

    if request.method == "POST":
        if not summary["cart_items"]:
            return redirect(url_for("cart"))

        customer = {
            "full_name": request.form.get("full_name", "").strip(),
            "email": request.form.get("email", "").strip(),
            "address": request.form.get("address", "").strip(),
            "city": request.form.get("city", "").strip(),
            "state": request.form.get("state", "").strip(),
            "zip": request.form.get("zip", "").strip()
        }

        if not all(customer.values()):
            flash("Please complete every checkout field before continuing.", "error")
            return render_template(
                "checkout.html",
                **summary,
                payment=get_payment_settings()
            )

        payment = get_payment_settings()

        if not payment["is_configured"]:
            flash("Stripe checkout is ready in the code, but the Stripe keys still need to be added.", "error")
            return render_template(
                "checkout.html",
                **summary,
                payment=payment
            )

        order_id = create_order(summary, customer)

        try:
            checkout_session = create_stripe_checkout_session(order_id, summary, customer)
        except Exception:
            flash("Stripe could not start checkout yet. Please check the Stripe setup details.", "error")
            return render_template(
                "checkout.html",
                **summary,
                payment=payment
            )

        update_order_payment(order_id, "pending", checkout_session.id)
        session["pending_order_id"] = order_id
        return redirect(checkout_session.url, code=303)

    return render_template(
        "checkout.html",
        **summary,
        payment=get_payment_settings()
    )


@app.route("/order-confirmation")
def order_confirmation():
    checkout_session_id = request.args.get("session_id")
    order = None

    if checkout_session_id:
        order = get_order_by_stripe_session(checkout_session_id)

        if order and stripe is not None and get_stripe_secret_key():
            stripe.api_key = get_stripe_secret_key()

            try:
                checkout_session = stripe.checkout.Session.retrieve(checkout_session_id)
            except Exception:
                checkout_session = None

            if checkout_session and checkout_session.payment_status == "paid":
                update_order_payment(
                    order["id"],
                    "paid",
                    checkout_session.id,
                    checkout_session.payment_intent,
                )
                order = get_order_by_id(order["id"])
                session.pop("cart", None)
                session.pop("pending_order_id", None)

    if not order:
        pending_order_id = session.get("pending_order_id")
        order = get_order_by_id(pending_order_id) if pending_order_id else session.get("last_order")

    if not order:
        return redirect(url_for("shop"))

    return render_template("order_confirmation.html", order=order)


@app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    if stripe is None:
        return "Stripe package is not installed.", 500

    webhook_secret = get_stripe_webhook_secret()

    if not webhook_secret:
        return "Stripe webhook secret is not configured.", 500

    payload = request.get_data()
    signature = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, signature, webhook_secret)
    except ValueError:
        return "Invalid payload.", 400
    except Exception as error:
        signature_error = getattr(getattr(stripe, "error", None), "SignatureVerificationError", None)
        direct_signature_error = getattr(stripe, "SignatureVerificationError", None)

        if (
            signature_error and isinstance(error, signature_error)
        ) or (
            direct_signature_error and isinstance(error, direct_signature_error)
        ):
            return "Invalid signature.", 400

        raise

    if event["type"] == "checkout.session.completed":
        checkout_session = event["data"]["object"]
        order_id = checkout_session.get("metadata", {}).get("order_id")

        if order_id and checkout_session.get("payment_status") == "paid":
            update_order_payment(
                int(order_id),
                "paid",
                checkout_session.get("id"),
                checkout_session.get("payment_intent"),
            )

    return "", 200

# ------------------ LOGIN SYSTEM ------------------

@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get("user"):
        flash("You are already logged in.", "success")
        return redirect(url_for("home"))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password')
        user = get_user_by_username(username)

        if user and password_matches(user, password):
            start_user_session(username)
            flash(f"Welcome back, {username}.", "success")
            return redirect(url_for('home'))
        else:
            return render_template(
                'login.html',
                error='Invalid username or password',
                entered_username=username
            )

    return render_template('login.html')


@app.route('/create-account', methods=['GET', 'POST'])
def create_account():
    if session.get("user"):
        flash("You are already logged in.", "success")
        return redirect(url_for("home"))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not username or not email or not password:
            return render_template(
                'create_account.html',
                error='Please enter a username, email, and password',
                entered_username=username,
                entered_email=email
            )

        existing_user = get_user_by_username(username)
        if existing_user:
            return render_template(
                'create_account.html',
                error='That username is already taken',
                entered_username=username,
                entered_email=email
            )

        existing_email = get_user_by_email(email)
        if existing_email:
            return render_template(
                'create_account.html',
                error='That email is already in use',
                entered_username=username,
                entered_email=email
            )

        if password != confirm_password:
            return render_template(
                'create_account.html',
                error='Passwords do not match',
                entered_username=username,
                entered_email=email
            )

        create_user(username, email, password)
        start_user_session(username)
        flash(f"Welcome, {username}. Your account is ready.", "success")
        return redirect(url_for('home'))

    return render_template('create_account.html')


@app.route('/logout')
def logout():
    username = session.get("user")
    clear_user_session_data()
    session.pop('user', None)
    if username:
        flash(f"You have been logged out, {username}.", "success")
    return redirect(url_for('home'))


@app.route('/change-password', methods=['GET', 'POST'])
def change_password():
    user = get_current_user()

    if not user:
        flash("Please log in to change your password.", "error")
        return redirect(url_for("login"))

    if request.method == 'POST':
        current_password = request.form.get('current_password', '')
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not current_password or not new_password or not confirm_password:
            return render_template(
                'change_password.html',
                error='Please fill out all password fields.'
            )

        if not password_matches(user, current_password):
            return render_template(
                'change_password.html',
                error='Your current password is incorrect.'
            )

        if new_password != confirm_password:
            return render_template(
                'change_password.html',
                error='New passwords do not match.'
            )

        if current_password == new_password:
            return render_template(
                'change_password.html',
                error='Choose a new password different from your current one.'
            )

        update_user_password(user["id"], new_password)
        flash("Your password was updated successfully.", "success")
        return redirect(url_for('change_password'))

    return render_template('change_password.html')


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if session.get("user"):
        flash("You are already logged in.", "success")
        return redirect(url_for("home"))

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip().lower()
        user = get_user_by_username(username) if username else None

        if user and user["email"] and user["email"].lower() == email:
            try:
                send_password_reset_email(user)
            except Exception:
                return render_template(
                    'forgot_password.html',
                    error='We could not send the reset email right now. Please try again.',
                    entered_username=username,
                    entered_email=email
                )

        flash("If that account matches our records, a reset link has been sent.", "success")
        return redirect(url_for('login'))

    return render_template('forgot_password.html')


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    try:
        username = get_reset_serializer().loads(token, salt="password-reset", max_age=3600)
    except SignatureExpired:
        flash("That reset link has expired. Please request a new one.", "error")
        return redirect(url_for("forgot_password"))
    except BadSignature:
        flash("That reset link is invalid.", "error")
        return redirect(url_for("forgot_password"))

    user = get_user_by_username(username)
    if not user:
        flash("That reset link is no longer valid.", "error")
        return redirect(url_for("forgot_password"))

    if request.method == 'POST':
        new_password = request.form.get('new_password', '')
        confirm_password = request.form.get('confirm_password', '')

        if not new_password or not confirm_password:
            return render_template(
                'reset_password.html',
                error='Please fill out both password fields.',
                token=token
            )

        if new_password != confirm_password:
            return render_template(
                'reset_password.html',
                error='Passwords do not match.',
                token=token
            )

        update_user_password(user["id"], new_password)
        flash("Your password has been reset. You can log in now.", "success")
        return redirect(url_for('login'))

    return render_template('reset_password.html', token=token)

if __name__ == "__main__":
    app.run(debug=True)
