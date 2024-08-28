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
import logging
import re


logging.basicConfig(level=logging.DEBUG)

app = Flask(__name__)
cors = CORS(app, supports_credentials=True,
            resources={r"/*": {"origins": ["http://localhost:3000", "http://127.0.0.1:3000"]}})
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

     # Determine the endpoint hint based on the user's account type
    user_email = session['user'].get('preferred_username', '')
    
    endpoint_hint = "api.onedrive.com"
    if 'live.com' in user_email.lower():
        endpoint_hint = 'api.onedrive.com'
    elif 'sharepoint_url' in session:
        endpoint_hint = f"{session['sharepoint_url']}"

    return jsonify({
        "isAuthenticated": True,
        "user": {
            "name": session['user'].get('name'),
            "email": session['user'].get('preferred_username')
        },
        "token": session["token"],
        "endpointHint": endpoint_hint
    })


@app.route('/login')
def login():
    state = str(uuid.uuid4())
    session['state'] = state  # Store state in server-side session
    frontend_redirect_uri = request.args.get('redirect_uri', 'http://localhost:3000/dashboard')
    session['frontend_redirect_uri'] = frontend_redirect_uri

    auth_url = msal_app.get_authorization_request_url(
        SCOPE,
        state=state,
        redirect_uri=url_for("authorized", _external=True),
        prompt='select_account'
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
        return jsonify({"error": "State mismatch"}), 400
    if "error" in request.args:
        return jsonify({"error": request.args.get("error")}), 400

    cache = msal.SerializableTokenCache()
    if session.get("token_cache"):
        cache.deserialize(session["token_cache"])
    
    result = msal_app.acquire_token_by_authorization_code(
        request.args['code'],
        scopes=SCOPE,
        redirect_uri=url_for("authorized", _external=True)
    )

    if "error" in result:
        return jsonify({"error": result.get("error")}), 400

    session["user"] = result.get("id_token_claims")
    session["token"] = result.get('access_token')
    session["token_expiry"] = (datetime.now() + timedelta(seconds=result['expires_in'])).isoformat()

    # Store the SharePoint URL
    graph_client = GraphClient(session["token"])
    sharepoint_url = graph_client.get_sharepoint_url()
    session["sharepoint_url"] = sharepoint_url

    cache.add(result)
    session["token_cache"] = cache.serialize()

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
    try:
        if 'token' in session:
            token = session['token']
            
            # Attempt to revoke the token
            revoke_url = f"{AUTHORITY}/oauth2/v2.0/logout"
            response = requests.post(revoke_url, data={'token': token})
            
            if response.status_code != 200:
                logging.warning(f"Token revocation failed with status code: {response.status_code}")

        # Clear the session
        session.clear()
        
        return jsonify({"message": "Logged out successfully"}), 200
    
    except Exception as e:
        logging.error(f"Error during logout: {str(e)}")
        return jsonify({"error": "An error occurred during logout"}), 500


@app.route('/download-and-save', methods=['POST', 'OPTIONS'])
def download_and_save_file():
    if request.method == 'OPTIONS':
        return add_cors_headers(make_response())

    file_data = request.json
    logging.debug(f"Received file data: {file_data}")

    if not file_data or 'value' not in file_data or not file_data['value']:
        return add_cors_headers(jsonify({"error": "Invalid file data"})), 400

    file_info = file_data['value'][0]
    download_url = file_info.get('@microsoft.graph.downloadUrl')
    file_name = file_info.get('name', 'unknown_file')

    if not download_url:
        return add_cors_headers(jsonify({"error": "Download URL not found in file data"})), 400

    try:
        logging.debug(f"Attempting to download file from: {download_url}")

        download_response = requests.get(download_url)
        download_response.raise_for_status()

        secure_name = secure_filename(file_name)
        save_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_name)

        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

        with open(save_path, 'wb') as f:
            f.write(download_response.content)

        logging.debug(f"File saved successfully at: {save_path}")

        return add_cors_headers(jsonify({
            "message": f"File {secure_name} downloaded successfully and saved",
            "saved_path": save_path,
        })), 200

    except requests.RequestException as e:
        logging.error(f"RequestException: {str(e)}")
        return add_cors_headers(jsonify({"error": f"Error downloading file: {str(e)}"})), 500
    except IOError as e:
        logging.error(f"IOError: {str(e)}")
        return add_cors_headers(jsonify({"error": f"Error saving file: {str(e)}"})), 500
    except Exception as e:
        logging.error(f"Unexpected error: {str(e)}")
        return add_cors_headers(jsonify({"error": f"Unexpected error: {str(e)}"})), 500


@app.route('/test', methods=['GET', 'OPTIONS'])
def test():
    return jsonify({
        "message": f"wawaw",
        "saved_path": 'awaw',
    })


@app.route('/debug_session')
def debug_session():
    return jsonify(dict(session))


class GraphClient:
    def __init__(self, access_token):
        self.access_token = access_token

    def get_sharepoint_url(self):
        # Use Microsoft Graph API to get the user's SharePoint URL
        # This is a placeholder implementation. You'll need to replace this with actual Graph API calls.
        headers = {
            'Authorization': f'Bearer {self.access_token}',
            'Accept': 'application/json'
        }
        response = requests.get('https://graph.microsoft.com/v1.0/me/drive/root', headers=headers)
        if response.status_code == 200:
            data = response.json()
            web_url = data.get('webUrl', '')
            # Extract the SharePoint URL from the webUrl
            return web_url.split('/personal/')[0] if '/personal/' in web_url else None
        return None


if __name__ == '__main__':
    app.run(debug=True, port=5000, host='0.0.0.0')
    # app.run(debug=True)



