from flask import Flask, request, jsonify, make_response
from flask_cors import CORS

import requests
import os
from werkzeug.utils import secure_filename

app = Flask(__name__)
cors = CORS(app)
app.config['CORS_HEADERS'] = 'application/json'

UPLOAD_FOLDER = 'downloads'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER


def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response


@app.route('/download-and-save', methods=['POST', 'OPTIONS'])
def download_and_save_file():
    if request.method == 'OPTIONS':
        return add_cors_headers(make_response())

    file_data = request.json

    if not file_data or 'accessToken' not in file_data or 'value' not in file_data or not file_data['value']:
        return add_cors_headers(jsonify({"error": "Invalid file data"})), 400

    access_token = file_data['accessToken']
    api_endpoint = file_data['apiEndpoint']
    file_id = file_data['value'][0]['id']

    if not all([access_token, api_endpoint, file_id]):
        return add_cors_headers(jsonify({"error": "Missing required file information"})), 400

    try:
        # First, get the file metadata
        metadata_url = f"{api_endpoint}drives/me/items/{file_id}"
        metadata_response = requests.get(metadata_url, headers={
            'Authorization': f'Bearer {access_token}'
        })
        metadata_response.raise_for_status()
        metadata = metadata_response.json()

        file_name = metadata.get('name', 'unknown_file')

        # Now download the file content
        content_url = f"{api_endpoint}drives/me/items/{file_id}/content"
        download_response = requests.get(content_url, headers={
            'Authorization': f'Bearer {access_token}'
        })
        download_response.raise_for_status()

        # Secure the filename to prevent any malicious file paths
        secure_name = secure_filename(file_name)

        # Determine the local path to save the file
        save_path = os.path.join(app.config['UPLOAD_FOLDER'], secure_name)

        # Ensure the upload folder exists
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

        # Write the file content to local storage
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


if __name__ == '__main__':
    app.run(debug=True)
