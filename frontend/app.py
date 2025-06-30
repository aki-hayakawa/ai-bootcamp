from flask import Flask, render_template, request, session, redirect, url_for
import requests
import os
from functools import wraps
from dotenv import load_dotenv
from jose import jwt, JWTError
import json

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "fallback-secret")

# Keycloak and callback settings from .env
KEYCLOAK_URL_PUBLIC   = os.getenv("KEYCLOAK_URL_PUBLIC")
KEYCLOAK_URL_INTERNAL = os.getenv("KEYCLOAK_URL_INTERNAL")
CLIENT_ID             = os.getenv("KEYCLOAK_CLIENT_ID")
CLIENT_SECRET         = os.getenv("KEYCLOAK_CLIENT_SECRET")
REDIRECT_URI          = os.getenv("OIDC_REDIRECT_URI")

BACKEND_URL         = os.getenv("BACKEND_URL", "http://backend:8000")

# Protect pages
def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if "access_token" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapped

@app.context_processor
def inject_user():
    # this makes `user` available in every Jinja template
    return dict(user=session.get("user"))

@app.route("/login")
def login():
    auth_url = (
        f"{KEYCLOAK_URL_PUBLIC}/protocol/openid-connect/auth"
        f"?response_type=code"
        f"&client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}"
        f"&scope=openid profile email roles"
    )
    return redirect(auth_url)


@app.route("/callback")
def callback():
    # 1) Handle Keycloak errors
    if "error" in request.args:
        err   = request.args.get("error")
        descr = request.args.get("error_description", "")
        return f"Keycloak returned error: {err} — {descr}", 400

    # 2) If no code → post-logout → go home
    code = request.args.get("code")
    if not code:
        return redirect(url_for("index"))

    # 3) Exchange code for tokens
    token_url = f"{KEYCLOAK_URL_INTERNAL}/protocol/openid-connect/token"
    data = {
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  REDIRECT_URI,
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    }
    try:
        token_resp = requests.post(token_url, data=data, timeout=5)
        token_resp.raise_for_status()
    except requests.RequestException as e:
        body = token_resp.text if "token_resp" in locals() else str(e)
        return f"Token exchange failed: {body}", 500

    tokens = token_resp.json()

    # 4) Decode ID token
    id_token = tokens.get("id_token")
    if not id_token:
        return "No ID token returned", 500

    try:
        payload = jwt.get_unverified_claims(id_token)
        # debug print if you need it:
        # print("ID token claims:\n", json.dumps(payload, indent=2), flush=True)
    except JWTError as e:
        return f"Invalid ID token: {e}", 400

    # 5) Build the user dict
    user = {
        "first_name": payload.get("given_name"),
        "last_name":  payload.get("family_name"),
        "email":      payload.get("email"),
    }

    # 6) Filter the realm_access.roles for only admin/superadmin
    realm_roles = payload.get("realm_access", {}).get("roles", [])
    allowed     = {"admin", "superadmin"}
    user["roles"] = [r for r in realm_roles if r in allowed]

    # 7) Persist into session
    session["user"]         = user
    session["access_token"] = tokens.get("access_token")

    # 8) Redirect to your home/dashboard
    return redirect(url_for("index"))

@app.route("/logout")
def logout():
    # 1) Clear our local session
    session.clear()
    print("LOGOUT → redirect_uri =", REDIRECT_URI, flush=True)
    # 2) Build the Keycloak logout URL and send the browser there
    logout_url = (
        f"{KEYCLOAK_URL_PUBLIC}/protocol/openid-connect/logout"
        f"?client_id={CLIENT_ID}"
        f"&post_logout_redirect_uri={REDIRECT_URI}"
    )
    return redirect(logout_url)


# ─── Application pages ────────────────────────────────────────────────────────
@app.route("/")
@login_required
def index():
    return render_template("index.html")

@app.route("/ask-llm", methods=["GET", "POST"])
@login_required
def ask_llm():
    token = session["access_token"]
    if request.method == "POST":
        prompt = request.form.get("prompt", "")
        res = requests.post(
            f"{BACKEND_URL}/ask-llm",
            json={"prompt": prompt},
            headers={"Authorization": f"Bearer {token}"}
        )
        return (res.json(), res.status_code)
    return render_template("ask-llm.html")

@app.route("/history")
@login_required
def prompt_history():
    token = session["access_token"]
    res = requests.get(
        f"{BACKEND_URL}/llm-history",
        headers={"Authorization": f"Bearer {token}"}
    )
    logs = res.json() if res.ok else []
    return render_template("history.html", logs=logs)

@app.route("/files", methods=["GET", "POST"])
@login_required
def file_manager():
    token = session["access_token"]
    message, files = "", []

    if request.method == "POST":
        f = request.files.get("file")
        if f:
            res = requests.post(
                f"{BACKEND_URL}/upload-file",
                files={"file": (f.filename, f.stream, f.mimetype)},
                headers={"Authorization": f"Bearer {token}"}
            )
            message = f"Uploaded: {f.filename}" if res.ok else f"Upload failed ({res.status_code})"

    res = requests.get(
        f"{BACKEND_URL}/list-files",
        headers={"Authorization": f"Bearer {token}"}
    )
    if res.ok:
        files = res.json() if isinstance(res.json(), list) else []

    return render_template("files.html", message=message, files=files)

if __name__ == "__main__":
    app.run(debug=True)