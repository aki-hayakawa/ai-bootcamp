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
    # Redirect home page to analytics dashboard
    return redirect(url_for("analytics"))

@app.route("/dashboard")
@login_required  
def dashboard():
    # Alias for analytics
    return redirect(url_for("analytics"))

@app.route("/ask-llm", methods=["GET", "POST"])
@login_required
def ask_llm():
    token = session["access_token"]

    if request.method == "POST":
        # 1) Parse the JSON body
        data = request.get_json()
        if not data:
            return {"detail": "Invalid JSON body"}, 400

        # 2) Extract prompt
        prompt = data.get("prompt")
        if not prompt:
            return {"detail": "Prompt is required"}, 400

        # 3) Proxy to FastAPI (with auth header)
        try:
            res = requests.post(
                f"{BACKEND_URL}/ask-llm",
                json={"prompt": prompt},
                headers={"Authorization": f"Bearer {token}"}
            )
            return res.json(), res.status_code
        except Exception as e:
            return {"detail": f"Error calling backend: {e}"}, 500

    # For GET, just render the page
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

@app.route("/api/files")
@login_required
def api_files():
    token = session["access_token"]
    try:
        res = requests.get(
            f"{BACKEND_URL}/list-files",
            headers={"Authorization": f"Bearer {token}"}
        )
        return res.json() if res.ok else []
    except Exception as e:
        return []

@app.route("/api/download/<filename>")
@login_required
def api_download_file(filename):
    token = session["access_token"]
    try:
        res = requests.get(
            f"{BACKEND_URL}/download-file/{filename}",
            headers={"Authorization": f"Bearer {token}"},
            stream=True
        )
        if res.ok:
            from flask import Response
            return Response(
                res.iter_content(chunk_size=8192),
                content_type=res.headers.get('content-type', 'application/octet-stream'),
                headers={
                    'Content-Disposition': res.headers.get('Content-Disposition', f'attachment; filename={filename}'),
                    'Content-Length': res.headers.get('Content-Length')
                }
            )
        else:
            return "File not found", 404
    except Exception as e:
        return "Error downloading file", 500

@app.route("/api/view/<filename>")
@login_required
def api_view_file(filename):
    token = session["access_token"]
    try:
        res = requests.get(
            f"{BACKEND_URL}/view-file/{filename}",
            headers={"Authorization": f"Bearer {token}"},
            stream=True
        )
        if res.ok:
            from flask import Response
            return Response(
                res.iter_content(chunk_size=8192),
                content_type=res.headers.get('content-type', 'application/octet-stream'),
                headers={
                    'Content-Length': res.headers.get('Content-Length')
                }
            )
        else:
            return "File not found", 404
    except Exception as e:
        return "Error viewing file", 500

@app.route("/analytics")
@login_required
def analytics():
    return render_template("analytics.html")

@app.route("/api/analytics/summary")
@login_required
def analytics_summary():
    token = session["access_token"]
    try:
        res = requests.get(
            f"{BACKEND_URL}/analytics/summary",
            headers={"Authorization": f"Bearer {token}"}
        )
        return res.json() if res.ok else {"error": "Failed to fetch analytics"}
    except Exception as e:
        return {"error": str(e)}

@app.route("/api/analytics/top-prompts")
@login_required
def analytics_top_prompts():
    token = session["access_token"]
    try:
        res = requests.get(
            f"{BACKEND_URL}/analytics/top-prompts",
            headers={"Authorization": f"Bearer {token}"}
        )
        return res.json() if res.ok else []
    except Exception as e:
        return []

@app.route("/api/analytics/files")
@login_required
def analytics_files():
    token = session["access_token"]
    try:
        res = requests.get(
            f"{BACKEND_URL}/analytics/files",
            headers={"Authorization": f"Bearer {token}"}
        )
        return res.json() if res.ok else {"total_files": 0, "total_size": 0, "by_type": {}}
    except Exception as e:
        return {"total_files": 0, "total_size": 0, "by_type": {}}

@app.route("/api/models")
@login_required
def get_models():
    try:
        res = requests.get(f"{BACKEND_URL}/models")
        return res.json() if res.ok else {"models": ["gemini-2.5-pro"], "default": "gemini-2.5-pro"}
    except Exception as e:
        return {"models": ["gemini-2.5-pro"], "default": "gemini-2.5-pro"}

@app.route("/api/delete-file/<filename>", methods=["DELETE"])
@login_required
def api_delete_file(filename):
    token = session["access_token"]
    try:
        res = requests.delete(
            f"{BACKEND_URL}/delete-file/{filename}",
            headers={"Authorization": f"Bearer {token}"}
        )
        
        if res.ok:
            return res.json()
        else:
            error_data = res.json() if res.headers.get('content-type', '').startswith('application/json') else {}
            return {"error": error_data.get('detail', f'HTTP {res.status_code}')}, res.status_code
            
    except Exception as e:
        return {"error": f"Failed to delete file: {str(e)}"}, 500

@app.route("/api/ask-ai-about-file", methods=["POST"])
@login_required
def ask_ai_about_file():
    token = session["access_token"]
    try:
        # Get the JSON data from the request
        data = request.get_json()
        
        # Forward the request to the backend
        res = requests.post(
            f"{BACKEND_URL}/ask-ai-about-file",
            json=data,
            headers={"Authorization": f"Bearer {token}"}
        )
        
        if res.ok:
            return res.json()
        else:
            error_data = res.json() if res.headers.get('content-type', '').startswith('application/json') else {}
            return {"error": error_data.get('detail', f'HTTP {res.status_code}')}, res.status_code
            
    except Exception as e:
        return {"error": f"Failed to process AI request: {str(e)}"}, 500

@app.route("/api/analytics/file-ai")
@login_required
def analytics_file_ai():
    token = session["access_token"]
    try:
        res = requests.get(
            f"{BACKEND_URL}/analytics/file-ai",
            headers={"Authorization": f"Bearer {token}"}
        )
        return res.json() if res.ok else {"total_requests": 0, "by_type": {}, "by_model": {}, "top_files": []}
    except Exception as e:
        return {"total_requests": 0, "by_type": {}, "by_model": {}, "top_files": []}

@app.route("/api/analytics/deleted-files")
@login_required
def analytics_deleted_files():
    token = session["access_token"]
    try:
        res = requests.get(
            f"{BACKEND_URL}/analytics/deleted-files",
            headers={"Authorization": f"Bearer {token}"}
        )
        return res.json() if res.ok else {"deleted_files": [], "total_deleted": 0}
    except Exception as e:
        return {"deleted_files": [], "total_deleted": 0}

if __name__ == "__main__":
    app.run(debug=True)