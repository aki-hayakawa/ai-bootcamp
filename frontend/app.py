from flask import Flask, render_template, request, session, redirect, url_for
import requests
import os

app = Flask(__name__)
app.secret_key = '1awgdkaysgkdsajd783271323'
BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000")

# -----------------------------
# Application Pages
# -----------------------------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/ask-llm", methods=["GET", "POST"])
def ask_llm():
    if request.method == "POST":
        data = request.get_json()
        prompt = data.get("prompt")
        if not prompt:
            return {"detail": "Prompt is required"}, 400

        try:
            res = requests.post(f"{BACKEND_URL}/ask-llm", json={"prompt": prompt})
            return res.json(), res.status_code
        except Exception as e:
            return {"detail": f"Error calling backend: {str(e)}"}, 500

    return render_template("ask_llm.html")


@app.route("/history")
def prompt_history():
    try:
        res = requests.get(f"{BACKEND_URL}/llm-history")
        logs = res.json()
        return render_template("history.html", logs=logs)
    except Exception as e:
        return f"Error loading history: {str(e)}"

@app.route("/files", methods=["GET", "POST"])
def file_manager():
    message = ""
    if request.method == "POST":
        file = request.files.get("file")
        if file:
            try:
                res = requests.post(
                    f"{BACKEND_URL}/upload-file",
                    files={"file": (file.filename, file.stream, file.mimetype)}
                )
                if res.ok:
                    message = f"Uploaded: {file.filename}"
                else:
                    message = "Upload failed"
            except Exception as e:
                message = f"Error: {e}"

    try:
        file_list = requests.get(f"{BACKEND_URL}/list-files").json()
    except Exception as e:
        file_list = []
        message += f" (List error: {e})"

    return render_template("files.html", message=message, files=file_list)

# -----------------------------
# Run the Flask App
# -----------------------------
if __name__ == "__main__":
    app.run(debug=True)
