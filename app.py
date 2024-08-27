from flask import Flask, request, jsonify, make_response, redirect, session, url_for
from flask_cors import CORS
import requests
import os
from werkzeug.utils import secure_filename
import msal
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlencode
from dotenv import load_dotenv



app = Flask(__name__)
cors = CORS(app, supports_credentials=True, resources={r"/*": {"origins": "http://localhost:3000"}})
app.config['CORS_HEADERS'] = 'application/json'
app.secret_key = os.urandom(24)  # Set a secret key for sessions

UPLOAD_FOLDER = 'downloads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Microsoft OAuth Configuration
CLIENT_ID = os.environ.get('MICROSOFT_CLIENT_ID')
CLIENT_KEY = os.environ.get('MICROSOFT_CLIENT_KEY')
AUTHORITY = "https://login.microsoftonline.com/common"
REDIRECT_PATH = "/getAToken"
SCOPE = [
    "User.Read",
    "Files.Read",
    "Files.Read.All",
    "Files.ReadWrite.All"
]

app.config['SESSION_COOKIE_SECURE'] = True
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'None'

# MSAL config
msal_app = msal.ConfidentialClientApplication(
    CLIENT_ID, authority=AUTHORITY, client_credential=CLIENT_KEY
)

def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response


@app.route('/auth_status')
def auth_status():
    print("Session contents:", session)  # Log entire session contents

    if 'user' not in session:
        print("No user in session")
        return jsonify({"isAuthenticated": False, "reason": "No user in session"})
    
    if 'token' not in session:
        print("No token in session")
        return jsonify({"isAuthenticated": False, "reason": "No token in session"})

    # Check if the token is expired
    expiry = session.get('token_expiry')
    if not expiry:
        print("No token expiry in session")
        return jsonify({"isAuthenticated": False, "reason": "No token expiry in session"})

    try:
        expiry_time = datetime.fromisoformat(expiry)
        if datetime.now() >= expiry_time:
            print("Token has expired")
            return jsonify({"isAuthenticated": False, "reason": "Token expired"})
    except ValueError as e:
        print(f"Error parsing expiry time: {e}")
        return jsonify({"isAuthenticated": False, "reason": f"Error parsing expiry time: {e}"})

    return jsonify({
        "isAuthenticated": True,
        "user": {
            "name": session['user'].get('name'),
            "email": session['user'].get('preferred_username')
        },
        "token": session["token"]
    })



@app.route('/login')
def login():
    session["state"] = str(uuid.uuid4())
    frontend_redirect_uri = request.args.get('redirect_uri', 'http://localhost:3000/dashboard')
    session['frontend_redirect_uri'] = frontend_redirect_uri

    auth_url = msal_app.get_authorization_request_url(
        SCOPE,
        state=session["state"],
        redirect_uri=url_for("authorized", _external=True)
    )
    
    return jsonify({"auth_url": auth_url})

@app.route('/auth/callback')
def auth_callback():
    if session.get("user"):
        return redirect("http://localhost:3000/dashboard") 
    else:
        return redirect("http://localhost:3000") 

@app.route(REDIRECT_PATH)
def authorized():
    if request.args.get('state') != session.get("state"):
        return "State mismatch", 400
    if "error" in request.args:
        return request.args.get("error"), 400
    
    token_result = msal_app.acquire_token_by_authorization_code(
        request.args['code'],
        scopes=SCOPE,
        redirect_uri=url_for("authorized", _external=True)
    )

    if "error" in token_result:
        return token_result.get("error"), 400

    if "error" not in token_result:
        session["user"] = token_result.get("id_token_claims")
        session["token"] = token_result['access_token']  # Store just the access token
        session["token_expiry"] = (datetime.now() + timedelta(seconds=token_result['expires_in'])).isoformat()

    frontend_redirect_uri = session.pop('frontend_redirect_uri', 'http://localhost:3000/dashboard')
    
    user_info = urlencode({
        "user_name": session["user"].get("name", ""),
        "user_email": session["user"].get("preferred_username", "")
    })
    redirect_url = f"{frontend_redirect_uri}?{user_info}"

    return redirect(redirect_url)

@app.route('/')
def index():
    if not session.get("user"):
        return redirect(url_for("login"))
    return jsonify({"message": "Logged in", "user": session["user"]})

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for("index"))

@app.route('/download-and-save', methods=['POST', 'OPTIONS'])
def download_and_save_file():
    if request.method == 'OPTIONS':
        return add_cors_headers(make_response())

    if not session.get("token"):
        return add_cors_headers(jsonify({"error": "Not authenticated"})), 401

    if datetime.now() >= session.get("token_expiry", datetime.min):
        token_result = msal_app.acquire_token_silent(SCOPE, account=session["user"])
        if not token_result:
            return add_cors_headers(jsonify({"error": "Token expired, please log in again"})), 401
        session["token"] = token_result
        session["token_expiry"] = datetime.now() + timedelta(seconds=token_result['expires_in'])

    file_data = request.json

    if not file_data or 'value' not in file_data or not file_data['value']:
        return add_cors_headers(jsonify({"error": "Invalid file data"})), 400

    access_token = session["token"]["access_token"]
    api_endpoint = "https://graph.microsoft.com/v1.0/"
    file_id = file_data['value'][0]['id']

    if not all([access_token, api_endpoint, file_id]):
        return add_cors_headers(jsonify({"error": "Missing required file information"})), 400

    try:
        metadata_url = f"{api_endpoint}drives/me/items/{file_id}"
        metadata_response = requests.get(metadata_url, headers={
            'Authorization': f'Bearer {access_token}'
        })
        metadata_response.raise_for_status()
        metadata = metadata_response.json()

        file_name = metadata.get('name', 'unknown_file')

        content_url = f"{api_endpoint}drives/me/items/{file_id}/content"
        download_response = requests.get(content_url, headers={
            'Authorization': f'Bearer {access_token}'
        })
        download_response.raise_for_status()

        secure_name = secure_filename(file_name)

        save_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_name)

        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

        with open(save_path, 'wb') as f:
            f.write(download_response.content)

        return add_cors_headers(jsonify({
            "message": f"File {secure_name} downloaded successfully and saved",
            "saved_path": save_path,
        })), 200

    except requests.RequestException as e:
        return add_cors_headers(jsonify({"error": f"Error downloading file: {str(e)}"})), 500
    except IOError as e:
        return add_cors_headers(jsonify({"error": f"Error saving file: {str(e)}"})), 500

@app.route('/test', methods=['GET', 'OPTIONS'])
def test():
    return jsonify({
        "message": f"wawaw",
        "saved_path": 'awaw',
    })


@app.route('/debug_session')
def debug_session():
    return jsonify(dict(session))

if __name__ == '__main__':
    app.run(debug=True)